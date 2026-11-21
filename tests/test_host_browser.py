from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from agent_zero_cli.remote_files import RemoteFileUtility

import pytest

import agent_zero_cli.host_browser_common as host_browser_common
import agent_zero_cli.host_browser_manager as host_browser_manager_module
import agent_zero_cli.host_browser_safari as host_browser_safari
import agent_zero_cli.host_browser_session as host_browser_session_module
from agent_zero_cli.config import CLIConfig
from agent_zero_cli.host_browser import (
    BrowserCandidate,
    BrowserProfile,
    CONTENT_HELPER_PATH,
    HostBrowserManager,
    HostBrowserSession,
    ProfileLockState,
    RELAUNCH_CONTEXT_ID,
    SafariContext,
    a0_managed_user_data_dir,
    chromium_launch_args,
    content_helper_sha256,
    remote_debugging_endpoint_from_active_port_file,
    discover_remote_debugging_profiles,
    discover_profiles,
    is_profile_locked,
    normalize_url,
    normalize_remote_debugging_endpoint,
    parse_content_helper_payload,
    parse_dom_helper_payload,
    profile_lock_state,
    remote_debugging_restriction_reason,
)


pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("args", [{"expression": "document.title"}, {}, {"script": " "}, {"script": 42}])
async def test_evaluate_rejects_missing_or_invalid_script(monkeypatch, args):
    session = HostBrowserSession(context_id="ctx-evaluate", profile=None)
    started = AsyncMock()
    monkeypatch.setattr(session, "ensure_started", started)
    with pytest.raises(ValueError, match="non-empty 'script'"):
        await session.dispatch({"action": "evaluate", **args})
    result = await session.dispatch({"action": "multi", "calls": [{"action": "evaluate", **args}]})
    assert result == [{"ok": False, "error": "evaluate requires a non-empty 'script' string"}]
    started.assert_not_awaited()


@pytest.mark.parametrize("value", [None, False, 0, {"answer": 42}])
async def test_evaluate_preserves_javascript_results(monkeypatch, value):
    session = HostBrowserSession(context_id="ctx-evaluate", profile=None)
    page = SimpleNamespace(
        evaluate=AsyncMock(return_value=value), is_closed=lambda: False,
    )
    session.pages[3] = host_browser_session_module.HostBrowserPage(3, page)
    monkeypatch.setattr(session, "ensure_started", AsyncMock())
    monkeypatch.setattr(session, "_resolve_browser_id", lambda _: 3)
    monkeypatch.setattr(session, "_page", lambda _: page)
    monkeypatch.setattr(session, "_state", AsyncMock(return_value={"id": 3}))
    script = "() => globalThis.probeValue"
    result = await session.evaluate(3, script)
    assert result == {"result": value, "state": {"id": 3}}
    page.evaluate.assert_awaited_once_with(script, timeout=45)


@pytest.fixture(autouse=True)
def _linux_host_browser_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_browser_common.platform, "system", lambda: "Linux")

MINIMAL_CONTENT_HELPER_SOURCE = """
(() => {
  globalThis.__spaceBrowserPageContent__ = {
    annotate() {},
    boundingBoxFor() {},
    capture() {},
    detail() {},
    fileInputElementFor() {},
    fileInputFor() {},
    pointFor() {},
    ready() { return true; },
    select() {},
    setChecked() {},
    requiredApis: [],
  };
})();
"""

MINIMAL_DOM_HELPER_SOURCE = """
(() => {
  globalThis.__spaceBrowserDomHelper__ = {
    captureDocument() {},
    clickNode() {},
    detailNode() {},
    scrollNode() {},
    submitNode() {},
    typeNode() {},
    typeSubmitNode() {},
    version: "test",
  };
})();
"""


class FakeKeyboard:
    async def down(self, key: str) -> None:
        del key

    async def up(self, key: str) -> None:
        del key

    async def press(self, key: str) -> None:
        del key

    async def type(self, text: str) -> None:
        del text

    async def insert_text(self, text: str) -> None:
        del text


class FakeMouse:
    async def click(self, x: float, y: float, button: str = "left") -> None:
        del x, y, button

    async def dblclick(self, x: float, y: float, button: str = "left") -> None:
        del x, y, button

    async def move(self, x: float, y: float, steps: int | None = None) -> None:
        del x, y, steps

    async def down(self) -> None:
        return None

    async def up(self) -> None:
        return None

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        del delta_x, delta_y


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()
        self.viewport_size = {"width": 1280, "height": 800}
        self.handlers = {}
        self.closed = False

    def on(self, event: str, callback) -> None:
        self.handlers[event] = callback

    async def goto(self, url: str, **_: object) -> None:
        self.url = url

    async def wait_for_load_state(self, *_: object, **__: object) -> None:
        return None

    async def title(self) -> str:
        return "Example"

    async def evaluate(self, script: str, arg: object = None) -> object:
        del arg
        if "history" in script:
            return 1
        if "__spaceBrowserPageContent__" in script and "Boolean" in script:
            return True
        return {"ok": True}

    async def screenshot(self, **kwargs: object) -> bytes:
        payload = b"fake-jpeg"
        path = kwargs.get("path")
        if path:
            Path(str(path)).write_bytes(payload)
        return payload

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed

    async def set_viewport_size(self, viewport: dict[str, int]) -> None:
        self.viewport_size = dict(viewport)


class FakeContext:
    is_connected = True

    async def discover_pages(self):
        return list(self.pages)

    def __init__(self) -> None:
        self.pages = []
        self.handlers = {}
        self.closed = False
        self.init_scripts: list[str] = []

    def on(self, event: str, callback) -> None:
        self.handlers[event] = callback

    async def add_init_script(self, script: object = None, *, path: object = None) -> None:
        if script is not None:
            self.init_scripts.append(str(script))
        elif path is not None:
            self.init_scripts.append(Path(str(path)).read_text(encoding="utf-8"))
        return None

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True
        return None


@pytest.fixture
def browser_context(monkeypatch):
    context = FakeContext()

    async def launch(adapter, session):
        session.browser = session.context = context

    monkeypatch.setattr(host_browser_session_module._CDPRuntimeAdapter, "_launch", launch)
    return context


def test_discover_profiles_reads_local_state_names(tmp_path: Path) -> None:
    root = tmp_path / "ChromeData"
    (root / "Default").mkdir(parents=True)
    (root / "Profile 1").mkdir()
    (root / "Local State").write_text(
        '{"profile":{"info_cache":{"Default":{"name":"Personal"},"Profile 1":{"name":"Work"}}}}',
        encoding="utf-8",
    )

    profiles = discover_profiles(BrowserCandidate("chrome", "Google Chrome", "/bin/echo", root))

    assert [(item.profile_label, item.display_name) for item in profiles] == [
        ("Default", "Personal"),
        ("Profile 1", "Work"),
    ]


def test_discover_profiles_exposes_a0_managed_profile_without_existing_root(tmp_path: Path) -> None:
    root = tmp_path / "a0-chrome"

    profiles = discover_profiles(
        BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", "/bin/echo", root)
    )

    assert len(profiles) == 1
    assert profiles[0].family == "chrome-a0"
    assert profiles[0].profile_label == "Default"
    assert profiles[0].profile_path == root


def test_a0_managed_user_data_dir_is_separate_from_default_chrome_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    path = a0_managed_user_data_dir("chrome")

    assert path == tmp_path / "data" / "a0/browser-profiles/chrome"
    assert path != tmp_path / "config" / "google-chrome"


def test_safari_profile_is_selectable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Safari"
    driver = tmp_path / "safaridriver"
    executable.touch()
    driver.touch()
    monkeypatch.setattr(host_browser_manager_module.sys, "platform", "darwin")
    monkeypatch.setattr(host_browser_manager_module, "SAFARI_DRIVER_PATH", driver)
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [
            BrowserCandidate("safari", "Safari", str(executable), Path())
        ],
    )

    metadata = manager.hello_metadata(
        profile_mode="existing",
        browser_selection="safari",
    )

    assert metadata["supported"] is True
    assert metadata["can_repair"] is False
    assert metadata["browser_id"] == "safari:default"
    assert metadata["browser_label"] == "Safari - Automation window"
    assert metadata["profile_path"] == str(executable)
    assert "safari_webdriver" in metadata["features"]


class FakeSafariProtocol:
    def __init__(self) -> None:
        self.current_handle = ""
        self.calls: list[tuple[str, str, dict | None]] = []
        self.url = "about:blank"

    async def session_request(
        self,
        method: str,
        path: str = "",
        payload: dict | None = None,
    ) -> object:
        self.calls.append((method, path, payload))
        if (method, path) == ("GET", "window/handles"):
            return ["tab-1"]
        if (method, path) == ("POST", "window"):
            self.current_handle = str((payload or {}).get("handle") or "")
            return None
        if (method, path) == ("POST", "url"):
            self.url = str((payload or {}).get("url") or "")
            return None
        if (method, path) == ("GET", "url"):
            return self.url
        if (method, path) == ("GET", "title"):
            return "Safari example"
        if (method, path) == ("POST", "execute/sync"):
            args = (payload or {}).get("args") or []
            return args[0] if args else "complete"
        if (method, path) == ("GET", "screenshot"):
            return base64.b64encode(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
            ).decode("ascii")
        return None


async def test_safari_page_maps_existing_browser_operations_to_webdriver() -> None:
    driver = FakeSafariProtocol()
    context = SafariContext(driver)  # type: ignore[arg-type]
    await context.discover_pages(notify=False)
    page = await context.new_page()

    await page.goto("https://example.com/")
    evaluated = await page.evaluate("(value) => value", {"answer": 42})
    await page.evaluate(MINIMAL_CONTENT_HELPER_SOURCE)
    await page.mouse.click(10, 20)
    await page.keyboard.press("Meta+Enter")
    screenshot = await page.screenshot(type="jpeg", quality=75)

    assert evaluated == {"answer": 42}
    assert screenshot.startswith(b"\xff\xd8")
    execute = [call for call in driver.calls if call[:2] == ("POST", "execute/sync")]
    assert execute[0][2] == {
        "script": "return ((value) => value)(arguments[0]);",
        "args": [{"answer": 42}],
    }
    assert execute[1][2] == {"script": MINIMAL_CONTENT_HELPER_SOURCE.strip(), "args": []}
    actions = [call for call in driver.calls if call[:2] == ("POST", "actions")]
    assert any(call[2]["actions"][0]["type"] == "pointer" for call in actions)
    assert any(call[2]["actions"][0]["type"] == "key" for call in actions)
    with pytest.raises(RuntimeError, match="viewport screenshots"):
        await page.screenshot(full_page=True)


async def test_safari_session_restarts_after_driver_or_session_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_session as host_browser_session_module

    instances = []

    class FakeProcess:
        returncode: int | None = None

    class FakeSafariDriver:
        def __init__(self) -> None:
            self.process = FakeProcess()
            self.context = FakeContext()
            self.closed = False
            self.session_active = True
            instances.append(self)

        async def start(self) -> FakeContext:
            return self.context

        async def session_request(self, *_: object) -> list[str]:
            if not self.session_active:
                raise host_browser_safari.SafariDriverError("invalid session id")
            return []

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(host_browser_session_module, "SafariDriver", FakeSafariDriver)
    session = HostBrowserSession(
        context_id="ctx-safari-recovery",
        profile=BrowserProfile(
            "safari",
            "Safari",
            "/Applications/Safari.app",
            Path(),
            "Default",
            "Automation window",
        ),
    )

    await session.ensure_started()
    instances[0].process.returncode = -15
    await session.ensure_started()
    instances[1].session_active = False
    await session.ensure_started()

    assert len(instances) == 3
    assert all(instance.closed for instance in instances[:2])
    assert session.browser is instances[2]
    assert session.context is instances[2].context
    await session.close()


async def test_safari_manager_releases_stale_session_for_new_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_session as host_browser_session_module

    instances = []

    class FakeProcess:
        returncode: int | None = None

    class FakeSafariContext(FakeContext):
        async def discover_pages(self) -> list[FakePage]:
            return []

    class FakeSafariDriver:
        def __init__(self) -> None:
            self.process = FakeProcess()
            self.context = FakeSafariContext()
            self.closed = False
            self.session_active = True
            instances.append(self)

        async def start(self) -> FakeSafariContext:
            return self.context

        async def session_request(self, *_: object) -> list[str]:
            if not self.session_active:
                raise host_browser_safari.SafariDriverError("invalid session id")
            return []

        async def close(self) -> None:
            self.closed = True

    executable = tmp_path / "Safari"
    driver = tmp_path / "safaridriver"
    executable.touch()
    driver.touch()
    monkeypatch.setattr(host_browser_manager_module.sys, "platform", "darwin")
    monkeypatch.setattr(host_browser_manager_module, "SAFARI_DRIVER_PATH", driver)
    monkeypatch.setattr(host_browser_session_module, "SafariDriver", FakeSafariDriver)
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [
            BrowserCandidate("safari", "Safari", str(executable), Path())
        ],
    )

    first = await manager.handle_op(
        {
            "op_id": "op-safari-first",
            "context_id": "ctx-safari-first",
            "action": "list",
            "browser_selection": "safari",
        }
    )
    instances[0].session_active = False
    second = await manager.handle_op(
        {
            "op_id": "op-safari-second",
            "context_id": "ctx-safari-second",
            "action": "list",
            "browser_selection": "safari",
        }
    )
    third = await manager.handle_op(
        {
            "op_id": "op-safari-third",
            "context_id": "ctx-safari-third",
            "action": "list",
            "browser_selection": "safari",
        }
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert third["code"] == "HOST_BROWSER_CONTEXT_ACTIVE"
    assert instances[0].closed is True
    assert len(instances) == 2
    assert set(manager._sessions) == {"ctx-safari-second"}
    await manager.close()


def test_safari_permission_error_names_the_current_settings_path() -> None:
    message = host_browser_safari._friendly_driver_error(
        "Remote automation is not enabled."
    )

    assert "Safari > Settings > Advanced" in message
    assert "Show features for web developers" in message
    assert "Developer" in message
    assert "Allow remote automation" in message


def test_linux_candidate_detection_includes_major_chromium_browsers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    executables = {
        "google-chrome": "/bin/google-chrome",
        "brave-browser": "/bin/brave-browser",
        "opera": "/bin/opera",
        "vivaldi": "/bin/vivaldi",
    }
    monkeypatch.setattr(host_browser_common.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common.shutil, "which", lambda name: executables.get(name))

    candidates = {candidate.family: candidate for candidate in host_browser_common.detect_browser_candidates()}

    assert candidates["chrome"].user_data_dir == config_root / "google-chrome"
    assert candidates["brave"].user_data_dir == config_root / "BraveSoftware/Brave-Browser"
    assert candidates["opera"].user_data_dir == config_root / "opera"
    assert candidates["vivaldi"].user_data_dir == config_root / "vivaldi"
    assert {"chrome-a0", "brave-a0", "opera-a0", "vivaldi-a0"} <= set(candidates)


def test_content_helper_source_is_owned_by_agent_zero_browser_plugin() -> None:
    assert not CONTENT_HELPER_PATH.exists()

    agent_zero_asset = (
        Path(__file__).resolve().parents[2]
        / "agent-zero"
        / "plugins"
        / "_browser"
        / "assets"
        / "browser-page-content.js"
    )
    if not agent_zero_asset.exists():
        pytest.skip("Agent Zero sibling repo is not available for content-helper contract")

    agent_zero_source = agent_zero_asset.read_text(encoding="utf-8")
    agent_zero_hash = content_helper_sha256(agent_zero_source)
    required_apis = [
        "annotate",
        "boundingBoxFor",
        "capture",
        "click",
        "detail",
        "fileInputElementFor",
        "fileInputFor",
        "pointFor",
        "scroll",
        "select",
        "setChecked",
        "submit",
        "type",
        "typeSubmit",
    ]

    assert parse_content_helper_payload(
        {
            "content_helper": {
                "required_apis": required_apis,
                "source": agent_zero_source,
                "sha256": agent_zero_hash,
            }
        }
    ) == (agent_zero_source, agent_zero_hash)
    assert "REQUIRED_API_NAMES" in agent_zero_source
    assert "ready()" in agent_zero_source
    for api_name in required_apis:
        assert api_name in agent_zero_source

    dom_helper_asset = agent_zero_asset.with_name("browser-dom-helper.js")
    assert dom_helper_asset.exists()
    dom_helper_source = dom_helper_asset.read_text(encoding="utf-8")
    dom_helper_hash = content_helper_sha256(dom_helper_source)
    dom_required_apis = [
        "captureDocument",
        "clickNode",
        "detailNode",
        "scrollNode",
        "submitNode",
        "typeNode",
        "typeSubmitNode",
    ]
    assert parse_dom_helper_payload(
        {
            "dom_helper": {
                "required_apis": dom_required_apis,
                "source": dom_helper_source,
                "sha256": dom_helper_hash,
            }
        }
    ) == (dom_helper_source, dom_helper_hash)
    assert "__spaceBrowserDomHelper__" in dom_helper_source
    assert "requestChildFrameOperation" in dom_helper_source
    assert "data-space-browser-frame-chain" in dom_helper_source
    for api_name in dom_required_apis:
        assert api_name in dom_helper_source


def test_host_browser_accepts_only_agent_zero_normalized_urls() -> None:
    assert normalize_url("https://example.com/") == "https://example.com/"
    assert normalize_url("about:blank") == "about:blank"
    with pytest.raises(ValueError, match="Agent Zero-normalized"):
        normalize_url("example.com")


def test_remote_debugging_profile_is_discovered_when_chrome_allows_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "google-chrome"
    root.mkdir()
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")

    profiles = discover_remote_debugging_profiles(
        [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)]
    )

    assert (
        remote_debugging_endpoint_from_active_port_file(root / "DevToolsActivePort")
        == "ws://localhost:9222/devtools/browser/test"
    )
    assert (
        normalize_remote_debugging_endpoint("ws://127.0.0.1:9222/devtools/browser/test")
        == "ws://127.0.0.1:9222/devtools/browser/test"
    )
    assert normalize_remote_debugging_endpoint("localhost:9222") == "http://localhost:9222"
    assert normalize_remote_debugging_endpoint("ws://localhost:9222") == "http://localhost:9222"
    assert (
        normalize_remote_debugging_endpoint("ws://localhost:9222/devtools/Browser/AbC?token=XyZ")
        == "ws://localhost:9222/devtools/Browser/AbC?token=XyZ"
    )
    assert len(profiles) == 1
    assert profiles[0].family == "chrome-cdp"
    assert profiles[0].browser_id == "chrome-cdp"
    assert profiles[0].profile_label == "localhost:9222"
    assert profiles[0].cdp_endpoint == "ws://localhost:9222/devtools/browser/test"
    assert profiles[0].as_dict()["locked"] is False


def test_remote_debugging_profile_is_discovered_when_opera_allows_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "opera"
    root.mkdir()
    (root / "DevToolsActivePort").write_text("34535\n/devtools/browser/test\n", encoding="utf-8")

    profiles = discover_remote_debugging_profiles(
        [BrowserCandidate("opera", "Opera", "/bin/opera", root)]
    )

    assert len(profiles) == 1
    assert profiles[0].family == "opera-cdp"
    assert profiles[0].family_label == "Opera (remote debugging)"
    assert profiles[0].cdp_endpoint == "ws://localhost:34535/devtools/browser/test"


def test_remote_debugging_discovery_reads_active_port_without_network_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_cdp as host_browser_cdp_module

    root = tmp_path / "google-chrome"
    root.mkdir()
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")

    def fail_client_session(*_: object, **__: object) -> object:
        raise AssertionError("remote debugging discovery must not open network connections")

    monkeypatch.setattr(host_browser_cdp_module.aiohttp, "ClientSession", fail_client_session)

    profiles = discover_remote_debugging_profiles(
        [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)]
    )

    assert [profile.cdp_endpoint for profile in profiles] == [
        "ws://localhost:9222/devtools/browser/test"
    ]


async def test_cdp_connection_resolves_discovery_address(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_zero_cli.host_browser_cdp as host_browser_cdp_module

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def json(self) -> dict[str, str]:
            return {
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/Browser/OperaAbC"
            }

    class FakeWebSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self) -> None:
            return None

    class FakeSession:
        def __init__(self) -> None:
            self.version_url = ""
            self.websocket_url = ""

        def get(self, url: str) -> FakeResponse:
            self.version_url = url
            return FakeResponse()

        async def ws_connect(self, url: str, **_: object) -> FakeWebSocket:
            self.websocket_url = url
            return FakeWebSocket()

        async def close(self) -> None:
            return None

    session = FakeSession()
    monkeypatch.setattr(
        host_browser_cdp_module.aiohttp,
        "ClientSession",
        lambda **_: session,
    )
    connection = host_browser_cdp_module.CDPConnection("http://localhost:9222")

    await connection.connect()
    await connection.close()

    assert session.version_url == "http://localhost:9222/json/version"
    assert session.websocket_url == "ws://localhost:9222/devtools/Browser/OperaAbC"


def test_selected_profile_prefers_user_allowed_remote_debugging_over_a0_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_manager as host_browser_manager_module

    a0_root = tmp_path / "a0-chrome"
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    remote_profile = BrowserProfile(
        "chrome-cdp",
        "Chrome (remote debugging)",
        "",
        Path(),
        "127.0.0.1:9222",
        "Remote debugging allowed",
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/test",
    )
    monkeypatch.setattr(host_browser_manager_module, "discover_remote_debugging_profiles", lambda *_: [remote_profile])
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(a0_root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), a0_root)
        ],
    )

    selected = manager.selected_profile(profile_mode="existing")

    assert selected is not None
    assert selected.family == "chrome-cdp"


def test_remote_debugging_profile_is_supported(tmp_path: Path) -> None:
    root = tmp_path / "google-chrome"
    root.mkdir()
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)],
    )

    metadata = manager.hello_metadata()

    assert metadata["supported"] is True
    assert metadata["browser_family"] == "chrome-cdp"
    assert metadata["cdp_endpoint"] == "ws://localhost:9222/devtools/browser/test"


def test_saved_default_profile_uses_authorized_remote_debugging(tmp_path: Path) -> None:
    root = tmp_path / "google-chrome"
    (root / "Default").mkdir(parents=True)
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_family="chrome",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)],
    )

    selected = manager.selected_profile(profile_mode="existing")

    assert selected is not None
    assert selected.family == "chrome-cdp"
    assert selected.cdp_endpoint == "ws://localhost:9222/devtools/browser/test"


def test_hello_metadata_advertises_host_browser_inventory(tmp_path: Path) -> None:
    root = tmp_path / "google-chrome"
    (root / "Default").mkdir(parents=True)
    (root / "Profile 1").mkdir()
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)],
    )

    metadata = manager.hello_metadata()
    advertised = {item["id"]: item for item in metadata["available_browsers"]}

    assert metadata["browser_id"] == "chrome-cdp"
    assert metadata["browser_label"] == "Google Chrome (remote debugging) - Remote debugging allowed"
    assert advertised["chrome-cdp"]["cdp_endpoint"] == "ws://localhost:9222/devtools/browser/test"
    assert advertised["chrome:default"]["family"] == "chrome"
    assert advertised["chrome:profile_1"]["label"] == "Google Chrome - Profile 1"


def test_discovered_remote_debugging_id_resolves_latest_endpoint(tmp_path: Path) -> None:
    root = tmp_path / "google-chrome"
    root.mkdir()
    active_port = root / "DevToolsActivePort"
    active_port.write_text("9222\n/devtools/browser/old\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)],
    )

    selection = manager.hello_metadata()["browser_id"]
    active_port.write_text("9333\n/devtools/browser/new\n", encoding="utf-8")
    selected = manager.selected_profile(
        profile_mode="existing",
        browser_selection=selection,
    )

    assert selection == "chrome-cdp"
    assert selected is not None
    assert selected.cdp_endpoint == "ws://localhost:9333/devtools/browser/new"


def test_browser_selection_accepts_family_id(tmp_path: Path) -> None:
    root = tmp_path / "google-chrome"
    (root / "Default").mkdir(parents=True)
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)],
    )

    selected = manager.selected_profile(profile_mode="existing", browser_selection="chrome")

    assert selected is not None
    assert selected.family == "chrome-cdp"
    assert selected.cdp_endpoint == "ws://localhost:9222/devtools/browser/test"


async def test_browser_selection_uses_advertised_profile_id(tmp_path: Path, browser_context) -> None:
    root = tmp_path / "google-chrome"
    (root / "Default").mkdir(parents=True)
    (root / "Profile 1").mkdir()
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", str(executable), root)],
    )

    result = await manager.handle_op(
        {
            "op_id": "op-open",
            "context_id": "ctx-1",
            "action": "open",
            "url": "https://example.com/",
            "browser_selection": "chrome:profile_1",
        }
    )

    assert result["ok"] is True
    assert manager._sessions["ctx-1"].profile.profile_label == "Profile 1"


def test_browser_selection_accepts_explicit_cdp_endpoint() -> None:
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [],
    )

    selected = manager.selected_profile(
        profile_mode="existing",
        browser_selection="ws://127.0.0.1:9333/devtools/browser/test",
    )

    assert selected is not None
    assert selected.family == "chrome-cdp"
    assert selected.browser_id == "ws://127.0.0.1:9333/devtools/browser/test"
    assert selected.cdp_endpoint == "ws://127.0.0.1:9333/devtools/browser/test"
    assert manager.status_snapshot(profile=selected)["supported"] is True


async def test_browser_prepare_waits_for_remote_debugging_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "google-chrome"
    (root / "Default").mkdir(parents=True)
    active_port = root / "DevToolsActivePort"
    waits = []

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)
        active_port.write_text("9222\n/devtools/browser/ready\n", encoding="utf-8")

    async def fake_ensure_started(session: HostBrowserSession) -> None:
        session.context = object()

    monkeypatch.setattr(host_browser_manager_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        host_browser_manager_module,
        "remote_debugging_restriction_reason",
        lambda profile: "waiting for remote debugging" if not profile.is_remote_debugging else "",
    )
    monkeypatch.setattr(HostBrowserSession, "ensure_started", fake_ensure_started)
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)],
    )

    result = await manager.ensure_available(profile_mode="existing")

    assert waits == [0.25]
    assert result["browser_id"] == "chrome-cdp"
    assert result["cdp_endpoint"] == "ws://localhost:9222/devtools/browser/ready"


def test_browser_selection_accepts_cdp_discovery_address() -> None:
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [],
    )

    selected = manager.selected_profile(
        profile_mode="existing",
        browser_selection="localhost:9333",
    )

    assert selected is not None
    assert selected.cdp_endpoint == "http://localhost:9333"


async def test_unknown_browser_selection_does_not_fall_back_to_first_profile(tmp_path: Path, browser_context) -> None:
    root = tmp_path / "google-chrome"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", str(executable), root)],
    )

    result = await manager.handle_op(
        {
            "op_id": "op-ensure",
            "context_id": "ctx-1",
            "action": "ensure",
            "profile_mode": "existing",
            "browser_selection": "edge",
        }
    )

    assert result["ok"] is False
    assert "selection 'edge'" in result["error"]
    assert manager._sessions == {}


def test_profile_lock_detection_reports_singleton_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    lock = tmp_path / "SingletonLock"
    try:
        os.symlink("host-12345", lock)
    except (PermissionError, OSError, NotImplementedError):
        pytest.skip("symlink not supported in this environment")
    monkeypatch.setattr(host_browser_common_module, "_pid_is_alive", lambda pid: pid == 12345)

    state = profile_lock_state(tmp_path)

    assert is_profile_locked(tmp_path) is True
    assert state.locked is True
    assert state.owner_pid == 12345
    assert str(lock) in state.lock_files


def test_profile_lock_detection_ignores_stale_singleton_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    lock = tmp_path / "SingletonLock"
    try:
        os.symlink("host-12345", lock)
    except (PermissionError, OSError, NotImplementedError):
        pytest.skip("symlink not supported in this environment")
    (tmp_path / "SingletonCookie").symlink_to("cookie")
    monkeypatch.setattr(host_browser_common_module, "_pid_is_alive", lambda pid: False)

    state = profile_lock_state(tmp_path)

    assert is_profile_locked(tmp_path) is False
    assert state.locked is False
    assert state.owner_pid == 12345
    assert state.lock_files == ()


def test_chromium_launch_args_do_not_request_a_devtools_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A0_HOST_BROWSER_OZONE_PLATFORM", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    args = chromium_launch_args("Default")

    assert args == ["--profile-directory=Default"]
    assert not any(arg.startswith("--remote-debugging-port=") for arg in args)


def test_chromium_launch_args_use_wayland_only_without_x_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A0_HOST_BROWSER_OZONE_PLATFORM", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    args = chromium_launch_args("Default")

    assert "--ozone-platform=wayland" in args


def test_windows_browser_version_uses_file_metadata_without_launching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_browser_common.browser_major_version.cache_clear()
    monkeypatch.setattr(host_browser_common.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        host_browser_common.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Windows browser version lookup launched the browser"),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        SimpleNamespace(GetFileVersionInfo=lambda _path, _query: {"FileVersionMS": 152 << 16}),
    )

    try:
        assert host_browser_common.browser_major_version("C:/Program Files/Browser/browser.exe") == 152
    finally:
        host_browser_common.browser_major_version.cache_clear()


def test_remote_debugging_restriction_blocks_default_chrome_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    profile = BrowserProfile("chrome", "Chrome", "/bin/chrome", default_root, "Default", "Default")

    reason = remote_debugging_restriction_reason(profile)

    assert "blocks command-line remote debugging" in reason
    assert "chrome://inspect/#remote-debugging" in reason
    assert "Allow remote debugging for this browser instance" in reason
    assert "/browser profile chrome-a0 Default" in reason


def test_remote_debugging_restriction_allows_a0_managed_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    profile = BrowserProfile("chrome-a0", "Chrome", "/bin/chrome", tmp_path, "Default", "Default")

    assert remote_debugging_restriction_reason(profile) == ""


def test_selected_profile_keeps_existing_profile_as_default_when_restricted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    (default_root / "Default").mkdir(parents=True)
    a0_root = tmp_path / "a0-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    manager = HostBrowserManager(
        CLIConfig(),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(executable), default_root),
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), a0_root),
        ],
    )

    selected = manager.selected_profile(profile_mode="existing")

    assert selected is not None
    assert selected.family == "chrome"
    assert "A0-controlled local profile" in manager._profile_support_reason(selected)


def test_agent_profile_mode_selects_supported_a0_profile_when_default_is_restricted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    (default_root / "Default").mkdir(parents=True)
    a0_root = tmp_path / "a0-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    manager = HostBrowserManager(
        CLIConfig(),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(executable), default_root),
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), a0_root),
        ],
    )

    selected = manager.selected_profile(profile_mode="agent")

    assert selected is not None
    assert selected.family == "chrome-a0"


def test_hello_metadata_advertises_native_profile_support(tmp_path: Path) -> None:
    root = tmp_path / "ChromeData"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=False,
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), root)
        ],
    )

    metadata = manager.hello_metadata()

    assert metadata["supported"] is True
    assert metadata["can_prepare"] is True
    assert metadata["can_repair"] is False
    assert {"cdp", "browser_file_scopes_v1"} <= set(metadata["features"])


def test_hello_metadata_marks_restricted_saved_profile_as_preparable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    (default_root / "Default").mkdir(parents=True)
    managed_root = tmp_path / "a0-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=False,
            host_browser_family="chrome",
            host_browser_profile_path=str(default_root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(executable), default_root),
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), managed_root),
        ],
    )

    metadata = manager.hello_metadata()

    assert metadata["supported"] is False
    assert metadata["can_prepare"] is True
    assert metadata["browser_family"] == "chrome"
    assert "A0-controlled local profile" in metadata["support_reason"]


async def test_host_browser_manager_dispatches_open_and_screenshot_artifact(tmp_path: Path, browser_context) -> None:
    root = tmp_path / "ChromeData"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config = CLIConfig(
        host_browser_enabled=True,
        host_browser_family="chrome",
        host_browser_profile_path=str(root),
        host_browser_profile_label="Default",
    )
    manager = HostBrowserManager(
        config,
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", str(executable), root)],
    )

    opened = await manager.handle_op(
        {"op_id": "op-open", "context_id": "ctx-1", "action": "open", "url": "https://example.com/"}
    )
    screenshot = await manager.handle_op(
        {"op_id": "op-shot", "context_id": "ctx-1", "action": "screenshot", "browser_id": 1}
    )

    assert opened["ok"] is True
    assert opened["result"]["state"]["currentUrl"] == "https://example.com/"
    assert screenshot["ok"] is True
    artifact = screenshot["result"]["artifact"]
    assert artifact["encoding"] == "base64"
    assert artifact["mime"] == "image/jpeg"
    assert artifact["data"]
    assert screenshot["result"]["ephemeral"] is True
    assert "host_path" not in screenshot["result"]


async def test_parallel_host_browser_open_reuses_startup_blank_page(tmp_path: Path, browser_context) -> None:
    initial_page = FakePage()
    browser_context.pages.append(initial_page)
    session = HostBrowserSession(
        context_id="chat-1",
        profile=BrowserProfile("chrome-a0", "Chrome", "/bin/chrome", tmp_path, "Default", "Default"),
    )
    urls = ["https://example.com/", "https://example.org/"]
    try:
        results = await asyncio.gather(*(session.open(url) for url in urls))

        assert len({result["id"] for result in results}) == 2
        assert len(browser_context.pages) == 2
        assert {page.url for page in browser_context.pages} == set(urls)
        assert session.pages[results[0]["id"]].page is initial_page
    finally:
        await session.close()


async def test_host_browser_readiness_waits_for_helper_installation(tmp_path: Path, browser_context) -> None:
    installing = asyncio.Event()
    release = asyncio.Event()

    async def install_helper(**kwargs):
        installing.set()
        await release.wait()

    browser_context.add_init_script = install_helper
    session = HostBrowserSession(
        context_id="chat-1",
        profile=BrowserProfile("chrome-a0", "Chrome", "/bin/chrome", tmp_path, "Default", "Default"),
    )
    await session.set_dom_helper_source(MINIMAL_DOM_HELPER_SOURCE)
    first = asyncio.create_task(session.ensure_started())
    second = None
    try:
        await asyncio.wait_for(installing.wait(), timeout=2)
        second = asyncio.create_task(session.ensure_started())
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []))
        await session.close()


async def test_host_browser_rejects_oversized_screenshot_before_base64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    browser_context,
) -> None:
    root = tmp_path / "ChromeData"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(host_browser_session_module, "_SCREENSHOT_ARTIFACT_MAX_BYTES", 4)
    monkeypatch.setattr(FakePage, "screenshot", lambda self, **kwargs: _oversized_screenshot(kwargs))
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=True,
            host_browser_family="chrome",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", str(executable), root)],
    )

    await manager.handle_op(
        {"op_id": "op-open", "context_id": "ctx-1", "action": "open", "url": "https://example.com/"}
    )
    result = await manager.handle_op(
        {"op_id": "op-shot", "context_id": "ctx-1", "action": "screenshot", "browser_id": 1}
    )

    assert result["ok"] is False
    assert result["code"] == "HOST_BROWSER_ERROR"
    assert "too large" in result["error"]


async def _oversized_screenshot(_kwargs: dict[str, object]) -> bytes:
    return b"12345"


async def test_host_browser_manager_uses_agent_zero_supplied_content_helper(tmp_path: Path, browser_context) -> None:
    root = tmp_path / "ChromeData"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=True,
            host_browser_family="chrome",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", str(executable), root)],
    )
    dom_helper_hash = content_helper_sha256(MINIMAL_DOM_HELPER_SOURCE)
    helper_hash = content_helper_sha256(MINIMAL_CONTENT_HELPER_SOURCE)

    opened = await manager.handle_op(
        {
            "op_id": "op-open",
            "context_id": "ctx-helper",
            "action": "open",
            "url": "https://example.com/",
            "dom_helper": {
                "source": MINIMAL_DOM_HELPER_SOURCE,
                "sha256": dom_helper_hash,
            },
            "content_helper": {
                "source": MINIMAL_CONTENT_HELPER_SOURCE,
                "sha256": helper_hash,
            },
        }
    )

    assert opened["ok"] is True
    assert manager.metadata()["dom_helper_sha256"] == dom_helper_hash
    assert manager.metadata()["content_helper_sha256"] == helper_hash
    assert MINIMAL_DOM_HELPER_SOURCE in browser_context.init_scripts
    assert MINIMAL_CONTENT_HELPER_SOURCE in browser_context.init_scripts
    dom_helper_index = browser_context.init_scripts.index(MINIMAL_DOM_HELPER_SOURCE)
    content_helper_index = browser_context.init_scripts.index(
        MINIMAL_CONTENT_HELPER_SOURCE
    )
    assert dom_helper_index < content_helper_index


async def test_relaunch_session_is_adopted_by_first_browser_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    browser_context,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    root = tmp_path / "ChromeData"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=True,
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), root)
        ],
    )
    await manager.relaunch()
    monkeypatch.setattr(
        host_browser_common_module,
        "profile_lock_state",
        lambda _: ProfileLockState(True, (str(root / "SingletonLock"),), 12345),
    )

    opened = await manager.handle_op(
        {
            "op_id": "op-open",
            "context_id": "chat-1",
            "action": "open",
            "url": "https://example.com/",
            "profile_mode": "agent",
        }
    )

    assert opened["ok"] is True
    assert RELAUNCH_CONTEXT_ID not in manager._sessions
    assert "chat-1" in manager._sessions
    assert manager._sessions["chat-1"].context_id == "chat-1"


@pytest.mark.parametrize(
    ("first_action", "second_action", "second_context"),
    [
        ("open", "open", "chat-1"),
        ("ensure", "open", "chat-1"),
        ("open", "ensure", "chat-1"),
        ("ensure", "ensure", "chat-1"),
        ("open", "open", "chat-2"),
        ("open", "shutdown", "chat-1"),
    ],
)
async def test_parallel_host_browser_startup_uses_one_profile_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    first_action: str,
    second_action: str,
    second_context: str,
) -> None:
    launched = asyncio.Event()
    release = asyncio.Event()
    launches = []
    context = FakeContext()

    async def launch(adapter, session):
        launches.append(session.profile)
        launched.set()
        await release.wait()
        session.browser = session.context = context

    monkeypatch.setattr(host_browser_session_module._CDPRuntimeAdapter, "_launch", launch)
    executable = tmp_path / "chrome"
    executable.touch()
    profile = BrowserProfile("chrome-a0", "Chrome", str(executable), tmp_path, "Default", "Default")
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        persist_enabled=False,
        candidate_provider=lambda: [],
    )
    monkeypatch.setattr(manager, "selected_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(manager, "_auto_start_profile", lambda **kwargs: profile)
    payload = {"context_id": "chat-1", "action": "open", "url": "https://example.com/"}
    first = asyncio.create_task(manager.handle_op({**payload, "op_id": "first", "action": first_action}))
    second = None
    try:
        await asyncio.wait_for(launched.wait(), timeout=2)
        second = asyncio.create_task(
            manager.close() if second_action == "shutdown" else manager.handle_op({
                **payload, "op_id": "second", "action": second_action, "context_id": second_context,
            })
        )
        await asyncio.sleep(0)
        release.set()
        first_result, second_result = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

        assert len(launches) == 1
        if second_action == "shutdown":
            assert manager._sessions == {}
            assert context.closed
            return
        assert first_result["ok"] is True
        if second_context == "chat-1":
            assert second_result["ok"] is True
            if second_action == "open":
                assert second_result["result"]["state"]["currentUrl"] == payload["url"]
        else:
            assert second_result["code"] == "HOST_BROWSER_CONTEXT_ACTIVE"
            assert second_context not in manager._sessions
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await manager.close()


async def test_remote_debugging_session_attaches_without_closing_user_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_session as host_browser_session_module

    instances = []

    class FakeCDPConnection:
        is_connected = True
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint
            self.closed = False
            instances.append(self)

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

        async def command(
            self,
            method: str,
            params: dict[str, object] | None = None,
            *,
            session_id: str | None = None,
            timeout: float = 30.0,
        ) -> dict[str, object]:
            del session_id, timeout
            if method == "Target.getTargetInfo":
                return {"targetInfo": {"targetId": "target-1", "url": "https://example.com/", "title": "Example"}}
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"targetId": "target-1", "type": "page", "url": "https://example.com/"}
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": "session-1"}
            if method in {"Page.enable", "Runtime.enable", "Page.addScriptToEvaluateOnNewDocument"}:
                return {}
            if method == "Runtime.evaluate":
                expression = str((params or {}).get("expression") or "")
                if "document.readyState" in expression:
                    return {"result": {"type": "string", "value": "complete"}}
                if "location.href" in expression:
                    return {"result": {"type": "string", "value": "https://example.com/"}}
                if "document.title" in expression:
                    return {"result": {"type": "string", "value": "Example"}}
                if "history" in expression:
                    return {"result": {"type": "number", "value": 1}}
                return {"result": {"type": "undefined"}}
            return {}

    monkeypatch.setattr(host_browser_session_module, "CDPConnection", FakeCDPConnection)
    profile = BrowserProfile(
        "chrome-cdp",
        "Chrome (remote debugging)",
        "",
        Path(),
        "127.0.0.1:9222",
        "Remote debugging allowed",
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/test",
    )
    session = HostBrowserSession(
        context_id="ctx-cdp",
        profile=profile,
    )

    await session.ensure_started()
    listed = await session.list()
    await session.close()

    assert instances[0].endpoint == "ws://127.0.0.1:9222/devtools/browser/test"
    assert listed["browsers"][0]["currentUrl"] == "https://example.com/"
    assert instances[0].closed is True


async def test_remote_debugging_connection_failure_reports_enable_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_session as host_browser_session_module

    instances = []

    class FailingCDPConnection:
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint
            self.closed = False
            instances.append(self)

        async def connect(self) -> None:
            raise TimeoutError

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(host_browser_session_module, "CDPConnection", FailingCDPConnection)
    profile = BrowserProfile(
        "chrome-cdp",
        "Chrome (remote debugging)",
        "",
        Path(),
        "127.0.0.1:9222",
        "Remote debugging allowed",
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/test",
    )
    session = HostBrowserSession(context_id="ctx-cdp-fail", profile=profile)

    with pytest.raises(RuntimeError) as excinfo:
        await session.ensure_started()

    message = str(excinfo.value)
    assert "chrome://inspect/#remote-debugging" in message
    assert "Allow remote debugging for this browser instance" in message
    assert "TimeoutError" in message
    assert instances[0].closed is True


async def test_remote_debugging_connection_retries_changed_active_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_session as host_browser_session_module

    root = tmp_path / "google-chrome"
    root.mkdir()
    active_port = root / "DevToolsActivePort"
    active_port.write_text("9222\n/devtools/browser/old\n", encoding="utf-8")
    instances = []

    class RefreshingCDPConnection:
        is_connected = True
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint
            self.closed = False
            instances.append(self)

        async def connect(self) -> None:
            if len(instances) == 1:
                active_port.write_text("9333\n/devtools/browser/new\n", encoding="utf-8")
                raise OSError("endpoint changed")

        async def close(self) -> None:
            self.closed = True

        async def command(
            self,
            method: str,
            params: dict[str, object] | None = None,
            *,
            session_id: str | None = None,
            timeout: float = 30.0,
        ) -> dict[str, object]:
            del params, session_id, timeout
            if method == "Target.getTargets":
                return {"targetInfos": []}
            return {}

    monkeypatch.setattr(host_browser_session_module, "CDPConnection", RefreshingCDPConnection)
    profile = BrowserProfile(
        "chrome-cdp",
        "Chrome (remote debugging)",
        "",
        root,
        "localhost:9222",
        "Remote debugging allowed",
        cdp_endpoint="ws://localhost:9222/devtools/browser/old",
    )
    session = HostBrowserSession(context_id="ctx-cdp-refresh", profile=profile)

    await session.ensure_started()
    await session.close()

    assert [instance.endpoint for instance in instances] == [
        "ws://localhost:9222/devtools/browser/old",
        "ws://localhost:9333/devtools/browser/new",
    ]
    assert instances[0].closed is True
    assert session.profile.cdp_endpoint == "ws://localhost:9333/devtools/browser/new"


async def test_remote_debugging_session_opens_lists_and_reads_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_session as host_browser_session_module

    instances = []

    class FakeCDPConnection:
        is_connected = True
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint
            self.closed = False
            self.targets: dict[str, dict[str, str]] = {}
            self.sessions: dict[str, str] = {}
            self.closed_targets: list[str] = []
            instances.append(self)

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

        async def command(
            self,
            method: str,
            params: dict[str, object] | None = None,
            *,
            session_id: str | None = None,
            timeout: float = 30.0,
        ) -> dict[str, object]:
            del timeout
            params = params or {}
            if method == "Target.getTargetInfo":
                return {"targetInfo": {"url": self.targets[params["targetId"]]["url"], "title": "Example"}}
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {
                            "targetId": target_id,
                            "type": "page",
                            "url": target["url"],
                        }
                        for target_id, target in self.targets.items()
                    ]
                }
            if method == "Target.createTarget":
                target_id = f"target-{len(self.targets) + 1}"
                self.targets[target_id] = {"url": str(params.get("url") or "about:blank")}
                return {"targetId": target_id}
            if method == "Target.attachToTarget":
                target_id = str(params.get("targetId") or "")
                session = f"session-{target_id}"
                self.sessions[session] = target_id
                return {"sessionId": session}
            if method == "Target.closeTarget":
                target_id = str(params.get("targetId") or "")
                self.closed_targets.append(target_id)
                self.targets.pop(target_id, None)
                return {"success": True}
            if method in {"Page.enable", "Runtime.enable", "Page.addScriptToEvaluateOnNewDocument"}:
                return {}
            if method == "Page.navigate":
                target_id = self.sessions[str(session_id)]
                self.targets[target_id]["url"] = str(params.get("url") or "")
                return {}
            if method == "Runtime.evaluate":
                expression = str(params.get("expression") or "")
                target_id = self.sessions[str(session_id)]
                url = self.targets[target_id]["url"]
                if "document.readyState" in expression:
                    return {"result": {"type": "string", "value": "complete"}}
                if "location.href" in expression:
                    return {"result": {"type": "string", "value": url}}
                if "document.title" in expression:
                    return {"result": {"type": "string", "value": "Example"}}
                if "history" in expression:
                    return {"result": {"type": "number", "value": 1}}
                if "__spaceBrowserPageContent__?.ready" in expression:
                    return {"result": {"type": "boolean", "value": True}}
                if "__spaceBrowserPageContent__.capture" in expression:
                    return {
                        "result": {
                            "type": "object",
                            "value": {"document": "[button 1] Continue"},
                        }
                    }
                return {"result": {"type": "undefined"}}
            return {}

    monkeypatch.setattr(host_browser_session_module, "CDPConnection", FakeCDPConnection)
    profile = BrowserProfile(
        "chrome-cdp",
        "Chrome (remote debugging)",
        "",
        Path(),
        "127.0.0.1:9222",
        "Remote debugging allowed",
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/test",
    )
    session = HostBrowserSession(context_id="ctx-cdp-actions", profile=profile)

    opened = await session.open("https://example.com/")
    reopened = await session.open("https://example.com")
    assert len(instances[0].targets) == 1
    content = await session.content(opened["id"])
    listed = await session.list(include_content=True)
    closed = await session.close_browser(opened["id"])
    await session.close()

    assert opened["state"]["currentUrl"] == "https://example.com/"
    assert reopened["id"] == opened["id"]
    assert reopened["reused"] is True
    assert content == {"document": "[button 1] Continue"}
    assert listed["browsers"][0]["content"] == {"document": "[button 1] Continue"}
    assert closed == {"closed": opened["id"]}
    assert instances[0].closed_targets == ["target-1"]
    assert instances[0].closed is True


async def test_remote_ensure_respects_disabled_state_while_local_preparation_can_enable(
    tmp_path: Path,
    browser_context,
) -> None:
    root = tmp_path / "ChromeData"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=False,
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), root)
        ],
    )

    remote_result = await manager.handle_op(
        {
            "op_id": "op-ensure",
            "context_id": "chat-1",
            "action": "ensure",
            "profile_mode": "agent",
        }
    )

    assert remote_result["ok"] is False
    assert remote_result["code"] == "HOST_BROWSER_DISABLED"
    assert manager.enabled is False
    assert manager._sessions == {}

    local_result = await manager.ensure_available(profile_mode="agent")

    assert manager.enabled is True
    assert local_result["status"] == "active"
    assert RELAUNCH_CONTEXT_ID in manager._sessions


async def test_ensure_existing_mode_reports_restricted_saved_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    browser_context,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    (default_root / "Default").mkdir(parents=True)
    managed_root = tmp_path / "a0-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=True,
            host_browser_family="chrome",
            host_browser_profile_path=str(default_root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(executable), default_root),
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), managed_root),
        ],
    )

    result = await manager.handle_op(
        {
            "op_id": "op-ensure",
            "context_id": "chat-1",
            "action": "ensure",
            "profile_mode": "existing",
        }
    )

    assert result["ok"] is False
    assert result["code"] == "HOST_BROWSER_ERROR"
    assert "A0-controlled local profile" in result["error"]
    assert manager.config.host_browser_family == "chrome"
    assert manager.config.host_browser_profile_path == str(default_root)


async def test_ensure_agent_mode_auto_selects_supported_managed_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    browser_context,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    (default_root / "Default").mkdir(parents=True)
    managed_root = tmp_path / "a0-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=True,
            host_browser_family="chrome",
            host_browser_profile_path=str(default_root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(executable), default_root),
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), managed_root),
        ],
    )

    result = await manager.handle_op(
        {
            "op_id": "op-ensure",
            "context_id": "chat-1",
            "action": "ensure",
            "profile_mode": "agent",
        }
    )

    assert result["ok"] is True
    assert result["result"]["browser_family"] == "chrome-a0"
    assert manager.config.host_browser_family == "chrome-a0"
    assert manager.config.host_browser_profile_path == str(managed_root)


@pytest.mark.parametrize("has_other_profile", [False, True])
async def test_locked_profile_owned_by_active_context_reports_context_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    has_other_profile: bool,
    browser_context,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    root = tmp_path / "ChromeData"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=True,
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), root)
        ],
    )
    await manager.handle_op(
        {
            "op_id": "op-open-1",
            "context_id": "chat-1",
            "action": "open",
            "url": "https://example.com/",
            "profile_mode": "agent",
        }
    )
    other_context = FakeContext()
    if has_other_profile:
        manager._sessions["chat-2"] = HostBrowserSession(
            context_id="chat-2",
            profile=BrowserProfile("chrome-a0", "Chrome", str(executable), tmp_path / "other", "Default", "Default"),
            context=other_context,
        )
    monkeypatch.setattr(
        host_browser_common_module,
        "profile_lock_state",
        lambda _: ProfileLockState(True, (str(root / "SingletonLock"),), 12345),
    )

    result = await manager.handle_op(
        {
            "op_id": "op-open-2",
            "context_id": "chat-2",
            "action": "open",
            "url": "https://example.org/",
            "profile_mode": "agent",
        }
    )

    assert result["ok"] is False
    assert result["code"] == "HOST_BROWSER_CONTEXT_ACTIVE"
    assert result["result"]["active_context"] == "chat-1"
    if has_other_profile:
        assert manager._sessions["chat-2"].context is other_context
        assert other_context.closed is False


async def test_host_browser_session_cleans_up_after_launch_failure(tmp_path, monkeypatch) -> None:
    profile = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path, "Default", "Default")
    process = SimpleNamespace(returncode=3)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    session = HostBrowserSession("ctx-launch-failure", profile)
    with pytest.raises(RuntimeError, match="exited during startup"):
        await session.ensure_started()
    args = spawn.await_args.args
    assert f"--user-data-dir={tmp_path}" in args
    assert "--remote-debugging-address=127.0.0.1" in args and "--remote-debugging-port=0" in args
    assert session.process is session.context is None


@pytest.mark.parametrize(
    ("family", "expected_url"),
    [
        ("chrome", "chrome://inspect/#remote-debugging"),
        ("opera", "opera://inspect/#remote-debugging"),
        ("edge", "edge://inspect/#remote-debugging"),
    ],
)
async def test_host_browser_opens_allowlisted_remote_debugging_page_while_disabled(
    family: str,
    expected_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(host_browser_manager_module.platform, "system", lambda: "Linux")
    candidates = [
        BrowserCandidate(name, name.title(), str(tmp_path / name), tmp_path / name)
        for name in ("chrome", "opera", "edge")
    ]
    monkeypatch.setattr(
        host_browser_manager_module.subprocess,
        "Popen",
        lambda command, **_kwargs: launched.append(command),
    )
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=False),
        candidate_provider=lambda: candidates,
    )

    result = await manager.handle_op(
        {
            "op_id": f"open-{family}-setup",
            "action": "open_remote_debugging",
            "browser_family": family,
        }
    )

    assert result["ok"] is True
    assert result["result"]["url"] == expected_url
    assert launched == [[str(tmp_path / family), expected_url]]


async def test_host_browser_uses_macos_open_for_internal_setup_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(host_browser_manager_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        host_browser_manager_module.subprocess,
        "Popen",
        lambda command, **_kwargs: launched.append(command),
    )
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=False),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(tmp_path / "chrome"), tmp_path)
        ],
    )

    result = await manager.handle_op(
        {
            "op_id": "open-chrome-setup-macos",
            "action": "open_remote_debugging",
            "browser_family": "chrome",
        }
    )

    assert result["ok"] is True
    assert launched == [
        [
            "/usr/bin/open",
            "-a",
            "Google Chrome",
            "chrome://inspect/#remote-debugging",
        ]
    ]


async def test_host_browser_rejects_arbitrary_remote_debugging_target(tmp_path: Path) -> None:
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=False),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(tmp_path / "chrome"), tmp_path)
        ],
    )

    result = await manager.handle_op(
        {
            "op_id": "open-arbitrary-setup",
            "action": "open_remote_debugging",
            "browser_family": "chrome --arbitrary-flag",
        }
    )

    assert result["ok"] is False
    assert result["code"] == "HOST_BROWSER_SETUP_UNSUPPORTED"


async def test_set_checked_dispatch_parses_false_string(tmp_path: Path) -> None:
    profile = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path, "Default", "Default")
    session = HostBrowserSession(context_id="ctx-checked", profile=profile)
    seen: dict[str, object] = {}

    async def fake_set_checked(browser_id: object, ref: object, checked: bool = True) -> dict[str, object]:
        seen.update({"browser_id": browser_id, "ref": ref, "checked": checked})
        return {"ok": True}

    session.set_checked = fake_set_checked  # type: ignore[method-assign]

    await session.dispatch(
        {"action": "set_checked", "browser_id": 1, "ref": "input-1", "checked": "false"}
    )

    assert seen == {"browser_id": 1, "ref": "input-1", "checked": False}


async def test_manager_preserves_error_payload_shape_for_unknown_action(tmp_path: Path) -> None:
    manager = HostBrowserManager(CLIConfig(host_browser_enabled=True))

    result = await manager.handle_op(
        {"op_id": "op-unknown", "context_id": "ctx-error-shape", "action": "dance"}
    )

    assert result == {
        "op_id": "op-unknown",
        "ok": False,
        "code": "UNKNOWN_ACTION",
        "error": "Unknown host browser action: 'dance'",
    }


async def test_goto_surfaces_navigation_failures(tmp_path: Path) -> None:
    profile = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path, "Default", "Default")
    session = HostBrowserSession(context_id="ctx-goto", profile=profile)

    class FailingPage(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.settled = False

        async def goto(self, url: str, **_: object) -> None:
            del url
            raise ValueError("navigation boom")

        async def wait_for_load_state(self, *_: object, **__: object) -> None:
            self.settled = True

    page = FailingPage()

    with pytest.raises(RuntimeError, match="Browser navigation failed"):
        await session._goto(page, "https://example.invalid")

    assert page.settled is False


async def test_manager_recreates_session_when_profile_changes(tmp_path: Path) -> None:
    profile_one = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path / "one", "Default", "One")
    profile_two = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path / "two", "Default", "Two")
    manager = HostBrowserManager(CLIConfig(host_browser_enabled=True))
    session_one = await manager._session("ctx-profile", profile=profile_one)
    closed = False

    async def fake_close() -> None:
        nonlocal closed
        closed = True

    session_one.close = fake_close  # type: ignore[method-assign]

    session_two = await manager._session("ctx-profile", profile=profile_two)

    assert closed is True
    assert session_two is not session_one
    assert session_two.profile == profile_two


async def test_browser_file_scope_applies_to_direct_and_multi(monkeypatch, tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    outside = tmp_path / "other"
    outside.write_bytes(b"fixture")
    workspace = RemoteFileUtility(scan_root=str(root))
    session = HostBrowserSession("chat", profile=None)
    monkeypatch.setattr(session, "ensure_started", AsyncMock())
    page = FakePage()
    session.pages[1] = host_browser_session_module.HostBrowserPage(1, page)
    monkeypatch.setattr(session, "_state", AsyncMock(return_value={"id": 1}))
    calls = [
        {"action": "upload_file", "browser_id": 1, "ref": 1, "path": str(outside)},
        {"action": "screenshot", "browser_id": 1, "path": str(outside)},
    ]
    for op in calls:
        with pytest.raises(PermissionError, match="Files folder"):
            await session.dispatch(op)
    session.file_workspace = lambda: workspace
    assert all(not item["ok"] for item in await session.multi(calls))
    workspace.set_write_enabled(False)
    with pytest.raises(PermissionError, match="writes are disabled"):
        await session.dispatch({"action": "screenshot", "browser_id": 1, "path": "proof.png"})
    session.file_workspace = lambda: None
    assert (await session.dispatch({"action": "screenshot", "browser_id": 1}))["ephemeral"]
    assert outside.read_bytes() == b"fixture" and list(root.iterdir()) == []


@pytest.mark.parametrize("while_claiming", [False, True])
def test_owned_marker_retirement_preserves_replacement(tmp_path, monkeypatch, while_claiming):
    marker = tmp_path / "DevToolsActivePort"
    marker.write_text("43500\n/devtools/browser/old")
    identity = host_browser_session_module._marker_identity(marker)
    endpoint = remote_debugging_endpoint_from_active_port_file(marker)
    replacement = tmp_path / "replacement"
    replacement.write_text("43501\n/devtools/browser/new")
    if while_claiming:
        replace = Path.replace

        def switch(source, target):
            if source == marker:
                replace(replacement, marker)
            return replace(source, target)

        monkeypatch.setattr(Path, "replace", switch)
    else:
        replacement.replace(marker)
    host_browser_session_module._retire_owned_marker(tmp_path, identity, endpoint)
    assert marker.read_text() == "43501\n/devtools/browser/new"


def test_session_browser_uses_live_gateway_file_and_write_scopes(tmp_path):
    from agent_zero_cli.session import ConnectorSession

    config = CLIConfig(host_browser_enabled=True)
    manager = HostBrowserManager(config)
    session = ConnectorSession(config, workspace=tmp_path, host_browser_manager=manager,
                               gateway={"scopes": {"browser": True, "files": False, "file_write": False}})
    assert manager.file_workspace() is None
    session.gateway["scopes"] = {"browser": True, "files": True, "file_write": False}
    session._apply_scope_state()
    assert manager.file_workspace() is session.remote_files and not session.remote_files.allow_writes
    session.gateway["scopes"]["file_write"] = True
    session._apply_scope_state()
    assert manager.file_workspace().allow_writes


def test_browser_upload_snapshots_keep_selected_bytes(tmp_path):
    from threading import Event
    from agent_zero_cli.host_browser_files import snapshot_uploads

    root = tmp_path / "shared"
    root.mkdir()
    snapshots = tmp_path / "staged"
    snapshots.mkdir()
    source = root / "note.txt"
    source.write_text("chosen")
    workspace = RemoteFileUtility(scan_root=str(root))
    (selected,), _ = snapshot_uploads(workspace, ["note.txt"], snapshots, 8192, Event())
    source.write_text("later edit")
    assert Path(selected).name == source.name and Path(selected).read_text() == "chosen"
    if os.name != "nt":
        link = root / "link"
        link.symlink_to(tmp_path / "staged", target_is_directory=True)
        with pytest.raises(PermissionError):
            snapshot_uploads(workspace, ["link/0/note.txt"], snapshots, 8192, Event())


async def test_snapshot_budget_is_shared_across_concurrent_uploads(tmp_path, monkeypatch):
    from agent_zero_cli import host_browser_files as files

    monkeypatch.setattr(files, "MAX_BYTES", 8192)
    workspace = RemoteFileUtility(scan_root=str(tmp_path))
    (tmp_path / "file").write_bytes(b"a" * 5000)
    store = files.UploadSnapshots()
    received = []

    async def transfer(paths):
        received.extend(map(Path, paths))

    async def upload(tab):
        await store.attach(tab, workspace, ["file"], lambda: None, transfer)

    try:
        results = await asyncio.gather(upload(1), upload(2), return_exceptions=True)
        assert results[0] is None and isinstance(results[1], ValueError)
        assert len(received) == 1 and received[0].stat().st_size == 5000
        await store.release(1)
        assert not received[0].exists()
        await upload(2)
        assert received[1].is_file()
        store.sync_workspace(RemoteFileUtility(scan_root=str(tmp_path / "different")))
        assert not received[1].exists()
    finally:
        await store.release()


async def test_files_revocation_clears_accepted_and_inflight_uploads(tmp_path, monkeypatch):
    from threading import Event
    from agent_zero_cli import host_browser_files as files
    from agent_zero_cli.session import ConnectorSession

    config = CLIConfig(host_browser_enabled=True)
    manager = HostBrowserManager(config, persist_enabled=False)
    gateway = ConnectorSession(config, workspace=tmp_path, host_browser_manager=manager,
                               gateway={"scopes": {"browser": True, "files": True}})
    browser = HostBrowserSession("chat", profile=None, file_workspace=manager.file_workspace)
    manager._sessions["chat"] = browser
    (tmp_path / "note").write_text("held")
    retained = []

    async def receive(paths):
        retained.extend(map(Path, paths))

    await browser._uploads.attach(1, gateway.remote_files, ["note"], lambda: None, receive)
    started, resume = Event(), Event()
    copy = files.snapshot_uploads

    def delayed(*args):
        started.set()
        assert resume.wait(3)
        return copy(*args)

    monkeypatch.setattr(files, "snapshot_uploads", delayed)
    upload = asyncio.create_task(browser._uploads.attach(2, gateway.remote_files, ["note"], lambda: None, receive))
    assert await asyncio.to_thread(started.wait, 2)
    try:
        revoke = asyncio.create_task(gateway.replace_gateway_scopes({"files": False, "browser": True}))
        async with asyncio.timeout(2):
            while retained[0].exists():
                await asyncio.sleep(0.01)
    finally:
        resume.set()
    with pytest.raises(PermissionError, match="revoked"):
        await upload
    await revoke
    assert manager.file_workspace() is None and len(retained) == 1


def test_growing_upload_cannot_write_past_remaining_budget(tmp_path, monkeypatch):
    from threading import Event
    from agent_zero_cli import host_browser_files as files

    shared = tmp_path / "shared"
    shared.mkdir()
    source = shared / "file"
    source.write_bytes(b"a")
    output = tmp_path / "partial"
    output.mkdir()
    original_open = files.open_regular

    def open_growing(*args):
        stream = original_open(*args)
        read = stream.read
        def grow(size):
            with source.open("ab") as append:
                append.write(b"x" * 8192)
            return read(size)
        stream.read = grow
        return stream

    monkeypatch.setattr(files, "open_regular", open_growing)
    with pytest.raises(ValueError, match="session budget"):
        files.snapshot_uploads(RemoteFileUtility(scan_root=str(shared)), ["file"], output, 4096, Event())
    assert (output / "0/file").stat().st_size <= 4096


@pytest.mark.skipif(not os.environ.get("A0_TEST_CHROMIUM"), reason="set A0_TEST_CHROMIUM to run a headed Chromium")
async def test_native_chromium_profile_and_attach(tmp_path: Path, monkeypatch) -> None:
    from aiohttp import web
    from PIL import Image
    from io import BytesIO

    app = web.Application()

    async def index(request):
        return web.Response(text='<title>CDP check</title><input id="text"><input type="file" id="file"><input type="file" id="second">'
                                 '<div style="height:3200px"></div>', content_type="text/html")

    app.router.add_get("/{page}", index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
    profile = BrowserProfile("chrome-a0", "Chrome", os.environ["A0_TEST_CHROMIUM"], tmp_path, "Default", "Test")
    shared = tmp_path / "shared"
    shared.mkdir()
    workspace = RemoteFileUtility(scan_root=str(shared))
    owner = HostBrowserSession("owned", profile, file_workspace=lambda: workspace)
    try:
        opened = await owner.open(origin + "/first")
        browser_id = opened["id"]
        assert opened["state"]["title"] == "CDP check"
        process = owner.process
        assert process is not None
        marker = tmp_path / "DevToolsActivePort"
        assert marker.is_file()
        for script in ("() => 7", "(async () => 7)", "function () {return 7}", "(() => 7)()"):
            assert (await owner.evaluate(browser_id, script))["result"] == 7
        for suffix in ("", ";"):
            result = await owner.evaluate(browser_id,
                "(() => {globalThis.runs = (globalThis.runs || 0) + 1; return {count: runs}})()" + suffix)
            assert result["result"] == {"count": 2 if suffix else 1}
        await owner.evaluate(browser_id, "document.querySelector('#text').focus()")
        await owner.keyboard(browser_id, text="replace me")
        await owner.key_chord(browser_id, ["Control", "a"])
        await owner.keyboard(browser_id, text="typed")
        assert (await owner.evaluate(browser_id, "document.querySelector('#text').value"))["result"] == "typed"

        upload = shared / "upload.txt"
        upload.write_text("CDP upload", encoding="utf-8")
        await owner.set_content_helper_source("""
            globalThis.__spaceBrowserPageContent__ = {
                ready: () => true, capture: () => ({version: 1}), fileInputFor: () => ({selector: "#file"}),
                fileInputElementFor: ref => document.querySelector(ref === 2 ? "#second" : "#file")
            }
        """)
        staged = []
        page_type = type(owner.pages[browser_id].page)
        set_files = page_type.upload_files
        async def record_files(page, ref, selector, paths):
            staged.extend(map(Path, paths))
            await set_files(page, ref, selector, paths)
        monkeypatch.setattr(page_type, "upload_files", record_files)
        attached_file = await owner.upload_file(browser_id, 1, str(upload))
        assert attached_file["action"]["files"] == ["upload.txt"]
        second_input = shared / "second.txt"
        second_input.write_text("second")
        await owner.upload_file(browser_id, 2, str(second_input))
        assert (await owner.evaluate(browser_id, "document.querySelector('#file').files[0].name"))["result"] == upload.name
        upload.unlink()
        assert (await owner.evaluate(browser_id, "document.querySelector('#file').files[0].text()"))["result"] == "CDP upload"
        another = (await owner.open(origin + "/uploads"))["id"]
        await owner.upload_file(another, 1, "second.txt")
        assert all(file.is_file() for file in staged)
        await owner.close_browser(another)
        assert not staged[2].exists() and all(file.is_file() for file in staged[:2])

        oversize = shared / "oversize"
        with oversize.open("wb") as stream:
            stream.truncate(100 * 1024 * 1024 + 1)
        with pytest.raises(ValueError, match="session budget"):
            await owner.upload_file(browser_id, 1, "oversize")
        owner.file_workspace = lambda: None
        await owner.state(browser_id)
        assert not any(file.exists() for file in staged)
        owner.file_workspace = lambda: workspace
        assert (await owner.content(browser_id))["version"] == 1
        await owner.set_content_helper_source("""
            globalThis.__spaceBrowserPageContent__ = {ready: () => true, capture: () => ({version: 2})}
        """)
        assert (await owner.content(browser_id))["version"] == 2
        saved = await owner.screenshot_file(browser_id, path="images/proof.png", image_format="jpeg")
        assert Path(saved["host_path"]).read_bytes().startswith(b"\x89PNG")
        replaced = await owner.screenshot_file(browser_id, path="images/proof.png")
        assert replaced["host_path"] == saved["host_path"]
        workspace.set_write_enabled(False)
        with pytest.raises(PermissionError, match="writes are disabled"):
            await owner.screenshot_file(browser_id, path="images/other.png")
        captures = await owner.dispatch({"action": "multi", "calls": [
            {"action": "screenshot", "browser_id": browser_id, "image_format": "png"},
            {"action": "screenshot_file", "browser_id": browser_id},
        ]})
        for capture, fmt, suffix in zip(captures, ["png", "jpeg"], [".png", ".jpg"], strict=True):
            assert capture["ok"]
            result = capture["result"]
            artifact = result["artifact"]
            assert result["ephemeral"] and "host_path" not in result
            assert artifact["filename"].endswith(suffix)
            assert artifact["mime"] == result["mime"] == f"image/{fmt}"
            assert Image.open(BytesIO(base64.b64decode(artifact["data"]))).format == fmt.upper()
        shot = await owner.screenshot_file(browser_id, full_page=True)
        assert Image.open(BytesIO(base64.b64decode(shot["artifact"]["data"]))).height >= 3200
        assert (await owner.navigate(browser_id, origin + "/second"))["currentUrl"].endswith("/second")
        back = await owner.back(browser_id)
        assert back["currentUrl"].endswith("/first") and back["canGoForward"]
        assert (await owner.forward(browser_id))["currentUrl"].endswith("/second")

        page = owner.pages[browser_id].page
        await page.evaluate("setTimeout(() => alert('Temporary test dialog'), 50); true")
        await asyncio.sleep(0.15)
        try:
            during_dialog = await asyncio.wait_for(owner.open(origin + "/unrelated"), 4)
            assert during_dialog["state"]["title"] == "CDP check"
            assert await owner.close_browser(during_dialog["id"]) == {"closed": during_dialog["id"]}
        finally:
            await page.send("Page.handleJavaScriptDialog", {"accept": False})
        external = (await owner.open(origin + "/manual-close"))["id"]
        await owner.browser.command("Target.closeTarget", {"targetId": owner.pages[external].page.target_id})
        async with asyncio.timeout(2):
            while external in owner.pages:
                await asyncio.sleep(0.01)
        with pytest.raises(KeyError):
            await owner.state(external)

        attached = HostBrowserSession("attached", BrowserProfile(
            "chrome-cdp", "Chrome", "", Path(), "localhost", "Test", cdp_endpoint=owner.browser.endpoint
        ))
        try:
            assert any(item["title"] == "CDP check" for item in (await attached.list())["browsers"])
            assert (await attached.open(origin + "/second"))["reused"]
        finally:
            await attached.close()
        assert marker.is_file()
        assert process.returncode is None and (await owner.state(browser_id))["title"] == "CDP check"
        await owner.set_content_helper_source("""
            globalThis.__spaceBrowserPageContent__ = {
                ready: () => true, fileInputFor: () => ({selector: "#file"}),
                fileInputElementFor: () => document.querySelector("#file")
            }
        """)
        await owner.upload_file(browser_id, 1, "second.txt")
        assert staged[-1].exists()
        await owner.browser.command("Browser.close")
        async with asyncio.timeout(3):
            while staged[-1].exists():
                await asyncio.sleep(0.01)
    finally:
        await owner.close()
        await runner.cleanup()
    assert process.returncode is not None and not marker.exists()
