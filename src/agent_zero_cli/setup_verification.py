"""Explicit local verification. Never capture personal browser tabs or return pixels."""
from __future__ import annotations

import base64
import asyncio
import time
import uuid


async def verify_connection(session, capability, browser_selection=""):
    # Leave time for Chrome's bounded 60-second native approval handshake and
    # the subsequent input/capture check. Preparation must not outlive its test.
    timeout = 90 if capability == "browser" else 40
    return await asyncio.wait_for(_verify_connection(session, capability, browser_selection), timeout=timeout)


async def _verify_connection(session, capability, browser_selection=""):
    if capability not in {"browser", "computer_use"} or not session._scope_available(capability):
        raise ValueError("Allow the selected capability in Launcher before testing it.")
    context = "launcher-setup-" + uuid.uuid4().hex
    async with session.host_control.setup_operation():
        if capability == "browser":
            manager = session.host_browser
            async def call(action, **fields):
                result = await manager.handle_op(dict(op_id=uuid.uuid4().hex, context_id=context,
                    action=action, browser_selection=browser_selection, **fields))
                if not result.get("ok"):
                    raise ValueError("Browser verification failed. Check browser access and try again.")
                return result.get("result") or {}
            # Empty URL creates a new about:blank page; it cannot reuse a personal tab.
            opened = await call("open", url="")
            browser_id = opened.get("id")
            if browser_id is None:
                raise ValueError("Browser did not return the test page identity.")
            try:
                browser = manager._sessions[context]
                page = browser._page(browser_id)
                if await browser._runtime.current_url(page) != "about:blank":
                    raise ValueError("Test page changed. No input was sent.")
                await page.evaluate("() => { document.title='Agent Zero connection test'; document.body.innerHTML='<h1>Agent Zero connection test</h1><p>This temporary page checks browser input and capture.</p><input id=setup-field aria-label=\"Connection test\">'; document.getElementById('setup-field').focus(); }")
                await call("keyboard", browser_id=browser_id, text="Agent Zero connected")
                if await browser._runtime.current_url(page) != "about:blank" or await page.evaluate("document.getElementById('setup-field')?.value") != "Agent Zero connected":
                    raise ValueError("Browser input could not be verified.")
                image = await page.screenshot(type="jpeg", quality=40, timeout=5000)
                if not image or len(image) > 25 * 1024 * 1024:
                    raise ValueError("Browser capture could not be verified.")
                evidence = ["test_page_input", "fresh_capture"]
            finally:
                # Close only the page this test created. Do not close a profile.
                try:
                    await call("close", browser_id=browser_id)
                finally:
                    # Return the prepared profile to the existing handoff slot.
                    # Closing its runtime could close unrelated personal pages.
                    from .host_browser_manager import RELAUNCH_CONTEXT_ID
                    prepared = manager._sessions.pop(context, None)
                    if prepared is not None:
                        prepared.context_id = RELAUNCH_CONTEXT_ID
                        manager._sessions[RELAUNCH_CONTEXT_ID] = prepared
        else:
            manager = session.computer_use
            async def call(action):
                result = await manager.handle_op(dict(op_id=uuid.uuid4().hex,context_id=context,action=action,allow_prompt=False,fresh=True))
                if not result.get("ok"):
                    raise ValueError("Computer capture test failed. Finish permission setup in Launcher.")
                return result.get("result") or {}
            try:
                await call("start_session")
                result = await call("capture")
                data = (result.get("artifact") or {}).get("data")
                if not isinstance(data,str) or len(data)>35_000_000 or not base64.b64decode(data,validate=True):
                    raise ValueError("A fresh computer capture was not returned.")
                evidence = ["fresh_capture"]
            finally:
                try:
                    await call("stop_session")
                finally:
                    manager._sessions.pop(context, None)
    return dict(capability=capability,verified=True,checked_at=time.time(),evidence=evidence)
