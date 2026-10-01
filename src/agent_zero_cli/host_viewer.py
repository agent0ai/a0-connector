"""Bounded ephemeral host frames and human input on the existing connector.

Uses existing sessions only. Browser input addresses the captured page; Computer
Use addresses the Mac display in normalized coordinates. No raw input is logged.
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import time
import uuid

from .host_control import ControlError


class HostViewer:
    def __init__(self, session, control):
        self.session = session
        self.control = control
        self.frame_lock = asyncio.Lock()

    def source(self, request):
        source = request.get("source")
        if source not in {"browser", "computer_use"} or not self.session._scope_available(source):
            raise ControlError("This capture source is unavailable. Check Host access in Launcher.")
        return source

    def browser(self, context):
        manager = self.session.host_browser
        session = manager._sessions.get(context) if manager else None
        if session is None or not session.pages:
            raise ControlError("No browser tab is active for this chat yet")
        browser_id = session._resolve_browser_id(None)
        return session, browser_id, session._page(browser_id)

    async def frame(self, request):
        if self.frame_lock.locked():
            raise ControlError("A capture is already in progress")
        async with self.frame_lock:
            source = self.source(request)
            context = request["context"]
            if source == "browser":
                _, browser_id, page = self.browser(context)
                geometry = await page.evaluate("({width:innerWidth,height:innerHeight})")
                url = page.url
                raw = await page.screenshot(type="jpeg", quality=55, scale="css", timeout=5000)
                identity = {"page": page, "browser_id": browser_id, "url": url, "geometry": geometry}
            else:
                manager = self.session.computer_use
                current = manager._sessions.get(context) if manager else None
                if current is None or not current.active:
                    raise ControlError("No Computer Use session is active for this chat yet")
                result = await manager.handle_op({"op_id": uuid.uuid4().hex, "context_id": context, "action": "capture", "fresh": True})
                if not result.get("ok"):
                    raise ControlError("The host could not capture a fresh display")
                data = result.get("result", {})
                artifact = data.get("artifact", {})
                raw = base64.b64decode(artifact.get("data", ""), validate=True)
                identity = {"session_id": current.session_id, "geometry": (data.get("width"), data.get("height"))}
            if not raw or len(raw) > 16_000_000:
                raise ControlError("Capture exceeded its size limit")
            # Pillow is already a connector dependency on supported Mac hosts.
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 40_000_000:
                    raise ControlError("Capture dimensions exceeded their limit")
                original = (image.width, image.height)
                image.thumbnail((1600, 1200))
                output = io.BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=60)
                width, height = image.size
            image_bytes = output.getvalue()
            if len(image_bytes) > 1_500_000:
                raise ControlError("Capture exceeded its transfer limit")
            frame_id = uuid.uuid4().hex
            self.control.remember_frame(source, {"id": frame_id, "time": self.control.clock(), "context": context, "original": original, **identity})
            return {"id": frame_id, "source": source, "width": width, "height": height,
                    "mime": "image/jpeg", "data": base64.b64encode(image_bytes).decode("ascii"),
                    "captured_at": time.time(), "scope": "tab" if source == "browser" else "display"}

    async def input(self, request, frame):
        source = self.source(request)
        context = request["context"]
        if frame["context"] != context:
            raise ControlError("Capture belongs to another chat")
        event = request.get("input")
        if not isinstance(event, dict):
            raise ControlError("Invalid input")
        action = event.get("kind")
        if action not in {"click", "drag", "scroll", "key", "text"}:
            raise ControlError("Unsupported input")
        def unit(name):
            n = event.get(name)
            if type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1:
                raise ControlError("Input is outside the displayed capture")
            return n
        text = event.get("text", "")
        if not isinstance(text, str) or len(text) > 4096:
            raise ControlError("Text exceeds input limit")
        keys = event.get("keys", [])
        allowed = {"Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown", "Meta", "Control", "Alt", "Shift", "Space"}
        if not isinstance(keys, list) or not 0 <= len(keys) <= 4 or any(not isinstance(k, str) or (k not in allowed and not (len(k) == 1 and k.isascii() and k.isalnum())) for k in keys):
            raise ControlError("Unsupported key chord")
        dx, dy = event.get("dx", 0), event.get("dy", 0)
        if any(type(n) not in (int, float) or not math.isfinite(n) or abs(n) > 2000 for n in (dx, dy)):
            raise ControlError("Invalid scroll distance")
        if source == "browser":
            _, browser_id, page = self.browser(context)
            geometry = await page.evaluate("({width:innerWidth,height:innerHeight})")
            if page is not frame["page"] or browser_id != frame["browser_id"] or page.url != frame["url"] or geometry != frame["geometry"]:
                raise ControlError("Browser viewport changed. Wait for a fresh frame.")
            if action in {"click", "drag"}:
                x, y = unit("x") * geometry["width"], unit("y") * geometry["height"]
                if action == "click":
                    await page.mouse.click(x, y)
                else:
                    end_x, end_y = unit("end_x") * geometry["width"], unit("end_y") * geometry["height"]
                    await page.mouse.move(x, y)
                    await page.mouse.down()
                    try:
                        await page.mouse.move(end_x, end_y, steps=10)
                    finally:
                        await page.mouse.up()
            elif action == "scroll":
                await page.mouse.wheel(dx, dy)
            elif action == "text":
                await page.keyboard.insert_text(text)
            elif keys:
                await page.keyboard.press("+".join(keys))
        else:
            manager = self.session.computer_use
            current = manager._sessions.get(context) if manager else None
            if current is None or current.session_id != frame["session_id"]:
                raise ControlError("Display session changed. Wait for a fresh frame.")
            check = await manager.handle_op({"op_id": uuid.uuid4().hex, "context_id": context,
                "session_id": current.session_id, "action": "capture", "fresh": True})
            geometry = check.get("result", {})
            if not check.get("ok") or (geometry.get("width"), geometry.get("height")) != frame["geometry"]:
                raise ControlError("Display geometry changed. Wait for a fresh frame.")
            if action == "drag":
                raise ControlError("This Computer Use backend does not support dragging")
            payload = {"op_id": uuid.uuid4().hex, "context_id": context, "session_id": current.session_id,
                       "action": {"text": "type", "key": "key"}.get(action, action)}
            if action == "click":
                payload.update(x=unit("x"), y=unit("y"))
            elif action == "scroll":
                payload.update(dx=int(dx), dy=int(dy))
            elif action == "text":
                payload["text"] = text
            else:
                payload["keys"] = [{"Meta":"super", "ArrowLeft":"left", "ArrowRight":"right", "ArrowUp":"up", "ArrowDown":"down", "Delete":"forwarddelete"}.get(k, k) for k in keys]
            result = await manager.handle_op(payload)
            if not result.get("ok"):
                raise ControlError("Computer input was not acknowledged; inspect the host before trying again")
        return {"accepted": True, "sequence": request["sequence"]}

    async def handle(self, request):
        gate = self.control
        if not gate.enabled:
            raise ControlError("Host takeover was not negotiated")
        action = request.get("command")
        if action == "status":
            return gate.snapshot()
        if action == "acquire":
            self.source(request)
            return await gate.acquire(request.get("lease"), request.get("context"), blocked=lambda: bool(self.session.remote_exec._sessions))
        if action == "heartbeat":
            return gate.heartbeat(request)
        if action == "hold":
            gate.validate(request, allow_held=True)
            gate.hold()
            return gate.snapshot()
        if action == "frame":
            state = gate.snapshot()
            if state["phase"] != "watching":
                gate.validate(request)
            return await self.frame(request)
        if action == "input":
            return await gate.input(request, lambda frame: self.input(request, frame))
        if action == "prepare_return":
            return await gate.prepare_return(request, lambda: self.return_observation(request))
        if action == "release":
            return await gate.release(request)
        raise ControlError("Unknown viewer command")

    async def return_observation(self, request):
        source = self.source(request)
        manager = self.session.host_browser if source == "browser" else self.session.computer_use
        current = manager._sessions.get(request["context"]) if manager else None
        if current is None or (source == "browser" and not current.pages) or (source == "computer_use" and not current.active):
            # A verified closed session is fresh state too. Never trap a lease
            # merely because the original tab/session no longer exists.
            return {"source": source, "session_closed": True, "captured_at": time.time()}
        # Return follows any capture already in flight. The returning phase
        # prevents new viewer frames from overtaking this final observation.
        async with self.frame_lock:
            pass
        return await self.frame(request)
