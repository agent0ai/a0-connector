from __future__ import annotations

from io import BytesIO
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import uuid


_HOST_UPLOAD_ROOT_ENV = "A0_CONNECTOR_UPLOADS_HOST_ROOT"
_CONTAINER_UPLOAD_ROOT_ENV = "A0_CONNECTOR_UPLOADS_CONTAINER_ROOT"
_DEFAULT_CONTAINER_UPLOAD_ROOT = "/a0/usr/uploads"
_CLIPBOARD_TIMEOUT_SECONDS = 2.0
_IMAGE_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
}
_PREFERRED_IMAGE_MIME_TYPES = (
    "image/png",
    "image/webp",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/bmp",
    "image/tiff",
    "image/svg+xml",
)
_IMAGE_EXTENSION_MIME_TYPES = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
}
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentError(RuntimeError):
    """Raised when an attachment cannot be created without changing representation."""


@dataclass(frozen=True)
class AttachmentRef:
    path: str
    name: str
    mime_type: str


@dataclass(frozen=True)
class AttachmentUpload:
    filename: str
    content: bytes
    mime_type: str


def attachment_label(count: int) -> str:
    noun = "attachment" if count == 1 else "attachments"
    return f"[{count} Image {noun}]"


def create_clipboard_image_upload() -> AttachmentUpload:
    mime_type, image_bytes = read_clipboard_image_bytes()
    extension = _IMAGE_MIME_EXTENSIONS[mime_type]
    filename = f"clipboard-{uuid.uuid4().hex}{extension}"
    return AttachmentUpload(filename=filename, content=image_bytes, mime_type=mime_type)


def create_image_file_upload(path: str | Path) -> AttachmentUpload:
    source = Path(path).expanduser()
    if not source.is_file():
        raise AttachmentError(f"Image file not found: {source}")

    mime_type = _mime_type_for_image_path(source)
    if not mime_type:
        supported = ", ".join(sorted(_IMAGE_EXTENSION_MIME_TYPES))
        raise AttachmentError(f"Unsupported image type: {source}. Supported extensions: {supported}.")

    return AttachmentUpload(
        filename=_unique_upload_filename(source),
        content=source.read_bytes(),
        mime_type=mime_type,
    )


def create_file_upload(path: str | Path, *, max_bytes: int = 0) -> AttachmentUpload:
    source = Path(path).expanduser()
    if not source.is_file():
        raise AttachmentError(f"File not found: {source}")

    read_limit = max(0, int(max_bytes))
    with source.open("rb") as stream:
        content = stream.read(read_limit + 1 if read_limit else -1)
    if read_limit and len(content) > read_limit:
        raise AttachmentError(f"File is larger than {read_limit} bytes: {source}")

    return AttachmentUpload(
        filename=_unique_upload_filename(source),
        content=content,
        mime_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
    )


def remote_upload_path(filename: str) -> str:
    safe_name = PurePosixPath(str(filename).replace("\\", "/")).name
    return f"{_container_upload_root()}/{safe_name}"


def save_clipboard_image_attachment() -> AttachmentRef:
    upload = create_clipboard_image_upload()
    host_root = _host_upload_root()
    host_root.mkdir(parents=True, exist_ok=True)
    host_path = host_root / upload.filename
    host_path.write_bytes(upload.content)
    return AttachmentRef(
        path=remote_upload_path(upload.filename),
        name=upload.filename,
        mime_type=upload.mime_type,
    )


def read_clipboard_image_bytes() -> tuple[str, bytes]:
    if sys.platform.startswith("linux"):
        result = _read_linux_clipboard_image()
    elif sys.platform in {"darwin", "win32"}:
        result = _read_pillow_clipboard_image()
    else:
        raise AttachmentError("Clipboard image paste is not supported on this platform yet.")

    if result is not None:
        return result
    raise AttachmentError("Clipboard does not currently expose a supported image MIME type.")


def _read_linux_clipboard_image() -> tuple[str, bytes] | None:
    if shutil.which("wl-paste"):
        result = _read_wl_paste_image()
        if result is not None:
            return result

    if shutil.which("xclip"):
        result = _read_xclip_image()
        if result is not None:
            return result

    return None


def _read_pillow_clipboard_image() -> tuple[str, bytes] | None:
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise AttachmentError("Clipboard image paste requires Pillow on this platform.") from exc

    try:
        contents = ImageGrab.grabclipboard()
    except Exception:
        return None

    if isinstance(contents, list):
        for value in contents:
            path = Path(value)
            mime_type = _mime_type_for_image_path(path)
            if mime_type and path.is_file():
                return mime_type, path.read_bytes()
        return None

    if contents is None or not callable(getattr(contents, "save", None)):
        return None

    image_bytes = BytesIO()
    contents.save(image_bytes, format="PNG")
    return "image/png", image_bytes.getvalue()


def _read_wl_paste_image() -> tuple[str, bytes] | None:
    types = _run_text_command(["wl-paste", "--list-types"])
    mime_type = _select_image_mime_type(types.splitlines())
    if not mime_type:
        return None
    data = _run_binary_command(["wl-paste", "--type", mime_type])
    if not data:
        return None
    return mime_type, data


def _read_xclip_image() -> tuple[str, bytes] | None:
    types = _run_text_command(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    mime_type = _select_image_mime_type(types.splitlines())
    if not mime_type:
        return None
    data = _run_binary_command(["xclip", "-selection", "clipboard", "-t", mime_type, "-o"])
    if not data:
        return None
    return mime_type, data


def _select_image_mime_type(types: list[str]) -> str:
    normalized = {item.strip().lower() for item in types if item.strip()}
    for mime_type in _PREFERRED_IMAGE_MIME_TYPES:
        if mime_type in normalized:
            return mime_type
    return ""


def _run_text_command(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_CLIPBOARD_TIMEOUT_SECONDS,
        )
    except Exception:
        return ""
    return completed.stdout.decode("utf-8", errors="replace")


def _run_binary_command(command: list[str]) -> bytes:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_CLIPBOARD_TIMEOUT_SECONDS,
        )
    except Exception:
        return b""
    return completed.stdout


def _mime_type_for_image_path(path: Path) -> str:
    return _IMAGE_EXTENSION_MIME_TYPES.get(path.suffix.lower(), "")


def _sanitize_filename_stem(stem: str) -> str:
    sanitized = _SAFE_STEM_RE.sub("-", stem).strip("._-")
    return sanitized or "image"


def _unique_upload_filename(path: Path) -> str:
    suffix = path.suffix.lower()[:16]
    stem = _sanitize_filename_stem(path.stem)[:180].rstrip("._-") or "file"
    return f"{stem}-{uuid.uuid4().hex}{suffix}"


def _container_upload_root() -> str:
    root = str(os.environ.get(_CONTAINER_UPLOAD_ROOT_ENV, _DEFAULT_CONTAINER_UPLOAD_ROOT) or "").strip()
    if not root:
        root = _DEFAULT_CONTAINER_UPLOAD_ROOT
    return root.rstrip("/")


def _host_upload_root() -> Path:
    configured = str(os.environ.get(_HOST_UPLOAD_ROOT_ENV, "")).strip()
    if configured:
        return Path(configured).expanduser()

    volume_root = _find_dockervolume_root()
    if volume_root is not None:
        return _host_path_from_container_root(_container_upload_root(), volume_root=volume_root)

    raise AttachmentError(
        "No Agent Zero upload directory is configured. Use the HTTP upload path instead."
    )


def _path_search_roots() -> list[Path]:
    roots: list[Path] = []
    for candidate in (Path.cwd(), Path(__file__).resolve(), Path(sys.executable).resolve()):
        resolved = Path(candidate)
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _find_dockervolume_root() -> Path | None:
    seen: set[str] = set()
    for anchor in _path_search_roots():
        for candidate in (anchor, *anchor.parents):
            marker = str(candidate).lower()
            if marker in seen:
                continue
            seen.add(marker)
            if candidate.name.lower() == "dockervolume" and candidate.is_dir():
                return candidate
            sibling = candidate / "dockervolume"
            if sibling.is_dir():
                return sibling
    return None


def _host_path_from_container_root(container_root: str, *, volume_root: Path) -> Path:
    normalized = container_root.strip().replace("\\", "/").rstrip("/")
    try:
        relative_root = PurePosixPath(normalized).relative_to("/a0")
    except ValueError:
        segments = [part for part in PurePosixPath(normalized).parts if part not in {"/", "\\"}]
        return volume_root.joinpath(*segments)
    return volume_root.joinpath(*relative_root.parts)
