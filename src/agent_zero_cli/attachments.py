from __future__ import annotations

import os
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
}
_PREFERRED_IMAGE_MIME_TYPES = (
    "image/png",
    "image/webp",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/bmp",
    "image/tiff",
)


class AttachmentError(RuntimeError):
    """Raised when an attachment cannot be created without changing representation."""


@dataclass(frozen=True)
class AttachmentRef:
    path: str
    name: str
    mime_type: str


def attachment_label(count: int) -> str:
    noun = "attachment" if count == 1 else "attachments"
    return f"[{count} Image {noun}]"


def save_clipboard_image_attachment() -> AttachmentRef:
    mime_type, image_bytes = read_clipboard_image_bytes()
    extension = _IMAGE_MIME_EXTENSIONS[mime_type]
    filename = f"clipboard-{uuid.uuid4().hex}{extension}"
    host_root = _host_upload_root()
    host_root.mkdir(parents=True, exist_ok=True)
    host_path = host_root / filename
    host_path.write_bytes(image_bytes)
    return AttachmentRef(
        path=f"{_container_upload_root()}/{filename}",
        name=filename,
        mime_type=mime_type,
    )


def read_clipboard_image_bytes() -> tuple[str, bytes]:
    if sys.platform.startswith("linux"):
        result = _read_linux_clipboard_image()
        if result is not None:
            return result
        raise AttachmentError(
            "Clipboard does not currently expose a supported image MIME type."
        )
    raise AttachmentError("Clipboard image paste is not supported on this platform yet.")


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
