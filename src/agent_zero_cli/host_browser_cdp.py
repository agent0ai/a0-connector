from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from agent_zero_cli.host_browser_common import (
    DEFAULT_VIEWPORT,
    REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS,
)


class CDPError(RuntimeError):
    pass


_HISTORY_EVENTS = {
    "Page.frameStartedNavigating", "Page.frameNavigated", "Page.frameDetached", "Page.navigatedWithinDocument",
    "Page.lifecycleEvent", "Target.attachedToTarget", "Target.detachedFromTarget",
}


class CDPConnection:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._history_events: dict[str, asyncio.Queue] = {}
        self.on_detached: Callable[[str | None], None] | None = None

    @property
    def is_connected(self) -> bool:
        return (
            self._ws is not None and not self._ws.closed
            and self._reader_task is not None and not self._reader_task.done()
        )

    async def connect(self) -> None:
        timeout = aiohttp.ClientTimeout(total=REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS)
        self._session = aiohttp.ClientSession(timeout=timeout)
        try:
            endpoint = self.endpoint
            parsed = urlsplit(endpoint)
            if parsed.scheme in {"http", "https"}:
                version_url = urlunsplit((parsed.scheme, parsed.netloc, "/json/version", parsed.query, ""))
                async with self._session.get(version_url) as response:
                    response.raise_for_status()
                    version = await response.json()
                endpoint = str(version.get("webSocketDebuggerUrl") or "").strip()
                resolved = urlsplit(endpoint)
                if resolved.scheme not in {"ws", "wss"} or not resolved.netloc or not resolved.path:
                    raise CDPError(f"{version_url} did not return webSocketDebuggerUrl.")
            self._ws = await self._session.ws_connect(
                endpoint,
                max_msg_size=36 * 1024 * 1024,
                autoclose=True,
                autoping=True,
            )
        except BaseException:
            await self.close()
            raise
        self._reader_task = asyncio.create_task(self._read_loop())

    async def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if self._ws is None:
            raise CDPError("Chrome DevTools connection is not open.")
        msg_id = None
        future = None
        try:
            async with self._send_lock:
                msg_id = self._next_id
                self._next_id += 1
                future = asyncio.get_running_loop().create_future()
                self._pending[msg_id] = future
                payload: dict[str, Any] = {"id": msg_id, "method": method}
                if params:
                    payload["params"] = params
                if session_id:
                    payload["sessionId"] = session_id
                await self._ws.send_json(payload)
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            if future is not None and not future.done():
                future.cancel()
            self._pending.pop(msg_id, None)
        if "error" in response:
            error = response.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise CDPError(str(message or f"CDP command failed: {method}"))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result({"error": {"message": "Chrome DevTools connection closed."}})
        self._pending.clear()
        self.on_detached = None

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    msg_id = payload.get("id")
                    if isinstance(msg_id, int):
                        future = self._pending.get(msg_id)
                        if future is not None and not future.done():
                            future.set_result(payload)
                    elif (method := payload.get("method")) in _HISTORY_EVENTS:
                        params = payload["params"]
                        events = self._history_events.get(payload.get("sessionId"))
                        if method == "Target.detachedFromTarget":
                            session_id = params["sessionId"]
                            events = self._history_events.pop(session_id, events)
                            if self.on_detached:
                                self.on_detached(session_id)
                        elif method == "Target.attachedToTarget" and events is not None:
                            self._history_events[params["sessionId"]] = events
                        if method == "Page.lifecycleEvent" and params["name"] != "DOMContentLoaded":
                            continue
                        if events is not None:
                            events.put_nowait((method, params))
                elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    break
        finally:
            for events in set(self._history_events.values()):
                events.put_nowait(("disconnected", {}))
            if self.on_detached:
                self.on_detached(None)
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_result({"error": {"message": "Chrome DevTools connection closed."}})


class CDPMouse:
    def __init__(self, page: "CDPPage") -> None:
        self.page = page
        self.x = self.y = 0.0
        self.buttons = 0

    async def _dispatch(self, event: str, **kwargs: Any) -> None:
        await self.page.send("Input.dispatchMouseEvent", {
            "type": event, "x": self.x, "y": self.y, "buttons": self.buttons,
            "modifiers": self.page.keyboard.modifiers, **kwargs,
        })

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        start_x, start_y = self.x, self.y
        for step in range(1, steps + 1):
            self.x = start_x + (x - start_x) * step / steps
            self.y = start_y + (y - start_y) * step / steps
            await self._dispatch("mouseMoved")

    async def click(self, x: float, y: float, button: str = "left") -> None:
        await self.move(x, y)
        await self.down(button)
        await self.up(button)

    async def dblclick(self, x: float, y: float, button: str = "left") -> None:
        await self.click(x, y, button)
        await self.down(button, 2)
        await self.up(button, 2)

    async def down(self, button: str = "left", count: int = 1) -> None:
        button = _cdp_mouse_button(button)
        self.buttons |= {"left": 1, "right": 2, "middle": 4}[button]
        await self._dispatch("mousePressed", button=button, clickCount=count)

    async def up(self, button: str = "left", count: int = 1) -> None:
        button = _cdp_mouse_button(button)
        self.buttons &= ~{"left": 1, "right": 2, "middle": 4}[button]
        await self._dispatch("mouseReleased", button=button, clickCount=count)

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self._dispatch("mouseWheel", deltaX=delta_x, deltaY=delta_y)


_MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
_KEY_CODES = {
    "Backspace": 8, "Tab": 9, "Enter": 13, "Shift": 16, "Control": 17,
    "Alt": 18, "Escape": 27, " ": 32, "PageUp": 33, "PageDown": 34,
    "End": 35, "Home": 36, "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39,
    "ArrowDown": 40, "Insert": 45, "Delete": 46, "Meta": 91,
    **{f"F{n}": 111 + n for n in range(1, 25)},
}
# CDP accepts DOM physical codes and Windows virtual-key codes on all host OSes.
_PRINTABLE_KEYS = {
    **{digit + shifted: ("Digit" + digit, ord(digit)) for digit, shifted in zip("1234567890", "!@#$%^&*()")},
    "`~": ("Backquote", 192), "-_": ("Minus", 189), "=+": ("Equal", 187),
    "[{": ("BracketLeft", 219), "]}": ("BracketRight", 221), "\\|": ("Backslash", 220),
    ";:": ("Semicolon", 186), "'\"": ("Quote", 222), ",<": ("Comma", 188),
    ".>": ("Period", 190), "/?": ("Slash", 191),
}
_KEY_ALIASES = {"Ctrl": "Control", "Esc": "Escape", "Space": " ", "Return": "Enter",
                "\n": "Enter", "\r": "Enter", "\t": "Tab"}


def _key_definition(raw: str, shifted: bool) -> tuple[str, str, int]:
    key = _KEY_ALIASES.get(raw, raw)
    for modifier in _MODIFIERS:
        if key in (modifier, modifier + "Left", modifier + "Right"):
            code = modifier + "Left" if key == modifier else key
            return modifier, code, 92 if code == "MetaRight" else _KEY_CODES[modifier]
    if key.startswith("Key") and len(key) == 4 and key[-1].isascii() and key[-1].isalpha():
        key = key[-1].lower()
    if len(key) == 1 and key.isascii() and key.isalpha():
        return key.upper() if shifted else key, "Key" + key.upper(), ord(key.upper())
    for pair, (code, virtual) in _PRINTABLE_KEYS.items():
        if key == code or key in (pair[0], pair[1]):
            return pair[1] if shifted or key == pair[1] else pair[0], code, virtual
    return key, "Space" if key == " " else key, _KEY_CODES.get(key, 0)


class CDPKeyboard:
    def __init__(self, page: "CDPPage") -> None:
        self.page = page
        self.modifiers = 0
        self._held: set[str] = set()

    async def type(self, text: str) -> None:
        for character in text:
            if character.isascii() and (character.isprintable() or character in _KEY_ALIASES):
                await self.press(character)
            else:
                await self.insert_text(character)

    async def insert_text(self, text: str) -> None:
        await self.page.send("Input.insertText", {"text": text})

    async def press(self, key: str) -> None:
        if key.endswith("++") and len(key) > 2:
            keys = [*key[:-2].split("+"), "+"]
        else:
            keys = [key] if key == "+" else key.split("+")
        if not all(keys):
            raise ValueError("Expected a key between chord separators")
        pressed = []
        try:
            for part in keys:
                await self.down(part)
                pressed.append(part)
        finally:
            for part in reversed(pressed):
                await self.up(part)

    async def _dispatch(self, raw: str, down: bool) -> None:
        key, code, keycode = _key_definition(raw, bool(self.modifiers & _MODIFIERS["Shift"]))
        modifier = _MODIFIERS.get(key, 0)
        modifiers = self.modifiers
        if modifier:
            if down:
                modifiers |= modifier
            elif not (self._held - {code}) & {key + "Left", key + "Right"}:
                modifiers &= ~modifier
        text = key if len(key) == 1 else "\r" if key == "Enter" else ""
        if modifiers & 7:
            text = ""
        params = {
            "type": ("keyDown" if text else "rawKeyDown") if down else "keyUp",
            "key": key, "code": code, "windowsVirtualKeyCode": keycode,
            "modifiers": modifiers, "text": text if down else "",
        }
        if down:
            params["autoRepeat"] = code in self._held
        if modifier:
            params["location"] = 2 if code.endswith("Right") else 1
        await self.page.send("Input.dispatchKeyEvent", params)
        self.modifiers = modifiers
        if down:
            self._held.add(code)
        else:
            self._held.discard(code)

    async def down(self, key: str) -> None:
        await self._dispatch(key, True)

    async def up(self, key: str) -> None:
        await self._dispatch(key, False)


class CDPPage:
    def __init__(self, context: "CDPContext", target_id: str, session_id: str, url: str = "") -> None:
        self.context = context
        self.connection = context.connection
        self.target_id = target_id
        self.session_id = session_id
        self.url = url or "about:blank"
        self.mouse = CDPMouse(self)
        self.keyboard = CDPKeyboard(self)
        self.viewport_size = dict(DEFAULT_VIEWPORT)
        self._handlers: dict[str, Any] = {}
        self._closed = False
        self._history_lock = asyncio.Lock()

    def on(self, event: str, callback: Any) -> None:
        self._handlers[event] = callback

    async def send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        return await self.connection.command(method, params, session_id=self.session_id, timeout=timeout)

    async def goto(self, url: str, *, timeout: int = 30000, **_: object) -> None:
        result = await self.send("Page.navigate", {"url": url}, timeout=timeout / 1000)
        if result.get("errorText"):
            raise CDPError(result["errorText"])
        self.url = url
        await self.wait_for_load_state(timeout=timeout)

    async def _traverse(self, offset: int, timeout: int) -> None:
        async with asyncio.timeout(timeout / 1000), self._history_lock:
            history = await self.send("Page.getNavigationHistory")
            index = history["currentIndex"] + offset
            if 0 <= index < len(history["entries"]):
                await self._wait_for_history_entry(history["entries"][index]["id"])

    async def _wait_for_history_entry(self, entry_id: int) -> None:
        events = asyncio.Queue()
        subscriptions = self.connection._history_events
        subscriptions[self.session_id] = events
        attach = {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True,
                  "filter": [{"type": "iframe"}, {"exclude": True}]}

        async def watch(session_id: str) -> None:
            if session_id != self.session_id:
                await self.connection.command("Page.enable", session_id=session_id)
            await self.connection.command("Page.setLifecycleEventsEnabled", {"enabled": True}, session_id=session_id)
            await self.connection.command("Target.setAutoAttach", attach, session_id=session_id)

        try:
            await watch(self.session_id)
            navigating = False
            frame_id = loader_id = None
            while True:
                # Chromium reports existing child targets before acknowledging setAutoAttach.
                if not navigating and events.empty():
                    await self.send("Page.navigateToHistoryEntry", {"entryId": entry_id})
                    navigating = True
                method, params = await events.get()
                if method == "Target.attachedToTarget":
                    child = params["sessionId"]
                    if child in subscriptions:
                        try:
                            await watch(child)
                        except CDPError:
                            # The iframe can disappear while enabling its observer.
                            if child in subscriptions:
                                raise
                elif method == "disconnected" or (
                    method == "Target.detachedFromTarget" and params["sessionId"] == self.session_id
                ):
                    raise CDPError("The host browser tab closed during history navigation.")
                elif not navigating:
                    continue
                elif method == "Page.frameStartedNavigating" and params["navigationType"].startswith("history"):
                    frame_id, loader_id = params["frameId"], params.get("loaderId")
                elif (method == "Page.frameDetached" and params["frameId"] == frame_id
                      and params["reason"] == "remove"):
                    raise CDPError("The frame was removed during host browser history navigation.")
                elif method == "Page.navigatedWithinDocument" and params["frameId"] == frame_id:
                    return
                elif method == "Page.frameNavigated" and params["frame"]["id"] == frame_id:
                    if params.get("type") == "BackForwardCacheRestore":
                        return
                    loader_id = params["frame"]["loaderId"]
                elif (method == "Page.lifecycleEvent" and params["frameId"] == frame_id
                      and params["loaderId"] == loader_id):
                    return
        finally:
            for session_id, queue in list(subscriptions.items()):
                if queue is events:
                    del subscriptions[session_id]
            if self.connection.is_connected:
                # Disabling auto-attach also releases its child debugger sessions.
                with contextlib.suppress(CDPError, aiohttp.ClientError, ConnectionError, TimeoutError):
                    await self.send("Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False},
                                    timeout=1)
                with contextlib.suppress(CDPError, aiohttp.ClientError, ConnectionError, TimeoutError):
                    await self.send("Page.setLifecycleEventsEnabled", {"enabled": False}, timeout=1)

    async def go_back(self, *, timeout: int = 10000, **_: object) -> None:
        await self._traverse(-1, timeout)

    async def go_forward(self, *, timeout: int = 10000, **_: object) -> None:
        await self._traverse(1, timeout)

    async def reload(self, *, timeout: int = 15000, **_: object) -> None:
        async with asyncio.timeout(timeout / 1000):
            previous = (await self.send("Page.getFrameTree"))["frameTree"]["frame"]["loaderId"]
            await self.send("Page.reload")
            while (await self.send("Page.getFrameTree"))["frameTree"]["frame"]["loaderId"] == previous:
                await asyncio.sleep(0.05)

    async def wait_for_load_state(self, *_: object, timeout: int = 30000) -> None:
        async with asyncio.timeout(timeout / 1000):
            while True:
                try:
                    if await self.evaluate("document.readyState") in {"interactive", "complete"}:
                        return
                except CDPError as exc:
                    if "context" not in str(exc).lower():
                        raise
                await asyncio.sleep(0.05)

    async def bring_to_front(self) -> None:
        await self.connection.command("Target.activateTarget", {"targetId": self.target_id})

    async def title(self) -> str:
        result = await self.evaluate("() => document.title")
        return str(result or "")

    async def evaluate(self, script: str, arg: object = None, *, timeout: float = 30.0) -> object:
        source = script.strip()
        if re.match(r"^(?:async\s+)?function\b", source) and source.rstrip(";").endswith("}"):
            source = f"({source.rstrip(';')})"
        if arg is not None:
            source = f"({source})({json.dumps(arg)})"
        result = await self.send("Runtime.evaluate", {
            "expression": source, "awaitPromise": True, "returnByValue": arg is not None,
        }, timeout=timeout)
        if result.get("exceptionDetails"):
            raise CDPError(str(result["exceptionDetails"]))
        remote = result["result"]
        object_id = remote.get("objectId")
        if not object_id:
            return remote.get("value", remote.get("unserializableValue"))
        try:
            result = await self.send("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": "function(){return this()}" if remote["type"] == "function" else "function(){return this}",
                "awaitPromise": True, "returnByValue": True,
            }, timeout=timeout)
            if result.get("exceptionDetails"):
                raise CDPError(str(result["exceptionDetails"]))
            return result["result"].get("value")
        finally:
            with contextlib.suppress(CDPError):
                await self.send("Runtime.releaseObject", {"objectId": object_id}, timeout=2)

    async def screenshot(self, **kwargs: object) -> bytes:
        image_type = str(kwargs.get("type") or "jpeg")
        params: dict[str, Any] = {"format": image_type}
        if image_type == "jpeg":
            params["quality"] = int(kwargs.get("quality") or 80)
        if kwargs.get("full_page"):
            metrics = await self.send("Page.getLayoutMetrics")
            size = metrics["cssContentSize"]
            params.update(captureBeyondViewport=True, clip={**size, "scale": 1})
        result = await self.send("Page.captureScreenshot", params, timeout=60.0)
        data = base64.b64decode(str(result.get("data") or ""))
        path = kwargs.get("path")
        if path:
            Path(str(path)).write_bytes(data)
        return data

    async def close(self) -> None:
        result = await self.connection.command("Target.closeTarget", {"targetId": self.target_id})
        if result.get("success") is not True:
            raise CDPError("Chrome did not confirm that the affected tab closed")
        self._closed = True

    def is_closed(self) -> bool:
        return self._closed

    async def set_viewport_size(self, viewport: dict[str, int]) -> None:
        self.viewport_size = dict(viewport)
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": int(viewport["width"]),
                "height": int(viewport["height"]),
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )

    async def install_in_frames(self, source: str) -> None:
        script = await self.send("Page.addScriptToEvaluateOnNewDocument", {"source": source, "runImmediately": True})
        await self.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script["identifier"]})

    async def upload_files(self, ref: int | str, selector: str, paths: list[str]) -> None:
        expression = f"globalThis.__spaceBrowserPageContent__.fileInputElementFor({json.dumps(ref)})"
        if selector:
            expression = f"({expression}) || document.querySelector({json.dumps(selector)})"
        result = await self.send("Runtime.evaluate", {"expression": expression})
        element = result.get("result", {})
        object_id = element.get("objectId")
        if not object_id:
            raise CDPError(f"Browser ref {ref!r} does not resolve to a file input")
        try:
            await self.send("DOM.setFileInputFiles", {"objectId": object_id, "files": paths})
        finally:
            with contextlib.suppress(CDPError):
                await self.send("Runtime.releaseObject", {"objectId": object_id})


class CDPContext:
    def __init__(self, connection: CDPConnection) -> None:
        self.connection = connection
        self.pages: list[CDPPage] = []
        self._pages_by_target: dict[str, CDPPage] = {}
        self._handlers: dict[str, Any] = {}
        self._init_scripts: list[str] = []
        self._target_lock = asyncio.Lock()
        connection.on_detached = self._detached

    def _detached(self, session_id: str | None) -> None:
        for page in list(self.pages):
            if session_id is None or page.session_id == session_id:
                self._remove_page(page)

    def _remove_page(self, page: CDPPage) -> None:
        self._pages_by_target.pop(page.target_id, None)
        self.pages.remove(page)
        page._closed = True
        if callback := page._handlers.get("close"):
            callback()

    def on(self, event: str, callback: Any) -> None:
        self._handlers[event] = callback

    async def add_init_script(self, script: str | None = None, *, path: str | None = None) -> None:
        source = script if script is not None else Path(str(path)).read_text(encoding="utf-8")
        self._init_scripts.append(str(source))
        for page in list(self.pages):
            with contextlib.suppress(Exception):
                await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": str(source)})

    async def new_page(self) -> CDPPage:
        result = await self.connection.command("Target.createTarget", {"url": "about:blank"})
        target_id = str(result.get("targetId") or "")
        try:
            async with self._target_lock:
                return self._pages_by_target.get(target_id) or await self._attach_page(target_id, "about:blank")
        except BaseException:
            with contextlib.suppress(Exception):
                await self.connection.command("Target.closeTarget", {"targetId": target_id})
            raise

    async def close(self) -> None:
        return None

    async def discover_pages(self) -> list[CDPPage]:
        async with self._target_lock:
            return await self._discover_pages()

    async def _discover_pages(self) -> list[CDPPage]:
        result = await self.connection.command("Target.getTargets", {})
        infos = result.get("targetInfos") if isinstance(result.get("targetInfos"), list) else []
        visible_targets: set[str] = set()
        for info in infos:
            if not isinstance(info, dict) or info.get("type") != "page":
                continue
            target_id = str(info.get("targetId") or "")
            url = str(info.get("url") or "about:blank")
            if not target_id or not _cdp_target_visible(url):
                continue
            visible_targets.add(target_id)
            page = self._pages_by_target.get(target_id)
            if page is None:
                page = await self._attach_page(target_id, url)
            else:
                page.url = url
        for page in list(self.pages):
            if page.target_id not in visible_targets:
                self._remove_page(page)
        return list(self.pages)

    async def _attach_page(self, target_id: str, url: str) -> CDPPage:
        attach = await self.connection.command("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = str(attach.get("sessionId") or "")
        if not target_id or not session_id:
            raise CDPError(f"Could not attach to Chrome target {target_id}.")
        page = CDPPage(self, target_id, session_id, url)
        self._pages_by_target[target_id] = page
        self.pages.append(page)
        with contextlib.suppress(Exception):
            await page.send("Page.enable", {})
        with contextlib.suppress(Exception):
            await page.send("Runtime.enable", {})
        for source in self._init_scripts:
            with contextlib.suppress(Exception):
                await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        return page


def _cdp_mouse_button(button: str) -> str:
    normalized = str(button or "left").strip().lower()
    if normalized == "right":
        return "right"
    if normalized in {"middle", "auxiliary"}:
        return "middle"
    return "left"


def _cdp_target_visible(url: str) -> bool:
    normalized = str(url or "")
    if normalized in {"", "about:blank", "chrome://newtab/"}:
        return True
    if normalized.startswith("chrome://inspect"):
        return True
    return not normalized.startswith(
        ("chrome://", "chrome-untrusted://", "chrome-extension://", "devtools://")
    )
