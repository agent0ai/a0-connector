"""Permission-checked detached Chromium sessions behind the durable journal."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
import os
import subprocess
from urllib.parse import urlsplit, urlunsplit

from .host_browser_durable import DurableBrowserError, browser_environment, export_profile, import_profile, profile_identity


def success(op_id, data):
    return {"op_id": op_id, "ok": True, "data": data}


def safe_tabs(value):
    if type(value) is not list or len(value) > 50:
        raise DurableBrowserError("Browser snapshot has too many tabs")
    result = []
    for item in value:
        url = item.get("url") if isinstance(item, dict) else item
        if not isinstance(url, str) or len(url) > 8192:
            raise DurableBrowserError("Invalid browser snapshot URL")
        parsed = urlsplit(url)
        if url != "about:blank" and (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password):
            raise DurableBrowserError("Browser snapshot URL requires explicit navigation")
        result.append(url)
    return result


class DurableHostBrowser:
    def __init__(self, journal):
        self.journal = journal
        self.content_ready = set()
        self.processes = {}

    @staticmethod
    def allowed(manager):
        if not manager.enabled or not getattr(manager, "_durable_permission", lambda: True)():
            raise DurableBrowserError("Current gateway Browser permission is disabled")

    def metadata(self, session, operation, state):
        return {"schema": 1, "instance_id": self.journal.instance_id,
                "session_id": session, "operation_id": operation, "state": state,
                "refs_expired": session not in self.content_ready}

    async def handle(self, manager, payload):
        from .host_browser_common import is_a0_managed_family, normalize_action
        self.allowed(manager)
        action = normalize_action(payload.get("action"))
        profile = manager.selected_profile(profile_mode=payload.get("profile_mode", "existing"),
                                           browser_selection=payload.get("browser_selection", ""))
        if profile is None or profile.is_safari or not (profile.is_remote_debugging or is_a0_managed_family(profile.family)):
            raise DurableBrowserError("Durable Browser requires an isolated agent profile or exact authorized CDP target")
        identity = profile_identity(profile, payload.get("profile_mode", "existing"))
        durable = self.journal.validate(payload, identity)
        session_id, operation = durable["session_id"], durable["operation_id"]
        if action == "durable_status":
            target = payload.get("target_operation_id")
            status = self.journal.status(session_id, target)
            if status["state"] == "completed":
                original = dict(status["response"])
                body = original.get("data")
                original["data"] = {**(body if isinstance(body, dict) else {}),
                                    "durable": self.metadata(session_id, target, "completed")}
                status["response"] = original
            return success(payload["op_id"], {**status,
                "durable": self.metadata(session_id, target, status["state"])})

        async def perform():
            lock = self.journal.session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                self.allowed(manager)
                manager._apply_helper_payloads(payload)
                isolated = self.journal.isolated_profile(profile, session_id)
                if action == "durable_import":
                    if not is_a0_managed_family(profile.family) or profile.is_remote_debugging:
                        raise DurableBrowserError("Personal profiles cannot be imported")
                    if self.journal.runtime(session_id) is not None:
                        raise DurableBrowserError("Import requires a fresh isolated session")
                    snapshot = payload.get("snapshot")
                    if not isinstance(snapshot, dict):
                        raise DurableBrowserError("Missing browser snapshot")
                    tabs = safe_tabs(snapshot.get("tabs", []))
                    import_profile(isolated, snapshot, expected_root=self.journal.root / "profiles" / session_id)
                    result = {"imported": True}
                    if payload.get("restore_documents") is True:
                        session = await self._session(manager, payload, isolated)
                        result["browsers"] = []
                        for url in tabs:
                            self.allowed(manager)
                            result["browsers"].append(await session.open(url))
                    return success(payload["op_id"], {**result, "durable": self.metadata(session_id, operation, "completed")})
                session = await self._session(manager, payload, isolated)
                if action == "durable_attach":
                    result = await session.list()
                elif action == "durable_export":
                    if not is_a0_managed_family(profile.family) or profile.is_remote_debugging:
                        raise DurableBrowserError("Personal profiles cannot be exported")
                    tabs = safe_tabs([row.get("currentUrl", "about:blank") for row in (await session.list())["browsers"]])
                    self.allowed(manager)
                    with suppress(ConnectionError, RuntimeError, asyncio.TimeoutError):
                        await session.browser.command("Browser.close")
                    await session.close()
                    from .host_browser_common import profile_lock_state_for_profile
                    for _ in range(100):
                        if not profile_lock_state_for_profile(isolated).locked:
                            break
                        await asyncio.sleep(.05)
                    else:
                        raise DurableBrowserError("Browser profile did not become quiescent")
                    self.journal.runtime(session_id, {"state": "stopped"})
                    manager._sessions.pop("durable:" + session_id, None)
                    self.content_ready.discard(session_id)
                    result = {"snapshot": export_profile(isolated, tabs, expected_root=self.journal.root / "profiles" / session_id)}
                else:
                    from .host_browser_common import _SUPPORTED_ACTIONS
                    if action not in _SUPPORTED_ACTIONS or action in {"ensure", "status", "open_remote_debugging"}:
                        raise DurableBrowserError("Unsupported durable Browser action")
                    calls = payload.get("calls") if action == "multi" else [payload]
                    if type(calls) is not list or not calls or len(calls) > 50:
                        raise DurableBrowserError("Invalid durable Browser batch")
                    results = []
                    for call in calls:
                        self.allowed(manager)
                        if not isinstance(call, dict) or normalize_action(call.get("action")) == "multi":
                            raise DurableBrowserError("Nested batches require separate intents")
                        if call.get("ref") is not None and session_id not in self.content_ready:
                            raise DurableBrowserError("Browser refs expired; request fresh content first")
                        results.append(await session.dispatch(call))
                        if normalize_action(call.get("action")) == "content":
                            self.content_ready.add(session_id)
                    result = {"results": results} if action == "multi" else results[0]
                return success(payload["op_id"], {**result, "durable": self.metadata(session_id, operation, "completed")})

        receipt = await self.journal.execute(payload, identity, perform)
        if receipt["state"] != "completed":
            return success(payload["op_id"], {"durable": self.metadata(session_id, operation, receipt["state"])})
        response = dict(receipt["response"])
        response["op_id"] = payload["op_id"]
        data = response.get("data")
        response["data"] = {**(data if isinstance(data, dict) else {"result": data}),
                            "durable": self.metadata(session_id, operation, "completed")}
        return response

    async def _session(self, manager, payload, profile):
        from .host_browser_common import remote_debugging_endpoint_from_user_data_dir, is_a0_managed_family
        session_id = payload["durable"]["session_id"]
        key = "durable:" + session_id
        existing = manager._sessions.get(key)
        if existing is not None:
            return existing
        if profile.is_remote_debugging:
            endpoint = profile.cdp_endpoint
            parsed = urlsplit(endpoint)
            if parsed.scheme in {"http", "https"}:
                import aiohttp
                self.allowed(manager)
                version_url = urlunsplit((parsed.scheme, parsed.netloc, "/json/version", parsed.query, ""))
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
                    async with client.get(version_url) as response:
                        response.raise_for_status()
                        endpoint = str((await response.json()).get("webSocketDebuggerUrl") or "")
            resolved = urlsplit(endpoint)
            if resolved.scheme not in {"ws", "wss"} or not resolved.netloc or not resolved.path.startswith("/devtools/browser/"):
                raise DurableBrowserError("Durable CDP requires an exact browser instance")
            runtime = self.journal.runtime(session_id)
            if runtime is not None and runtime.get("endpoint") != endpoint:
                raise DurableBrowserError("Authorized CDP browser instance changed")
            if runtime is None:
                self.journal.runtime(session_id, {"state": "running", "endpoint": endpoint, "external": True})
            profile = replace(profile, cdp_endpoint=endpoint)
        if is_a0_managed_family(profile.family) and not profile.is_remote_debugging:
            runtime = self.journal.runtime(session_id)
            endpoint = remote_debugging_endpoint_from_user_data_dir(profile.user_data_dir)
            if runtime is None or runtime.get("state") == "stopped":
                self.journal.runtime(session_id, {"state": "launching"})
                args = [profile.executable_path, "--user-data-dir=" + str(profile.user_data_dir),
                        "--profile-directory=" + profile.profile_directory, "--remote-debugging-port=0",
                        "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check", "about:blank"]
                env = browser_environment()
                self.allowed(manager)
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=env, start_new_session=os.name != "nt",
                    creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0)
                self.processes[session_id] = process
                endpoint = ""
                for _ in range(200):
                    endpoint = remote_debugging_endpoint_from_user_data_dir(profile.user_data_dir)
                    if endpoint:
                        break
                    if process.poll() is not None:
                        raise DurableBrowserError("Isolated browser did not start")
                    await asyncio.sleep(.05)
                if not endpoint:
                    raise DurableBrowserError("Isolated browser did not expose its private connection")
                self.journal.runtime(session_id, {"state": "running", "endpoint": endpoint})
            elif not endpoint:
                raise DurableBrowserError("Prior isolated browser launch outcome requires review")
            elif runtime.get("endpoint") and runtime["endpoint"] != endpoint:
                raise DurableBrowserError("Isolated browser instance changed")
            profile = replace(profile, cdp_endpoint=endpoint)
        self.allowed(manager)
        session = await manager._session(key, profile=profile)
        session.context_id = payload["context_id"]
        await session.ensure_started()
        self.content_ready.discard(session_id)
        return session
