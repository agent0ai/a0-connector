"""Receipt custody and real companion/browser process recovery."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
import zipfile

import pytest
from cryptography.fernet import Fernet

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.client import A0Client
from agent_zero_cli.host_browser_common import BrowserProfile
from agent_zero_cli.host_browser_durable import BrowserJournal, DurableBrowserError, encoded, fingerprint, export_profile, import_profile
from agent_zero_cli.host_browser_manager import HostBrowserManager


def request(action="open", *, operation="b" * 64, instance="", **extra):
    value = {"op_id": "transport", "context_id": "synthetic-chat", "profile_mode": "agent",
             "action": action, **extra}
    value["durable"] = {"schema": 1, "deployment": "d" * 64, "session_id": "a" * 64,
                        "operation_id": operation, "instance_id": instance, "fingerprint": fingerprint(value)}
    return value


def test_killed_journal_owner_never_resends_partial_intent_and_reuses_known_unicode_result(tmp_path):
    key = Fernet.generate_key().decode()
    child = r'''
import asyncio,json,sys,time
from pathlib import Path
from agent_zero_cli.host_browser_durable import BrowserJournal
payload=json.loads(sys.argv[3]);journal=BrowserJournal(sys.argv[1],sys.argv[2])
async def effect():
 Path(sys.argv[1],"effect").write_text("one")
 if sys.argv[4]=="intent":
  print("READY",flush=True)
  await asyncio.Event().wait()
 return {"op_id":"original","ok":True,"data":{"value":"café private-result"}}
async def main():
 await journal.execute(payload,{"target":"synthetic"},effect)
 print("READY",flush=True)
 await asyncio.Event().wait()
asyncio.run(main())
'''
    for cut in ("intent", "result"):
        root = tmp_path / cut
        payload = request(url="https://synthetic.invalid/café")
        process = subprocess.Popen([sys.executable, "-c", child, str(root), key, json.dumps(payload), cut], stdout=subprocess.PIPE)
        try:
            assert select.select([process.stdout], [], [], 15)[0]
            assert process.stdout.readline().strip() == b"READY"
        finally:
            process.kill(); process.wait(timeout=10)
        journal = BrowserJournal(root, key)
        async def forbidden():
            pytest.fail("An existing intent was sent again")
        try:
            result = asyncio.run(journal.execute(payload, {"target": "synthetic"}, forbidden))
            assert result["state"] == ("outcome_unknown" if cut == "intent" else "completed")
            if cut == "result":
                assert result["response"]["data"]["value"] == "café private-result"
                assert b"private-result" not in (root / "receipts.sqlite3").read_bytes()
            assert (root / "effect").read_text() == "one"
        finally:
            journal.close()


def test_manager_deduplicates_and_rechecks_revoked_permission_inside_queued_actions(tmp_path, monkeypatch):
    async def scenario():
        journal = BrowserJournal(tmp_path / "journal", Fernet.generate_key().decode())
        manager = HostBrowserManager(CLIConfig(host_browser_enabled=True), persist_enabled=False, durable_journal=journal)
        profile = BrowserProfile("chrome-a0", "Agent Chrome", sys.executable, tmp_path / "source", "Default", "Agent")
        monkeypatch.setattr(manager, "selected_profile", lambda **kwargs: profile)
        controller = manager._durable_controller()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def dispatch(payload):
            calls.append(payload["action"])
            started.set()
            await release.wait()
            return {"value": "known"}
        async def session(*args):
            return SimpleNamespace(dispatch=dispatch)
        monkeypatch.setattr(controller, "_session", session)
        first = request(instance=journal.instance_id)
        second = request("navigate", operation="c" * 64, instance=journal.instance_id)
        task = asyncio.create_task(manager.handle_op(first))
        await started.wait()
        task.cancel()  # Losing the network requester must not cancel the effect receipt.
        with pytest.raises(asyncio.CancelledError):
            await task
        queued = asyncio.create_task(manager.handle_op(second))
        await asyncio.sleep(0)
        manager.set_enabled(False)
        release.set()
        assert (await queued)["code"] == "DURABLE_BROWSER_HELD"
        manager.set_enabled(True)
        repeated = await manager.handle_op(first)
        assert repeated["data"]["value"] == "known" and calls == ["open"]
        client = A0Client("http://synthetic.invalid")
        client.on_browser_op = manager.handle_op
        wire = await client._handle_browser_op(first)
        assert wire["result"]["value"] == "known" and "data" not in wire
        status = await manager.handle_op(request("durable_status", target_operation_id="c" * 64, instance=journal.instance_id))
        assert status["data"]["durable"]["state"] == "outcome_unknown"
        monkeypatch.setattr(manager, "selected_profile", lambda **kwargs: replace(profile, user_data_dir=tmp_path / "different"))
        assert (await manager.handle_op(first))["code"] == "DURABLE_BROWSER_HELD"
        await manager.close()
    asyncio.run(scenario())


def test_agent_profile_import_is_atomic_and_rejects_revision_paths_and_personal_profiles(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir(); target.mkdir()
    (source / "one").write_bytes(b"one")
    (source / "two").write_bytes(b"two")
    profile = BrowserProfile("chrome-a0", "Agent", sys.executable, source, "Default", "Agent")
    snapshot = export_profile(profile, ["about:blank"], expected_root=source)
    read = zipfile.ZipFile.read
    def broken(self, member, *args, **kwargs):
        if getattr(member, "filename", member) == "two":
            raise zipfile.BadZipFile("synthetic interrupted import")
        return read(self, member, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(zipfile.ZipFile, "read", broken)
        with pytest.raises(zipfile.BadZipFile):
            import_profile(replace(profile, user_data_dir=target), snapshot, expected_root=target)
    assert list(target.iterdir()) == []
    assert not list(tmp_path.glob(".profile-import-*"))
    changed = dict(snapshot, browser_binary="0" * 64)
    with pytest.raises(DurableBrowserError):
        import_profile(replace(profile, user_data_dir=target), changed, expected_root=target)
    with pytest.raises(DurableBrowserError):
        export_profile(replace(profile, family="chrome"), [], expected_root=source)
    for name in ("../escape", "C:/escape", "safe:alternate-stream"):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(name, "unsafe")
        body = archive.getvalue()
        changed = dict(snapshot, archive=base64.b64encode(body).decode(), size=len(body), digest=hashlib.sha256(body).hexdigest())
        with pytest.raises(DurableBrowserError):
            import_profile(replace(profile, user_data_dir=target), changed, expected_root=target)
    import_profile(replace(profile, user_data_dir=target), snapshot, expected_root=target)
    assert (target / "two").read_bytes() == b"two"


@pytest.mark.skipif(not os.environ.get("A0_TEST_DURABLE_CHROMIUM"), reason="Requires explicit isolated Chromium binary")
@pytest.mark.parametrize("cut", ["intent", "result"])
def test_real_chromium_survives_companion_kill_without_repeating_effect_or_navigation(tmp_path, cut):
    posts, gets = [], []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            gets.append(self.path)
            self.send_response(200)
            if self.path == "/page" and gets.count("/page") == 1:
                self.send_header("Set-Cookie", "durable_cookie=synthetic; Max-Age=3600; Path=/")
            self.end_headers(); self.wfile.write(b"<title>Synthetic</title>")
        def do_POST(self):
            posts.append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(b"effect-known")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    wrapper = tmp_path / "chromium"
    wrapper.write_text("#!/bin/sh\nexec '" + os.environ["A0_TEST_DURABLE_CHROMIUM"].replace("'", "'\\''") + "' --headless=new --no-sandbox \"$@\"\n")
    wrapper.chmod(0o700)
    root, key = tmp_path / "journal", Fernet.generate_key().decode()
    child = r'''
import asyncio,json,sys,time
from pathlib import Path
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.host_browser_common import BrowserProfile
from agent_zero_cli.host_browser_durable import BrowserJournal,fingerprint
from agent_zero_cli.host_browser_manager import HostBrowserManager
class Journal(BrowserJournal):
 def seal(self,value):
  if sys.argv[5]=="intent" and value.get("response",{}).get("data",{}).get("result")=="effect-known":
   print("READY",flush=True)
   while True:time.sleep(1)
  return super().seal(value)
journal=Journal(sys.argv[1],sys.argv[2])
manager=HostBrowserManager(CLIConfig(host_browser_enabled=True),persist_enabled=False,durable_journal=journal)
profile=BrowserProfile("chrome-a0","Agent",sys.argv[3],Path(sys.argv[1])/"unused","Default","Agent")
manager.selected_profile=lambda **kwargs:profile
def req(action,op,**kwargs):
 p={"op_id":"transport","context_id":"synthetic-chat","profile_mode":"agent","action":action,**kwargs}
 p["durable"]={"schema":1,"deployment":"d"*64,"session_id":"a"*64,"operation_id":op,"instance_id":journal.instance_id,"fingerprint":fingerprint(p)}
 return p
async def main():
 opened=await manager.handle_op(req("open","b"*64,url=sys.argv[4]));assert opened["ok"],opened
 evaluated=req("evaluate","c"*64,browser_id=opened["data"]["id"],script="async () => await (await fetch('/effect',{method:'POST'})).text()")
 Path(sys.argv[1],"request.json").write_text(json.dumps(evaluated))
 result=await manager.handle_op(evaluated);assert result["ok"],result
 print("READY",flush=True)
 await asyncio.Event().wait()
asyncio.run(main())
'''
    process = subprocess.Popen([sys.executable, "-c", child, str(root), key, str(wrapper),
                                f"http://127.0.0.1:{server.server_port}/page", cut], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([process.stdout], [], [], 25)[0], "Browser child did not reach requested cut"
        line = process.stdout.readline()
        assert line.strip() == b"READY", (line, process.stderr.read() if process.poll() is not None else b"")
    finally:
        process.kill(); process.wait(timeout=10)
    journal = BrowserJournal(root, key)
    manager = HostBrowserManager(CLIConfig(host_browser_enabled=True), persist_enabled=False, durable_journal=journal)
    profile = BrowserProfile("chrome-a0", "Agent", str(wrapper), root / "unused", "Default", "Agent")
    manager.selected_profile = lambda **kwargs: profile
    async def verify():
        payload = json.loads((root / "request.json").read_text())
        before = len(gets)
        status = await manager.handle_op(request("durable_status", target_operation_id="c" * 64, instance=journal.instance_id))
        assert status["data"]["durable"]["state"] == ("outcome_unknown" if cut == "intent" else "completed")
        assert status["data"]["durable"]["refs_expired"]
        attached = await manager.handle_op(request("durable_attach", operation="e" * 64, instance=journal.instance_id))
        assert attached["ok"] and attached["data"]["browsers"]
        assert len(gets) == before
        repeated = await manager.handle_op(payload)
        assert repeated["data"]["durable"]["state"] == ("outcome_unknown" if cut == "intent" else "completed")
        assert posts == ["/effect"]
        if cut == "result":
            exported = await manager.handle_op(request("durable_export", operation="f" * 64, instance=journal.instance_id))
            assert exported["ok"], exported
            snapshot = exported["data"]["snapshot"]
            imported_request = request("durable_import", operation="1" * 64, instance=journal.instance_id,
                                       snapshot=snapshot, restore_documents=True)
            imported_request["durable"]["session_id"] = "9" * 64
            imported = await manager.handle_op(imported_request)
            assert imported["ok"], imported
            assert imported["data"]["imported"] and imported["data"]["browsers"]
            assert imported["data"]["durable"]["refs_expired"]
            assert len(gets) > before and posts == ["/effect"]
            page = next(row for row in imported["data"]["browsers"] if row["state"]["currentUrl"].endswith("/page"))
            cookie_request = request("evaluate", operation="2" * 64, instance=journal.instance_id,
                                     browser_id=page["id"],
                                     script="() => document.cookie")
            cookie_request["durable"]["session_id"] = "9" * 64
            cookie = await manager.handle_op(cookie_request)
            assert cookie["ok"] and "durable_cookie=synthetic" in cookie["data"]["result"], cookie
            imported_session = manager._sessions["durable:" + "9" * 64]
            with suppress(Exception):
                await imported_session.browser.command("Browser.close")
        session = manager._sessions.get("durable:" + "a" * 64)
        if session is not None:
            with suppress(Exception):
                await session.browser.command("Browser.close")
        await manager.close()
    from contextlib import suppress
    async def run_verify():
        try:
            await verify()
        finally:
            for session in list(manager._sessions.values()):
                with suppress(Exception):
                    await session.browser.command("Browser.close")
            await manager.close()
    try:
        asyncio.run(run_verify())
    finally:
        server.shutdown(); server.server_close(); thread.join()
