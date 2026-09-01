from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MACOS_PACKAGE_SRC = PROJECT_ROOT / "packages" / "a0-computer-use-macos" / "src"
if str(MACOS_PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(MACOS_PACKAGE_SRC))

from a0_computer_use_macos.backend import MACOS_BACKEND_SPEC, MacOSComputerUseBackend
import a0_computer_use_macos.runtime as macos_runtime_mod
from a0_computer_use_macos.runtime import (
    MacOSComputerUseError,
    MacOSComputerUseRuntime,
    MacOSSessionStore,
)
from a0_computer_use_macos.shared import normalize_action_payload


def _python_index_for_utf16(text: str, offset: int) -> int:
    units = 0
    for index, character in enumerate(text):
        if units == offset:
            return index
        units += len(character.encode("utf-16-le")) // 2
        if units > offset:
            raise ValueError("UTF-16 offset split a surrogate pair")
    if units == offset:
        return len(text)
    raise ValueError("UTF-16 offset exceeds text")


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self._width = 1280
        self._height = 720

    def screen_size(self) -> tuple[int, int]:
        self.calls.append(("screen_size", tuple(), {}))
        return self._width, self._height

    def capture_png(self) -> tuple[bytes, int, int]:
        self.calls.append(("capture_png", tuple(), {}))
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/5wAAAABJRU5ErkJggg=="
        )
        return png_bytes, 1, 1

    def capture_window_png(
        self,
        *,
        pid: int,
        bounds: tuple[float, float, float, float],
        title: str,
    ) -> tuple[bytes, int, int, int]:
        self.calls.append(("capture_window_png", (pid, bounds, title), {}))
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/5wAAAABJRU5ErkJggg=="
        )
        return png_bytes, 1, 1, 42

    def move(self, x: float, y: float) -> None:
        self.calls.append(("move", (x, y), {}))

    def click(self, x: float, y: float, *, button: str, count: int) -> None:
        self.calls.append(("click", (x, y), {"button": button, "count": count}))

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", (dx, dy), {}))

    def key(self, keys: list[str]) -> None:
        self.calls.append(("key", (tuple(keys),), {}))

    def type_text(self, text: str, *, submit: bool) -> None:
        self.calls.append(("type_text", (text,), {"submit": submit}))


class _FakeAXElement:
    def __init__(
        self,
        attrs: dict[str, object],
        *,
        children: list["_FakeAXElement"] | None = None,
        windows: list["_FakeAXElement"] | None = None,
        actions: list[str] | None = None,
    ) -> None:
        self.attrs = dict(attrs)
        self.children = children or []
        self.windows = windows or []
        self.actions = actions or []


def _install_fake_ax_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[type, _FakeAXElement, _FakeAXElement, _FakeAXElement]:
    button = _FakeAXElement(
        {
            "AXRole": "AXButton",
            "AXTitle": "Save",
            "AXDescription": "Save changes",
            "AXEnabled": True,
            "AXPosition": (100, 200),
            "AXSize": (80, 30),
        },
        actions=["AXPress"],
    )
    text_field = _FakeAXElement(
        {
            "AXRole": "AXTextField",
            "AXTitle": "Name",
            "AXValue": "draft",
            "AXEnabled": True,
            "AXPosition": (20, 80),
            "AXSize": (300, 28),
        }
    )
    window = _FakeAXElement(
        {
            "AXRole": "AXWindow",
            "AXTitle": "Document",
            "AXPosition": (10, 20),
            "AXSize": (640, 480),
        },
        children=[button, text_field],
    )
    text_field.attrs.update(
        {
            "AXFocused": True,
            "AXNumberOfCharacters": len("draft"),
            "AXSelectedTextRange": (len("draft"), 0),
            "AXWindow": window,
        }
    )
    app_root = _FakeAXElement(
        {
            "AXRole": "AXApplication",
            "AXTitle": "Fake App",
            "AXFocusedWindow": window,
            "AXFocusedUIElement": text_field,
        },
        windows=[window],
    )

    class FakeApplication:
        def localizedName(self) -> str:
            return FakeAccessibility.app_name

        def bundleIdentifier(self) -> str:
            return FakeAccessibility.bundle_id

        def processIdentifier(self) -> int:
            return FakeAccessibility.pid

    class FakeWorkspace:
        def frontmostApplication(self) -> FakeApplication:
            return FakeApplication()

    class FakeNSWorkspace:
        @staticmethod
        def sharedWorkspace() -> FakeWorkspace:
            return FakeWorkspace()

    class FakeAccessibility:
        kAXChildrenAttribute = "AXChildren"
        kAXDescriptionAttribute = "AXDescription"
        kAXEnabledAttribute = "AXEnabled"
        kAXFocusedAttribute = "AXFocused"
        kAXFocusedUIElementAttribute = "AXFocusedUIElement"
        kAXFocusedWindowAttribute = "AXFocusedWindow"
        kAXIdentifierAttribute = "AXIdentifier"
        kAXNumberOfCharactersAttribute = "AXNumberOfCharacters"
        kAXPositionAttribute = "AXPosition"
        kAXPressAction = "AXPress"
        kAXRoleAttribute = "AXRole"
        kAXSelectedTextAttribute = "AXSelectedText"
        kAXSelectedTextRangeAttribute = "AXSelectedTextRange"
        kAXSizeAttribute = "AXSize"
        kAXStringForRangeParameterizedAttribute = "AXStringForRange"
        kAXSubroleAttribute = "AXSubrole"
        kAXTitleAttribute = "AXTitle"
        kAXValueAttribute = "AXValue"
        kAXValueCFRangeType = 4
        kAXWindowAttribute = "AXWindow"
        kAXWindowsAttribute = "AXWindows"
        app_name = "Fake App"
        bundle_id = "com.example.fake"
        pid = 123
        performed: list[tuple[_FakeAXElement, str]] = []
        set_values: list[tuple[_FakeAXElement, str, object]] = []
        parameterized_reads = 0

        @staticmethod
        def AXUIElementCreateApplication(pid: int) -> _FakeAXElement:
            assert pid == FakeAccessibility.pid
            return app_root

        @staticmethod
        def AXUIElementCopyAttributeValue(
            element: _FakeAXElement,
            attribute: str,
            stop: object = None,
        ) -> tuple[int, object]:
            del stop
            if attribute == "AXChildren":
                return 0, element.children
            if attribute == "AXWindows":
                return 0, element.windows
            if attribute == "AXNumberOfCharacters" and "AXValue" in element.attrs:
                return 0, len(str(element.attrs["AXValue"]).encode("utf-16-le")) // 2
            if attribute in element.attrs:
                return 0, element.attrs[attribute]
            return 1, None

        @staticmethod
        def AXUIElementCopyParameterizedAttributeValue(
            element: _FakeAXElement,
            attribute: str,
            value: object,
            stop: object = None,
        ) -> tuple[int, object]:
            del stop
            FakeAccessibility.parameterized_reads += 1
            if attribute != "AXStringForRange" or not isinstance(value, tuple):
                return 1, None
            text = str(element.attrs.get("AXValue") or "")
            start, length = (int(value[0]), int(value[1]))
            start_index = _python_index_for_utf16(text, start)
            end_index = _python_index_for_utf16(text, start + length)
            return 0, text[start_index:end_index]

        @staticmethod
        def AXUIElementIsAttributeSettable(
            element: _FakeAXElement,
            attribute: str,
            stop: object = None,
        ) -> tuple[int, bool]:
            del stop
            overrides = getattr(element, "settable", {})
            if attribute in overrides:
                return 0, bool(overrides[attribute])
            return 0, attribute in {"AXValue", "AXSelectedTextRange", "AXSelectedText"}

        @staticmethod
        def AXValueCreate(value_type: int, value: object) -> object:
            assert value_type == 4
            return value

        @staticmethod
        def CFEqual(left: object, right: object) -> bool:
            return left is right

        @staticmethod
        def AXUIElementCopyActionNames(
            element: _FakeAXElement,
            stop: object = None,
        ) -> tuple[int, list[str]]:
            del stop
            return 0, element.actions

        @staticmethod
        def AXUIElementPerformAction(element: _FakeAXElement, action: str) -> int:
            FakeAccessibility.performed.append((element, action))
            return 0

        @staticmethod
        def AXUIElementSetAttributeValue(
            element: _FakeAXElement,
            attribute: str,
            value: object,
        ) -> int:
            FakeAccessibility.set_values.append((element, attribute, value))
            if attribute == "AXSelectedText" and getattr(element, "reject_selected_text", False):
                return 1
            if attribute == "AXSelectedText":
                text = str(element.attrs.get("AXValue") or "")
                start, length = element.attrs.get("AXSelectedTextRange", (0, 0))
                start_index = _python_index_for_utf16(text, int(start))
                end_index = _python_index_for_utf16(text, int(start) + int(length))
                replacement = str(value)
                normalizer = getattr(element, "normalize_replacement", None)
                if callable(normalizer):
                    replacement = str(normalizer(replacement))
                element.attrs["AXValue"] = text[:start_index] + replacement + text[end_index:]
                element.attrs["AXSelectedTextRange"] = (
                    int(start) + len(replacement.encode("utf-16-le")) // 2,
                    0,
                )
                return 0
            element.attrs[attribute] = value
            return 0

    FakeAccessibility.app_root = app_root
    FakeAccessibility.tag_field = text_field
    FakeAccessibility.tag_window = window

    class FakeQuartz:
        @staticmethod
        def CGPreflightScreenCaptureAccess() -> bool:
            return True

    fake_appkit = type("FakeAppKit", (), {"NSWorkspace": FakeNSWorkspace})
    monkeypatch.setattr(macos_runtime_mod, "_load_appkit_module", lambda: fake_appkit)
    monkeypatch.setattr(macos_runtime_mod, "_load_accessibility_module", lambda: FakeAccessibility)
    monkeypatch.setattr(macos_runtime_mod, "_load_quartz_module", lambda: FakeQuartz)
    return FakeAccessibility, window, button, text_field


def _runtime(tmp_path: Path) -> MacOSComputerUseRuntime:
    runtime = MacOSComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
    runtime._ensure_accessibility_permission = lambda **kwargs: None  # type: ignore[method-assign]
    runtime._probe_capture_dimensions = lambda **kwargs: (1280, 720)  # type: ignore[method-assign]
    return runtime


def _set_tag_text(
    field: _FakeAXElement,
    text: str,
    *,
    caret: int | None = None,
) -> None:
    field.attrs["AXValue"] = text
    field.attrs["AXSelectedTextRange"] = (
        len(text.encode("utf-16-le")) // 2 if caret is None else caret,
        0,
    )


def _tag_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    *,
    caret: int | None = None,
) -> tuple[MacOSComputerUseRuntime, type, _FakeAXElement, _FakeAXElement]:
    runtime = _runtime(tmp_path)
    fake_accessibility, window, _button, field = _install_fake_ax_tree(monkeypatch)
    _set_tag_text(field, text, caret=caret)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )
    return runtime, fake_accessibility, window, field


def test_macos_backend_spec_exports_expected_metadata() -> None:
    spec = MACOS_BACKEND_SPEC

    assert spec.backend_id == "macos"
    assert spec.backend_family == "macos"
    assert spec.interpreter_strategy == "current_python"
    assert Path(spec.helper_target).name == "runtime.py"
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("allow") is True
    assert "inline-png-capture" in spec.features
    assert "coregraphics-screen-capture" in spec.features
    assert "background-screen-capture" in spec.features
    assert "no-cursor-steal-capture" in spec.features
    assert "accessibility-trust" in spec.features
    assert "global-pixel-actions" in spec.features
    assert "keyboard-targets-frontmost-app" in spec.features
    assert "accessibility-tree-snapshot" in spec.features
    assert "accessibility-structural-targeting" in spec.features
    assert "accessibility-element-click" in spec.features
    assert "native-window-list" in spec.features
    assert "window-state" in spec.features
    assert "element-index-targeting" in spec.features
    assert "background-dispatch" in spec.features
    assert "foreground-dispatch-fallback" in spec.features
    assert "semantic-click-before-quartz-fallback" in spec.features
    assert "no-cursor-steal-accessibility-click" in spec.features
    assert "real-cursor-may-move" in spec.features
    assert "cursor-position-restore-after-click" in spec.features
    assert "frontmost-app-restore-after-click" in spec.features
    assert "a0-tag" in spec.features


def test_macos_backend_wrapper_uses_current_python() -> None:
    backend = MacOSComputerUseBackend()

    assert backend.spec is MACOS_BACKEND_SPEC
    assert backend.helper_command()[0] == sys.executable
    assert backend.helper_command()[-1] == "--stdio"


def test_macos_permission_status_and_requests_are_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAccessibility:
        kAXTrustedCheckOptionPrompt = "prompt"
        trusted = False
        prompt_calls = 0

        @classmethod
        def AXIsProcessTrusted(cls) -> bool:
            return cls.trusted

        @classmethod
        def AXIsProcessTrustedWithOptions(cls, options: dict[str, bool]) -> bool:
            assert options == {"prompt": True}
            cls.prompt_calls += 1
            return cls.trusted

    class FakeQuartz:
        granted = False
        request_calls = 0

        @classmethod
        def CGPreflightScreenCaptureAccess(cls) -> bool:
            return cls.granted

        @classmethod
        def CGRequestScreenCaptureAccess(cls) -> bool:
            cls.request_calls += 1
            cls.granted = True
            return True

    monkeypatch.setattr(macos_runtime_mod, "_load_accessibility_module", lambda: FakeAccessibility)
    monkeypatch.setattr(macos_runtime_mod, "_load_quartz_module", lambda: FakeQuartz)
    runtime = MacOSComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")

    assert runtime.permission_status()["state"] == "accessibility_required"
    assert runtime.request_accessibility()["state"] == "accessibility_required"
    assert FakeAccessibility.prompt_calls == 1

    FakeAccessibility.trusted = True
    assert runtime.permission_status()["state"] == "screen_recording_required"
    requested = runtime.request_screen_recording()
    assert requested["state"] == "ready"
    assert requested["screen_recording"] == "granted"
    assert requested["restart_required"] is False
    assert FakeQuartz.request_calls == 1


def test_macos_screen_recording_request_reports_restart_when_fresh_process_is_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeQuartz:
        @staticmethod
        def CGPreflightScreenCaptureAccess() -> bool:
            return False

        @staticmethod
        def CGRequestScreenCaptureAccess() -> bool:
            return True

    monkeypatch.setattr(macos_runtime_mod, "_load_quartz_module", lambda: FakeQuartz)
    runtime = MacOSComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")

    result = runtime.request_screen_recording()

    assert result["state"] == "screen_recording_required"
    assert result["restart_required"] is True


def test_macos_action_normalization_matches_shared_surface() -> None:
    move = normalize_action_payload("move", {"x": 0.25, "y": 0.75}, context_id="ctx-1")
    click = normalize_action_payload(
        "click",
        {"x": 0.4, "y": 0.6, "button": "right", "count": 2},
        context_id="ctx-1",
    )
    scroll = normalize_action_payload("scroll", {"dx": 1, "dy": -2}, context_id="ctx-1")
    keys = normalize_action_payload("key", {"key": "cmd+shift+t"}, context_id="ctx-1")
    typed = normalize_action_payload("type", {"text": "hello", "submit": True}, context_id="ctx-1")
    ax_snapshot = normalize_action_payload(
        "ax_snapshot",
        {"max_depth": 3, "max_nodes": 50},
        context_id="ctx-1",
    )
    ax_action = normalize_action_payload(
        "ax_action",
        {"target": {"role": "AXButton", "title": "Save"}, "operation": "press"},
        context_id="ctx-1",
    )
    window_state = normalize_action_payload(
        "get_window_state",
        {"pid": "123", "window_id": "ax-pid:123:path:0", "max_depth": 2},
        context_id="ctx-1",
    )
    element_action = normalize_action_payload(
        "element_action",
        {
            "window_id": "ax-pid:123:path:0",
            "element_index": "2",
            "operation": "set_value",
            "dispatch": "background",
            "text": "final",
        },
        context_id="ctx-1",
    )

    assert move["x"] == 0.25 and move["y"] == 0.75
    assert click["button"] == "right" and click["count"] == 2
    assert scroll["dx"] == 1 and scroll["dy"] == -2
    assert keys["keys"] == ["cmd", "shift", "t"]
    assert typed["text"] == "hello" and typed["submit"] is True
    assert ax_snapshot["max_depth"] == 3 and ax_snapshot["max_nodes"] == 50
    assert ax_action["target"]["title"] == "Save"
    assert ax_action["operation"] == "press"
    assert window_state["pid"] == 123
    assert window_state["window_id"] == "ax-pid:123:path:0"
    assert element_action["element_index"] == 2
    assert element_action["dispatch"] == "background"
    assert element_action["text"] == "final"


def test_macos_runtime_rejects_allow_without_restore_token(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(MacOSComputerUseError) as exc_info:
        runtime.start_session({"context_id": "ctx-1", "trust_mode": "allow"})

    assert exc_info.value.code == "COMPUTER_USE_REARM_REQUIRED"


def test_macos_runtime_session_policies_are_persisted_when_valid(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    restore_token = "123e4567-e89b-12d3-a456-426614174000"

    first = runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": restore_token,
        }
    )
    runtime.stop_session({"context_id": "ctx-1"})
    second = runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": restore_token,
        }
    )

    assert first["session_id"] == second["session_id"]
    assert second["reused"] is True
    stored = MacOSSessionStore(state_dir=tmp_path / "state").get("ctx-1")
    assert stored is not None
    assert stored.restore_token == restore_token


def test_macos_launcher_tag_session_does_not_probe_screen_capture(tmp_path: Path) -> None:
    runtime = MacOSComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
    runtime._ensure_accessibility_permission = lambda **kwargs: None  # type: ignore[method-assign]

    def unexpected_capture_probe(**_kwargs: object) -> tuple[int, int]:
        raise AssertionError("tag startup must not require Screen Recording")

    runtime._probe_capture_dimensions = unexpected_capture_probe  # type: ignore[method-assign]

    session = runtime.start_session(
        {
            "context_id": "launcher-tag",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    assert session["width"] == 0
    assert session["height"] == 0


def test_macos_runtime_capture_returns_inline_png_payload(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    capture = runtime.capture({"context_id": "ctx-1"})

    assert capture["width"] == 1
    assert capture["height"] == 1
    assert capture["png_base64"]
    assert base64.b64decode(capture["png_base64"])


def test_macos_runtime_capture_debug_dir_does_not_persist_screenshot(tmp_path: Path) -> None:
    runtime = MacOSComputerUseRuntime(
        driver=_FakeDriver(),
        state_dir=tmp_path / "state",
        capture_debug_dir=tmp_path / "debug-captures",
    )
    runtime._ensure_accessibility_permission = lambda **kwargs: None  # type: ignore[method-assign]
    runtime._probe_capture_dimensions = lambda **kwargs: (1280, 720)  # type: ignore[method-assign]
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    capture = runtime.capture({"context_id": "ctx-1"})

    assert capture["png_base64"]
    assert not (tmp_path / "debug-captures").exists()


def test_macos_runtime_capture_writes_requested_path_without_inline_payload(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )
    capture_path = tmp_path / "captures" / "capture.png"

    capture = runtime.capture({"context_id": "ctx-1", "capture_path": str(capture_path)})

    assert capture["width"] == 1
    assert capture["height"] == 1
    assert capture["capture_path"] == str(capture_path)
    assert "png_base64" not in capture
    assert capture_path.exists()


def test_macos_a0_tag_captures_ascii_and_replaces_only_the_exact_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "prefix untouched\n"
    tag = "@a0 draft a reply"
    suffix = "\nsuffix untouched"
    runtime, _accessibility, _window, field = _tag_runtime(
        tmp_path,
        monkeypatch,
        prefix + tag + suffix,
        caret=len((prefix + tag).encode("utf-16-le")) // 2,
    )

    captured = runtime.tag_context({"context_id": "ctx-1"})

    assert captured["query"] == "draft a reply"
    assert captured["tag_text"] == tag
    assert captured["app_name"] == "Fake App"
    assert captured["window_title"] == "Document"
    assert captured["replace_supported"] is True
    assert captured["screenshot_status"] == "attached"
    assert base64.b64decode(captured["artifact"]["data"]).startswith(b"\x89PNG\r\n\x1a\n")

    replaced = runtime.tag_replace(
        {
            "context_id": "ctx-1",
            "target_token": captured["target_token"],
            "replacement": "Concise answer",
        }
    )

    assert replaced["replaced"] is True
    assert field.attrs["AXValue"] == prefix + "Concise answer" + suffix
    assert field.attrs["AXSelectedTextRange"] == (
        len((prefix + "Concise answer").encode("utf-16-le")) // 2,
        0,
    )
    assert [call[0] for call in runtime._driver.calls] == ["capture_window_png"]


@pytest.mark.parametrize("native_x", [13.0, float("nan")])
def test_macos_a0_tag_window_capture_rejects_unverified_native_bounds(native_x: float) -> None:
    class FakeQuartz:
        kCGNullWindowID = 0
        kCGWindowBounds = "bounds"
        kCGWindowLayer = "layer"
        kCGWindowName = "title"
        kCGWindowNumber = "number"
        kCGWindowOwnerPID = "pid"

        @staticmethod
        def CGWindowListCopyWindowInfo(_options: int, _window_id: int) -> list[dict[str, object]]:
            return [
                {
                    "pid": 123,
                    "layer": 0,
                    "number": 42,
                    "title": "Document",
                    "bounds": {"X": native_x, "Y": 20, "Width": 640, "Height": 480},
                }
            ]

        @staticmethod
        def CGWindowListCreateImage(*_args: object) -> object:
            raise AssertionError("an unverified window must not be captured")

    driver = object.__new__(macos_runtime_mod._MacOSDesktopAutomation)
    driver._quartz = FakeQuartz()

    with pytest.raises(MacOSComputerUseError) as exc_info:
        driver.capture_window_png(
            pid=123,
            bounds=(10.0, 20.0, 640.0, 480.0),
            title="Document",
        )

    assert exc_info.value.code == "A0_TAG_SCREENSHOT_UNAVAILABLE"


def test_macos_a0_tag_preserves_unicode_before_inside_and_after_the_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "🙂 café before\n  "
    tag = "@a0 translate naïve 🌟"
    suffix = "\n終わり 🐍"
    runtime, _accessibility, _window, field = _tag_runtime(
        tmp_path,
        monkeypatch,
        prefix + tag + suffix,
        caret=len((prefix + tag).encode("utf-16-le")) // 2,
    )

    captured = runtime.tag_context({"context_id": "ctx-1"})
    runtime.tag_replace(
        {
            "context_id": "ctx-1",
            "target_token": captured["target_token"],
            "replacement": "Réponse 世界 ✨",
        }
    )

    assert captured["query"] == "translate naïve 🌟"
    assert field.attrs["AXValue"] == prefix + "Réponse 世界 ✨" + suffix


def test_macos_a0_tag_preserves_unicode_at_bounded_ax_range_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tag = "@a0 preserve unicode"
    prefix_units = 4097 - len(tag.encode("utf-16-le")) // 2
    prefix = "🙂" + "x" * (prefix_units - 3) + "\n"
    suffix = "\n" + "y" * 4094 + "🙂tail"
    runtime, _accessibility, _window, field = _tag_runtime(
        tmp_path,
        monkeypatch,
        prefix + tag + suffix,
        caret=len((prefix + tag).encode("utf-16-le")) // 2,
    )

    captured = runtime.tag_context({"context_id": "ctx-1"})
    runtime.tag_replace(
        {
            "context_id": "ctx-1",
            "target_token": captured["target_token"],
            "replacement": "Preserved 🌍",
        }
    )

    assert captured["query"] == "preserve unicode"
    assert captured["focused_text"] == prefix + tag + "\n" + "y" * 4094
    assert field.attrs["AXValue"] == prefix + "Preserved 🌍" + suffix


def test_macos_a0_tag_keeps_caret_after_untouched_trailing_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "before\n"
    tag = "@a0 answer"
    trailing = " \t"
    suffix = "\nafter"
    runtime, _accessibility, _window, field = _tag_runtime(
        tmp_path,
        monkeypatch,
        prefix + tag + trailing + suffix,
        caret=len((prefix + tag + trailing).encode("utf-16-le")) // 2,
    )

    captured = runtime.tag_context({"context_id": "ctx-1"})
    runtime.tag_replace(
        {
            "context_id": "ctx-1",
            "target_token": captured["target_token"],
            "replacement": "result",
        }
    )

    assert captured["tag_text"] == tag
    assert field.attrs["AXValue"] == prefix + "result" + trailing + suffix
    assert field.attrs["AXSelectedTextRange"] == (
        len((prefix + "result" + trailing).encode("utf-16-le")) // 2,
        0,
    )


def test_macos_a0_tag_parses_suffixed_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _accessibility, _window, _field = _tag_runtime(
        tmp_path,
        monkeypatch,
        "@A0.developer review this",
    )

    captured = runtime.tag_context({"context_id": "ctx-1"})

    assert captured["profile_override"] == "developer"
    assert captured["query"] == "review this"


def test_macos_a0_tag_rejects_caret_before_the_logical_request_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "@a0 draft more text"
    runtime, _accessibility, _window, _field = _tag_runtime(
        tmp_path,
        monkeypatch,
        text,
        caret=len("@a0 draft".encode("utf-16-le")) // 2,
    )

    with pytest.raises(MacOSComputerUseError) as exc_info:
        runtime.tag_context({"context_id": "ctx-1"})

    assert exc_info.value.code == "A0_TAG_CARET_POSITION"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("@a0   ", "A0_TAG_EMPTY_QUERY"),
        ("@a0 " + "x" * 2049, "A0_TAG_QUERY_TOO_LONG"),
    ],
)
def test_macos_a0_tag_rejects_empty_or_oversized_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    code: str,
) -> None:
    runtime, _accessibility, _window, _field = _tag_runtime(tmp_path, monkeypatch, text)

    with pytest.raises(MacOSComputerUseError) as exc_info:
        runtime.tag_context({"context_id": "ctx-1"})

    assert exc_info.value.code == code


def test_macos_a0_tag_rejects_protected_field_before_text_or_screenshot_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, accessibility, _window, field = _tag_runtime(
        tmp_path,
        monkeypatch,
        "@a0 reveal this",
    )
    field.attrs["AXSubrole"] = "AXSecureTextField"

    with pytest.raises(MacOSComputerUseError) as exc_info:
        runtime.tag_context({"context_id": "ctx-1"})

    assert exc_info.value.code == "A0_TAG_PROTECTED_FIELD"
    assert accessibility.parameterized_reads == 0
    assert [call[0] for call in runtime._driver.calls] == []


@pytest.mark.parametrize("change", ["process", "window", "element", "text", "caret"])
def test_macos_a0_tag_rejects_changed_process_window_element_text_or_focus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    original = "@a0 keep the exact target"
    runtime, accessibility, window, field = _tag_runtime(tmp_path, monkeypatch, original)
    captured = runtime.tag_context({"context_id": "ctx-1"})

    if change == "process":
        accessibility.pid = 456
    elif change == "window":
        accessibility.app_root.attrs["AXFocusedWindow"] = _FakeAXElement(
            {"AXRole": "AXWindow", "AXTitle": "Other"}
        )
    elif change == "element":
        accessibility.app_root.attrs["AXFocusedUIElement"] = _FakeAXElement(
            {"AXRole": "AXTextField", "AXWindow": window}
        )
    elif change == "text":
        field.attrs["AXValue"] = "@a0 changed by the user"
    else:
        field.attrs["AXSelectedTextRange"] = (0, 0)

    with pytest.raises(MacOSComputerUseError) as exc_info:
        runtime.tag_replace(
            {
                "context_id": "ctx-1",
                "target_token": captured["target_token"],
                "replacement": "unsafe",
            }
        )

    assert exc_info.value.code == "A0_TAG_TARGET_CHANGED"
    assert "unsafe" not in str(field.attrs.get("AXValue") or "")


def test_macos_a0_tag_rejects_wrong_and_expired_target_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _accessibility, _window, _field = _tag_runtime(
        tmp_path,
        monkeypatch,
        "@a0 summarize this",
    )
    captured = runtime.tag_context({"context_id": "ctx-1"})

    with pytest.raises(MacOSComputerUseError) as wrong:
        runtime.tag_replace(
            {"context_id": "ctx-1", "target_token": "wrong", "replacement": "unsafe"}
        )
    assert wrong.value.code == "A0_TAG_TARGET_EXPIRED"

    assert runtime._tag_target is not None
    runtime._tag_target.captured_at -= 16 * 60
    with pytest.raises(MacOSComputerUseError) as expired:
        runtime.tag_replace(
            {
                "context_id": "ctx-1",
                "target_token": captured["target_token"],
                "replacement": "unsafe",
            }
        )
    assert expired.value.code == "A0_TAG_TARGET_EXPIRED"
    assert runtime._tag_target is None


def test_macos_a0_tag_restores_original_when_editor_normalizes_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "intro\n@a0 preserve café — 🌟\noutro"
    caret = len("intro\n@a0 preserve café — 🌟".encode("utf-16-le")) // 2
    runtime, _accessibility, _window, field = _tag_runtime(
        tmp_path,
        monkeypatch,
        original,
        caret=caret,
    )
    captured = runtime.tag_context({"context_id": "ctx-1"})
    field.normalize_replacement = lambda value: value.upper() if value == "mixed Case" else value

    with pytest.raises(MacOSComputerUseError) as exc_info:
        runtime.tag_replace(
            {
                "context_id": "ctx-1",
                "target_token": captured["target_token"],
                "replacement": "mixed Case",
            }
        )

    assert exc_info.value.code == "A0_TAG_REPLACE_FAILED"
    assert field.attrs["AXValue"] == original
    assert field.attrs["AXSelectedTextRange"] == (caret, 0)


def test_macos_a0_tag_reports_noneditable_and_clears_on_release_and_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _accessibility, _window, field = _tag_runtime(
        tmp_path,
        monkeypatch,
        "@a0 summarize this",
    )
    field.settable = {"AXSelectedText": False}
    captured = runtime.tag_context({"context_id": "ctx-1"})
    assert captured["replace_supported"] is False
    with pytest.raises(MacOSComputerUseError) as unsupported:
        runtime.tag_replace(
            {
                "context_id": "ctx-1",
                "target_token": captured["target_token"],
                "replacement": "answer",
            }
        )
    assert unsupported.value.code == "A0_TAG_REPLACE_UNSUPPORTED"
    assert runtime.tag_release({"context_id": "ctx-1", "target_token": "wrong"}) == {"released": False}
    assert runtime.tag_release(
        {"context_id": "ctx-1", "target_token": captured["target_token"]}
    ) == {"released": True}
    assert runtime._tag_target is None

    field.settable = {}
    runtime.tag_context({"context_id": "ctx-1"})
    runtime.stop_session({"context_id": "ctx-1"})
    assert runtime._tag_target is None


def test_macos_a0_tag_continues_without_screen_recording_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _accessibility, _window, _field = _tag_runtime(
        tmp_path,
        monkeypatch,
        "@a0 summarize this",
    )
    quartz = macos_runtime_mod._load_quartz_module()
    monkeypatch.setattr(quartz, "CGPreflightScreenCaptureAccess", staticmethod(lambda: False))

    captured = runtime.tag_context({"context_id": "ctx-1"})

    assert captured["screenshot_status"] == "unavailable"
    assert "Screen Recording permission" in captured["screenshot_error"]
    assert "artifact" not in captured
    assert [call[0] for call in runtime._driver.calls] == []


def test_macos_a0_tag_continues_when_screen_recording_preflight_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _accessibility, _window, _field = _tag_runtime(
        tmp_path,
        monkeypatch,
        "@a0 summarize this",
    )
    quartz = macos_runtime_mod._load_quartz_module()

    def failed_preflight() -> bool:
        raise RuntimeError("preflight failed")

    monkeypatch.setattr(quartz, "CGPreflightScreenCaptureAccess", staticmethod(failed_preflight))

    captured = runtime.tag_context({"context_id": "ctx-1"})

    assert captured["screenshot_status"] == "unavailable"
    assert "could not be verified" in captured["screenshot_error"]
    assert "artifact" not in captured
    assert [call[0] for call in runtime._driver.calls] == []


def test_macos_a0_tag_private_actions_do_not_leak_into_public_computer_use_surface() -> None:
    from agent_zero_cli import computer_use as computer_use_mod

    assert {"tag_context", "tag_replace", "tag_release"}.isdisjoint(computer_use_mod._SUPPORTED_ACTIONS)


def test_macos_runtime_ax_snapshot_returns_bounded_structural_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    _fake_accessibility, _window, _button, _text_field = _install_fake_ax_tree(monkeypatch)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    snapshot = runtime.ax_snapshot({"context_id": "ctx-1", "max_depth": 3, "max_nodes": 10})

    assert snapshot["app"]["name"] == "Fake App"
    assert snapshot["app"]["bundle_id"] == "com.example.fake"
    assert snapshot["node_count"] == 4
    assert snapshot["truncated"] is False
    tree = snapshot["tree"]
    assert tree["role"] == "AXApplication"
    window = tree["children"][0]
    assert window["path"] == [0]
    assert window["role"] == "AXWindow"
    assert window["title"] == "Document"
    button = window["children"][0]
    assert button["path"] == [0, 0]
    assert button["role"] == "AXButton"
    assert button["title"] == "Save"
    assert button["actions"] == ["AXPress"]
    assert button["frame"]["normalized"]["x"] == round(100 / 1280, 6)


def test_macos_runtime_ax_action_presses_semantic_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    fake_accessibility, _window, button, _text_field = _install_fake_ax_tree(monkeypatch)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.ax_action(
        {
            "context_id": "ctx-1",
            "target": {"role": "AXButton", "title": "Save"},
            "operation": "press",
        }
    )

    assert result["operation"] == "press"
    assert result["target"]["path"] == [0, 0]
    assert result["target"]["title"] == "Save"
    assert fake_accessibility.performed == [(button, "AXPress")]


def test_macos_runtime_window_state_indexes_elements_for_background_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    fake_accessibility, _window, button, text_field = _install_fake_ax_tree(monkeypatch)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    windows = runtime.list_windows({"context_id": "ctx-1"})
    state = runtime.get_window_state(
        {
            "context_id": "ctx-1",
            "window_id": windows["windows"][0]["window_id"],
            "max_depth": 2,
        }
    )
    button_index = state["tree"]["children"][0]["element_index"]
    text_index = state["tree"]["children"][1]["element_index"]
    press = runtime.element_action(
        {
            "context_id": "ctx-1",
            "window_id": state["window_id"],
            "element_index": button_index,
            "operation": "press",
            "dispatch": "background",
        }
    )
    typed = runtime.element_action(
        {
            "context_id": "ctx-1",
            "window_id": state["window_id"],
            "element_index": text_index,
            "operation": "set_value",
            "dispatch": "background",
            "value": "final",
        }
    )

    assert windows["windows"][0]["window_id"] == "ax-pid:123:path:0"
    assert state["tree"]["element_index"] == 0
    assert button_index == 1
    assert text_index == 2
    assert press["actual_dispatch"] == "background"
    assert press["background_unavailable"] is False
    assert typed["actual_dispatch"] == "background"
    assert fake_accessibility.performed == [(button, "AXPress")]
    assert fake_accessibility.set_values == [(text_field, "AXValue", "final")]
    assert text_field.attrs["AXValue"] == "final"


def test_macos_runtime_background_focus_reports_unavailable_and_auto_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    fake_accessibility, _window, _button, text_field = _install_fake_ax_tree(monkeypatch)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )
    state = runtime.get_window_state({"context_id": "ctx-1", "max_depth": 2})
    text_index = state["tree"]["children"][1]["element_index"]

    background_only = runtime.element_action(
        {
            "context_id": "ctx-1",
            "element_index": text_index,
            "operation": "focus",
            "dispatch": "background",
        }
    )
    assert background_only["background_unavailable"] is True
    assert fake_accessibility.set_values == []

    auto = runtime.element_action(
        {
            "context_id": "ctx-1",
            "element_index": text_index,
            "operation": "focus",
            "dispatch": "auto",
        }
    )

    assert auto["actual_dispatch"] == "foreground"
    assert auto["foreground_fallback_used"] is True
    assert fake_accessibility.set_values == [(text_field, "AXFocused", True)]


def test_macos_runtime_ax_action_sets_value_by_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    fake_accessibility, _window, _button, text_field = _install_fake_ax_tree(monkeypatch)
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.ax_action(
        {
            "context_id": "ctx-1",
            "path": [0, 1],
            "operation": "set_value",
            "value": "final",
        }
    )

    assert result["operation"] == "set_value"
    assert result["target"]["role"] == "AXTextField"
    assert fake_accessibility.set_values == [(text_field, "AXValue", "final")]
    assert text_field.attrs["AXValue"] == "final"


def test_macos_runtime_ax_action_rejects_ambiguous_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    _fake_accessibility, window, _button, _text_field = _install_fake_ax_tree(monkeypatch)
    window.children.append(_FakeAXElement({"AXRole": "AXButton", "AXTitle": "Cancel"}))
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    with pytest.raises(MacOSComputerUseError) as exc_info:
        runtime.ax_action({"context_id": "ctx-1", "target": {"role": "AXButton"}})

    assert exc_info.value.code == "COMPUTER_USE_AX_TARGET_AMBIGUOUS"


def test_macos_driver_capture_prefers_coregraphics_without_screencapture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_window_ids: list[int] = []

    class FakePNGData(bytes):
        pass

    class FakeImageRep:
        def initWithCGImage_(self, image: object) -> "FakeImageRep":
            return self

        def representationUsingType_properties_(self, png_type: object, properties: dict[str, object]) -> bytes:
            del png_type, properties
            return base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAIAAAADCAQAAABWKLW/AAAADElEQVR42mP8z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
            )

    class FakeNSBitmapImageRep:
        @classmethod
        def alloc(cls) -> FakeImageRep:
            return FakeImageRep()

    class FakeQuartz:
        kCGEventMouseMoved = 5
        kCGMouseButtonLeft = 0
        kCGNullWindowID = 0
        kCGWindowBounds = "bounds"
        kCGWindowImageBestResolution = 8
        kCGWindowImageBoundsIgnoreFraming = 1
        kCGWindowLayer = "layer"
        kCGWindowListExcludeDesktopElements = 16
        kCGWindowListOptionIncludingWindow = 8
        kCGWindowListOptionOnScreenOnly = 1
        kCGWindowName = "title"
        kCGWindowNumber = "number"
        kCGWindowOwnerPID = "pid"
        CGRectNull = "null"

        @staticmethod
        def CGMainDisplayID() -> int:
            return 1

        @staticmethod
        def CGDisplayCreateImage(display_id: int) -> object:
            assert display_id == 1
            return object()

        @staticmethod
        def CGWindowListCopyWindowInfo(options: int, window_id: int) -> list[dict[str, object]]:
            assert options == 17
            assert window_id == 0
            return [
                {
                    "pid": 123,
                    "layer": 0,
                    "bounds": {"X": 10, "Y": 20, "Width": 640, "Height": 480},
                    "number": 42,
                    "title": "Document",
                },
                {
                    "pid": 999,
                    "layer": 0,
                    "bounds": {"X": 10, "Y": 20, "Width": 640, "Height": 480},
                    "number": 99,
                    "title": "Other",
                },
            ]

        @staticmethod
        def CGWindowListCreateImage(
            bounds: object,
            options: int,
            window_id: int,
            image_options: int,
        ) -> object:
            assert bounds == "null"
            assert options == 8
            assert image_options == 9
            captured_window_ids.append(window_id)
            return object()

    fake_appkit = type(
        "FakeAppKit",
        (),
        {
            "NSBitmapImageRep": FakeNSBitmapImageRep,
            "NSBitmapImageFileTypePNG": 4,
        },
    )
    monkeypatch.setattr(macos_runtime_mod, "_load_quartz_module", lambda: FakeQuartz)
    monkeypatch.setattr(macos_runtime_mod, "_load_appkit_module", lambda: fake_appkit)
    monkeypatch.setattr(macos_runtime_mod.shutil, "which", lambda name: None)

    driver = macos_runtime_mod._MacOSDesktopAutomation()

    _png_bytes, width, height = driver.capture_png()
    assert driver.last_capture_strategy == "coregraphics"
    _window_png, window_width, window_height, window_id = driver.capture_window_png(
        pid=123,
        bounds=(10.0, 20.0, 640.0, 480.0),
        title="Document",
    )

    assert (width, height) == (2, 3)
    assert (window_width, window_height, window_id) == (2, 3, 42)
    assert captured_window_ids == [42]
    assert driver.last_capture_strategy == "coregraphics-window"

    with pytest.raises(MacOSComputerUseError) as unverified:
        driver.capture_window_png(
            pid=123,
            bounds=(11.0, 20.0, 630.0, 480.0),
            title="Document",
        )
    assert unverified.value.code == "A0_TAG_SCREENSHOT_UNAVAILABLE"


def test_macos_driver_click_restores_cursor_and_frontmost_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakePoint:
        x = 11
        y = 22

    class FakeApplication:
        def activateWithOptions_(self, options: int) -> None:
            events.append(("activate", options))

    class FakeWorkspace:
        def frontmostApplication(self) -> FakeApplication:
            return FakeApplication()

    class FakeNSWorkspace:
        @staticmethod
        def sharedWorkspace() -> FakeWorkspace:
            return FakeWorkspace()

    class FakeQuartz:
        kCGEventMouseMoved = 1
        kCGEventLeftMouseDown = 2
        kCGEventLeftMouseUp = 3
        kCGEventRightMouseDown = 4
        kCGEventRightMouseUp = 5
        kCGEventOtherMouseDown = 6
        kCGEventOtherMouseUp = 7
        kCGMouseButtonLeft = 0
        kCGMouseButtonRight = 1
        kCGMouseButtonCenter = 2
        kCGMouseEventClickState = 1
        kCGHIDEventTap = 0

        @staticmethod
        def CGEventCreate(source: object) -> object:
            del source
            return object()

        @staticmethod
        def CGEventGetLocation(event: object) -> FakePoint:
            del event
            return FakePoint()

        @staticmethod
        def CGEventCreateMouseEvent(source: object, event_type: int, point: tuple[float, float], button: int) -> dict[str, object]:
            del source
            return {"type": event_type, "point": point, "button": button}

        @staticmethod
        def CGEventSetIntegerValueField(event: dict[str, object], field: int, value: int) -> None:
            del field
            event["click_state"] = value

        @staticmethod
        def CGEventPost(tap: int, event: dict[str, object]) -> None:
            del tap
            events.append(("post", event["point"]))

    fake_appkit = type(
        "FakeAppKit",
        (),
        {
            "NSWorkspace": FakeNSWorkspace,
            "NSApplicationActivateIgnoringOtherApps": 2,
        },
    )
    monkeypatch.setattr(macos_runtime_mod, "_load_quartz_module", lambda: FakeQuartz)
    monkeypatch.setattr(macos_runtime_mod, "_load_appkit_module", lambda: fake_appkit)
    monkeypatch.setattr(
        macos_runtime_mod,
        "_load_accessibility_module",
        lambda: (_ for _ in ()).throw(MacOSComputerUseError("UNAVAILABLE", "no ax")),
    )

    driver = macos_runtime_mod._MacOSDesktopAutomation()

    driver.click(100, 200, button="left", count=1)

    posted_points = [event[1] for event in events if event[0] == "post"]
    assert posted_points[0] == (100.0, 200.0)
    assert posted_points[-1] == (11.0, 22.0)
    assert ("activate", 2) in events
    assert driver.last_click_strategy == "quartz-cursor-restore"


def test_macos_driver_click_prefers_accessibility_press_without_cursor_motion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeQuartz:
        pass

    class FakeAccessibility:
        kAXPressAction = "AXPress"

        @staticmethod
        def AXUIElementCreateSystemWide() -> str:
            return "system"

        @staticmethod
        def AXUIElementCopyElementAtPosition(system: str, x: float, y: float, stop: object) -> tuple[int, str]:
            del stop
            events.append(("element_at_position", (system, x, y)))
            return 0, "button"

        @staticmethod
        def AXUIElementPerformAction(element: str, action: str) -> int:
            events.append(("perform_action", (element, action)))
            return 0

    monkeypatch.setattr(macos_runtime_mod, "_load_quartz_module", lambda: FakeQuartz)
    monkeypatch.setattr(macos_runtime_mod, "_load_accessibility_module", lambda: FakeAccessibility)

    driver = macos_runtime_mod._MacOSDesktopAutomation()

    driver.click(100, 200, button="left", count=1)

    assert events == [
        ("element_at_position", ("system", 100.0, 200.0)),
        ("perform_action", ("button", "AXPress")),
    ]
    assert driver.last_click_strategy == "accessibility-press"


def test_macos_runtime_debug_logs_start_session_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("A0_COMPUTER_USE_DEBUG", "1")
    runtime = _runtime(tmp_path)

    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    stderr = capsys.readouterr().err
    assert "start_session.begin" in stderr
    assert "start_session.accessibility.begin" in stderr
    assert "start_session.capture_probe.begin" in stderr
    assert "start_session.created_session" in stderr


def test_macos_runtime_normalizes_actions_and_routes_input(tmp_path: Path) -> None:
    driver = _FakeDriver()
    runtime = MacOSComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime._ensure_accessibility_permission = lambda **kwargs: None  # type: ignore[method-assign]
    runtime._probe_capture_dimensions = lambda **kwargs: (1280, 720)  # type: ignore[method-assign]
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    runtime.move({"context_id": "ctx-1", "x": 0.25, "y": 0.75})
    runtime.click({"context_id": "ctx-1", "x": 0.5, "y": 0.5, "button": "left", "count": 2})
    runtime.scroll({"context_id": "ctx-1", "dx": 1, "dy": -2})
    runtime.key({"context_id": "ctx-1", "keys": ["cmd", "shift", "t"]})
    runtime.type_text({"context_id": "ctx-1", "text": "hello", "submit": True})

    assert [call[0] for call in driver.calls if call[0] != "capture_png"] == [
        "move",
        "click",
        "scroll",
        "key",
        "type_text",
    ]
