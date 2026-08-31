from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from agent_zero_cli import attachments as attachments_mod


def test_attachment_label_pluralizes() -> None:
    assert attachments_mod.attachment_label(1) == "[1 Image attachment]"
    assert attachments_mod.attachment_label(2) == "[2 Image attachments]"


def test_save_clipboard_image_attachment_writes_exact_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("A0_CONNECTOR_UPLOADS_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("A0_CONNECTOR_UPLOADS_CONTAINER_ROOT", "/a0/usr/uploads")
    monkeypatch.setattr(
        attachments_mod,
        "read_clipboard_image_bytes",
        lambda: ("image/png", b"png-bytes"),
    )

    attachment = attachments_mod.save_clipboard_image_attachment()

    assert attachment.path.startswith("/a0/usr/uploads/clipboard-")
    assert attachment.path.endswith(".png")
    assert attachment.name == attachment.path.rsplit("/", maxsplit=1)[-1]
    assert attachment.mime_type == "image/png"
    assert (tmp_path / attachment.name).read_bytes() == b"png-bytes"


def test_create_clipboard_image_upload_reads_exact_bytes(monkeypatch) -> None:
    monkeypatch.setattr(
        attachments_mod,
        "read_clipboard_image_bytes",
        lambda: ("image/webp", b"webp-bytes"),
    )

    upload = attachments_mod.create_clipboard_image_upload()

    assert upload.filename.startswith("clipboard-")
    assert upload.filename.endswith(".webp")
    assert upload.mime_type == "image/webp"
    assert upload.content == b"webp-bytes"


def test_read_clipboard_image_uses_pillow_on_macos_and_windows(monkeypatch) -> None:
    expected = ("image/png", b"png-bytes")
    monkeypatch.setattr(attachments_mod, "_read_pillow_clipboard_image", lambda: expected)

    for platform in ("darwin", "win32"):
        monkeypatch.setattr(attachments_mod.sys, "platform", platform)
        assert attachments_mod.read_clipboard_image_bytes() == expected


def test_pillow_clipboard_image_is_encoded_as_png(monkeypatch) -> None:
    class FakeImage:
        def save(self, output, *, format: str) -> None:
            assert format == "PNG"
            output.write(b"png-bytes")

    monkeypatch.setitem(
        sys.modules,
        "PIL",
        SimpleNamespace(ImageGrab=SimpleNamespace(grabclipboard=lambda: FakeImage())),
    )

    assert attachments_mod._read_pillow_clipboard_image() == ("image/png", b"png-bytes")


def test_pillow_clipboard_image_reads_copied_image_file(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "copied.webp"
    source.write_bytes(b"webp-bytes")
    monkeypatch.setitem(
        sys.modules,
        "PIL",
        SimpleNamespace(ImageGrab=SimpleNamespace(grabclipboard=lambda: [str(source)])),
    )

    assert attachments_mod._read_pillow_clipboard_image() == ("image/webp", b"webp-bytes")


def test_create_image_file_upload_reads_supported_image(tmp_path: Path) -> None:
    source = tmp_path / "diagram final.PNG"
    source.write_bytes(b"png-bytes")

    upload = attachments_mod.create_image_file_upload(source)

    assert upload.filename.startswith("diagram-final-")
    assert upload.filename.endswith(".png")
    assert upload.mime_type == "image/png"
    assert upload.content == b"png-bytes"


def test_create_image_file_upload_rejects_non_image(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("not an image", encoding="utf-8")

    try:
        attachments_mod.create_image_file_upload(source)
    except attachments_mod.AttachmentError as exc:
        assert "Unsupported image type" in str(exc)
    else:
        raise AssertionError("Expected AttachmentError")


def test_create_file_upload_preserves_bytes_and_detects_mime_type(tmp_path: Path) -> None:
    source = tmp_path / "project notes.txt"
    source.write_bytes(b"plain-text")

    upload = attachments_mod.create_file_upload(source, max_bytes=32)

    assert upload.filename.startswith("project-notes-")
    assert upload.filename.endswith(".txt")
    assert upload.mime_type == "text/plain"
    assert upload.content == b"plain-text"


def test_create_file_upload_enforces_read_limit(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"12345")

    try:
        attachments_mod.create_file_upload(source, max_bytes=4)
    except attachments_mod.AttachmentError as exc:
        assert "larger than 4 bytes" in str(exc)
    else:
        raise AssertionError("Expected AttachmentError")


def test_remote_upload_path_normalizes_server_filename() -> None:
    assert (
        attachments_mod.remote_upload_path("nested\\server-image.png")
        == "/a0/usr/uploads/server-image.png"
    )
