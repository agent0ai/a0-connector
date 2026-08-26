from __future__ import annotations

import asyncio
import base64
import io
import threading
from unittest.mock import Mock

from PIL import Image as PILImage
import pytest

from agent_zero_cli.image_store import ImageAsset, ImageStore, ImageUnavailableError
from agent_zero_cli.media_refs import ImageReference


pytestmark = pytest.mark.anyio


def png_bytes(size: tuple[int, int], *, alpha: bool = False) -> bytes:
    output = io.BytesIO()
    image = PILImage.new("RGBA" if alpha else "RGB", size, (255, 0, 0, 128) if alpha else "#123456")
    image.save(output, format="PNG")
    return output.getvalue()


def image_bytes(format_name: str, size: tuple[int, int] = (40, 20)) -> bytes:
    output = io.BytesIO()
    image = PILImage.new("RGB", size, "#123456")
    image.save(output, format=format_name)
    return output.getvalue()


def image_reference(
    key: str,
    *,
    source: str = "agent_zero_path",
    value: str | None = None,
) -> ImageReference:
    return ImageReference(
        entry_key=f"1:{key}",
        cache_key=key,
        context_id="ctx-1",
        sequence=1,
        owner="assistant",
        caption="Assistant image",
        source=source,  # type: ignore[arg-type]
        value=value or f"/a0/usr/uploads/{key}.png",
    )


class FakeImageClient:
    def __init__(self, payload: bytes, mime: str = "image/png") -> None:
        self.payload = payload
        self.mime = mime
        self.calls = 0

    async def fetch_image(self, path: str) -> tuple[bytes, str]:
        assert path.startswith("/a0/")
        self.calls += 1
        return self.payload, self.mime


class BlockingImageClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.started = asyncio.Event()
        self.four_started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_image(self, path: str) -> tuple[bytes, str]:
        assert path.startswith("/a0/")
        self.calls += 1
        self.active += 1
        self.started.set()
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active == 4:
            self.four_started.set()
        try:
            await self.release.wait()
            return self.payload, "image/png"
        finally:
            self.active -= 1


async def test_store_coalesces_same_source_and_returns_oriented_surface() -> None:
    client = FakeImageClient(png_bytes((40, 20)))
    store = ImageStore(client, max_surface_pixels=(96, 64))

    first, second = await asyncio.gather(
        store.load(image_reference("same")),
        store.load(image_reference("same")),
    )

    assert first is not second
    assert first.image is not second.image
    assert client.calls == 1
    assert first.cache_key == second.cache_key == "same"
    assert first.mime_type == second.mime_type == "image/png"
    assert (first.width, first.height) == (40, 20)
    first.close()
    second.close()


async def test_image_asset_clone_preserves_metadata_and_surface_ownership() -> None:
    image = PILImage.new("RGB", (3, 2), "#123456")
    master = ImageAsset("key", "image/png", image, 3, 2, 18)

    clone = master.clone()

    assert clone is not master
    assert clone.image is not master.image
    assert (
        clone.cache_key,
        clone.mime_type,
        clone.width,
        clone.height,
        clone.cost_bytes,
    ) == ("key", "image/png", 3, 2, 18)
    clone.width = 2
    assert clone.width == 2
    master.close()
    clone.close()


async def test_canceling_one_coalesced_waiter_keeps_shared_load_alive() -> None:
    client = BlockingImageClient(png_bytes((4, 4)))
    store = ImageStore(client, max_surface_pixels=(96, 64))
    first = asyncio.create_task(store.load(image_reference("same")))
    second = asyncio.create_task(store.load(image_reference("same")))
    await client.started.wait()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()

    client.release.set()
    asset = await second
    assert client.calls == 1
    asset.close()


async def test_store_limits_concurrency_to_four() -> None:
    client = BlockingImageClient(png_bytes((4, 4)))
    store = ImageStore(client, max_surface_pixels=(96, 64), max_concurrent=4)
    tasks = [
        asyncio.create_task(store.load(image_reference(str(index))))
        for index in range(5)
    ]

    await client.four_started.wait()
    assert client.maximum_active == 4
    client.release.set()
    assets = await asyncio.gather(*tasks)
    for asset in assets:
        asset.close()


async def test_store_evicts_lru_and_closes_surface() -> None:
    client = FakeImageClient(png_bytes((4, 4)))
    store = ImageStore(client, max_surface_pixels=(4, 4), max_cache_bytes=60)

    first = await store.load(image_reference("first"))
    first_master = store._cache["first"]
    second = await store.load(image_reference("second"))
    with pytest.raises(ValueError, match="closed image"):
        first_master.image.getpixel((0, 0))
    reloaded = await store.load(image_reference("first"))

    assert client.calls == 3
    assert store.cache_bytes <= 60
    with pytest.raises(AttributeError):
        store.cache_bytes = 0
    first.close()
    second.close()
    reloaded.close()


async def test_store_returns_surface_that_exceeds_cache_budget() -> None:
    store = ImageStore(
        FakeImageClient(png_bytes((4, 4))),
        max_surface_pixels=(4, 4),
        max_cache_bytes=1,
    )

    asset = await store.load(image_reference("oversized"))

    assert asset.image.size == (4, 4)
    assert store.cache_bytes == 0
    asset.close()


async def test_store_decodes_data_uris() -> None:
    payload = png_bytes((4, 4))
    reference = image_reference(
        "inline",
        source="data_uri",
        value="data:image/png;base64," + base64.b64encode(payload).decode("ascii"),
    )
    store = ImageStore(FakeImageClient(b""), max_surface_pixels=(96, 64))

    asset = await store.load(reference)

    assert asset.image.size == (4, 4)
    asset.close()


async def test_store_rejects_oversized_data_uri_before_base64_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.image_store as image_store

    decode = Mock(side_effect=AssertionError("base64 decode must not run"))
    monkeypatch.setattr(image_store, "MAX_ENCODED_BYTES", 3)
    monkeypatch.setattr(image_store.base64, "b64decode", decode)
    reference = image_reference(
        "oversized-inline",
        source="data_uri",
        value="data:image/png;base64,AAAAAAA=",
    )
    store = ImageStore(FakeImageClient(b""), max_surface_pixels=(96, 64))

    with pytest.raises(ImageUnavailableError, match="too large"):
        await store.load(reference)

    decode.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "mime", "reason"),
    [
        (png_bytes((4, 4)), "image/jpeg", "MIME type does not match image format"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", "unsupported format"),
        (image_bytes("TIFF"), "image/tiff", "unsupported format"),
        (b"corrupt", "image/png", "could not be decoded"),
    ],
    ids=["mime-mismatch", "svg", "tiff", "corrupt"],
)
async def test_store_rejects_invalid_images(payload: bytes, mime: str, reason: str) -> None:
    store = ImageStore(FakeImageClient(payload, mime), max_surface_pixels=(96, 64))

    with pytest.raises(ImageUnavailableError, match=reason):
        await store.load(image_reference("invalid"))


async def test_store_uses_first_gif_frame() -> None:
    output = io.BytesIO()
    first = PILImage.new("RGB", (4, 4), "#ff0000")
    second = PILImage.new("RGB", (4, 4), "#0000ff")
    first.save(output, format="GIF", save_all=True, append_images=[second])
    store = ImageStore(FakeImageClient(output.getvalue(), "image/gif"), max_surface_pixels=(96, 64))

    asset = await store.load(image_reference("animated"))

    assert asset.image.getpixel((0, 0)) == (255, 0, 0)
    asset.close()


async def test_store_downsamples_before_exif_orientation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.image_store as image_store

    exif = PILImage.Exif()
    exif[274] = 6
    oriented_output = io.BytesIO()
    PILImage.new("RGB", (200, 400), "#123456").save(
        oriented_output,
        format="JPEG",
        exif=exif,
    )
    original_transpose = image_store.ImageOps.exif_transpose
    transpose_inputs: list[tuple[int, int]] = []

    def tracking_transpose(image: PILImage.Image) -> PILImage.Image:
        transpose_inputs.append(image.size)
        return original_transpose(image)

    monkeypatch.setattr(image_store.ImageOps, "exif_transpose", tracking_transpose)
    oriented_store = ImageStore(
        FakeImageClient(oriented_output.getvalue(), "image/jpeg"),
        max_surface_pixels=(40, 20),
    )
    oriented = await oriented_store.load(image_reference("oriented"))

    assert transpose_inputs == [(20, 40)]
    assert oriented.image.size == (40, 20)
    oriented.close()


async def test_store_downsamples_before_alpha_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = png_bytes((400, 200), alpha=True)
    original_convert = PILImage.Image.convert
    conversions: list[tuple[str | None, tuple[int, int]]] = []

    def tracking_convert(
        image: PILImage.Image,
        mode: str | None = None,
        *args,
        **kwargs,
    ) -> PILImage.Image:
        conversions.append((mode, image.size))
        return original_convert(image, mode, *args, **kwargs)

    monkeypatch.setattr(PILImage.Image, "convert", tracking_convert)
    store = ImageStore(FakeImageClient(payload), max_surface_pixels=(40, 20))

    asset = await store.load(image_reference("alpha"))

    assert ("RGBA", (40, 20)) in conversions
    assert asset.image.size == (40, 20)
    assert asset.image.getpixel((0, 0)) == (139, 12, 13)
    asset.close()


async def test_store_rejects_excessive_decoded_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_zero_cli.image_store as image_store

    monkeypatch.setattr(image_store, "MAX_DECODED_PIXELS", 10)
    store = ImageStore(FakeImageClient(png_bytes((4, 4))), max_surface_pixels=(96, 64))

    with pytest.raises(ImageUnavailableError, match="dimensions are too large"):
        await store.load(image_reference("large"))


async def test_cancel_pending_holds_decoder_permits_and_closes_late_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.image_store as image_store

    original_decode = image_store._decode_image
    event_loop = asyncio.get_running_loop()
    state_lock = threading.Lock()
    release_decoders = threading.Event()
    decoder_started = asyncio.Event()
    decoded_assets: list[ImageAsset] = []
    active = 0
    maximum_active = 0

    def blocking_decode(*args) -> ImageAsset:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
            event_loop.call_soon_threadsafe(decoder_started.set)
        release_decoders.wait()
        try:
            asset = original_decode(*args)
            decoded_assets.append(asset)
            return asset
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(image_store, "_decode_image", blocking_decode)
    store = ImageStore(FakeImageClient(png_bytes((4, 4))), max_surface_pixels=(96, 64))
    canceled = [
        asyncio.create_task(store.load(image_reference(f"canceled-{index}")))
        for index in range(4)
    ]
    await decoder_started.wait()

    store.cancel_pending()
    fifth = asyncio.create_task(store.load(image_reference("fifth")))
    barrier = asyncio.Event()
    event_loop.call_soon(barrier.set)
    try:
        await barrier.wait()
        await asyncio.to_thread(lambda: None)
        with state_lock:
            assert active == 1
            assert maximum_active == 1
        assert not fifth.done()
    finally:
        release_decoders.set()

    canceled_results = await asyncio.gather(*canceled, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in canceled_results)
    fifth_asset = await fifth
    assert maximum_active == 1
    for discarded in decoded_assets:
        if discarded.cache_key.startswith("canceled-"):
            with pytest.raises(ValueError, match="closed image"):
                discarded.image.getpixel((0, 0))
    fifth_asset.close()


async def test_store_cancels_pending_and_clear_resets_cached_assets() -> None:
    client = BlockingImageClient(png_bytes((4, 4)))
    store = ImageStore(client, max_surface_pixels=(96, 64))
    pending = asyncio.create_task(store.load(image_reference("pending")))
    await client.started.wait()

    store.cancel_pending()
    with pytest.raises(asyncio.CancelledError):
        await pending

    cached_store = ImageStore(FakeImageClient(png_bytes((4, 4))), max_surface_pixels=(96, 64))
    asset = await cached_store.load(image_reference("cached"))
    assert cached_store.cache_bytes > 0
    cached_store.clear()
    assert cached_store.cache_bytes == 0
    asset.close()
    assert store.cache_bytes == 0
    store.clear()
    assert store.cache_bytes == 0
