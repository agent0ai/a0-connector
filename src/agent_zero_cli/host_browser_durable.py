"""Encrypted host-browser receipts, isolated profiles and explicit reattachment."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sqlite3
import stat
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile

_ID = re.compile(r"[a-f0-9]{64}\Z")
MAX_BYTES = 64 * 1024 * 1024
MAX_PROFILE_BYTES = 128 * 1024 * 1024
MAX_FILES = 20000
MAX_JOURNAL_BYTES = 256 * 1024 * 1024
_TRANSIENT = {"SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort", "LOCK", "lockfile"}
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def fingerprint(payload):
    return hashlib.sha256(encoded({key: value for key, value in payload.items()
                                  if key not in {"op_id", "durable"}})).hexdigest()


class DurableBrowserError(ValueError):
    pass


def browser_environment():
    return {name: value for name, value in os.environ.items() if name in
            {"PATH", "HOME", "USER", "LOGNAME", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
             "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "LOCALAPPDATA", "APPDATA"}
            or name.startswith(("XDG_", "LANG", "LC_"))}


def private_directory(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise DurableBrowserError("Durable browser path cannot contain links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise DurableBrowserError("Durable browser directory has a different owner")
    path.chmod(0o700)
    return path


class BrowserJournal:
    def __init__(self, root=None, key=None):
        from cryptography.fernet import Fernet
        self.cipher = Fernet((key or os.environ["A0_CONNECTOR_DURABILITY_KEY"]).encode())
        if root is None:
            from platformdirs import user_data_path
            root = os.environ.get("A0_CONNECTOR_DURABILITY_ROOT") or user_data_path("a0") / "durability" / "browser"
        self.root = private_directory(root)
        owner_path = self.root / "owner.lock"
        fd = os.open(owner_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise DurableBrowserError("Unsafe durable browser owner file")
        self._owner = os.fdopen(fd, "a+b")
        owner_path.chmod(0o600)
        try:
            if os.name == "nt":
                import msvcrt
                self._owner.write(b"0"); self._owner.flush(); self._owner.seek(0)
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._owner.close()
            raise DurableBrowserError("Another companion owns this durable browser journal") from None
        path = self.root / "receipts.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.is_symlink() or candidate.exists() and (not candidate.is_file() or candidate.stat().st_nlink != 1):
                self._owner.close()
                raise DurableBrowserError("Unsafe durable browser receipt file")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions(session TEXT PRIMARY KEY,binding BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS runtimes(session TEXT PRIMARY KEY,value BLOB NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS operations(session TEXT NOT NULL,operation TEXT NOT NULL,
            fingerprint TEXT NOT NULL,state TEXT NOT NULL,result BLOB,PRIMARY KEY(session,operation))""")
        with self.transaction():
            row = self.db.execute("SELECT value FROM metadata WHERE key='instance'").fetchone()
            if row is None:
                self.instance_id = hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()
                self.db.execute("INSERT INTO metadata VALUES('instance',?)", (self.seal({"instance": self.instance_id}),))
            else:
                self.instance_id = self.open(row["value"])["instance"]
        self.pending = {}
        self.session_locks = {}
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.chmod(0o600)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def seal(self, value):
        data = encoded(value)
        if len(data) > MAX_BYTES:
            raise DurableBrowserError("Durable browser result exceeds its bound")
        return self.cipher.encrypt(data)

    def open(self, body):
        try:
            return json.loads(self.cipher.decrypt(body))
        except Exception:
            raise DurableBrowserError("Durable browser receipt cannot be authenticated") from None

    def validate(self, payload, identity):
        durable = payload.get("durable")
        if (not isinstance(durable, dict) or type(durable.get("schema")) is not int or durable.get("schema") != 1
                or any(not isinstance(durable.get(key), str) or not _ID.fullmatch(durable[key])
                       for key in ("session_id", "operation_id", "fingerprint"))
                or not isinstance(durable.get("deployment"), str) or not _ID.fullmatch(durable["deployment"])
                or durable["fingerprint"] != fingerprint(payload)):
            raise DurableBrowserError("Invalid durable browser identity")
        if durable.get("instance_id") not in (None, "", self.instance_id):
            raise DurableBrowserError("Durable browser connector instance changed")
        binding = {"context": payload.get("context_id"), "deployment": durable["deployment"], "identity": identity}
        if not isinstance(binding["context"], str) or not 0 < len(binding["context"]) <= 240:
            raise DurableBrowserError("Invalid durable browser context")
        with self.transaction():
            row = self.db.execute("SELECT binding FROM sessions WHERE session=?", (durable["session_id"],)).fetchone()
            if row and self.open(row["binding"]) != binding:
                raise DurableBrowserError("Durable browser session target changed")
            if row is None:
                if self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 1000:
                    raise DurableBrowserError("Durable browser session limit reached")
                self.db.execute("INSERT INTO sessions VALUES(?,?)", (durable["session_id"], self.seal(binding)))
        return durable

    def status(self, session, operation):
        if not isinstance(operation, str) or not _ID.fullmatch(operation):
            raise DurableBrowserError("Invalid durable browser operation")
        with self.lock:
            row = self.db.execute("SELECT * FROM operations WHERE session=? AND operation=?", (session, operation)).fetchone()
            if row is None:
                return {"state": "absent", "found": False}
            if row["state"] == "completed":
                value = self.open(row["result"])
                if (value["session"], value["operation"], value["fingerprint"]) != (session, operation, row["fingerprint"]):
                    raise DurableBrowserError("Durable browser receipt identity changed")
                return {"state": "completed", "response": value["response"], "found": True}
            if row["state"] != "intent" or row["result"] is not None:
                raise DurableBrowserError("Durable browser receipt is corrupt")
            return {"state": "running" if (session, operation) in self.pending else "outcome_unknown", "found": True}

    async def execute(self, payload, identity, handler):
        durable = self.validate(payload, identity)
        session, operation = durable["session_id"], durable["operation_id"]
        with self.transaction():
            row = self.db.execute("SELECT fingerprint FROM operations WHERE session=? AND operation=?", (session, operation)).fetchone()
            if row and row["fingerprint"] != durable["fingerprint"]:
                raise DurableBrowserError("Durable browser operation content changed")
            if row is None:
                if self.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] >= 10000:
                    raise DurableBrowserError("Durable browser session receipt limit reached")
                self.db.execute("INSERT INTO operations VALUES(?,?,?,'intent',NULL)", (session, operation, durable["fingerprint"]))
        key = (session, operation)
        if row is not None:
            if key in self.pending:
                return await asyncio.shield(self.pending[key])
            return self.status(session, operation)

        async def perform():
            response = await handler()
            try:
                result = self.seal({"session": session, "operation": operation,
                                    "fingerprint": durable["fingerprint"], "response": response})
            except (DurableBrowserError, ValueError, TypeError, RecursionError):
                response = {"op_id": payload.get("op_id", ""), "ok": False, "code": "DURABLE_RESULT_LIMIT", "error": "Completed browser result exceeded durable retention limits"}
                result = self.seal({"session": session, "operation": operation,
                                    "fingerprint": durable["fingerprint"], "response": response})
            with self.transaction():
                used = self.db.execute("SELECT COALESCE(SUM(length(result)),0) FROM operations").fetchone()[0]
                if used + len(result) > MAX_JOURNAL_BYTES:
                    response = {"op_id": payload.get("op_id", ""), "ok": False, "code": "DURABLE_RESULT_LIMIT", "error": "Completed browser result exceeded durable retention limits"}
                    result = self.seal({"session": session, "operation": operation,
                                        "fingerprint": durable["fingerprint"], "response": response})
                self.db.execute("UPDATE operations SET state='completed',result=? WHERE session=? AND operation=? AND state='intent'", (result, session, operation))
            return {"state": "completed", "response": response}
        task = self.pending[key] = asyncio.create_task(perform())
        def settled(done):
            self.pending.pop(key, None)
            if not done.cancelled():
                done.exception()  # Retain an unresolved intent; retrieve exception without logging private arguments.
        task.add_done_callback(settled)
        return await asyncio.shield(task)

    def isolated_profile(self, profile, session):
        if not _ID.fullmatch(str(session)):
            raise DurableBrowserError("Invalid durable browser session")
        if profile.is_safari or profile.is_remote_debugging:
            return profile
        # Dedicated agent profiles are isolated per durable context. Existing
        # personal profiles remain anchored to their explicitly chosen target.
        from agent_zero_cli.host_browser_common import is_a0_managed_family
        if not is_a0_managed_family(profile.family):
            return profile
        root = private_directory(self.root / "profiles" / session)
        return replace(profile, user_data_dir=root)

    def runtime(self, session, value=None):
        if value is not None:
            with self.transaction():
                self.db.execute("INSERT INTO runtimes VALUES(?,?) ON CONFLICT(session) DO UPDATE SET value=excluded.value", (session, self.seal(value)))
        row = self.db.execute("SELECT value FROM runtimes WHERE session=?", (session,)).fetchone()
        return self.open(row["value"]) if row else None

    def close(self):
        with self.lock:
            self.db.close()
            self._owner.close()


def profile_identity(profile, mode):
    return {"family": profile.family, "mode": mode,
            "path": hashlib.sha256(str(profile.user_data_dir).encode()).hexdigest(),
            "selection": profile.browser_id, "platform": platform.system(),
            "source": hashlib.sha256(b"".join((Path(__file__).parent / name).read_bytes() for name in
                ("host_browser_durable.py", "host_browser_durable_runtime.py", "host_browser_manager.py", "host_browser_common.py", "host_browser_session.py", "host_browser_cdp.py"))).hexdigest(),
            "executable": hashlib.sha256(str(Path(profile.executable_path).resolve()).encode()).hexdigest(),
            "endpoint": hashlib.sha256(profile.cdp_endpoint.encode()).hexdigest()}


def browser_binary(profile):
    digest = hashlib.sha256()
    size = 0
    with Path(profile.executable_path).open("rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise DurableBrowserError("Browser executable is not a regular file")
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 512 * 1024 * 1024:
                raise DurableBrowserError("Browser executable exceeds verification bound")
            digest.update(chunk)
    return digest.hexdigest()


def browser_version(profile):
    # A selected launcher can remain unchanged while its Chromium binary updates.
    # Pin the actual launcher's reported browser version as well as its bytes.
    result = subprocess.run([profile.executable_path, "--version"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
        env=browser_environment())
    if result.returncode != 0 or not 0 < len(result.stdout) <= 4096:
        raise DurableBrowserError("Browser version cannot be verified")
    return result.stdout.decode("utf-8", errors="strict").strip()


def export_profile(profile, tabs, *, expected_root):
    from agent_zero_cli.host_browser_common import is_a0_managed_family
    if profile.is_safari or profile.is_remote_debugging or not is_a0_managed_family(profile.family):
        raise DurableBrowserError("Only isolated agent browser profiles may be exported")
    root = Path(profile.user_data_dir)
    if root != Path(expected_root) or root.is_symlink():
        raise DurableBrowserError("Only the session's isolated profile may be exported")
    output, total, count = io.BytesIO(), 0, 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            count += 1
            if count > MAX_FILES:
                raise DurableBrowserError("Browser profile exceeds its bound")
            relative = path.relative_to(root)
            if path.name in _TRANSIENT or "Sessions" in relative.parts:
                continue  # Never import Chromium's session/POST restore records.
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DurableBrowserError("Browser profile contains unsupported linked state")
            total += info.st_size
            if total > MAX_PROFILE_BYTES:
                raise DurableBrowserError("Browser profile exceeds its bound")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                data = stream.read(MAX_PROFILE_BYTES + 1)
                after = os.fstat(stream.fileno())
            if (before.st_nlink != 1 or not stat.S_ISREG(before.st_mode) or len(data) != before.st_size
                    or len(data) > MAX_PROFILE_BYTES or any(getattr(before, key) != getattr(after, key)
                        for key in ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))):
                raise DurableBrowserError("Browser profile changed during export")
            archive.writestr(relative.as_posix(), data)
    body = output.getvalue()
    if len(body) > MAX_BYTES // 2:
        raise DurableBrowserError("Browser profile archive exceeds its bound")
    return {"schema": 1, "archive": base64.b64encode(body).decode(), "digest": hashlib.sha256(body).hexdigest(),
            "size": len(body), "tabs": tabs, "family": profile.family, "platform": platform.system(),
            "browser_binary": browser_binary(profile), "browser_version": browser_version(profile),
            "implementation": profile_identity(profile, "agent")["source"]}


def import_profile(profile, snapshot, *, expected_root):
    from agent_zero_cli.host_browser_common import is_a0_managed_family
    if (not is_a0_managed_family(profile.family) or profile.is_safari or profile.is_remote_debugging
            or snapshot.get("schema") != 1 or snapshot.get("family") != profile.family or snapshot.get("platform") != platform.system()
            or snapshot.get("browser_binary") != browser_binary(profile)
            or snapshot.get("browser_version") != browser_version(profile)
            or snapshot.get("implementation") != profile_identity(profile, "agent")["source"]):
        raise DurableBrowserError("Browser profile target is incompatible")
    try:
        if not isinstance(snapshot["archive"], str) or len(snapshot["archive"]) > ((MAX_BYTES // 2 + 2) // 3) * 4:
            raise DurableBrowserError("Browser profile archive exceeds its bound")
        body = base64.b64decode(snapshot["archive"], validate=True)
    except Exception:
        raise DurableBrowserError("Browser profile archive is invalid") from None
    if len(body) > MAX_BYTES // 2 or len(body) != snapshot.get("size") or hashlib.sha256(body).hexdigest() != snapshot.get("digest"):
        raise DurableBrowserError("Browser profile archive integrity failed")
    root = Path(profile.user_data_dir)
    if root != Path(expected_root) or root.is_symlink() or root.exists() and any(root.iterdir()):
        raise DurableBrowserError("Browser import requires a fresh isolated profile")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES or sum(info.file_size for info in infos) > MAX_PROFILE_BYTES:
            raise DurableBrowserError("Browser profile archive exceeds its bound")
        names = set()
        for info in infos:
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (not path.parts or path.is_absolute() or any(part in (".", "..", "") for part in path.parts)
                    or any(":" in part or part.endswith((".", " "))
                           or part.split(".")[0].upper() in _DEVICE_NAMES
                           for part in path.parts)
                    or "\\" in info.filename or "\x00" in info.filename or path.as_posix() != info.filename
                    or info.filename in names or "Sessions" in path.parts or path.name in _TRANSIENT
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise DurableBrowserError("Unsafe browser profile archive entry")
            names.add(info.filename)
        staging = Path(tempfile.mkdtemp(prefix=".profile-import-", dir=root.parent))
        try:
            for info in infos:
                path = staging.joinpath(*PurePosixPath(info.filename).parts)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with path.open("xb") as output:
                    os.chmod(path, 0o600)
                    data = archive.read(info)
                    if len(data) != info.file_size:
                        raise DurableBrowserError("Browser profile archive size mismatch")
                    output.write(data); output.flush(); os.fsync(output.fileno())
            for directory in sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True):
                _sync_directory(directory)
            _sync_directory(staging)
            # The journal's process owner and session lock fence import. Require
            # the destination to remain empty immediately before publication.
            if root.exists():
                root.rmdir()
            os.rename(staging, root)
            _sync_directory(root.parent)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
