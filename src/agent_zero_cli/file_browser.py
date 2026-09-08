"""Binary-safe Files operations inside the Connector's existing workspace boundary."""
from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
import stat
import tempfile
import asyncio
import io
import uuid
from agent_zero_cli.file_browser_paths import parent_directory, open_regular, rename_new


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


def copy_file(source, target, limit=None):
    size, digest = 0, hashlib.sha256()
    while chunk := source.read(1024 * 1024):
        size += len(chunk)
        if limit is not None and size > limit:
            raise ValueError("File exceeds the requested transfer size.")
        if target is not None:
            target.write(chunk)
        digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def publish(workspace, data, source, check_access=None):
    path = checked_path(workspace, data, writing=True)
    with parent_directory(workspace, path) as (parent, name):
        temporary = ".partial-" + uuid.uuid4().hex
        if parent is None:
            temporary = str(path.with_name(temporary))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as target:
                receipt = copy_file(source, target, data.get("size"))
                target.flush()
                os.fsync(target.fileno())
            if data.get("sha256") is not None and receipt["sha256"] != data["sha256"]:
                raise ValueError("Upload SHA-256 mismatch.")
            if data.get("size") is not None and receipt["size"] != data["size"]:
                raise ValueError("Upload size mismatch.")
            if check_access:
                check_access()
            checked_path(workspace, data, writing=True)
            with parent_directory(workspace, path) as (current, _):
                if parent is not None:
                    before, after = os.fstat(parent), os.fstat(current)
                    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                        raise PermissionError("The destination folder changed during upload.")
            expected = data.get("expected")
            if expected is None:
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            else:
                with open_regular(workspace, parent, name) as original:
                    mode = stat.S_IMODE(os.fstat(original.fileno()).st_mode)
                    if copy_file(original, None)["sha256"] != expected:
                        raise ValueError("File changed on the host. Reopen it before saving.")
                os.chmod(temporary, mode, dir_fd=parent)
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            return {"revision": receipt["sha256"]}
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


async def write_http(workspace, data, client, check_access=None):
    size = data.get("size")
    if type(size) is not int or size < 0:
        raise ValueError("Invalid upload size.")
    path = checked_path(workspace, data, writing=True)
    if data.get("expected") is None:
        with parent_directory(workspace, path) as (parent, name):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError("The destination already exists.")
    connection = client.sio.sid
    def check():
        if not client.connected or client.sio.sid != connection:
            raise ConnectionError("The host disconnected during upload.")
        if check_access:
            check_access()
    with tempfile.TemporaryDirectory(prefix="a0-files-upload-") as directory:
        temporary = Path(directory) / "content"
        result = await client.download_file(data["source_path"], temporary, expected_size=size)
        if result["sha256"] != data.get("sha256"):
            raise ValueError("Upload SHA-256 mismatch.")
        check()
        with temporary.open("rb") as source:
            return await asyncio.to_thread(publish, workspace, data, source, check)


async def read_http(workspace, data, client, check_access=None):
    from agent_zero_cli.attachments import AttachmentUpload
    limit = data.get("limit")
    if type(limit) is not int or limit < 0:
        raise ValueError("Invalid download size limit.")
    connection = client.sio.sid
    def snapshot(target):
        path = checked_path(workspace, data)
        with parent_directory(workspace, path) as (parent, name):
            with open_regular(workspace, parent, name) as source, target.open("wb") as output:
                return copy_file(source, output, limit)
    with tempfile.TemporaryDirectory(prefix="a0-files-download-") as directory:
        temporary = Path(directory) / "content"
        receipt = await asyncio.to_thread(snapshot, temporary)
        checked_path(workspace, data)
        if check_access:
            check_access()
        if not client.connected or client.sio.sid != connection:
            raise ConnectionError("The host disconnected during download.")
        await client.upload_attachments([AttachmentUpload("content", temporary, "application/octet-stream")],
                                        transfer_token=data["transfer_token"])
        return {"revision": receipt["sha256"]}


def handle(workspace, data):
    op = data["op"].removeprefix("files_")
    path = checked_path(workspace, data, writing=op in {"write", "mkdir", "rename", "remove"})
    def info(name, value):
        return {"name": name, "is_dir": stat.S_ISDIR(value.st_mode),
                "is_link": stat.S_ISLNK(value.st_mode), "size": value.st_size,
                "modified": datetime.fromtimestamp(value.st_mtime, timezone.utc).isoformat()}
    with parent_directory(workspace, path) as (parent, name):
        if op == "list":
            entries = []
            if parent is None:
                iterator = os.scandir(path)
                descriptor = None
            else:
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                iterator = os.scandir(descriptor)
            try:
                with iterator:
                    for entry in iterator:
                        if len(entries) >= 10000:
                            raise ValueError("This folder exceeds 10,000 entries.")
                        try:
                            value = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode):
                            entries.append(info(entry.name, value))
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            return {"entries": entries}
        if op == "stat":
            return info(path.name, os.stat(name, dir_fd=parent, follow_symlinks=False))
        if op == "read":
            limit = min(MAX_BYTES, max(0, int(data.get("limit", MAX_BYTES))))
            with open_regular(workspace, parent, name) as stream:
                content = stream.read(limit + 1)
            if len(content) > limit:
                raise ValueError("File exceeds the transfer size limit.")
            return {"content": base64.b64encode(content).decode("ascii"), "revision": hashlib.sha256(content).hexdigest()}
        if op == "write":
            encoded = data.get("content", "")
            if not isinstance(encoded, str) or len(encoded) > (INLINE_BYTES + 2) // 3 * 4:
                raise ValueError("Use HTTP for file writes larger than 1 MiB.")
            content = base64.b64decode(encoded, validate=True)
            if len(content) > INLINE_BYTES:
                raise ValueError("Use HTTP for file writes larger than 1 MiB.")
            return publish(workspace, data, io.BytesIO(content))
        if op == "mkdir":
            os.mkdir(name, dir_fd=parent)
        elif op == "rename":
            target_data = dict(data, path=data.get("destination", ""))
            destination = checked_path(workspace, target_data, writing=True)
            if path == destination or path in destination.parents:
                raise ValueError("A folder cannot be moved into itself.")
            with parent_directory(workspace, destination) as (target_parent, target_name):
                try:
                    os.stat(target_name, dir_fd=target_parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError("The destination already exists.")
                rename_new(parent, name, target_parent, target_name)
        elif op == "remove":
            value = os.stat(name, dir_fd=parent, follow_symlinks=False)
            (os.rmdir if stat.S_ISDIR(value.st_mode) else os.unlink)(name, dir_fd=parent)
        else:
            raise ValueError("Unknown File Browser operation.")
    return {}
