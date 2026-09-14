from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from agent_zero_cli.host_browser_common import (
    DEFAULT_VIEWPORT,
    REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS,
)

class CDPError(RuntimeError):
    pass


class CDPConnection:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()

    async def connect(self) -> None:
        timeout = aiohttp.ClientTimeout(total=REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS)
        self._session = aiohttp.ClientSession(timeout=timeout)
        try:
            endpoint = self.endpoint
            parsed = urlsplit(endpoint)
            if parsed.scheme in {"http", "https"}:
                path = parsed.path if parsed.path == "/json/version" else "/json/version"
                version_url = urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
                async with self._session.get(version_url) as response:
                    response.raise_for_status()
                    version = await response.json()
                endpoint = str(version.get("webSocketDebuggerUrl") or "").strip()
                resolved = urlsplit(endpoint)
                if resolved.scheme not in {"ws", "wss"} or not resolved.netloc or not resolved.path:
                    raise CDPError(f"{version_url} did not return webSocketDebuggerUrl.")
            self._ws = await self._session.ws_connect(
                endpoint,
                timeout=REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS,
                autoclose=True,
                autoping=True,
            )
        except Exception:
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

    async def _read_loop(self) -> None:
        assert self._ws is not None
        async for message in self._ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except Exception:
                    continue
                msg_id = payload.get("id")
                if isinstance(msg_id, int):
                    future = self._pending.get(msg_id)
                    if future is not None and not future.done():
                        future.set_result(payload)
            elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                break
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result({"error": {"message": "Chrome DevTools connection closed."}})


class CDPMouse:
    def __init__(self, page: "CDPPage") -> None:
        self.page = page
        self._x = 0.0
        self._y = 0.0

    async def move(self, x: float, y: float, steps: int | None = None) -> None:
        del steps
        self._x = float(x)
        self._y = float(y)
        await self.page.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": self._x, "y": self._y})

    async def click(self, x: float, y: float, button: str = "left") -> None:
        self._x = float(x)
        self._y = float(y)
        params = {"x": float(x), "y": float(y), "button": _cdp_mouse_button(button), "clickCount": 1}
        await self.page.send("Input.dispatchMouseEvent", {"type": "mousePressed", **params})
        await self.page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **params})

    async def dblclick(self, x: float, y: float, button: str = "left") -> None:
        self._x = float(x)
        self._y = float(y)
        params = {"x": float(x), "y": float(y), "button": _cdp_mouse_button(button), "clickCount": 2}
        await self.page.send("Input.dispatchMouseEvent", {"type": "mousePressed", **params})
        await self.page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **params})

    async def down(self) -> None:
        await self.page.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": self._x, "y": self._y, "button": "left", "clickCount": 1},
        )

    async def up(self) -> None:
        await self.page.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": self._x, "y": self._y, "button": "left", "clickCount": 1},
        )

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self.page.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": self._x, "y": self._y, "deltaX": float(delta_x), "deltaY": float(delta_y)},
        )


class CDPKeyboard:
    def __init__(self, page: "CDPPage") -> None:
        self.page = page

    async def type(self, text: str) -> None:
        await self.insert_text(text)

    async def insert_text(self, text: str) -> None:
        await self.page.send("Input.insertText", {"text": str(text or "")})

    async def press(self, key: str) -> None:
        key_text = str(key or "")
        if len(key_text) == 1:
            await self.page.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": key_text, "key": key_text})
            await self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key_text})
            return
        await self.page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key_text})
        await self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key_text})

    async def down(self, key: str) -> None:
        await self.page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": str(key or "")})

    async def up(self, key: str) -> None:
        await self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": str(key or "")})


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

    def on(self, event: str, callback: Any) -> None:
        self._handlers[event] = callback

    async def send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        return await self.connection.command(method, params, session_id=self.session_id, timeout=timeout)

    async def goto(self, url: str, **_: object) -> None:
        await self.send("Page.navigate", {"url": url})
        self.url = url

    async def go_back(self, **_: object) -> None:
        await self.evaluate("() => history.back()")

    async def go_forward(self, **_: object) -> None:
        await self.evaluate("() => history.forward()")

    async def reload(self, **kwargs: object) -> None:
        previous = await self.send("Page.getFrameTree") if kwargs.get("wait_until") == "commit" else None
        await self.send("Page.reload", {})
        if previous is not None:
            loader = previous["frameTree"]["frame"]["loaderId"]
            while (await self.send("Page.getFrameTree"))["frameTree"]["frame"]["loaderId"] == loader:
                await asyncio.sleep(0.05)

    async def wait_for_load_state(self, *_: object, **__: object) -> None:
        await asyncio.sleep(0.15)

    async def bring_to_front(self) -> None:
        await self.connection.command("Target.activateTarget", {"targetId": self.target_id})

    async def title(self) -> str:
        result = await self.evaluate("() => document.title")
        return str(result or "")

    async def evaluate(self, script: str, arg: object = None, *, timeout: float = 30.0) -> object:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": _cdp_evaluate_expression(script, arg),
                "awaitPromise": True,
                "returnByValue": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise CDPError(str(result["exceptionDetails"]))
        remote = result.get("result") if isinstance(result.get("result"), dict) else {}
        if "value" in remote:
            return remote.get("value")
        if remote.get("type") == "undefined":
            return None
        return remote.get("description")

    async def screenshot(self, **kwargs: object) -> bytes:
        image_type = str(kwargs.get("type") or "jpeg")
        params: dict[str, Any] = {"format": image_type}
        if image_type == "jpeg":
            params["quality"] = int(kwargs.get("quality") or 80)
        if kwargs.get("full_page"):
            params["captureBeyondViewport"] = True
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

    async def evaluate_handle(self, *_: object, **__: object) -> object:
        raise NotImplementedError(
            "CDPPage.evaluate_handle is not supported for user-authorized Chrome remote debugging yet."
        )

    async def set_input_files(self, *_: object, **__: object) -> None:
        raise NotImplementedError("File uploads are not supported for user-authorized Chrome remote debugging yet.")


class CDPContext:
    def __init__(self, connection: CDPConnection) -> None:
        self.connection = connection
        self.pages: list[CDPPage] = []
        self._pages_by_target: dict[str, CDPPage] = {}
        self._handlers: dict[str, Any] = {}
        self._init_scripts: list[str] = []

    def set_default_timeout(self, timeout: int) -> None:
        del timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        del timeout

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
        return await self._attach_page(target_id, "about:blank")

    async def close(self) -> None:
        return None

    async def discover_pages(self) -> list[CDPPage]:
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
        for target_id in list(self._pages_by_target):
            if target_id not in visible_targets:
                self._pages_by_target.pop(target_id, None)
        self.pages = [page for page in self.pages if page.target_id in self._pages_by_target]
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


def _cdp_evaluate_expression(script: str, arg: object = None) -> str:
    source = str(script or "undefined")
    if arg is not None:
        return f"({source})({json.dumps(arg)})"
    stripped = source.strip()
    if _cdp_expression_is_iife(stripped):
        return source
    if _cdp_expression_is_function(stripped):
        return f"({source})()"
    return source


def _cdp_expression_is_function(source: str) -> bool:
    return bool(
        re.match(r"^(?:async\s+)?function\b", source)
        or re.match(r"^(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", source)
    )


def _cdp_expression_is_iife(source: str) -> bool:
    compact = source.rstrip()
    return compact.endswith(")();") or compact.endswith("})();")


def _cdp_target_visible(url: str) -> bool:
    normalized = str(url or "")
    if normalized in {"", "about:blank", "chrome://newtab/"}:
        return True
    if normalized.startswith("chrome://inspect"):
        return True
    return not normalized.startswith(
        ("chrome://", "chrome-untrusted://", "chrome-extension://", "devtools://")
    )
