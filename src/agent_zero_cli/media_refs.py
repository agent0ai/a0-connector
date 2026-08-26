"""Pure normalization of connector image references for transcript rendering."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Mapping
from urllib.parse import parse_qs, unquote, urlsplit


ImageOwner = Literal["browser", "user", "assistant"]
ImageSource = Literal["agent_zero_path", "data_uri", "unavailable"]

_APPROVED_RASTER_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "image/bmp",
    }
)
_MAX_DATA_BYTES = 25 * 1024 * 1024
_MAX_DATA_URI_LENGTH = 36 * 1024 * 1024
_MAX_REFERENCES_PER_EVENT = 64
_EPHEMERAL_SCREENSHOT_MESSAGE = "ephemeral screenshot is not fetchable"


@dataclass(frozen=True)
class ImageReference:
    entry_key: str
    cache_key: str
    context_id: str
    sequence: int
    owner: ImageOwner
    caption: str
    source: ImageSource
    value: str

    @property
    def copy_text(self) -> str:
        return f"[image: {self.caption}]"


def extract_image_references(
    event: Mapping[str, object], *, base_url: str
) -> tuple[ImageReference, ...]:
    """Return supported image references from one normalized connector event.

    This boundary only validates and normalizes values. It deliberately does not
    perform network, filesystem, image-decoding, or UI work.
    """

    data = _mapping(event.get("data"))
    meta = _mapping(data.get("meta"))
    context_id = str(event.get("context_id", ""))
    sequence = event.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        return ()

    candidates: list[tuple[ImageOwner, str, ImageSource, str]] = []
    event_name = event.get("event")
    tool_name = (
        meta.get("_tool_name") if "_tool_name" in meta else meta.get("tool_name")
    )
    if event_name in {"tool_start", "tool_output"} and tool_name == "browser":
        screenshot = meta.get("Screenshot")
        if isinstance(screenshot, str):
            _append_reference_candidate(
                candidates,
                owner="browser",
                caption="Browser screenshot",
                raw_value=screenshot,
                base_url=base_url,
            )
        _append_browser_snapshot_candidate(candidates, meta.get("browser_snapshot"), base_url)
    elif event_name == "user_message":
        _append_attachment_candidates(candidates, meta.get("attachments"))
    elif event_name == "assistant_message":
        _append_assistant_metadata_candidates(candidates, meta, base_url)
        text = data.get("text")
        if isinstance(text, str):
            for caption, raw_value in _markdown_image_values(text):
                _append_reference_candidate(
                    candidates,
                    owner="assistant",
                    caption=caption or "Assistant image",
                    raw_value=raw_value,
                    base_url=base_url,
                )

    references: list[ImageReference] = []
    seen_cache_keys: set[str] = set()
    for owner, caption, source, value in candidates:
        source_identity = f"{source}:{value}".encode("utf-8")
        cache_key = sha256(source_identity).hexdigest()
        if cache_key in seen_cache_keys:
            continue
        seen_cache_keys.add(cache_key)
        references.append(
            ImageReference(
                entry_key=f"{sequence}:{cache_key}",
                cache_key=cache_key,
                context_id=context_id,
                sequence=sequence,
                owner=owner,
                caption=caption,
                source=source,
                value=value,
            )
        )
    return tuple(references)


def _append_browser_snapshot_candidate(
    candidates: list[tuple[ImageOwner, str, ImageSource, str]],
    snapshot: object,
    base_url: str,
) -> None:
    snapshot_data = _mapping(snapshot)
    for key in ("uri", "a0_path", "path"):
        value = snapshot_data.get(key)
        if isinstance(value, str):
            appended = _append_reference_candidate(
                candidates,
                owner="browser",
                caption="Browser screenshot",
                raw_value=value,
                base_url=base_url,
                allow_direct_a0_path=key in {"a0_path", "path"},
            )
            if appended:
                return
    if isinstance(snapshot_data.get("ephemeral_ref"), str):
        candidates.append(
            (
                "browser",
                "Browser screenshot",
                "unavailable",
                _EPHEMERAL_SCREENSHOT_MESSAGE,
            )
        )


def _append_attachment_candidates(
    candidates: list[tuple[ImageOwner, str, ImageSource, str]], attachments: object
) -> None:
    if not isinstance(attachments, (list, tuple)):
        return
    for attachment in attachments[:_MAX_REFERENCES_PER_EVENT]:
        raw_name = attachment
        if isinstance(attachment, Mapping):
            raw_name = attachment.get("path")
        if not isinstance(raw_name, str):
            continue
        basename = _attachment_basename(raw_name)
        if not basename:
            continue
        candidates.append(
            (
                "user",
                f"User attachment — {basename}",
                "agent_zero_path",
                f"/a0/usr/uploads/{basename}",
            )
        )


def _append_assistant_metadata_candidates(
    candidates: list[tuple[ImageOwner, str, ImageSource, str]],
    meta: Mapping[str, object],
    base_url: str,
) -> None:
    for key in ("image", "image_url", "image_uri", "image_path"):
        value = meta.get(key)
        if isinstance(value, str):
            _append_reference_candidate(
                candidates,
                owner="assistant",
                caption="Assistant image",
                raw_value=value,
                base_url=base_url,
            )

    images = meta.get("images")
    if isinstance(images, (list, tuple)):
        for value in images[:_MAX_REFERENCES_PER_EVENT]:
            if isinstance(value, str):
                _append_reference_candidate(
                    candidates,
                    owner="assistant",
                    caption="Assistant image",
                    raw_value=value,
                    base_url=base_url,
                )


def _append_reference_candidate(
    candidates: list[tuple[ImageOwner, str, ImageSource, str]],
    *,
    owner: ImageOwner,
    caption: str,
    raw_value: str,
    base_url: str,
    allow_direct_a0_path: bool = False,
) -> bool:
    normalized = None
    if allow_direct_a0_path and raw_value.startswith("/a0/"):
        normalized = _agent_zero_reference(raw_value)
    if normalized is None:
        normalized = _normalize_reference(raw_value, base_url)
    if normalized is None:
        return False
    source, value = normalized
    candidates.append((owner, caption, source, value))
    return True


def _normalize_reference(raw_value: str, base_url: str) -> tuple[ImageSource, str] | None:
    if not raw_value or len(raw_value) > _MAX_DATA_URI_LENGTH:
        return None
    if raw_value.startswith("img://"):
        return _agent_zero_reference(raw_value[6:].split("&t=", maxsplit=1)[0])
    if raw_value.startswith("data:"):
        return _data_uri_reference(raw_value)
    return _image_get_reference(raw_value, base_url)


def _agent_zero_reference(value: str) -> tuple[ImageSource, str] | None:
    path = _safe_a0_path(value)
    if path is None:
        return None
    return "agent_zero_path", path


def _image_get_reference(raw_value: str, base_url: str) -> tuple[ImageSource, str] | None:
    try:
        parsed = urlsplit(raw_value)
    except ValueError:
        return None
    if parsed.path != "/api/image_get":
        return None
    if parsed.scheme or parsed.netloc:
        if not _same_origin(parsed, base_url):
            return None
    values = parse_qs(parsed.query, keep_blank_values=True).get("path")
    if not values:
        return None
    return _agent_zero_reference(values[0])


def _same_origin(reference, base_url: str) -> bool:
    try:
        base = urlsplit(base_url)
        return (
            reference.scheme,
            reference.hostname,
            reference.port,
        ) == (base.scheme, base.hostname, base.port)
    except ValueError:
        return False


def _safe_a0_path(value: str) -> str | None:
    path = unquote(value)
    if not path.startswith("/a0/") or "?" in path or "#" in path:
        return None
    if any(part in {".", ".."} for part in path.split("/")):
        return None
    return path


def _data_uri_reference(value: str) -> tuple[ImageSource, str] | None:
    header, separator, payload = value.partition(",")
    if separator != "," or not header.endswith(";base64"):
        return None
    mime_type = header[5:-7].lower()
    if mime_type not in _APPROVED_RASTER_MIME_TYPES or not _valid_base64_payload(payload):
        return None
    padding = payload[-2:].count("=")
    decoded_size = (len(payload) * 3) // 4 - padding
    if decoded_size > _MAX_DATA_BYTES:
        return None
    return "data_uri", value


def _valid_base64_payload(payload: str) -> bool:
    if not payload or len(payload) % 4:
        return False
    padding = payload[-2:].count("=")
    if padding and payload[-padding:] != "=" * padding:
        return False
    content = payload[:-padding] if padding else payload
    if "=" in content:
        return False
    return all(
        "A" <= char <= "Z"
        or "a" <= char <= "z"
        or "0" <= char <= "9"
        or char in "+/"
        for char in content
    )


def _attachment_basename(value: str) -> str:
    normalized = value.replace("\\", "/")
    normalized = normalized.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    basename = normalized.rsplit("/", maxsplit=1)[-1]
    if basename in {"", ".", ".."}:
        return ""
    return basename


def _markdown_image_values(text: str) -> tuple[tuple[str, str], ...]:
    if len(text) > _MAX_DATA_URI_LENGTH:
        return ()
    values: list[tuple[str, str]] = []
    cursor = 0
    while len(values) < _MAX_REFERENCES_PER_EVENT:
        start = text.find("![", cursor)
        if start < 0:
            break
        alt_end = text.find("](", start + 2)
        if alt_end < 0:
            break
        value_end = text.find(")", alt_end + 2)
        if value_end < 0:
            break
        values.append((text[start + 2 : alt_end], text[alt_end + 2 : value_end]))
        cursor = value_end + 1
    return tuple(values)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
