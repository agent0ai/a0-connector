"""Host browser transfers use the connector's shared folder and live file grants."""
from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
import tempfile
import threading

from agent_zero_cli.file_browser import MAX_BYTES, checked_path, copy_file, publish
from agent_zero_cli.file_browser_paths import open_regular, parent_directory


_MIN_FILE_CHARGE = 4096  # Empty files still consume staging directories and inodes.
_UPLOAD_LIMIT_ERROR = "Host browser upload exceeds the 100 MiB session budget. Close a tab with staged files to free space."


def snapshot_uploads(workspace, paths: list[str], directory: Path, available: int, revoked: threading.Event):
    snapshots = []
    charged = 0
    for index, name in enumerate(paths):
        source = checked_path(workspace, {"root_path": workspace.scan_root, "path": name})
        destination = directory / str(index) / source.name
        with parent_directory(workspace, source) as (parent, filename), open_regular(workspace, parent, filename) as stream:
            remaining = available - charged
            if max(os.fstat(stream.fileno()).st_size, _MIN_FILE_CHARGE) > remaining:
                raise ValueError(_UPLOAD_LIMIT_ERROR)
            destination.parent.mkdir()
            size = 0
            with destination.open("xb") as target:
                while True:
                    if revoked.is_set():
                        raise PermissionError("The host upload was revoked.")
                    chunk = stream.read(min(1024 * 1024, remaining - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > remaining:
                        raise ValueError(_UPLOAD_LIMIT_ERROR)
                    target.write(chunk)
        snapshots.append(str(destination))
        charged += max(size, _MIN_FILE_CHARGE)
    return snapshots, charged


class UploadSnapshots:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._batches: dict[int, list[tuple[tempfile.TemporaryDirectory, int]]] = {}
        self._pending: tuple[int, threading.Event] | None = None
        self._grant = None
        self._generation = 0

    def sync_workspace(self, workspace):
        grant = (workspace, workspace.scan_root) if workspace is not None else None
        if self._grant != grant:
            self.clear()
            self._grant = grant

    def clear(self, tab: int | None = None):
        if self._pending and (tab is None or tab == self._pending[0]):
            self._pending[1].set()
        if tab is None:
            self._generation += 1
        for key in list(self._batches) if tab is None else [tab]:
            batches = self._batches.get(key)
            while batches:
                batches[-1][0].cleanup()
                batches.pop()
            self._batches.pop(key, None)

    async def release(self, tab: int | None = None):
        self.clear(tab)
        if self._pending and self._pending[1].is_set():
            async with self._lock:
                pass  # The worker has removed its partial copy.

    async def attach(self, tab, workspace, paths, check_access, transfer):
        check_access()
        self.sync_workspace(workspace)
        generation = self._generation
        async with self._lock:
            check_access()
            if generation != self._generation:
                raise PermissionError("The host upload was revoked.")
            available = MAX_BYTES - sum(size for batches in self._batches.values() for _, size in batches)
            batch = tempfile.TemporaryDirectory(prefix="a0-browser-files-")
            revoked = threading.Event()
            self._pending = (tab, revoked)
            copy = asyncio.create_task(asyncio.to_thread(snapshot_uploads, workspace, paths, Path(batch.name), available, revoked))
            try:
                snapshots, size = await asyncio.shield(copy)
                check_access()
                if revoked.is_set():
                    raise PermissionError("The host upload was revoked.")
                await transfer(snapshots)
                check_access()
                if revoked.is_set():
                    raise PermissionError("The host upload was revoked.")
                self._batches.setdefault(tab, []).append((batch, size))
            except BaseException:
                revoked.set()
                while not copy.done():
                    try:
                        await asyncio.shield(copy)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not copy.cancelled():
                    copy.exception()
                batch.cleanup()
                raise
            finally:
                self._pending = None


def save_screenshot(workspace, path: Path, image: bytes, check_access) -> str:
    data = {"root_path": workspace.scan_root, "path": str(path)}
    check_access()
    destination = checked_path(workspace, data, writing=True)
    root = Path(workspace.scan_root)
    for directory in reversed(destination.parents):
        if root in directory.parents:
            check_access()
            with parent_directory(workspace, directory) as (parent, name):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
    with parent_directory(workspace, destination) as (parent, name):
        try:
            with open_regular(workspace, parent, name) as existing:
                data["expected"] = copy_file(existing, None)["sha256"]
        except FileNotFoundError:
            pass
    publish(workspace, data, io.BytesIO(image), check_access)
    return str(destination)
