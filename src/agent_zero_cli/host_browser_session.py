from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import dataclass, field, replace
import platform
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from agent_zero_cli.host_browser_cdp import CDPConnection, CDPContext, CDPPage
from agent_zero_cli.host_browser_safari import (
    SafariDriver,
    SafariDriverError,
    SafariPage,
)
from agent_zero_cli.host_browser_common import (
    DEFAULT_VIEWPORT,
    BrowserProfile,
    ProfileLockState,
    chromium_launch_args,
    coerce_bool,
    coerce_float,
    coerce_int,
    content_helper_sha256,
    has_ref,
    multi_group_key,
    normalize_action,
    normalize_modifiers,
    normalize_upload_paths,
    normalize_url,
    profile_lock_state_for_profile,
    remote_debugging_endpoint_from_user_data_dir,
    remote_debugging_endpoint_label,
    remote_debugging_enable_hint,
    require_ref,
    screenshot_output_path,
)

@dataclass
class HostBrowserPage:
    id: int
    page: Any


class _RuntimeAdapter:
    close_pages_on_session_close = True

    async def is_started(self, session: "HostBrowserSession") -> bool:
        return session.context is not None

    async def start(self, session: "HostBrowserSession") -> None:
        raise NotImplementedError

    async def refresh_pages(self, session: "HostBrowserSession") -> None:
        del session

    async def current_url(self, page: Any) -> str:
        return str(getattr(page, "url", "") or "")

    async def close_runtime(self, session: "HostBrowserSession") -> None:
        if self.close_pages_on_session_close:
            for browser_id in list(session.pages):
                with contextlib.suppress(Exception):
                    await session.pages[browser_id].page.close()
        session.pages.clear()
        if session.context is not None:
            with contextlib.suppress(Exception):
                await session.context.close()
            session.context = None
        if session.playwright is not None:
            with contextlib.suppress(Exception):
                await session.playwright.stop()
            session.playwright = None
        session.browser = None
        session.last_interacted_browser_id = None


class _PlaywrightRuntimeAdapter(_RuntimeAdapter):
    async def start(self, session: "HostBrowserSession") -> None:
        if session.playwright_starter is not None:
            starter = session.playwright_starter
        else:
            from playwright.async_api import async_playwright

            starter = async_playwright
        session.playwright = await starter().start()
        launch_args = chromium_launch_args(session.profile.profile_directory)
        session.context = await session.playwright.chromium.launch_persistent_context(
            user_data_dir=str(session.profile.user_data_dir),
            executable_path=session.profile.executable_path,
            headless=False,
            accept_downloads=True,
            viewport=DEFAULT_VIEWPORT,
            screen=DEFAULT_VIEWPORT,
            no_viewport=False,
            args=launch_args,
        )


class _CDPRuntimeAdapter(_RuntimeAdapter):
    close_pages_on_session_close = False

    async def start(self, session: "HostBrowserSession") -> None:
        profile = _refreshed_remote_debugging_profile(session.profile)
        session.profile = profile
        for attempt in range(2):
            connection = CDPConnection(profile.cdp_endpoint)
            try:
                await connection.connect()
                break
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await connection.close()
                refreshed = _refreshed_remote_debugging_profile(profile)
                if attempt or refreshed == profile:
                    detail = str(exc).strip() or type(exc).__name__
                    raise RuntimeError(
                        "Cannot connect to the host browser remote-debugging endpoint "
                        f"{profile.cdp_endpoint}. {remote_debugging_enable_hint()} "
                        f"Original error: {detail}"
                    ) from exc
                profile = refreshed
                session.profile = profile
        session.browser = connection
        session.context = CDPContext(connection)
        await session.context.discover_pages()

    async def refresh_pages(self, session: "HostBrowserSession") -> None:
        discovered_pages = await session.context.discover_pages()
        visible_pages = set(discovered_pages)
        for browser_id, browser_page in list(session.pages.items()):
            if browser_page.page not in visible_pages:
                session.pages.pop(browser_id, None)
                if session.last_interacted_browser_id == browser_id:
                    session.last_interacted_browser_id = None
        for page in discovered_pages:
            await session._register_page(page)
        if session.last_interacted_browser_id not in session.pages:
            session.last_interacted_browser_id = next(iter(sorted(session.pages)), None)

    async def current_url(self, page: Any) -> str:
        if isinstance(page, CDPPage):
            with contextlib.suppress(Exception):
                current_url = await page.evaluate("() => location.href")
                if current_url:
                    page.url = str(current_url)
        return await super().current_url(page)

    async def close_runtime(self, session: "HostBrowserSession") -> None:
        session.pages.clear()
        if session.browser is not None:
            with contextlib.suppress(Exception):
                await session.browser.close()
        session.browser = None
        session.context = None
        session.last_interacted_browser_id = None


class _SafariRuntimeAdapter(_RuntimeAdapter):
    close_pages_on_session_close = False

    async def is_started(self, session: "HostBrowserSession") -> bool:
        process = getattr(session.browser, "process", None)
        if session.context is None or process is None or process.returncode is not None:
            return False
        try:
            await session.browser.session_request("GET", "window/handles")
        except SafariDriverError:
            return False
        return True

    async def start(self, session: "HostBrowserSession") -> None:
        driver = SafariDriver()
        session.browser = driver
        session.context = await driver.start()

    async def refresh_pages(self, session: "HostBrowserSession") -> None:
        discovered_pages = await session.context.discover_pages()
        visible_pages = set(discovered_pages)
        for browser_id, browser_page in list(session.pages.items()):
            if browser_page.page not in visible_pages:
                session.pages.pop(browser_id, None)
                if session.last_interacted_browser_id == browser_id:
                    session.last_interacted_browser_id = None
        for page in discovered_pages:
            await session._register_page(page)
        if session.last_interacted_browser_id not in session.pages:
            session.last_interacted_browser_id = next(iter(sorted(session.pages)), None)

    async def current_url(self, page: Any) -> str:
        if isinstance(page, SafariPage):
            return await page.current_url()
        return await super().current_url(page)

    async def close_runtime(self, session: "HostBrowserSession") -> None:
        session.pages.clear()
        if session.browser is not None:
            with contextlib.suppress(Exception):
                await session.browser.close()
        session.browser = None
        session.context = None
        session.last_interacted_browser_id = None


def _refreshed_remote_debugging_profile(profile: BrowserProfile) -> BrowserProfile:
    if not profile.is_remote_debugging or profile.user_data_dir == Path():
        return profile
    endpoint = remote_debugging_endpoint_from_user_data_dir(profile.user_data_dir)
    if not endpoint or endpoint == profile.cdp_endpoint:
        return profile
    return replace(
        profile,
        profile_directory=remote_debugging_endpoint_label(endpoint),
        cdp_endpoint=endpoint,
    )


@dataclass
class HostBrowserSession:
    context_id: str
    profile: BrowserProfile
    playwright_starter: Callable[[], Any] | None = None
    playwright: Any = None
    browser: Any = None
    context: Any = None
    pages: dict[int, HostBrowserPage] = field(default_factory=dict)
    next_browser_id: int = 1
    last_interacted_browser_id: int | None = None
    _dom_helper_source: str | None = None
    _dom_helper_sha256: str = ""
    _content_helper_source: str | None = None
    _content_helper_sha256: str = ""
    _start_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _registry_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closing: bool = False

    @property
    def _runtime(self) -> _RuntimeAdapter:
        if self.profile.is_safari:
            return _SafariRuntimeAdapter()
        if self.profile.is_remote_debugging:
            return _CDPRuntimeAdapter()
        return _PlaywrightRuntimeAdapter()

    async def set_dom_helper_source(self, source: str, source_hash: str = "") -> None:
        normalized_source = str(source or "")
        if not normalized_source:
            return
        normalized_hash = str(source_hash or "").strip().lower() or content_helper_sha256(normalized_source)
        if (
            self._dom_helper_source == normalized_source
            and self._dom_helper_sha256 == normalized_hash
        ):
            return
        self._dom_helper_source = normalized_source
        self._dom_helper_sha256 = normalized_hash
        if self.context is not None:
            with contextlib.suppress(Exception):
                await self.context.add_init_script(script=normalized_source)

    async def set_content_helper_source(self, source: str, source_hash: str = "") -> None:
        normalized_source = str(source or "")
        if not normalized_source:
            return
        normalized_hash = str(source_hash or "").strip().lower() or content_helper_sha256(normalized_source)
        if (
            self._content_helper_source == normalized_source
            and self._content_helper_sha256 == normalized_hash
        ):
            return
        self._content_helper_source = normalized_source
        self._content_helper_sha256 = normalized_hash
        if self.context is not None:
            with contextlib.suppress(Exception):
                await self.context.add_init_script(script=normalized_source)

    async def dispatch(self, payload: dict[str, Any]) -> Any:
        action = normalize_action(payload.get("action"))
        if action == "open":
            return await self.open(str(payload.get("url") or ""))
        if action == "list":
            return await self.list(include_content=coerce_bool(payload.get("include_content")))
        if action in {"state", "set_active", "back", "forward", "reload"}:
            return await self._dispatch_navigation_action(action, payload)
        if action == "navigate":
            return await self.navigate(payload.get("browser_id"), str(payload.get("url") or ""))
        if action in {"content", "detail", "evaluate", "click", "type", "submit", "type_submit", "scroll"}:
            return await self._dispatch_content_action(action, payload)
        if action in {"hover", "double_click", "right_click", "drag", "wheel", "mouse"}:
            return await self._dispatch_pointer_action(action, payload)
        if action in {"keyboard", "key_chord", "clipboard"}:
            return await self._dispatch_keyboard_action(action, payload)
        if action in {"set_viewport", "select_option", "set_checked", "upload_file"}:
            return await self._dispatch_form_action(action, payload)
        if action in {"screenshot", "screenshot_file", "close", "close_all", "multi"}:
            return await self._dispatch_session_action(action, payload)
        raise ValueError(f"Unsupported host browser action: {action}")

    async def _dispatch_navigation_action(self, action: str, payload: dict[str, Any]) -> Any:
        browser_id = payload.get("browser_id")
        if action == "state":
            return await self.state(browser_id)
        if action == "set_active":
            return await self.set_active(browser_id)
        if action == "back":
            return await self.back(browser_id)
        if action == "forward":
            return await self.forward(browser_id)
        return await self.reload(browser_id)

    async def _dispatch_content_action(self, action: str, payload: dict[str, Any]) -> Any:
        browser_id = payload.get("browser_id")
        if action == "content":
            selector_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else None
            return await self.content(browser_id, selector_payload)
        if action == "detail":
            return await self.detail(browser_id, require_ref(payload.get("ref"), "detail"))
        if action == "evaluate":
            return await self.evaluate(browser_id, str(payload.get("script") or ""))
        if action == "click":
            return await self.click(
                browser_id,
                require_ref(payload.get("ref"), "click"),
                modifiers=payload.get("modifiers"),
                focus_popup=payload.get("focus_popup"),
            )
        if action == "type":
            return await self.type(browser_id, require_ref(payload.get("ref"), "type"), str(payload.get("text") or ""))
        if action == "submit":
            return await self.submit(browser_id, require_ref(payload.get("ref"), "submit"))
        if action == "type_submit":
            return await self.type_submit(
                browser_id,
                require_ref(payload.get("ref"), "type_submit"),
                str(payload.get("text") or ""),
            )
        return await self.scroll(browser_id, require_ref(payload.get("ref"), "scroll"))

    async def _dispatch_pointer_action(self, action: str, payload: dict[str, Any]) -> Any:
        browser_id = payload.get("browser_id")
        if action == "hover":
            return await self.hover(
                browser_id,
                ref=payload.get("ref"),
                x=coerce_float(payload.get("x")),
                y=coerce_float(payload.get("y")),
                offset_x=coerce_float(payload.get("offset_x")),
                offset_y=coerce_float(payload.get("offset_y")),
            )
        if action == "double_click":
            return await self.double_click(
                browser_id,
                ref=payload.get("ref"),
                x=coerce_float(payload.get("x")),
                y=coerce_float(payload.get("y")),
                button=str(payload.get("button") or "left"),
                modifiers=payload.get("modifiers"),
                offset_x=coerce_float(payload.get("offset_x")),
                offset_y=coerce_float(payload.get("offset_y")),
            )
        if action == "right_click":
            return await self.right_click(
                browser_id,
                ref=payload.get("ref"),
                x=coerce_float(payload.get("x")),
                y=coerce_float(payload.get("y")),
                modifiers=payload.get("modifiers"),
                offset_x=coerce_float(payload.get("offset_x")),
                offset_y=coerce_float(payload.get("offset_y")),
            )
        if action == "drag":
            return await self.drag(
                browser_id,
                ref=payload.get("ref"),
                target_ref=payload.get("target_ref"),
                x=coerce_float(payload.get("x")),
                y=coerce_float(payload.get("y")),
                to_x=coerce_float(payload.get("to_x")),
                to_y=coerce_float(payload.get("to_y")),
                offset_x=coerce_float(payload.get("offset_x")),
                offset_y=coerce_float(payload.get("offset_y")),
                target_offset_x=coerce_float(payload.get("target_offset_x")),
                target_offset_y=coerce_float(payload.get("target_offset_y")),
            )
        if action == "wheel":
            return await self.wheel(
                browser_id,
                coerce_float(payload.get("x")),
                coerce_float(payload.get("y")),
                coerce_float(payload.get("delta_x")),
                coerce_float(payload.get("delta_y")),
            )
        return await self.mouse(
            browser_id,
            str(payload.get("event_type") or "click"),
            coerce_float(payload.get("x")),
            coerce_float(payload.get("y")),
            button=str(payload.get("button") or "left"),
            modifiers=payload.get("modifiers"),
        )

    async def _dispatch_keyboard_action(self, action: str, payload: dict[str, Any]) -> Any:
        browser_id = payload.get("browser_id")
        if action == "keyboard":
            return await self.keyboard(
                browser_id,
                key=str(payload.get("key") or ""),
                text=str(payload.get("text") or ""),
            )
        if action == "key_chord":
            keys = payload.get("keys")
            if not isinstance(keys, list) or not keys:
                raise ValueError("key_chord requires non-empty keys")
            return await self.key_chord(browser_id, [str(key) for key in keys])
        return await self.clipboard(
            browser_id,
            action=str(payload.get("clipboard_action") or payload.get("operation") or ""),
            text=str(payload.get("text") or ""),
        )

    async def _dispatch_form_action(self, action: str, payload: dict[str, Any]) -> Any:
        browser_id = payload.get("browser_id")
        if action == "set_viewport":
            return await self.set_viewport(
                browser_id,
                coerce_int(payload.get("width"), default=0),
                coerce_int(payload.get("height"), default=0),
            )
        if action == "select_option":
            return await self.select_option(
                browser_id,
                require_ref(payload.get("ref"), "select_option"),
                value=str(payload.get("value") or ""),
                values=payload.get("values"),
            )
        if action == "set_checked":
            return await self.set_checked(
                browser_id,
                require_ref(payload.get("ref"), "set_checked"),
                checked=True if payload.get("checked") is None else coerce_bool(payload.get("checked")),
            )
        return await self.upload_file(
            browser_id,
            require_ref(payload.get("ref"), "upload_file"),
            path=str(payload.get("path") or ""),
            paths=payload.get("paths"),
        )

    async def _dispatch_session_action(self, action: str, payload: dict[str, Any]) -> Any:
        if action in {"screenshot", "screenshot_file"}:
            return await self.screenshot_file(
                payload.get("browser_id"),
                quality=coerce_int(payload.get("quality"), default=80),
                full_page=coerce_bool(payload.get("full_page")),
                path=str(payload.get("path") or ""),
            )
        if action == "close":
            return await self.close_browser(payload.get("browser_id"))
        if action == "close_all":
            return await self.close_all_browsers()
        calls = payload.get("calls")
        if not isinstance(calls, list) or not calls:
            raise ValueError("multi requires a non-empty calls list")
        return await self.multi(calls)

    async def ensure_started(self) -> None:
        runtime = self._runtime
        if await runtime.is_started(self):
            return
        async with self._start_lock:
            runtime = self._runtime
            if await runtime.is_started(self):
                return
            if self.context is not None:
                await runtime.close_runtime(self)
            await self._start()

    async def _start(self) -> None:
        lock = profile_lock_state_for_profile(self.profile)
        if lock.locked:
            raise ProfileLockedError(
                "Chrome profile is already in use. Run /browser relaunch after closing that browser, "
                "or select a profile that is not currently open.",
                lock_state=lock,
            )

        runtime = self._runtime
        try:
            await runtime.start(self)
        except BaseException:
            with contextlib.suppress(Exception):
                await runtime.close_runtime(self)
            raise
        self.context.set_default_timeout(30000)
        self.context.set_default_navigation_timeout(30000)
        self.context.on("close", self._on_context_closed)
        self.context.on("page", self._on_new_page_sync)
        if self._dom_helper_source:
            with contextlib.suppress(Exception):
                await self.context.add_init_script(script=self._dom_helper_source)
        if self._content_helper_source:
            with contextlib.suppress(Exception):
                await self.context.add_init_script(script=self._content_helper_source)

        for page in list(getattr(self.context, "pages", []) or []):
            if getattr(page, "url", "") == "about:blank":
                continue
            await self._register_page(page)

    async def open(self, url: str = "") -> dict[str, Any]:
        await self.ensure_started()
        target_url = normalize_url(url) if str(url or "").strip() else "about:blank"
        if target_url != "about:blank":
            existing_id = await self._browser_id_for_url(target_url)
            if existing_id is not None:
                self.last_interacted_browser_id = existing_id
                with contextlib.suppress(Exception):
                    await self._page(existing_id).bring_to_front()
                return {
                    "id": existing_id,
                    "reused": True,
                    "state": await self._state(existing_id),
                }

        page = await self.context.new_page()
        browser_page = await self._register_page(page)
        self.last_interacted_browser_id = browser_page.id
        if target_url != "about:blank":
            await self._goto(page, target_url)
        else:
            await self._settle(page)
        return {"id": browser_page.id, "state": await self._state(browser_page.id)}

    async def list(self, include_content: bool = False) -> dict[str, Any]:
        await self.ensure_started()
        await self._runtime.refresh_pages(self)
        ids = sorted(self.pages)
        if not ids:
            return {"browsers": [], "last_interacted_browser_id": self.last_interacted_browser_id}
        states = await asyncio.gather(*(self._state(browser_id) for browser_id in ids))
        if include_content:
            contents = await asyncio.gather(
                *(self.content(browser_id) for browser_id in ids),
                return_exceptions=True,
            )
            for idx, content in enumerate(contents):
                if isinstance(content, Exception):
                    states[idx]["content_error"] = str(content)
                else:
                    states[idx]["content"] = content
        return {"browsers": states, "last_interacted_browser_id": self.last_interacted_browser_id}

    async def multi(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[Any, list[tuple[int, dict[str, Any]]]] = {}
        for idx, call in enumerate(calls):
            if not isinstance(call, dict):
                raise ValueError(f"calls[{idx}] is not an object")
            groups.setdefault(multi_group_key(call), []).append((idx, call))

        results: list[dict[str, Any] | None] = [None] * len(calls)

        async def run_group(group: list[tuple[int, dict[str, Any]]]) -> None:
            for idx, call in group:
                try:
                    normalized = dict(call)
                    normalized["action"] = normalize_action(normalized.get("action"))
                    out = await self.dispatch(normalized)
                    results[idx] = {"ok": True, "result": out}
                except Exception as exc:
                    results[idx] = {"ok": False, "error": str(exc)}

        await asyncio.gather(*(run_group(group) for group in groups.values()))
        return [item if item is not None else {"ok": False, "error": "missing"} for item in results]

    async def set_active(self, browser_id: int | str | None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        self.last_interacted_browser_id = resolved_id
        with contextlib.suppress(Exception):
            await self._page(resolved_id).bring_to_front()
        return await self._state(resolved_id)

    async def state(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        return await self._state(self._resolve_browser_id(browser_id))

    async def navigate(self, browser_id: int | str | None, url: str) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        await self._goto(self._page(resolved_id), normalize_url(url))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def back(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        await self._page(resolved_id).go_back(wait_until="domcontentloaded", timeout=10000)
        await self._settle(self._page(resolved_id))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def forward(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        await self._page(resolved_id).go_forward(wait_until="domcontentloaded", timeout=10000)
        await self._settle(self._page(resolved_id))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def reload(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        await self._page(resolved_id).reload(wait_until="domcontentloaded", timeout=15000)
        await self._settle(self._page(resolved_id))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def content(
        self,
        browser_id: int | str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        result = await page.evaluate(
            "(payload) => globalThis.__spaceBrowserPageContent__.capture(payload || null)",
            payload or None,
        )
        self._maybe_promote(resolved_id)
        return result or {}

    async def detail(self, browser_id: int | str | None, reference_id: int | str) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        result = await page.evaluate(
            "(ref) => globalThis.__spaceBrowserPageContent__.detail(ref)",
            reference_id,
        )
        self._maybe_promote(resolved_id)
        return result or {}

    async def evaluate(self, browser_id: int | str | None, script: str) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        result = await page.evaluate(str(script or "undefined"))
        self._maybe_promote(resolved_id)
        return {"result": result, "state": await self._state(resolved_id)}

    async def click(
        self,
        browser_id: int | str | None,
        reference_id: int | str,
        modifiers: list[str] | str | None = None,
        focus_popup: bool | None = None,
    ) -> dict[str, Any]:
        del focus_popup
        normalized_modifiers = normalize_modifiers(modifiers)
        if normalized_modifiers:
            return await self._modifier_click(browser_id, reference_id, normalized_modifiers)
        return await self._reference_action("click", browser_id, reference_id)

    async def _modifier_click(
        self,
        browser_id: int | str | None,
        reference_id: int | str,
        modifiers: list[str],
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        point = await self._point_for(page, reference_id)
        pressed: list[str] = []
        try:
            for modifier in modifiers:
                await page.keyboard.down(modifier)
                pressed.append(modifier)
            await page.mouse.click(float(point["x"]), float(point["y"]))
        finally:
            for modifier in reversed(pressed):
                with contextlib.suppress(Exception):
                    await page.keyboard.up(modifier)
        await self._settle(page)
        self._maybe_promote(resolved_id)
        return {
            "action": {"ref": reference_id, "modifiers": modifiers, "point": point},
            "state": await self._state(resolved_id),
        }

    async def type(
        self,
        browser_id: int | str | None,
        reference_id: int | str,
        text: str,
    ) -> dict[str, Any]:
        return await self._reference_action("type", browser_id, reference_id, text)

    async def submit(self, browser_id: int | str | None, reference_id: int | str) -> dict[str, Any]:
        return await self._reference_action("submit", browser_id, reference_id)

    async def type_submit(
        self,
        browser_id: int | str | None,
        reference_id: int | str,
        text: str,
    ) -> dict[str, Any]:
        return await self._reference_action("typeSubmit", browser_id, reference_id, text)

    async def scroll(self, browser_id: int | str | None, reference_id: int | str) -> dict[str, Any]:
        return await self._reference_action("scroll", browser_id, reference_id)

    async def key_chord(self, browser_id: int | str | None, keys: list[str]) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        pressed: list[str] = []
        try:
            for key in keys:
                await page.keyboard.down(str(key))
                pressed.append(str(key))
        finally:
            for key in reversed(pressed):
                with contextlib.suppress(Exception):
                    await page.keyboard.up(key)
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def hover(
        self,
        browser_id: int | str | None,
        ref: int | str | None = None,
        x: float = 0,
        y: float = 0,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        point = await self._input_point(page, ref, x=x, y=y, offset_x=offset_x, offset_y=offset_y)
        await page.mouse.move(float(point["x"]), float(point["y"]))
        self._maybe_promote(resolved_id)
        return {"action": {"point": point, "ref": ref if has_ref(ref) else None}, "state": await self._state(resolved_id)}

    async def double_click(
        self,
        browser_id: int | str | None,
        ref: int | str | None = None,
        x: float = 0,
        y: float = 0,
        button: str = "left",
        modifiers: list[str] | str | None = None,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        normalized_modifiers = normalize_modifiers(modifiers)
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        point = await self._input_point(page, ref, x=x, y=y, offset_x=offset_x, offset_y=offset_y)
        pressed: list[str] = []
        try:
            for modifier in normalized_modifiers or []:
                await page.keyboard.down(modifier)
                pressed.append(modifier)
            await page.mouse.dblclick(float(point["x"]), float(point["y"]), button=button or "left")
        finally:
            for modifier in reversed(pressed):
                with contextlib.suppress(Exception):
                    await page.keyboard.up(modifier)
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": {"button": button or "left", "modifiers": normalized_modifiers or [], "point": point, "ref": ref if has_ref(ref) else None}, "state": await self._state(resolved_id)}

    async def right_click(
        self,
        browser_id: int | str | None,
        ref: int | str | None = None,
        x: float = 0,
        y: float = 0,
        modifiers: list[str] | str | None = None,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        normalized_modifiers = normalize_modifiers(modifiers)
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        point = await self._input_point(page, ref, x=x, y=y, offset_x=offset_x, offset_y=offset_y)
        pressed: list[str] = []
        try:
            for modifier in normalized_modifiers or []:
                await page.keyboard.down(modifier)
                pressed.append(modifier)
            await page.mouse.click(float(point["x"]), float(point["y"]), button="right")
        finally:
            for modifier in reversed(pressed):
                with contextlib.suppress(Exception):
                    await page.keyboard.up(modifier)
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {
            "action": {
                "button": "right",
                "modifiers": normalized_modifiers or [],
                "point": point,
                "ref": ref if has_ref(ref) else None,
            },
            "state": await self._state(resolved_id),
        }

    async def drag(
        self,
        browser_id: int | str | None,
        ref: int | str | None = None,
        target_ref: int | str | None = None,
        x: float = 0,
        y: float = 0,
        to_x: float = 0,
        to_y: float = 0,
        offset_x: float = 0,
        offset_y: float = 0,
        target_offset_x: float = 0,
        target_offset_y: float = 0,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        start_point = await self._input_point(page, ref, x=x, y=y, offset_x=offset_x, offset_y=offset_y)
        end_point = await self._input_point(
            page,
            target_ref,
            x=to_x,
            y=to_y,
            offset_x=target_offset_x,
            offset_y=target_offset_y,
        )
        await page.mouse.move(float(start_point["x"]), float(start_point["y"]))
        await page.mouse.down()
        await page.mouse.move(float(end_point["x"]), float(end_point["y"]), steps=12)
        await page.mouse.up()
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": {"from": start_point, "to": end_point, "ref": ref if has_ref(ref) else None, "target_ref": target_ref if has_ref(target_ref) else None}, "state": await self._state(resolved_id)}

    async def wheel(
        self,
        browser_id: int | str | None,
        x: float,
        y: float,
        delta_x: float = 0,
        delta_y: float = 0,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await page.mouse.move(float(x), float(y))
        await page.mouse.wheel(float(delta_x), float(delta_y))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def mouse(
        self,
        browser_id: int | str | None,
        event_type: str,
        x: float,
        y: float,
        button: str = "left",
        modifiers: list[str] | str | None = None,
    ) -> dict[str, Any]:
        normalized_modifiers = normalize_modifiers(modifiers)
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        event_type_lower = str(event_type or "click").strip().lower()
        if event_type_lower == "move":
            await page.mouse.move(float(x), float(y))
        elif event_type_lower == "down":
            await page.mouse.down()
        elif event_type_lower == "up":
            await page.mouse.up()
        else:
            pressed: list[str] = []
            try:
                for modifier in normalized_modifiers or []:
                    await page.keyboard.down(modifier)
                    pressed.append(modifier)
                await page.mouse.click(float(x), float(y), button=button or "left")
            finally:
                for modifier in reversed(pressed):
                    with contextlib.suppress(Exception):
                        await page.keyboard.up(modifier)
            await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def keyboard(
        self,
        browser_id: int | str | None,
        *,
        key: str = "",
        text: str = "",
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        if text:
            await page.keyboard.type(str(text))
        elif key:
            await page.keyboard.press(str(key))
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def clipboard(
        self,
        browser_id: int | str | None,
        *,
        action: str,
        text: str = "",
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"copy", "cut", "paste"}:
            raise ValueError(f"Unsupported clipboard action: {normalized_action}")

        shortcut_key = "Meta" if platform.system() == "Darwin" else "Control"
        result: dict[str, Any] = {"action": normalized_action, "changed": False, "handled": True}
        if normalized_action == "paste":
            insert_text = getattr(page.keyboard, "insert_text", None)
            if callable(insert_text):
                await insert_text(str(text or ""))
            else:
                await page.keyboard.type(str(text or ""))
            result["changed"] = bool(text)
        else:
            await page.keyboard.press(f"{shortcut_key}+{'C' if normalized_action == 'copy' else 'X'}")
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"state": await self._state(resolved_id), "clipboard": result}

    async def set_viewport(self, browser_id: int | str | None, width: int, height: int) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        viewport = {
            "width": max(320, min(4096, int(width or DEFAULT_VIEWPORT["width"]))),
            "height": max(200, min(4096, int(height or DEFAULT_VIEWPORT["height"]))),
        }
        await self._page(resolved_id).set_viewport_size(viewport)
        await self._settle(self._page(resolved_id), short=True)
        self._maybe_promote(resolved_id)
        return {"state": await self._state(resolved_id), "viewport": viewport}

    async def select_option(
        self,
        browser_id: int | str | None,
        ref: int | str,
        value: str = "",
        values: list[str] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        action = await page.evaluate(
            "(args) => globalThis.__spaceBrowserPageContent__.select(args.ref, args.values)",
            {"ref": ref, "values": values if values is not None else value},
        )
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": action or {}, "state": await self._state(resolved_id)}

    async def set_checked(self, browser_id: int | str | None, ref: int | str, checked: bool = True) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        action = await page.evaluate(
            "(args) => globalThis.__spaceBrowserPageContent__.setChecked(args.ref, args.checked)",
            {"ref": ref, "checked": bool(checked)},
        )
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": action or {}, "state": await self._state(resolved_id)}

    async def upload_file(
        self,
        browser_id: int | str | None,
        ref: int | str,
        path: str = "",
        paths: list[str] | None = None,
    ) -> dict[str, Any]:
        upload_paths = normalize_upload_paths(path=path, paths=paths)
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        metadata = await page.evaluate(
            "(ref) => globalThis.__spaceBrowserPageContent__.fileInputFor(ref)",
            ref,
        )
        handle = None
        try:
            handle = await page.evaluate_handle(
                "(ref) => globalThis.__spaceBrowserPageContent__.fileInputElementFor(ref)",
                ref,
            )
            element = handle.as_element() if handle else None
            if element:
                await element.set_input_files(upload_paths)
            elif metadata and metadata.get("selector"):
                await page.set_input_files(metadata["selector"], upload_paths)
            else:
                raise ValueError(f"Browser ref {ref!r} does not resolve to a file input")
        finally:
            if handle:
                with contextlib.suppress(Exception):
                    await handle.dispose()
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": {"files": upload_paths, "input": metadata or {}, "ref": ref}, "state": await self._state(resolved_id)}

    async def screenshot_file(
        self,
        browser_id: int | str | None = None,
        *,
        quality: int = 80,
        full_page: bool = False,
        path: str = "",
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        raw_path = str(path or "").strip()
        output_path, image_type, mime = screenshot_output_path(self.context_id, resolved_id, path)
        kwargs: dict[str, Any] = {
            "type": image_type,
            "full_page": bool(full_page),
        }
        if raw_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            kwargs["path"] = str(output_path)
        if image_type == "jpeg":
            kwargs["quality"] = max(20, min(95, int(quality)))
        image = await page.screenshot(**kwargs)
        if not image and raw_path:
            image = output_path.read_bytes()
        result = {
            "browser_id": resolved_id,
            "mime": mime,
            "artifact": {
                "filename": output_path.name,
                "mime": mime,
                "encoding": "base64",
                "data": base64.b64encode(image).decode("ascii"),
            },
            "state": await self._state(resolved_id),
        }
        if raw_path:
            result["host_path"] = str(output_path)
        else:
            result["ephemeral"] = True
        return result

    async def close_browser(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await page.close()
        self.pages.pop(resolved_id, None)
        if self.last_interacted_browser_id == resolved_id:
            self.last_interacted_browser_id = next(iter(sorted(self.pages)), None)
        return await self.list()

    async def close_all_browsers(self) -> dict[str, Any]:
        await self.ensure_started()
        for browser_id in list(self.pages):
            with contextlib.suppress(Exception):
                await self.pages[browser_id].page.close()
        self.pages.clear()
        self.last_interacted_browser_id = None
        return {"browsers": [], "last_interacted_browser_id": None}

    async def close(self) -> None:
        self._closing = True
        await self._runtime.close_runtime(self)

    def _maybe_promote(self, resolved_id: int) -> None:
        current = self.last_interacted_browser_id
        if current is None or current == resolved_id:
            self.last_interacted_browser_id = int(resolved_id)

    async def _reference_action(
        self,
        helper_method: str,
        browser_id: int | str | None,
        reference_id: int | str,
        text: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        if text is None:
            action = await page.evaluate(
                "(args) => globalThis.__spaceBrowserPageContent__[args.method](args.ref)",
                {"method": helper_method, "ref": reference_id},
            )
        else:
            action = await page.evaluate(
                "(args) => globalThis.__spaceBrowserPageContent__[args.method](args.ref, args.text)",
                {"method": helper_method, "ref": reference_id, "text": text},
            )
        await self._settle(page)
        self._maybe_promote(resolved_id)
        return {"action": action or {}, "state": await self._state(resolved_id)}

    async def _point_for(
        self,
        page: Any,
        reference_id: int | str,
        *,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        await self._ensure_content_helper(page)
        point = await page.evaluate(
            "(args) => globalThis.__spaceBrowserPageContent__.pointFor(args.ref, args.offsets)",
            {
                "ref": reference_id,
                "offsets": {
                    "offset_x": float(offset_x),
                    "offset_y": float(offset_y),
                    "useOffsets": bool(offset_x or offset_y),
                },
            },
        )
        if not point or not isinstance(point, dict):
            raise ValueError(f"Could not resolve Browser ref {reference_id!r} to a viewport point")
        return point

    async def _input_point(
        self,
        page: Any,
        reference_id: int | str | None,
        *,
        x: float = 0,
        y: float = 0,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        if has_ref(reference_id):
            return await self._point_for(
                page,
                reference_id,
                offset_x=offset_x,
                offset_y=offset_y,
            )
        return {"x": float(x), "y": float(y), "rect": None, "selector": None}

    async def _goto(self, page: Any, url: str) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            raise RuntimeError(f"Browser navigation failed for {url!r}: {exc}") from exc
        await self._settle(page)

    async def _settle(self, page: Any, short: bool = False) -> None:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=1000 if short else 5000)
        await asyncio.sleep(0.1 if short else 0.35)

    async def _state(self, browser_id: int) -> dict[str, Any]:
        browser_page = self.pages.get(int(browser_id))
        if not browser_page:
            raise KeyError(f"Browser {browser_id} is not open.")
        page = browser_page.page
        current_url = await self._runtime.current_url(page)
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            history_length = await page.evaluate("() => globalThis.history?.length || 0")
        except Exception:
            history_length = 0
        return {
            "id": browser_page.id,
            "context_id": self.context_id,
            "currentUrl": current_url,
            "title": title,
            "canGoBack": bool(history_length and int(history_length) > 1),
            "canGoForward": False,
            "loading": False,
            "runtime": "host",
        }

    async def _register_page(self, page: Any) -> HostBrowserPage:
        async with self._registry_lock:
            existing = self._browser_id_for_page(page)
            if existing is not None:
                return self.pages[existing]
            browser_id = self.next_browser_id
            self.next_browser_id += 1
            browser_page = HostBrowserPage(id=browser_id, page=page)
            self.pages[browser_id] = browser_page

            def on_close() -> None:
                try:
                    asyncio.create_task(self._unregister_page_async(browser_id))
                except RuntimeError:
                    self.pages.pop(browser_id, None)

            with contextlib.suppress(Exception):
                page.on("close", on_close)
            return browser_page

    async def _unregister_page_async(self, browser_id: int) -> None:
        async with self._registry_lock:
            self.pages.pop(browser_id, None)
            if self.last_interacted_browser_id == browser_id:
                self.last_interacted_browser_id = next(iter(sorted(self.pages)), None)

    def _on_new_page_sync(self, page: Any) -> None:
        if self._closing:
            return
        with contextlib.suppress(RuntimeError):
            asyncio.create_task(self._on_new_page_async(page))

    async def _on_new_page_async(self, page: Any) -> None:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=2000)
        if self._closing:
            return
        browser_page = await self._register_page(page)
        if self.last_interacted_browser_id is None:
            self.last_interacted_browser_id = browser_page.id

    def _on_context_closed(self) -> None:
        if self._closing:
            return
        self.context = None
        self.pages.clear()
        self.last_interacted_browser_id = None

    def _browser_id_for_page(self, page: Any) -> int | None:
        for browser_id, browser_page in self.pages.items():
            if browser_page.page == page:
                return browser_id
        return None

    async def _browser_id_for_url(self, url: str) -> int | None:
        await self._runtime.refresh_pages(self)
        target = _canonical_compare_url(url)
        if not target:
            return None
        for browser_id in sorted(self.pages):
            current_url = await self._runtime.current_url(self.pages[browser_id].page)
            if _canonical_compare_url(current_url) == target:
                return browser_id
        return None

    def _resolve_browser_id(self, browser_id: int | str | None = None) -> int:
        if browser_id is None or str(browser_id).strip() == "":
            if self.last_interacted_browser_id in self.pages:
                return int(self.last_interacted_browser_id)
            if self.pages:
                return sorted(self.pages)[0]
            raise KeyError("No browser is open. Use action=open first.")
        value = str(browser_id).strip()
        if value.startswith("browser-"):
            value = value.split("-", 1)[1]
        resolved = int(value)
        if resolved not in self.pages:
            raise KeyError(f"Browser {resolved} is not open.")
        return resolved

    def _page(self, browser_id: int) -> Any:
        return self.pages[int(browser_id)].page

    async def _ensure_content_helper(self, page: Any) -> None:
        await self._ensure_dom_helper(page)
        has_helper = await page.evaluate(
            "() => Boolean(globalThis.__spaceBrowserPageContent__?.ready?.())"
        )
        if has_helper:
            return
        if self._content_helper_source is None:
            raise RuntimeError(
                "Host browser content helper source was not provided by Agent Zero Browser plugin."
            )
        await page.evaluate(self._content_helper_source)

    async def _ensure_dom_helper(self, page: Any) -> None:
        if self._dom_helper_source is None:
            return
        await self._ensure_helper_source(
            page,
            self._dom_helper_source,
            "() => Boolean(globalThis.__spaceBrowserDomHelper__?.captureDocument)",
        )

    async def _ensure_helper_source(self, page: Any, source: str, ready_script: str) -> None:
        targets = [page]
        frames = getattr(page, "frames", None)
        if isinstance(frames, list) and frames:
            targets = frames
        for target in targets:
            try:
                has_helper = await target.evaluate(ready_script)
            except Exception:
                continue
            if has_helper:
                continue
            with contextlib.suppress(Exception):
                await target.evaluate(source)

class ProfileLockedError(RuntimeError):
    def __init__(self, message: str, *, lock_state: ProfileLockState):
        super().__init__(message)
        self.lock_state = lock_state


def _canonical_compare_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw == "about:blank":
        return raw
    normalized = normalize_url(raw)
    parsed = urlsplit(normalized)
    if not parsed.scheme or not parsed.netloc:
        return normalized.rstrip("/")
    path = parsed.path or ""
    if path == "/":
        path = ""
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path.rstrip("/"),
            parsed.query,
            parsed.fragment,
        )
    )
