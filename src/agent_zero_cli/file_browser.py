"""Binary-safe Files operations inside the Connector's existing workspace boundary."""
from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
import stat
import tempfile


MAX_BYTES = 100 * 1024 * 1024
INLINE_BYTES = 1024 * 1024


def checked_path(workspace, data, writing=False):
    if data.get("root_path") != workspace.scan_root:
        raise PermissionError("The exposed host folder changed. Reopen it from Files settings.")
    if writing and not workspace.allow_writes:
        raise PermissionError("Host file writes are disabled. Enable writing in the CLI or Launcher.")
    path = Path(workspace._expand_file_path(data.get("path") or "."))
    root = Path(workspace.scan_root)
    if writing and path == root:
        raise PermissionError("The exposed folder itself cannot be changed.")
    if path.is_symlink():
        raise PermissionError("File Browser does not follow symbolic links.")
    return path


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def write_http(workspace, data, client):
    path = checked_path(workspace, data, writing=True)
    size = data.get("size")
    if type(size) is not int or size < 0:
        raise ValueError("Invalid upload size.")
    expected = data.get("expected")
    if expected is None and os.path.lexists(path):
        raise FileExistsError("The destination already exists.")
    parent = path.parent.resolve()
    descriptor, temporary = tempfile.mkstemp(prefix=".partial-", dir=parent)
    os.close(descriptor)
    temporary = Path(temporary)
    connection = client.sio.sid
    try:
        result = await client.download_file(data["source_path"], temporary, expected_size=size)
        if result["sha256"] != data.get("sha256"):
            raise ValueError("Upload SHA-256 mismatch.")
        if not client.connected or client.sio.sid != connection:
            raise ConnectionError("The host disconnected during upload.")
        checked_path(workspace, data, writing=True)
        if path.parent.resolve() != parent:
            raise PermissionError("The destination folder changed during upload.")
        if expected is None:
            os.link(temporary, path)
        else:
            if not path.is_file() or digest_file(path) != expected:
                raise ValueError("File changed on the host. Reopen it before saving.")
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
        return {"revision": result["sha256"]}
    finally:
        temporary.unlink(missing_ok=True)


async def read_http(workspace, data, client):
    from agent_zero_cli.attachments import AttachmentUpload
    path = checked_path(workspace, data)
    limit = data.get("limit")
    if type(limit) is not int or limit < 0:
        raise ValueError("Invalid download size limit.")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Choose a regular file.")
    if path.stat().st_size > limit:
        raise ValueError("File exceeds the transfer size limit.")
    digest = digest_file(path)
    await client.upload_attachments([AttachmentUpload("content", path, "application/octet-stream")],
                                    transfer_token=data["transfer_token"])
    return {"revision": digest}


def handle(workspace, data):
    op = data["op"].removeprefix("files_")
    path = checked_path(workspace, data, writing=op in {"write", "mkdir", "rename", "remove"})
    root = Path(workspace.scan_root)

    def info(target):
        value = target.lstat()
        return {"name": target.name, "is_dir": stat.S_ISDIR(value.st_mode),
                "is_link": stat.S_ISLNK(value.st_mode), "size": value.st_size,
                "modified": datetime.fromtimestamp(value.st_mtime, timezone.utc).isoformat()}

    def read(limit):
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Choose a regular file.")
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Choose a regular file.")
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("File exceeds the transfer size limit.")
        return content

    if op == "list":
        entries = []
        for target in path.iterdir():
            if len(entries) >= 10000:
                raise ValueError("This folder exceeds 10,000 entries.")
            try:
                entry = info(target)
            except FileNotFoundError:
                continue
            if entry["is_dir"] or stat.S_ISREG(target.lstat().st_mode):
                entries.append(entry)
        return {"entries": entries}
    if op == "stat":
        return info(path)
    if op == "read":
        content = read(min(MAX_BYTES, max(0, int(data.get("limit", MAX_BYTES)))))
        return {"content": base64.b64encode(content).decode("ascii"),
                "revision": hashlib.sha256(content).hexdigest()}
    if op == "write":
        encoded = data.get("content", "")
        if not isinstance(encoded, str) or len(encoded) > (INLINE_BYTES + 2) // 3 * 4:
            raise ValueError("Use HTTP for file writes larger than 1 MiB.")
        content = base64.b64decode(encoded, validate=True)
        if len(content) > INLINE_BYTES:
            raise ValueError("Use HTTP for file writes larger than 1 MiB.")
        expected = data.get("expected")
        if expected is None:
            with path.open("xb") as stream:
                stream.write(content)
        else:
            if hashlib.sha256(read(MAX_BYTES)).hexdigest() != expected:
                raise ValueError("File changed on the host. Reopen it before saving.")
            descriptor, temporary = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
                if hashlib.sha256(read(MAX_BYTES)).hexdigest() != expected:
                    raise ValueError("File changed on the host. Reopen it before saving.")
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {"revision": hashlib.sha256(content).hexdigest()}
    if op == "mkdir":
        path.mkdir()
    elif op == "rename":
        destination = Path(workspace._expand_file_path(data.get("destination", "")))
        if destination == root or os.path.lexists(destination):
            raise ValueError("The destination already exists.")
        if path.is_dir() and path in destination.parents:
            raise ValueError("A folder cannot be moved into itself.")
        path.rename(destination)
    elif op == "remove":
        path.rmdir() if path.is_dir() else path.unlink()
    else:
        raise ValueError("Unknown File Browser operation.")
    return {}
