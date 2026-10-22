"""Instance-bound desktop operation receipts, without desktop transplantation."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import platform

from .host_browser_durable import BrowserJournal, DurableBrowserError, encoded

CURRENT_GUARD = ContextVar("computer_use_durable_guard", default=None)


def check_current_permission():
    guard = CURRENT_GUARD.get()
    if guard is not None:
        guard()


class DurableComputerUse:
    def __init__(self, journal=None):
        if journal is None:
            from platformdirs import user_data_path
            root = (Path(os.environ["A0_CONNECTOR_DURABILITY_ROOT"]) / "computer_use" if os.environ.get("A0_CONNECTOR_DURABILITY_ROOT")
                    else user_data_path("a0") / "durability" / "computer_use")
            journal = BrowserJournal(root)
        self.journal = journal

    @staticmethod
    def allowed(manager):
        if (not manager.enabled or not manager.supported
                or not getattr(manager, "_durable_permission", lambda: True)()):
            raise DurableBrowserError("Current Computer Use permission is disabled")

    def identity(self, manager):
        helper = Path(str(getattr(manager._backend_spec, "helper_target", "")))
        if not helper.is_file():
            raise DurableBrowserError("Desktop helper source cannot be verified")
        desktop = {key: os.environ.get(key, "") for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_ID", "SESSIONNAME")}
        desktop["uid"] = os.getuid() if hasattr(os, "getuid") else str(Path.home())
        source = hashlib.sha256(Path(__file__).read_bytes() + (Path(__file__).parent / "computer_use.py").read_bytes())
        total = 0
        for count, path in enumerate(sorted(helper.parent.rglob("*.py")), 1):
            if count > 256 or path.is_symlink():
                raise DurableBrowserError("Desktop helper sources cannot be verified")
            body = path.read_bytes()
            total += len(body)
            if total > 16 * 1024 * 1024:
                raise DurableBrowserError("Desktop helper sources exceed verification bounds")
            source.update(encoded([path.relative_to(helper.parent).as_posix(), hashlib.sha256(body).hexdigest()]))
        return {"tool": "computer_use", "platform": platform.system(),
                "desktop": hashlib.sha256(encoded(desktop)).hexdigest(), "source": source.hexdigest(),
                "backend": manager._backend_metadata.get("backend_id"), "trust_mode": manager.trust_mode}

    def metadata(self, durable, state, operation=None):
        return {"schema": 1, "instance_id": self.journal.instance_id,
                "session_id": durable["session_id"], "operation_id": operation or durable["operation_id"],
                "state": state, "refs_expired": True}

    async def handle(self, manager, payload):
        self.allowed(manager)
        identity = self.identity(manager)
        durable = self.journal.validate(payload, identity)
        if payload.get("action") == "durable_status":
            target = payload.get("target_operation_id")
            status = self.journal.status(durable["session_id"], target)
            if status["state"] == "completed":
                original = dict(status["response"])
                body = original.get("data")
                original["data"] = {**(body if isinstance(body, dict) else {}),
                                    "durable": self.metadata(durable, "completed", target)}
                status["response"] = original
            return {"op_id": payload["op_id"], "ok": True, "data": {**status,
                    "durable": self.metadata(durable, status["state"], target)}}
        async def perform():
            lock = self.journal.session_locks.setdefault(durable["session_id"], asyncio.Lock())
            async with lock:
                self.allowed(manager)
                token = CURRENT_GUARD.set(lambda: self.allowed(manager))
                try:
                    # Existing start_session still owns prompts/trust/rearm;
                    # no receipt or declaration arms a new desktop implicitly.
                    response = await manager._handle_legacy_op(payload)
                finally:
                    CURRENT_GUARD.reset(token)
                if response.get("code") == "COMPUTER_USE_HELPER_TIMEOUT":
                    raise DurableBrowserError("Desktop action outcome is unknown")
                result = response.pop("result", None)
                if result is not None:
                    response["data"] = result
                data = response.get("data")
                response["data"] = {**(data if isinstance(data, dict) else {}),
                                    "durable": self.metadata(durable, "completed")}
                return response
        receipt = await self.journal.execute(payload, identity, perform)
        if receipt["state"] != "completed":
            return {"op_id": payload["op_id"], "ok": True,
                    "data": {"durable": self.metadata(durable, receipt["state"])}}
        response = dict(receipt["response"])
        response["op_id"] = payload["op_id"]
        data = response.get("data")
        response["data"] = {**(data if isinstance(data, dict) else {}), "durable": self.metadata(durable, "completed")}
        return response
