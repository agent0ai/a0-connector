import base64
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from agent_zero_cli.remote_files import RemoteFileUtility


def test_host_files_roundtrip_and_boundaries(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    host = RemoteFileUtility(scan_root=str(root))

    def call(op, path="", **extra):
        return host.handle_file_op(dict(op="files_" + op, path=path, root_path=str(root), **extra))

    content = b"binary\0\xff\r\n"
    encoded = base64.b64encode(content).decode()
    assert call("write", "sample", content=encoded)["ok"]
    assert not call("write", "sample", content=encoded)["ok"]
    result = call("read", "sample")["result"]
    assert base64.b64decode(result["content"]) == content
    assert call("write", "sample", content=encoded, expected=result["revision"])["ok"]
    (root / "sample").write_text("external change")
    assert not call("write", "sample", content=encoded, expected=result["revision"])["ok"]
    assert call("mkdir", "folder")["ok"]
    assert call("rename", "sample", destination="folder/sample")["ok"]
    assert call("list")["result"]["entries"][0]["name"] == "folder"
    assert not call("rename", "folder", destination="folder/nested")["ok"]
    assert not call("remove")["ok"]
    assert not call("read", "../outside")["ok"]
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    assert not call("list", "escape")["ok"]
    assert not call("write", "escape/outside", content=encoded)["ok"]
    assert not call("rename", "folder/sample", destination="../outside")["ok"]
    assert not host.handle_file_op(dict(op="files_list", root_path="/another"))["ok"]
    host.set_write_enabled(False)
    for op in ("write", "mkdir", "rename", "remove"):
        assert not call(op, "folder/sample", content=encoded, destination="other")["ok"]
    assert call("read", "folder/sample")["ok"]
    host.set_write_enabled(True)
    assert call("remove", "folder/sample")["ok"]
    assert call("remove", "folder")["ok"]


def test_launcher_session_uses_configured_folder_and_scopes(tmp_path):
    from agent_zero_cli.config import CLIConfig
    from agent_zero_cli.session import ConnectorSession

    (tmp_path / "launcher.txt").write_text("launcher")
    session = ConnectorSession(CLIConfig(), workspace=tmp_path, tools_only=True,
        remote_file_write_enabled=False,
        gateway={"version": 1, "kind": "launcher", "id": "test", "master_enabled": True,
                 "scopes": {"files": True, "file_write": False}})
    metadata = session._remote_file_metadata()
    assert metadata["file_browser"] == 1 and metadata["root_path"] == str(tmp_path)
    request = {"op": "files_list", "root_path": str(tmp_path)}
    assert asyncio.run(session._handle_file_op(request))["ok"]
    assert not asyncio.run(session._handle_file_op(request | {"op": "files_remove", "path": "launcher.txt"}))["ok"]
    session.master_enabled = False
    assert not asyncio.run(session._handle_file_op(request))["ok"]
    assert (tmp_path / "launcher.txt").read_text() == "launcher"


@pytest.mark.parametrize("failure", [None, "permission", "disconnect", "digest", "cancel"])
def test_streamed_host_upload_is_atomic_and_rechecks_access(tmp_path, failure):
    from agent_zero_cli.file_browser import write_http
    content = b"\0binary\xff" * (400 * 1024)
    digest = hashlib.sha256(content).hexdigest()
    host = RemoteFileUtility(scan_root=str(tmp_path))
    class Client:
        connected = True
        sio = SimpleNamespace(sid="connection")
        async def download_file(self, source, destination, expected_size):
            assert source == "/a0/tmp/staged" and expected_size == len(content)
            destination.write_bytes(content)
            assert not (tmp_path / "destination").exists()
            if failure == "permission": host.set_write_enabled(False)
            if failure == "disconnect": self.connected = False
            if failure == "cancel": raise asyncio.CancelledError()
            return {"sha256": "bad" if failure == "digest" else digest}
    request = {"path": "destination", "root_path": str(tmp_path), "source_path": "/a0/tmp/staged",
               "size": len(content), "sha256": digest}
    if failure:
        with pytest.raises((PermissionError, ConnectionError, ValueError, asyncio.CancelledError)):
            asyncio.run(write_http(host, request, Client()))
        assert not list(tmp_path.iterdir())
    else:
        assert asyncio.run(write_http(host, request, Client())) == {"revision": digest}
        assert (tmp_path / "destination").read_bytes() == content
        assert not list(tmp_path.glob(".partial-*"))
        with pytest.raises(FileExistsError):
            asyncio.run(write_http(host, request, Client()))
    with pytest.raises(ValueError, match="Invalid upload size"):
        host.set_write_enabled(True)
        asyncio.run(write_http(host, request | {"size": -1}, Client()))
