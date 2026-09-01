from __future__ import annotations

import asyncio
import base64
import contextlib
from io import BytesIO
from pathlib import Path
import re
import socket
from typing import Any

import httpx
from PIL import Image

from agent_zero_cli.host_browser_common import DEFAULT_VIEWPORT, SAFARI_DRIVER_PATH
_ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"
_FUNCTION_RE = re.compile(
    r"^(?:async\s+)?(?:function\b|(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>)"
)
_KEYS = {
    "Backspace": "\ue003",
    "Tab": "\ue004",
    "Enter": "\ue007",
    "Return": "\ue007",
    "Shift": "\ue008",
    "Control": "\ue009",
    "Alt": "\ue00a",
    "Escape": "\ue00c",
    "Esc": "\ue00c",
    "Space": " ",
    "PageUp": "\ue00e",
    "PageDown": "\ue00f",
    "End": "\ue010",
    "Home": "\ue011",
    "ArrowLeft": "\ue012",
    "ArrowUp": "\ue013",
    "ArrowRight": "\ue014",
    "ArrowDown": "\ue015",
    "Insert": "\ue016",
    "Delete": "\ue017",
    "Meta": "\ue03d",
    "Command": "\ue03d",
}


class SafariDriverError(RuntimeError):
    pass


class SafariDriver:
    def __init__(self, executable: Path | str = SAFARI_DRIVER_PATH) -> None:
        self.executable = Path(executable)
        self.process: asyncio.subprocess.Process | None = None
        self.client: httpx.AsyncClient | None = None
        self.session_id = ""
        self.current_handle = ""
        self.context: SafariContext | None = None

    async def start(self) -> "SafariContext":
        if not self.executable.is_file():
            raise SafariDriverError(f"Safari WebDriver was not found at {self.executable}.")

        port = _free_loopback_port()
        self.process = await asyncio.create_subprocess_exec(
            str(self.executable),
            "--port",
            str(port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout=httpx.Timeout(35.0, connect=1.0),
            trust_env=False,
        )
        try:
            await self._wait_until_ready()
            value = await self.request(
                "POST",
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "browserName": "safari",
                            "pageLoadStrategy": "normal",
                        }
                    }
                },
            )
            if not isinstance(value, dict):
                raise SafariDriverError("Safari WebDriver returned an invalid session response.")
            self.session_id = str(value.get("sessionId") or "")
            if not self.session_id:
                raise SafariDriverError("Safari WebDriver did not return a session ID.")
            self.context = SafariContext(self)
            await self.context.discover_pages(notify=False)
            return self.context
        except BaseException:
            await self.close()
            raise

    async def _wait_until_ready(self) -> None:
        last_error = ""
        for _ in range(100):
            if self.process is not None and self.process.returncode is not None:
                raise SafariDriverError(
                    f"Safari WebDriver exited with code {self.process.returncode}."
                )
            try:
                await self.request("GET", "/status")
                return
            except SafariDriverError as exc:
                last_error = str(exc)
            await asyncio.sleep(0.05)
        raise SafariDriverError(
            "Safari WebDriver did not become ready."
            + (f" {last_error}" if last_error else "")
        )

    async def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if self.client is None:
            raise SafariDriverError("Safari WebDriver is not running.")
        try:
            response = await self.client.request(method, path, json=payload)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SafariDriverError(f"Safari WebDriver request failed: {exc}") from exc

        value = body.get("value") if isinstance(body, dict) else None
        if response.is_error or (isinstance(value, dict) and value.get("error")):
            message = ""
            if isinstance(value, dict):
                message = str(value.get("message") or value.get("error") or "")
            raise SafariDriverError(_friendly_driver_error(message or response.reason_phrase))
        return value

    async def session_request(
        self,
        method: str,
        path: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if not self.session_id:
            raise SafariDriverError("Safari WebDriver session is not active.")
        suffix = f"/{path.lstrip('/')}" if path else ""
        return await self.request(method, f"/session/{self.session_id}{suffix}", payload)

    async def close(self) -> None:
        if self.session_id and self.client is not None:
            with contextlib.suppress(Exception):
                await self.session_request("DELETE")
        self.session_id = ""
        self.current_handle = ""
        self.context = None
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


class SafariContext:
    def __init__(self, driver: SafariDriver) -> None:
        self.driver = driver
        self._pages_by_handle: dict[str, SafariPage] = {}
        self._handle_order: list[str] = []
        self._claimed_handles: set[str] = set()
        self._handlers: dict[str, Any] = {}
        self._init_scripts: list[str] = []

    @property
    def pages(self) -> list["SafariPage"]:
        return [
            self._pages_by_handle[handle]
            for handle in self._handle_order
            if handle in self._claimed_handles and handle in self._pages_by_handle
        ]

    def set_default_timeout(self, timeout: int) -> None:
        del timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        del timeout

    def on(self, event: str, callback: Any) -> None:
        self._handlers[event] = callback

    async def add_init_script(
        self,
        script: str | None = None,
        *,
        path: str | None = None,
    ) -> None:
        source = script if script is not None else Path(str(path)).read_text(encoding="utf-8")
        self._init_scripts.append(str(source))

    async def new_page(self) -> "SafariPage":
        await self.discover_pages()
        for handle in self._handle_order:
            if handle not in self._claimed_handles:
                self._claimed_handles.add(handle)
                return self._pages_by_handle[handle]

        try:
            value = await self.driver.session_request(
                "POST",
                "window/new",
                {"type": "tab"},
            )
            handle = str(value.get("handle") or "") if isinstance(value, dict) else ""
        except SafariDriverError:
            handle = await self._open_page_with_script()
        if not handle:
            raise SafariDriverError("Safari did not create a new tab.")
        page = self._page_for_handle(handle)
        self._claimed_handles.add(handle)
        return page

    async def _open_page_with_script(self) -> str:
        existing = set(self._handle_order)
        page = next(iter(self._pages_by_handle.values()), None)
        if page is None:
            return ""
        await page._execute("window.open('about:blank', '_blank'); return null;")
        for _ in range(20):
            await asyncio.sleep(0.05)
            handles = await self._window_handles()
            created = next((handle for handle in handles if handle not in existing), "")
            if created:
                return created
        return ""

    async def discover_pages(self, *, notify: bool = True) -> list["SafariPage"]:
        handles = await self._window_handles()
        removed = set(self._pages_by_handle) - set(handles)
        for handle in removed:
            page = self._pages_by_handle.pop(handle)
            self._claimed_handles.discard(handle)
            page._emit_close()

        new_pages: list[SafariPage] = []
        for handle in handles:
            if handle not in self._pages_by_handle:
                new_pages.append(self._page_for_handle(handle))
        self._handle_order = handles

        callback = self._handlers.get("page")
        if notify and callable(callback):
            for page in new_pages:
                self._claimed_handles.add(page.handle)
                callback(page)
        return self.pages

    async def close(self) -> None:
        await self.driver.close()
        self._pages_by_handle.clear()
        self._handle_order.clear()
        self._claimed_handles.clear()

    async def activate(self, handle: str) -> None:
        if self.driver.current_handle == handle:
            return
        await self.driver.session_request("POST", "window", {"handle": handle})
        self.driver.current_handle = handle

    async def release(self, handle: str) -> None:
        self._claimed_handles.discard(handle)

    async def remove(self, handle: str) -> None:
        page = self._pages_by_handle.pop(handle, None)
        self._claimed_handles.discard(handle)
        self._handle_order = [item for item in self._handle_order if item != handle]
        if page is not None:
            page._emit_close()

    def _page_for_handle(self, handle: str) -> "SafariPage":
        page = self._pages_by_handle.get(handle)
        if page is None:
            page = SafariPage(self, handle)
            self._pages_by_handle[handle] = page
            self._handle_order.append(handle)
        return page

    async def _window_handles(self) -> list[str]:
        value = await self.driver.session_request("GET", "window/handles")
        return [str(handle) for handle in value] if isinstance(value, list) else []


class SafariPage:
    def __init__(self, context: SafariContext, handle: str) -> None:
        self.context = context
        self.driver = context.driver
        self.handle = handle
        self.url = "about:blank"
        self.viewport_size = dict(DEFAULT_VIEWPORT)
        self.mouse = SafariMouse(self)
        self.keyboard = SafariKeyboard(self)
        self._handlers: dict[str, Any] = {}

    @property
    def frames(self) -> list["SafariPage"]:
        return [self]

    def on(self, event: str, callback: Any) -> None:
        self._handlers[event] = callback

    async def goto(self, url: str, **_: object) -> None:
        await self._activate()
        await self.driver.session_request("POST", "url", {"url": url})
        self.url = url

    async def go_back(self, **_: object) -> None:
        await self._activate()
        await self.driver.session_request("POST", "back", {})

    async def go_forward(self, **_: object) -> None:
        await self._activate()
        await self.driver.session_request("POST", "forward", {})

    async def reload(self, **_: object) -> None:
        await self._activate()
        await self.driver.session_request("POST", "refresh", {})

    async def wait_for_load_state(self, *_: object, **kwargs: object) -> None:
        timeout = max(0.05, float(kwargs.get("timeout") or 1000) / 1000)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            with contextlib.suppress(Exception):
                state = await self.evaluate("() => document.readyState")
                if state in {"interactive", "complete"}:
                    break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)
        await self.context.discover_pages()

    async def bring_to_front(self) -> None:
        await self._activate()

    async def title(self) -> str:
        await self._activate()
        return str(await self.driver.session_request("GET", "title") or "")

    async def current_url(self) -> str:
        await self._activate()
        self.url = str(await self.driver.session_request("GET", "url") or "")
        return self.url

    async def evaluate(self, script: str, arg: object = None) -> object:
        source, args = _webdriver_script(script, arg)
        return await self._execute(source, args)

    async def _execute(self, source: str, args: list[object] | None = None) -> object:
        await self._activate()
        return await self.driver.session_request(
            "POST",
            "execute/sync",
            {"script": source, "args": args or []},
        )

    async def screenshot(self, **kwargs: object) -> bytes:
        if kwargs.get("full_page"):
            raise SafariDriverError(
                "Safari WebDriver supports viewport screenshots, not full-page screenshots."
            )
        await self._activate()
        encoded = str(await self.driver.session_request("GET", "screenshot") or "")
        image = base64.b64decode(encoded)
        image_type = str(kwargs.get("type") or "png")
        if image_type == "jpeg":
            with Image.open(BytesIO(image)) as source:
                output = BytesIO()
                source.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=int(kwargs.get("quality") or 80),
                )
                image = output.getvalue()
        path = kwargs.get("path")
        if path:
            Path(str(path)).write_bytes(image)
        return image

    async def close(self) -> None:
        await self._activate()
        handles = await self.context._window_handles()
        if len(handles) <= 1:
            await self.goto("about:blank")
            await self.context.release(self.handle)
            self._emit_close()
            return
        await self.driver.session_request("DELETE", "window")
        self.driver.current_handle = ""
        await self.context.remove(self.handle)

    async def set_viewport_size(self, viewport: dict[str, int]) -> None:
        await self._activate()
        chrome = await self.evaluate(
            "() => ({width: Math.max(0, outerWidth - innerWidth), "
            "height: Math.max(0, outerHeight - innerHeight)})"
        )
        width_extra = int(chrome.get("width") or 0) if isinstance(chrome, dict) else 0
        height_extra = int(chrome.get("height") or 0) if isinstance(chrome, dict) else 0
        await self.driver.session_request(
            "POST",
            "window/rect",
            {
                "width": int(viewport["width"]) + width_extra,
                "height": int(viewport["height"]) + height_extra,
            },
        )
        self.viewport_size = dict(viewport)

    async def evaluate_handle(
        self,
        script: str,
        arg: object = None,
    ) -> "SafariElementHandle | None":
        value = await self.evaluate(script, arg)
        if not isinstance(value, dict) or not value.get(_ELEMENT_KEY):
            return None
        return SafariElementHandle(self, str(value[_ELEMENT_KEY]))

    async def set_input_files(self, selector: str, paths: list[str]) -> None:
        await self._activate()
        value = await self.driver.session_request(
            "POST",
            "element",
            {"using": "css selector", "value": str(selector)},
        )
        if not isinstance(value, dict) or not value.get(_ELEMENT_KEY):
            raise SafariDriverError(f"Safari could not find file input {selector!r}.")
        await SafariElementHandle(self, str(value[_ELEMENT_KEY])).set_input_files(paths)

    async def _activate(self) -> None:
        await self.context.activate(self.handle)

    def _emit_close(self) -> None:
        callback = self._handlers.get("close")
        if callable(callback):
            callback()


class SafariElementHandle:
    def __init__(self, page: SafariPage, element_id: str) -> None:
        self.page = page
        self.element_id = element_id

    def as_element(self) -> "SafariElementHandle":
        return self

    async def set_input_files(self, paths: list[str]) -> None:
        await self.page._activate()
        text = "\n".join(str(path) for path in paths)
        await self.page.driver.session_request(
            "POST",
            f"element/{self.element_id}/value",
            {"text": text, "value": list(text)},
        )

    async def dispose(self) -> None:
        return None


class SafariMouse:
    def __init__(self, page: SafariPage) -> None:
        self.page = page
        self.x = 0
        self.y = 0
        self.button = "left"

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.x, self.y = round(x), round(y)
        await self._actions(
            [
                {
                    "type": "pointerMove",
                    "duration": min(1000, max(0, int(steps) * 16)),
                    "origin": "viewport",
                    "x": self.x,
                    "y": self.y,
                }
            ]
        )

    async def down(self, button: str = "left") -> None:
        self.button = button
        await self._actions([{"type": "pointerDown", "button": _mouse_button(button)}])

    async def up(self, button: str | None = None) -> None:
        await self._actions(
            [{"type": "pointerUp", "button": _mouse_button(button or self.button)}]
        )

    async def click(self, x: float, y: float, button: str = "left") -> None:
        await self.move(x, y)
        await self._actions(
            [
                {"type": "pointerDown", "button": _mouse_button(button)},
                {"type": "pointerUp", "button": _mouse_button(button)},
            ]
        )

    async def dblclick(self, x: float, y: float, button: str = "left") -> None:
        await self.move(x, y)
        action = {"type": "pointerDown", "button": _mouse_button(button)}
        release = {"type": "pointerUp", "button": _mouse_button(button)}
        await self._actions([action, release, action, release])

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self.page._activate()
        await self.page.driver.session_request(
            "POST",
            "actions",
            {
                "actions": [
                    {
                        "type": "wheel",
                        "id": "a0-wheel",
                        "actions": [
                            {
                                "type": "scroll",
                                "duration": 0,
                                "origin": "viewport",
                                "x": self.x,
                                "y": self.y,
                                "deltaX": round(delta_x),
                                "deltaY": round(delta_y),
                            }
                        ],
                    }
                ]
            },
        )

    async def _actions(self, actions: list[dict[str, Any]]) -> None:
        await self.page._activate()
        await self.page.driver.session_request(
            "POST",
            "actions",
            {
                "actions": [
                    {
                        "type": "pointer",
                        "id": "a0-mouse",
                        "parameters": {"pointerType": "mouse"},
                        "actions": actions,
                    }
                ]
            },
        )


class SafariKeyboard:
    def __init__(self, page: SafariPage) -> None:
        self.page = page

    async def type(self, text: str) -> None:
        await self.insert_text(text)

    async def insert_text(self, text: str) -> None:
        await self.page._activate()
        value = await self.page.driver.session_request("GET", "element/active")
        if not isinstance(value, dict) or not value.get(_ELEMENT_KEY):
            raise SafariDriverError("Safari has no active element for keyboard input.")
        element_id = str(value[_ELEMENT_KEY])
        await self.page.driver.session_request(
            "POST",
            f"element/{element_id}/value",
            {"text": str(text or ""), "value": list(str(text or ""))},
        )

    async def press(self, key: str) -> None:
        parts = [part for part in str(key or "").split("+") if part]
        actions = [{"type": "keyDown", "value": _key_value(part)} for part in parts]
        actions.extend(
            {"type": "keyUp", "value": _key_value(part)} for part in reversed(parts)
        )
        await self._actions(actions)

    async def down(self, key: str) -> None:
        await self._actions([{"type": "keyDown", "value": _key_value(key)}])

    async def up(self, key: str) -> None:
        await self._actions([{"type": "keyUp", "value": _key_value(key)}])

    async def _actions(self, actions: list[dict[str, Any]]) -> None:
        await self.page._activate()
        await self.page.driver.session_request(
            "POST",
            "actions",
            {"actions": [{"type": "key", "id": "a0-keyboard", "actions": actions}]},
        )


def _webdriver_script(script: str, arg: object = None) -> tuple[str, list[object]]:
    source = str(script or "undefined").strip()
    if arg is not None:
        return f"return ({source})(arguments[0]);", [arg]
    if source.endswith(")();") or source.endswith("})();"):
        return source, []
    if _FUNCTION_RE.match(source):
        return f"return ({source})();", []
    return f"return ({source});", []


def _friendly_driver_error(message: str) -> str:
    detail = str(message or "Safari WebDriver error").strip()
    normalized = detail.lower()
    if "remote automation" in normalized and (
        "off" in normalized
        or "disabled" in normalized
        or "not enabled" in normalized
        or "must enable" in normalized
    ):
        return (
            "Safari remote automation is off. In Safari > Settings > Advanced, "
            "enable Show features for web developers; then open Developer and enable "
            "Allow remote automation before retrying Set up browser."
        )
    if "session" in normalized and ("already" in normalized or "in progress" in normalized):
        return "Safari is already controlled by another automation session. Close it and retry."
    return detail


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _mouse_button(button: str) -> int:
    return {"left": 0, "middle": 1, "auxiliary": 1, "right": 2}.get(
        str(button or "left").lower(),
        0,
    )


def _key_value(key: str) -> str:
    value = str(key or "")
    return _KEYS.get(value, value)
