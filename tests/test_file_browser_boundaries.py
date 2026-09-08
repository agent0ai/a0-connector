import asyncio
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_zero_cli import file_browser as files
from agent_zero_cli.remote_files import RemoteFileUtility


@pytest.fixture
def folders(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir(); outside.mkdir(); (root / "child").mkdir()
    (root / "child" / "note").write_bytes(b"inside")
    (outside / "note").write_bytes(b"outside")
    return RemoteFileUtility(scan_root=str(root)), root, outside


def swap_parent(root, outside):
    (root / "child").rename(root / "moved")
    (root / "child").symlink_to(outside, target_is_directory=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink race reproduction")
@pytest.mark.parametrize("operation", ["list", "stat", "read", "mkdir", "remove", "rename"])
def test_parent_swap_cannot_escape_workspace(folders, monkeypatch, operation):
    workspace, root, outside = folders
    original = files.checked_path
    def checked(*args, **kwargs):
        path = original(*args, **kwargs)
        swap_parent(root, outside)
        return path
    monkeypatch.setattr(files, "checked_path", checked)
    request = dict(op="files_" + operation, root_path=str(root), path="child/note", destination="other")
    with pytest.raises((OSError, ValueError)):
        files.handle(workspace, request)
    assert (outside / "note").read_bytes() == b"outside"
    assert list(outside.iterdir()) == [outside / "note"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink race reproduction")
def test_http_read_does_not_upload_replacement_symlink(folders, monkeypatch):
    workspace, root, outside = folders
    original = files.checked_path
    def checked(*args, **kwargs):
        path = original(*args, **kwargs)
        swap_parent(root, outside)
        return path
    monkeypatch.setattr(files, "checked_path", checked)
    class Client:
        connected = True
        sio = SimpleNamespace(sid="sid")
        async def upload_attachments(self, *args, **kwargs):
            pytest.fail("An out-of-scope file reached the upload client")
    request = dict(root_path=str(root), path="child/note", limit=100, transfer_token="token")
    with pytest.raises(OSError):
        asyncio.run(files.read_http(workspace, request, Client()))


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink race reproduction")
def test_http_write_never_stages_outside_changed_parent(folders):
    workspace, root, outside = folders
    body = b"upload"
    class Client:
        connected = True
        sio = SimpleNamespace(sid="sid")
        async def download_file(self, source, destination, expected_size):
            swap_parent(root, outside)
            destination.write_bytes(body)
            assert list(outside.iterdir()) == [outside / "note"]
            return {"sha256": hashlib.sha256(body).hexdigest()}
    request = dict(root_path=str(root), path="child/new", size=len(body), source_path="staged",
                   sha256=hashlib.sha256(body).hexdigest())
    with pytest.raises(OSError):
        asyncio.run(files.write_http(workspace, request, Client()))
    assert list(outside.iterdir()) == [outside / "note"]
    assert not list((root / "moved").glob(".partial-*"))


def test_read_rechecks_scope_before_upload(folders):
    workspace, root, _ = folders
    snapshots = []
    class Client:
        connected = True
        sio = SimpleNamespace(sid="sid")
        async def upload_attachments(self, *args, **kwargs):
            pytest.fail("Revoked access reached the upload client")
    def revoked():
        raise PermissionError("Files paused")
    with pytest.raises(PermissionError, match="paused"):
        asyncio.run(files.read_http(workspace, dict(root_path=str(root), path="child/note", limit=100), Client(), revoked))


def test_rename_does_not_overwrite_racing_destination(folders, monkeypatch):
    from agent_zero_cli import file_browser_paths
    workspace, root, _ = folders
    original = files.rename_new
    def concurrent(source_fd, source, target_fd, target):
        (root / "destination").write_bytes(b"keep")
        return original(source_fd, source, target_fd, target)
    monkeypatch.setattr(files, "rename_new", concurrent)
    with pytest.raises(FileExistsError):
        files.handle(workspace, dict(op="files_rename", root_path=str(root), path="child/note", destination="destination"))
    assert (root / "destination").read_bytes() == b"keep"
    assert (root / "child" / "note").read_bytes() == b"inside"
