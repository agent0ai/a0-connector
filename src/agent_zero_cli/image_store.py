"""Bounded, authenticated image loading for transcript references."""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
import io
from typing import Protocol
import warnings

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from agent_zero_cli.media_refs import ImageReference


MAX_ENCODED_BYTES = 25 * 1024 * 1024
MAX_DECODED_PIXELS = 32_000_000
DEFAULT_MAX_CACHE_BYTES = 64 * 1024 * 1024
_FORMATS_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
}


class ImageClient(Protocol):
    async def fetch_image(self, path: str) -> tuple[bytes, str]: ...


class ImageUnavailableError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "image unavailable")
        super().__init__(self.reason)


@dataclass
class ImageAsset:
    """One independently-owned display surface."""

    cache_key: str
    mime_type: str
    image: PILImage.Image
    width: int
    height: int
    cost_bytes: int

    def clone(self) -> "ImageAsset":
        image = self.image.copy()
        image.load()
        return ImageAsset(
            cache_key=self.cache_key,
            mime_type=self.mime_type,
            image=image,
            width=self.width,
            height=self.height,
            cost_bytes=self.cost_bytes,
        )

    def close(self) -> None:
        self.image.close()


class ImageStore:
    """Load, normalize, and retain a small LRU of immutable image masters."""

    def __init__(
        self,
        client: ImageClient,
        *,
        max_surface_pixels: tuple[int, int],
        max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES,
        max_concurrent: int = 4,
    ) -> None:
        self.client = client
        self.max_surface_pixels = (
            max(1, int(max_surface_pixels[0])),
            max(1, int(max_surface_pixels[1])),
        )
        self.max_cache_bytes = max(0, int(max_cache_bytes))
        self._cache_bytes = 0
        self._cache: OrderedDict[str, ImageAsset] = OrderedDict()
        self._pending: dict[str, asyncio.Task[ImageAsset]] = {}
        self._pending_waiters: dict[str, int] = {}
        self._semaphore = asyncio.Semaphore(min(4, max(1, int(max_concurrent))))
        self._decode_semaphore = asyncio.Semaphore(1)

    @property
    def cache_bytes(self) -> int:
        return self._cache_bytes

    async def load(self, reference: ImageReference) -> ImageAsset:
        """Return a fresh surface for ``reference``, sharing only in-flight work."""
        cached = self._cache.get(reference.cache_key)
        if cached is not None:
            self._cache.move_to_end(reference.cache_key)
            return cached.clone()

        task = self._pending.get(reference.cache_key)
        if task is None:
            task = asyncio.create_task(self._load_master(reference))
            self._pending[reference.cache_key] = task
            self._pending_waiters[reference.cache_key] = 0
            task.add_done_callback(
                lambda completed, cache_key=reference.cache_key: self._finish_pending(
                    cache_key,
                    completed,
                )
            )
        self._pending_waiters[reference.cache_key] += 1
        master: ImageAsset | None = None
        try:
            master = await asyncio.shield(task)
            return master.clone()
        finally:
            self._release_waiter(reference.cache_key, task)

    def cancel_pending(self) -> None:
        """Cancel unfinished loads while retaining completed cache entries."""
        tasks = tuple(self._pending.values())
        self._pending.clear()
        self._pending_waiters.clear()
        for task in tasks:
            task.cancel()

    def clear(self) -> None:
        """Release every retained master and reset all accounting."""
        self.cancel_pending()
        for asset in self._cache.values():
            asset.close()
        self._cache.clear()
        self._cache_bytes = 0

    async def _load_master(self, reference: ImageReference) -> ImageAsset:
        async with self._semaphore:
            content, mime = await self._source_bytes(reference)
            async with self._decode_semaphore:
                decode_task = asyncio.create_task(
                    asyncio.to_thread(
                        _decode_image,
                        content,
                        mime,
                        self.max_surface_pixels,
                        reference.cache_key,
                    )
                )
                try:
                    master = await asyncio.shield(decode_task)
                except asyncio.CancelledError:
                    try:
                        discarded = await decode_task
                    except BaseException:
                        pass
                    else:
                        discarded.close()
                    raise
        self._cache[reference.cache_key] = master
        self._cache.move_to_end(reference.cache_key)
        self._cache_bytes += master.cost_bytes
        self._evict_to_limit(protected=master)
        return master

    async def _source_bytes(self, reference: ImageReference) -> tuple[bytes, str]:
        if reference.source == "agent_zero_path":
            return await self.client.fetch_image(reference.value)
        if reference.source == "data_uri":
            return _decode_data_uri(reference.value)
        raise ImageUnavailableError("image unavailable")

    def _release_waiter(
        self,
        cache_key: str,
        task: asyncio.Task[ImageAsset],
    ) -> None:
        if self._pending.get(cache_key) is not task:
            return
        remaining = self._pending_waiters[cache_key] - 1
        if remaining:
            self._pending_waiters[cache_key] = remaining
            return
        self._pending_waiters[cache_key] = 0
        self._finish_pending(cache_key, task)

    def _finish_pending(self, cache_key: str, task: asyncio.Task[ImageAsset]) -> None:
        if (
            self._pending.get(cache_key) is not task
            or self._pending_waiters.get(cache_key) != 0
            or not task.done()
        ):
            return
        self._pending.pop(cache_key, None)
        self._pending_waiters.pop(cache_key, None)
        if task.cancelled():
            return
        try:
            master = task.result()
        except BaseException:
            return
        if self._cache.get(cache_key) is not master:
            master.close()

    def _evict_to_limit(self, *, protected: ImageAsset) -> None:
        while self._cache_bytes > self.max_cache_bytes and self._cache:
            _, asset = self._cache.popitem(last=False)
            self._cache_bytes -= asset.cost_bytes
            if asset is not protected:
                asset.close()


def _decode_data_uri(value: str) -> tuple[bytes, str]:
    header, separator, payload = value.partition(",")
    if (
        not separator
        or not header.lower().startswith("data:")
        or ";base64" not in header.lower()
    ):
        raise ImageUnavailableError("invalid image data URI")
    mime = header[5:].split(";", 1)[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if not mime.startswith("image/"):
        raise ImageUnavailableError("image data URI did not include an image MIME type")
    padding = len(payload) - len(payload.rstrip("="))
    decoded_upper_bound = ((len(payload) + 3) // 4) * 3
    if len(payload) % 4 == 0 and padding <= 2:
        decoded_upper_bound -= padding
    if decoded_upper_bound > MAX_ENCODED_BYTES:
        raise ImageUnavailableError("image data is too large")
    try:
        content = base64.b64decode(payload, validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ImageUnavailableError("invalid image data URI") from exc
    if len(content) > MAX_ENCODED_BYTES:
        raise ImageUnavailableError("image data is too large")
    return content, mime


def _decode_image(
    content: bytes,
    declared_mime: str,
    max_surface_pixels: tuple[int, int],
    cache_key: str,
) -> ImageAsset:
    if len(content) > MAX_ENCODED_BYTES:
        raise ImageUnavailableError("image data is too large")
    normalized_mime = declared_mime.split(";", 1)[0].strip().lower()
    if normalized_mime == "image/jpg":
        normalized_mime = "image/jpeg"
    if normalized_mime not in _FORMATS_TO_MIME.values():
        raise ImageUnavailableError("unsupported format")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(content)) as probe:
                probe.verify()
            with PILImage.open(io.BytesIO(content)) as decoded:
                image_format = decoded.format
                expected_mime = _FORMATS_TO_MIME.get(image_format or "")
                if expected_mime is None:
                    raise ImageUnavailableError("unsupported format")
                if normalized_mime != expected_mime:
                    raise ImageUnavailableError("MIME type does not match image format")
                if decoded.width * decoded.height > MAX_DECODED_PIXELS:
                    raise ImageUnavailableError("image dimensions are too large")
                decoded.seek(0)
                orientation = decoded.getexif().get(274, 1)
                preorientation_target = max_surface_pixels
                if orientation in {5, 6, 7, 8}:
                    preorientation_target = (
                        max_surface_pixels[1],
                        max_surface_pixels[0],
                    )
                decoded.thumbnail(
                    preorientation_target,
                    PILImage.Resampling.LANCZOS,
                )
                oriented = ImageOps.exif_transpose(decoded)
                rgba = oriented.convert("RGBA")
                background = PILImage.new("RGBA", rgba.size, (23, 24, 26, 255))
                background.alpha_composite(rgba)
                surface = background.convert("RGB")
                surface.thumbnail(max_surface_pixels, PILImage.Resampling.LANCZOS)
                surface.load()
    except ImageUnavailableError:
        raise
    except (PILImage.DecompressionBombWarning, PILImage.DecompressionBombError):
        raise ImageUnavailableError("image dimensions are too large") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ImageUnavailableError("image could not be decoded") from None

    return ImageAsset(
        cache_key=cache_key,
        mime_type=normalized_mime,
        image=surface,
        width=surface.width,
        height=surface.height,
        cost_bytes=surface.width * surface.height * 3,
    )
