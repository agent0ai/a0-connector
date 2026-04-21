from __future__ import annotations

from pathlib import Path

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
