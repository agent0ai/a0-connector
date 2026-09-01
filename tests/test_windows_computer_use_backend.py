from __future__ import annotations

import base64
import io
import json
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PACKAGE_SRC = PROJECT_ROOT / "packages" / "a0-computer-use-windows" / "src"
if str(WINDOWS_PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(WINDOWS_PACKAGE_SRC))

from a0_computer_use_windows.backend import WINDOWS_BACKEND_SPEC, WindowsComputerUseBackend
import a0_computer_use_windows.runtime as windows_runtime_mod
from a0_computer_use_windows.runtime import (
    ScreenGeometry,
    WindowsComputerUseError,
    WindowsComputerUseRuntime,
    WindowsSessionStore,
)
from a0_computer_use_windows.shared import normalize_action_payload


def _python_index_for_utf16(text: str, offset: int) -> int:
    units = 0
    for index, character in enumerate(text):
        if units == offset:
            return index
        units += len(character.encode("utf-16-le")) // 2
    if units == offset:
        return len(text)
    raise ValueError("invalid UTF-16 offset")


class _FakeRect:
    def __init__(self, x: float, y: float, width: float, height: float) -> None:
        self.left = x
        self.top = y
        self.right = x + width
        self.bottom = y + height


class _FakeTextModel:
    def __init__(self, value: str, caret: int) -> None:
        self.value = value
        self.selection = (caret, caret)
        self.read_only = False
        self.normalize_input = None


class _FakeTextRange:
    def __init__(self, model: _FakeTextModel, start: int, end: int) -> None:
        self.model = model
        self.start = start
        self.end = end

    def Clone(self) -> "_FakeTextRange":
        return _FakeTextRange(self.model, self.start, self.end)

    def GetText(self, limit: int) -> str:
        text = self.model.value[self.start : self.end]
        return text if limit < 0 else text[:limit]

    def CompareEndpoints(
        self,
        endpoint: int,
        other: "_FakeTextRange",
        other_endpoint: int,
    ) -> int:
        left = self.start if endpoint == 0 else self.end
        right = other.start if other_endpoint == 0 else other.end
        return left - right

    def MoveEndpointByRange(
        self,
        endpoint: int,
        other: "_FakeTextRange",
        other_endpoint: int,
    ) -> None:
        value = other.start if other_endpoint == 0 else other.end
        if endpoint == 0:
            self.start = value
        else:
            self.end = value

    def MoveEndpointByUnit(self, endpoint: int, _unit: int, count: int) -> int:
        if endpoint == 0:
            old = self.start
            self.start = min(self.end, max(0, self.start + count))
            return self.start - old
        old = self.end
        self.end = max(self.start, min(len(self.model.value), self.end + count))
        return self.end - old

    def Move(self, _unit: int, count: int) -> int:
        if self.start != self.end:
            raise AssertionError("test model only moves collapsed ranges")
        old = self.start
        position = max(0, min(len(self.model.value), old + count))
        self.start = position
        self.end = min(len(self.model.value), position + 1)
        return position - old

    def Select(self) -> None:
        self.model.selection = (self.start, self.end)


class _FakeSelectionArray:
    def __init__(self, selection: _FakeTextRange) -> None:
        self._selection = selection
        self.Length = 1

    def GetElement(self, index: int) -> _FakeTextRange:
        assert index == 0
        return self._selection


class _FakeTextPattern:
    def __init__(self, model: _FakeTextModel) -> None:
        self.model = model

    @property
    def DocumentRange(self) -> _FakeTextRange:
        return _FakeTextRange(self.model, 0, len(self.model.value))

    def GetSelection(self) -> _FakeSelectionArray:
        return _FakeSelectionArray(_FakeTextRange(self.model, *self.model.selection))


class _FakeValuePattern:
    def __init__(self, model: _FakeTextModel) -> None:
        self.model = model

    @property
    def CurrentValue(self) -> str:
        return self.model.value

    @property
    def CurrentIsReadOnly(self) -> bool:
        return self.model.read_only

    def SetValue(self, value: str) -> None:
        self.model.value = value
        self.model.selection = (0, 0)


class _FakeUIAElement:
    def __init__(
        self,
        *,
        role: str,
        title: str = "",
        automation_id: str = "",
        class_name: str = "",
        rect: _FakeRect | None = None,
        children: list["_FakeUIAElement"] | None = None,
        enabled: bool = True,
        visible: bool = True,
        editable: bool = False,
        handle: int | None = None,
        invokable: bool | None = None,
        clickable: bool = True,
        focusable: bool = True,
        process_id: int = 123,
        runtime_id: tuple[int, ...] | None = None,
        is_password: bool = False,
    ) -> None:
        self.element_info = self
        self.control_type = role
        self.name = title
        self.automation_id = automation_id
        self.class_name = class_name
        self.rectangle = rect
        self.handle = handle
        self.process_id = process_id
        self.enabled = enabled
        self.visible = visible
        self.CurrentIsPassword = is_password
        self._runtime_id = runtime_id or (7, process_id, id(self))
        self._children = children or []
        self._parent: _FakeUIAElement | None = None
        for child in self._children:
            child._parent = self
        self.invoked = False
        self.clicked = False
        self.focused = False
        self.window_actions: list[str] = []
        self.value = ""
        if invokable is None:
            invokable = role.lower() in {"button", "menuitem", "menu item", "checkbox", "radio button", "hyperlink"}
        if not invokable:
            self.invoke = None  # type: ignore[method-assign]
        if not clickable:
            self.click_input = None  # type: ignore[method-assign]
        if not focusable:
            self.set_focus = None  # type: ignore[method-assign]
        if editable:
            self.set_edit_text = self._set_edit_text  # type: ignore[method-assign]

    def children(self) -> list["_FakeUIAElement"]:
        return list(self._children)

    def parent(self) -> "_FakeUIAElement | None":
        return self._parent

    def top_level_parent(self) -> "_FakeUIAElement":
        element = self
        while element._parent is not None:
            element = element._parent
        return element

    def invoke(self) -> None:
        self.invoked = True

    def click_input(self) -> None:
        self.clicked = True

    def set_focus(self) -> None:
        self.focused = True

    def has_keyboard_focus(self) -> bool:
        return self.focused

    def GetRuntimeId(self) -> tuple[int, ...]:
        return self._runtime_id

    def _set_edit_text(self, value: str) -> None:
        self.value = value

    def restore(self) -> None:
        self.window_actions.append("restore")

    def show(self) -> None:
        self.window_actions.append("show")

    def minimize(self) -> None:
        self.window_actions.append("minimize")

    def maximize(self) -> None:
        self.window_actions.append("maximize")

    def close(self) -> None:
        self.window_actions.append("close")


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self._origin_x = 0
        self._origin_y = 0
        self._width = 1280
        self._height = 720
        self.uia_root_elements: list[_FakeUIAElement] = []
        self.active: dict[str, object] = {
            "hwnd": 100,
            "pid": 123,
            "title": "Document",
            "bounds": (0, 0, 640, 480),
        }
        self.focused_element: _FakeUIAElement | None = None
        self.native_value: str | None = None
        self.native_selection_value = (0, 0)
        self.native_is_editable = True
        self.native_is_protected = False
        self.normalize_native_replacement = None
        self.native_text_reads = 0

    def screen_geometry(self) -> ScreenGeometry:
        self.calls.append(("screen_geometry", tuple(), {}))
        return ScreenGeometry(
            origin_x=self._origin_x,
            origin_y=self._origin_y,
            width=self._width,
            height=self._height,
        )

    def screen_size(self) -> tuple[int, int]:
        self.calls.append(("screen_size", tuple(), {}))
        return self._width, self._height

    def capture_png(self) -> tuple[bytes, int, int]:
        self.calls.append(("capture_png", tuple(), {}))
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/5wAAAABJRU5ErkJggg=="
        )
        return png_bytes, 1, 1

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

    def uia_roots(self) -> list[_FakeUIAElement]:
        self.calls.append(("uia_roots", tuple(), {}))
        return list(self.uia_root_elements)

    def active_window(self) -> dict[str, object]:
        self.calls.append(("active_window", tuple(), {}))
        return dict(self.active)

    def focused_uia_element(self) -> _FakeUIAElement:
        self.calls.append(("focused_uia_element", tuple(), {}))
        assert self.focused_element is not None
        return self.focused_element

    @staticmethod
    def uia_elements_equal(left: object, right: object) -> bool:
        return left is right

    def native_text(self, hwnd: int, *, max_chars: int) -> str | None:
        self.calls.append(("native_text", (hwnd,), {"max_chars": max_chars}))
        self.native_text_reads += 1
        if self.native_value is None or len(self.native_value) > max_chars:
            return None
        return self.native_value

    def native_selection(self, hwnd: int) -> tuple[int, int]:
        self.calls.append(("native_selection", (hwnd,), {}))
        return self.native_selection_value

    def set_native_selection(self, hwnd: int, start: int, end: int) -> None:
        self.calls.append(("set_native_selection", (hwnd, start, end), {}))
        self.native_selection_value = (start, end)

    def replace_native_selection(self, hwnd: int, text: str) -> None:
        self.calls.append(("replace_native_selection", (hwnd, text), {}))
        assert self.native_value is not None
        start, end = self.native_selection_value
        start_index = _python_index_for_utf16(self.native_value, start)
        end_index = _python_index_for_utf16(self.native_value, end)
        replacement = text
        if callable(self.normalize_native_replacement):
            replacement = str(self.normalize_native_replacement(text))
        self.native_value = self.native_value[:start_index] + replacement + self.native_value[end_index:]
        caret = start + len(replacement.encode("utf-16-le")) // 2
        self.native_selection_value = (caret, caret)

    def native_editable(self, hwnd: int) -> bool:
        self.calls.append(("native_editable", (hwnd,), {}))
        return self.native_is_editable

    def native_protected(self, hwnd: int) -> bool:
        self.calls.append(("native_protected", (hwnd,), {}))
        return self.native_is_protected

    def focused_native_handle(self) -> int:
        return int(self.focused_element.handle or 0) if self.focused_element is not None else 0

    def type_unicode_text(self, text: str) -> None:
        self.calls.append(("type_unicode_text", (text,), {}))
        assert self.focused_element is not None
        model = self.focused_element.text_model
        start, end = model.selection
        replacement = model.normalize_input(text) if callable(model.normalize_input) else text
        model.value = model.value[:start] + replacement + model.value[end:]
        caret = start + len(replacement)
        model.selection = (caret, caret)

    def capture_window_png(
        self,
        *,
        hwnd: int,
        bounds: tuple[int, int, int, int],
    ) -> tuple[bytes, int, int]:
        self.calls.append(("capture_window_png", (hwnd, bounds), {}))
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/5wAAAABJRU5ErkJggg=="
        )
        return png_bytes, bounds[2] - bounds[0], bounds[3] - bounds[1]


def _start_tag_runtime(
    tmp_path: Path,
    driver: _FakeDriver,
) -> WindowsComputerUseRuntime:
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )
    return runtime


def _native_tag_runtime(
    tmp_path: Path,
    text: str,
    *,
    caret: int | None = None,
) -> tuple[WindowsComputerUseRuntime, _FakeDriver, _FakeUIAElement, _FakeUIAElement]:
    field = _FakeUIAElement(role="Edit", handle=101, runtime_id=(42, 101))
    field.focused = True
    window = _FakeUIAElement(
        role="Window",
        title="Document",
        class_name="Notepad",
        handle=100,
        runtime_id=(42, 100),
        children=[field],
    )
    driver = _FakeDriver()
    driver.uia_root_elements = [window]
    driver.focused_element = field
    driver.native_value = text
    native_caret = (
        len(text.encode("utf-16-le")) // 2
        if caret is None
        else caret
    )
    driver.native_selection_value = (native_caret, native_caret)
    return _start_tag_runtime(tmp_path, driver), driver, window, field


def _uia_tag_runtime(
    tmp_path: Path,
    text: str,
    *,
    caret: int | None = None,
) -> tuple[WindowsComputerUseRuntime, _FakeDriver, _FakeUIAElement, _FakeTextModel]:
    model = _FakeTextModel(text, len(text) if caret is None else caret)
    field = _FakeUIAElement(role="Edit", handle=None, runtime_id=(7, 123, 456))
    field.focused = True
    field.text_model = model
    field.iface_text = _FakeTextPattern(model)
    field.iface_value = _FakeValuePattern(model)
    window = _FakeUIAElement(
        role="Window",
        title="Document",
        class_name="WPF",
        handle=100,
        runtime_id=(7, 123, 100),
        children=[field],
    )
    driver = _FakeDriver()
    driver.uia_root_elements = [window]
    driver.focused_element = field
    return _start_tag_runtime(tmp_path, driver), driver, field, model


def test_windows_backend_spec_exports_expected_metadata() -> None:
    spec = WINDOWS_BACKEND_SPEC

    assert spec.backend_id == "windows"
    assert spec.backend_family == "windows"
    assert spec.interpreter_strategy == "current_python"
    assert Path(spec.helper_target).name == "runtime.py"
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("allow") is True
    assert "inline-png-capture" in spec.features
    assert "uia-automation" in spec.features
    assert "uia-tree-snapshot" in spec.features
    assert "uia-structural-targeting" in spec.features
    assert "uia-element-action" in spec.features
    assert "uia-window-management" in spec.features
    assert "native-window-list" in spec.features
    assert "window-state" in spec.features
    assert "element-index-targeting" in spec.features
    assert "background-dispatch" in spec.features
    assert "foreground-dispatch-fallback" in spec.features
    assert "global-pixel-actions" in spec.features
    assert "virtual-screen-coordinates" in spec.features
    assert "multi-monitor-virtual-screen" in spec.features
    assert "real-cursor-may-move" in spec.features
    assert "a0-tag" in spec.features


def test_windows_backend_wrapper_uses_current_python() -> None:
    backend = WindowsComputerUseBackend()

    assert backend.spec is WINDOWS_BACKEND_SPEC
    assert backend.helper_command()[0] == sys.executable
    assert backend.helper_command()[-1] == "--stdio"


def test_windows_key_aliases_use_pywinauto_left_windows_key() -> None:
    for key in ("WIN", "windows", "super", "meta", "cmd", "command"):
        assert windows_runtime_mod._format_key_sequence([key]) == "{LWIN}"
    assert windows_runtime_mod._format_key_sequence(["CTRL", "ESC"]) == (
        "{VK_CONTROL down}{ESC}{VK_CONTROL up}"
    )
    assert windows_runtime_mod._format_key_sequence(["ALT", "SHIFT", "TAB"]) == (
        "{VK_MENU down}{VK_SHIFT down}{TAB}{VK_SHIFT up}{VK_MENU up}"
    )


def test_windows_type_text_sends_literal_utf16_units(monkeypatch: pytest.MonkeyPatch) -> None:
    typed_units: list[int] = []
    submitted: list[tuple[str, float, bool]] = []

    class KeyAction:
        def __init__(self, character: str) -> None:
            self.character = character

        def run(self) -> None:
            typed_units.append(ord(self.character))

    keyboard = types.SimpleNamespace(
        KeyAction=KeyAction,
        send_keys=lambda sequence, *, pause, with_spaces: submitted.append(
            (sequence, pause, with_spaces)
        ),
    )
    monkeypatch.setattr(
        windows_runtime_mod,
        "_load_pywinauto_modules",
        lambda: (keyboard, None),
    )
    driver = object.__new__(windows_runtime_mod._WindowsDesktopAutomation)
    text = "19+23={}🙂"

    driver.type_text(text, submit=True)

    encoded = text.encode("utf-16-le")
    assert typed_units == [
        int.from_bytes(encoded[index : index + 2], "little")
        for index in range(0, len(encoded), 2)
    ]
    assert submitted == [("{ENTER}", 0.01, True)]


def test_windows_action_normalization_matches_shared_surface() -> None:
    move = normalize_action_payload("move", {"x": 0.25, "y": 0.75}, context_id="ctx-1")
    click = normalize_action_payload(
        "click",
        {"x": 0.4, "y": 0.6, "button": "right", "count": 2},
        context_id="ctx-1",
    )
    scroll = normalize_action_payload("scroll", {"dx": 1, "dy": -2}, context_id="ctx-1")
    keys = normalize_action_payload("key", {"key": "ctrl+alt+t"}, context_id="ctx-1")
    typed = normalize_action_payload("type", {"text": "hello", "submit": True}, context_id="ctx-1")
    uia_snapshot = normalize_action_payload(
        "uia_snapshot",
        {"max_depth": 3, "max_nodes": 50},
        context_id="ctx-1",
    )
    uia_action = normalize_action_payload(
        "uia_action",
        {
            "target": {"role": "Button", "title": "Save"},
            "selector": "role:Button && name:Save",
            "operation": "invoke",
        },
        context_id="ctx-1",
    )
    window_state = normalize_action_payload(
        "get_window_state",
        {"pid": "1234", "window_id": "uia-hwnd:5678", "max_depth": 2, "max_nodes": 25},
        context_id="ctx-1",
    )
    element_action = normalize_action_payload(
        "element_action",
        {
            "window_id": "uia-hwnd:5678",
            "element_index": "3",
            "operation": "invoke",
            "dispatch": "background",
        },
        context_id="ctx-1",
    )

    assert move["x"] == 0.25 and move["y"] == 0.75
    assert click["button"] == "right" and click["count"] == 2
    assert scroll["dx"] == 1 and scroll["dy"] == -2
    assert keys["keys"] == ["ctrl", "alt", "t"]
    assert typed["text"] == "hello" and typed["submit"] is True
    assert uia_snapshot["max_depth"] == 3 and uia_snapshot["max_nodes"] == 50
    assert uia_action["target"]["title"] == "Save"
    assert uia_action["target"]["selector"] == "role:Button && name:Save"
    assert uia_action["operation"] == "invoke"
    assert window_state["pid"] == 1234
    assert window_state["window_id"] == "uia-hwnd:5678"
    assert window_state["max_depth"] == 2
    assert window_state["max_nodes"] == 25
    assert element_action["window_id"] == "uia-hwnd:5678"
    assert element_action["element_index"] == 3
    assert element_action["dispatch"] == "background"


def test_windows_runtime_rejects_allow_without_restore_token(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.start_session({"context_id": "ctx-1", "trust_mode": "allow"})

    assert exc_info.value.code == "COMPUTER_USE_REARM_REQUIRED"


def test_windows_runtime_session_policies_are_persisted_when_valid(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
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
    stored = WindowsSessionStore(state_dir=tmp_path / "state").get("ctx-1")
    assert stored is not None
    assert stored.restore_token == restore_token


def test_windows_runtime_interactive_sessions_are_fresh_each_time(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")

    first = runtime.start_session({"context_id": "ctx-1", "trust_mode": "interactive"})
    runtime.stop_session({"context_id": "ctx-1"})
    second = runtime.start_session({"context_id": "ctx-1", "trust_mode": "interactive"})

    assert first["session_id"] != second["session_id"]
    assert "restore_token" not in first
    assert "restore_token" not in second


def test_windows_runtime_capture_returns_inline_png_payload(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
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
    assert capture["origin_x"] == 0
    assert capture["origin_y"] == 0
    assert capture["png_base64"]
    assert base64.b64decode(capture["png_base64"])


def test_windows_runtime_capture_writes_requested_path_without_inline_payload(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
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


def test_windows_desktop_automation_prefers_dxcam_numpy_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeCamera:
        def grab(self):
            return np.zeros((1, 1, 4), dtype=np.uint8)

    def create(**kwargs):
        calls.append(dict(kwargs))
        return FakeCamera()

    automation = windows_runtime_mod._WindowsDesktopAutomation.__new__(windows_runtime_mod._WindowsDesktopAutomation)
    automation._camera = None
    automation.screen_geometry = lambda: ScreenGeometry(origin_x=0, origin_y=0, width=1, height=1)
    automation._capture_all_screens_png = lambda _geometry: None
    monkeypatch.setattr(windows_runtime_mod, "_load_dxcam_module", lambda: types.SimpleNamespace(create=create))

    png_bytes, width, height, origin_x, origin_y = automation.capture_png()

    assert png_bytes
    assert (width, height) == (1, 1)
    assert (origin_x, origin_y) == (0, 0)
    assert calls == [{"output_idx": 0, "processor_backend": "numpy"}]


def test_windows_runtime_uia_snapshot_returns_bounded_structural_tree(tmp_path: Path) -> None:
    driver = _FakeDriver()
    driver._origin_x = -100
    driver._origin_y = -50
    driver._width = 1400
    driver._height = 900
    save_button = _FakeUIAElement(
        role="Button",
        title="Save",
        automation_id="save-button",
        rect=_FakeRect(100, 200, 80, 30),
    )
    text_field = _FakeUIAElement(
        role="Edit",
        title="File name",
        automation_id="file-name",
        rect=_FakeRect(200, 300, 240, 24),
        editable=True,
    )
    window = _FakeUIAElement(
        role="Window",
        title="Document",
        class_name="FakeWindow",
        rect=_FakeRect(0, 100, 800, 600),
        children=[save_button, text_field],
    )
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    snapshot = runtime.uia_snapshot({"context_id": "ctx-1", "max_depth": 3, "max_nodes": 10})

    assert snapshot["app"]["name"] == "Windows desktop"
    assert snapshot["node_count"] == 4
    assert snapshot["truncated"] is False
    tree = snapshot["tree"]
    assert tree["role"] == "Desktop"
    window_node = tree["children"][0]
    assert window_node["path"] == [0]
    assert window_node["role"] == "Window"
    assert window_node["title"] == "Document"
    assert window_node["actions"] == ["focus_window", "minimize", "restore", "maximize"]
    button_node = window_node["children"][0]
    assert button_node["path"] == [0, 0]
    assert button_node["role"] == "Button"
    assert button_node["automation_id"] == "save-button"
    assert button_node["actions"] == ["invoke", "focus"]
    assert button_node["selector"] == "role:Button && id:save-button && name:Save"
    assert button_node["frame"]["normalized"]["x"] == round((100 - (-100)) / 1400, 6)


def test_windows_runtime_uia_action_invokes_semantic_target(tmp_path: Path) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save", automation_id="save-button")
    window = _FakeUIAElement(role="Window", title="Document", children=[button])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "target": {"role": "Button", "title": "Save"},
            "operation": "invoke",
        }
    )

    assert result["operation"] == "invoke"
    assert result["target"]["path"] == [0, 0]
    assert result["target"]["automation_id"] == "save-button"
    assert button.invoked is True
    assert button.clicked is False
    assert window.window_actions == ["restore"]


def test_windows_runtime_window_state_indexes_elements_for_background_actions(tmp_path: Path) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save", automation_id="save-button")
    window = _FakeUIAElement(
        role="Window",
        title="Document",
        class_name="FakeWindow",
        handle=123456,
        children=[button],
    )
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
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
    result = runtime.element_action(
        {
            "context_id": "ctx-1",
            "window_id": state["window_id"],
            "element_index": button_index,
            "operation": "invoke",
            "dispatch": "background",
        }
    )

    assert windows["windows"][0]["window_id"] == "uia-hwnd:123456"
    assert state["tree"]["element_index"] == 0
    assert button_index == 1
    assert result["actual_dispatch"] == "background"
    assert result["background_unavailable"] is False
    assert button.invoked is True
    assert window.window_actions == []


def test_windows_runtime_list_windows_honors_visibility_filters(tmp_path: Path) -> None:
    driver = _FakeDriver()
    visible = _FakeUIAElement(role="Window", title="Visible", rect=_FakeRect(10, 10, 100, 100))
    hidden = _FakeUIAElement(
        role="Window",
        title="Hidden",
        rect=_FakeRect(20, 20, 100, 100),
        visible=False,
    )
    offscreen = _FakeUIAElement(role="Window", title="Offscreen", rect=_FakeRect(2000, 20, 100, 100))
    driver.uia_root_elements = [visible, hidden, offscreen]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    default_titles = [item["title"] for item in runtime.list_windows({"context_id": "ctx-1"})["windows"]]
    hidden_titles = [
        item["title"]
        for item in runtime.list_windows({"context_id": "ctx-1", "include_hidden": True})["windows"]
    ]
    offscreen_titles = [
        item["title"]
        for item in runtime.list_windows({"context_id": "ctx-1", "include_offscreen": True})["windows"]
    ]
    all_titles = [
        item["title"]
        for item in runtime.list_windows(
            {"context_id": "ctx-1", "include_hidden": True, "include_offscreen": True}
        )["windows"]
    ]

    assert default_titles == ["Visible"]
    assert hidden_titles == ["Visible", "Hidden"]
    assert offscreen_titles == ["Visible", "Offscreen"]
    assert all_titles == ["Visible", "Hidden", "Offscreen"]


def test_windows_runtime_background_click_reports_unavailable_and_auto_falls_back(
    tmp_path: Path,
) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save", automation_id="save-button")
    window = _FakeUIAElement(role="Window", title="Document", children=[button])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )
    state = runtime.get_window_state({"context_id": "ctx-1", "max_depth": 2})
    button_index = state["tree"]["children"][0]["element_index"]

    background_only = runtime.element_action(
        {
            "context_id": "ctx-1",
            "element_index": button_index,
            "operation": "click",
            "dispatch": "background",
        }
    )
    assert background_only["background_unavailable"] is True
    assert button.clicked is False

    auto = runtime.element_action(
        {
            "context_id": "ctx-1",
            "element_index": button_index,
            "operation": "click",
            "dispatch": "auto",
        }
    )

    assert button.clicked is True
    assert auto["actual_dispatch"] == "foreground"
    assert auto["foreground_fallback_used"] is True
    assert window.window_actions == ["restore"]


def test_windows_runtime_uia_action_matches_terminator_style_selector(tmp_path: Path) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save As", automation_id="save-as-button")
    window = _FakeUIAElement(role="Window", title="Document", children=[button])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "target": {"selector": 'role:Button && name:"Save As"'},
            "operation": "invoke",
        }
    )

    assert result["target"]["path"] == [0, 0]
    assert button.invoked is True


def test_windows_runtime_uia_invoke_does_not_fallback_to_click(tmp_path: Path) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save", invokable=False, clickable=True)
    window = _FakeUIAElement(role="Window", title="Document", children=[button])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.uia_action(
            {
                "context_id": "ctx-1",
                "target": {"role": "Button", "title": "Save"},
                "operation": "invoke",
            }
        )

    assert exc_info.value.code == "COMPUTER_USE_UIA_ACTION_UNSUPPORTED"
    assert button.clicked is False


def test_windows_runtime_uia_action_minimizes_owning_window_structurally(tmp_path: Path) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save")
    window = _FakeUIAElement(role="Window", title="Document", children=[button])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "path": [0, 0],
            "operation": "hide",
        }
    )

    assert result["operation"] == "minimize"
    assert window.window_actions == ["minimize"]


def test_windows_runtime_uia_set_value_focuses_owner_before_typing_fallback(tmp_path: Path) -> None:
    driver = _FakeDriver()
    text_field = _FakeUIAElement(role="Edit", title="File name", automation_id="file-name")
    window = _FakeUIAElement(role="Window", title="Document", children=[text_field])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "path": [0, 0],
            "operation": "set_value",
            "value": "final.txt",
        }
    )

    assert result["operation"] == "set_value"
    assert window.window_actions == ["restore", "restore"]
    assert window.focused is True
    assert text_field.focused is True
    assert ("type_text", ("final.txt",), {"submit": False}) in driver.calls


def test_windows_runtime_uia_action_sets_value_by_path(tmp_path: Path) -> None:
    driver = _FakeDriver()
    text_field = _FakeUIAElement(role="Edit", title="File name", automation_id="file-name", editable=True)
    window = _FakeUIAElement(role="Window", title="Document", children=[text_field])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "path": [0, 0],
            "operation": "set_value",
            "value": "final.txt",
        }
    )

    assert result["operation"] == "set_value"
    assert result["target"]["role"] == "Edit"
    assert text_field.value == "final.txt"


def test_windows_runtime_uia_action_rejects_ambiguous_targets(tmp_path: Path) -> None:
    driver = _FakeDriver()
    window = _FakeUIAElement(
        role="Window",
        title="Document",
        children=[
            _FakeUIAElement(role="Button", title="Save"),
            _FakeUIAElement(role="Button", title="Cancel"),
        ],
    )
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.uia_action({"context_id": "ctx-1", "target": {"role": "Button"}})

    assert exc_info.value.code == "COMPUTER_USE_UIA_TARGET_AMBIGUOUS"


def test_windows_runtime_uses_virtual_screen_origin_for_normalized_actions(tmp_path: Path) -> None:
    driver = _FakeDriver()
    driver._origin_x = -1920
    driver._origin_y = -120
    driver._width = 3200
    driver._height = 1200
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    session = runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    moved = runtime.move({"context_id": "ctx-1", "x": 0.25, "y": 0.5})
    clicked = runtime.click({"context_id": "ctx-1", "x": 1.0, "y": 0.0, "button": "left", "count": 1})

    assert session["origin_x"] == -1920
    assert session["origin_y"] == -120
    assert moved["pixel_x"] == -1120
    assert moved["pixel_y"] == 480
    assert clicked["pixel_x"] == 1280
    assert clicked["pixel_y"] == -120
    assert ("move", (-1120.0, 480.0), {}) in driver.calls
    assert ("click", (1280.0, -120.0), {"button": "left", "count": 1}) in driver.calls


def test_windows_runtime_normalizes_actions_and_routes_input(tmp_path: Path) -> None:
    driver = _FakeDriver()
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
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
    runtime.key({"context_id": "ctx-1", "keys": ["ctrl", "alt", "t"]})
    runtime.type_text({"context_id": "ctx-1", "text": "hello", "submit": True})

    setup_calls = {"screen_geometry", "screen_size"}
    assert [call[0] for call in driver.calls if call[0] not in setup_calls] == [
        "move",
        "click",
        "scroll",
        "key",
        "type_text",
    ]


def test_windows_a0_tag_native_edit_replaces_exact_unicode_range(
    tmp_path: Path,
) -> None:
    prefix = "🙂 café before\r\n  "
    tag = "@a0 draft naïve 🌟"
    trailing = " \t"
    suffix = "\r\nafter 世界"
    caret = len((prefix + tag + trailing).encode("utf-16-le")) // 2
    runtime, driver, _window, _field = _native_tag_runtime(
        tmp_path,
        prefix + tag + trailing + suffix,
        caret=caret,
    )

    captured = runtime.tag_context({"context_id": "ctx-1"})
    replaced = runtime.tag_replace(
        {
            "context_id": "ctx-1",
            "target_token": captured["target_token"],
            "replacement": "Réponse 終 ✨",
        }
    )

    expected = prefix + "Réponse 終 ✨" + trailing + suffix
    expected_caret = len((prefix + "Réponse 終 ✨" + trailing).encode("utf-16-le")) // 2
    assert captured["query"] == "draft naïve 🌟"
    assert captured["tag_text"] == tag
    assert captured["replace_supported"] is True
    assert captured["screenshot_status"] == "attached"
    assert base64.b64decode(captured["artifact"]["data"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert replaced["replaced"] is True
    assert driver.native_value == expected
    assert driver.native_selection_value == (expected_caret, expected_caret)


def test_windows_a0_tag_uia_textpattern_replaces_exact_profile_range(
    tmp_path: Path,
) -> None:
    prefix = "before 🙂\r\n\t"
    tag = "@A0.developer review café 🌟"
    trailing = "  "
    suffix = "\r\nafter 世界"
    runtime, _driver, _field, model = _uia_tag_runtime(
        tmp_path,
        prefix + tag + trailing + suffix,
        caret=len(prefix + tag + trailing),
    )

    captured = runtime.tag_context({"context_id": "ctx-1"})
    runtime.tag_replace(
        {
            "context_id": "ctx-1",
            "target_token": captured["target_token"],
            "replacement": "Reviewed ✅\nsecond line",
        }
    )

    expected = prefix + "Reviewed ✅\nsecond line" + trailing + suffix
    expected_caret = len(prefix + "Reviewed ✅\nsecond line" + trailing)
    assert captured["profile_override"] == "developer"
    assert captured["query"] == "review café 🌟"
    assert captured["replace_supported"] is True
    assert model.value == expected
    assert model.selection == (expected_caret, expected_caret)


def test_windows_a0_tag_rejects_protected_field_before_text_tree_or_screenshot(
    tmp_path: Path,
) -> None:
    runtime, driver, _window, _field = _native_tag_runtime(tmp_path, "@a0 reveal this")
    driver.native_is_protected = True

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.tag_context({"context_id": "ctx-1"})

    assert exc_info.value.code == "A0_TAG_PROTECTED_FIELD"
    assert driver.native_text_reads == 0
    assert not any(call[0] in {"uia_roots", "capture_window_png"} for call in driver.calls)


def test_windows_a0_tag_failed_context_does_not_retain_private_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _driver, _window, _field = _native_tag_runtime(tmp_path, "@a0 summarize this")

    def fail_screenshot(*_args: Any, **_kwargs: Any) -> tuple[str, str, dict[str, str] | None]:
        raise WindowsComputerUseError("A0_TAG_TARGET_CHANGED", "focus changed")

    monkeypatch.setattr(runtime, "_tag_window_screenshot", fail_screenshot)

    with pytest.raises(WindowsComputerUseError):
        runtime.tag_context({"context_id": "ctx-1"})

    assert runtime._tag_target is None


@pytest.mark.parametrize("change", ["process", "hwnd", "element", "value", "caret"])
def test_windows_a0_tag_revalidates_process_hwnd_element_value_and_caret(
    tmp_path: Path,
    change: str,
) -> None:
    original = "@a0 keep the exact target"
    runtime, driver, _window, field = _native_tag_runtime(tmp_path, original)
    captured = runtime.tag_context({"context_id": "ctx-1"})

    if change == "process":
        driver.active["pid"] = 456
    elif change == "hwnd":
        driver.active["hwnd"] = 999
    elif change == "element":
        field._runtime_id = (42, 999)
    elif change == "value":
        driver.native_value = "@a0 changed by the user"
    else:
        driver.native_selection_value = (0, 0)

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.tag_replace(
            {
                "context_id": "ctx-1",
                "target_token": captured["target_token"],
                "replacement": "unsafe",
            }
        )

    assert exc_info.value.code in {"A0_TAG_FOCUS_UNAVAILABLE", "A0_TAG_TARGET_CHANGED"}
    assert "unsafe" not in str(driver.native_value)


@pytest.mark.parametrize("mode", ["native", "uia"])
def test_windows_a0_tag_rolls_back_editor_normalization(
    tmp_path: Path,
    mode: str,
) -> None:
    original = "intro\r\n@a0 preserve café — 🌟  \r\noutro"
    caret_text = "intro\r\n@a0 preserve café — 🌟  "
    if mode == "native":
        runtime, driver, _window, _field = _native_tag_runtime(
            tmp_path,
            original,
            caret=len(caret_text.encode("utf-16-le")) // 2,
        )
        driver.normalize_native_replacement = lambda value: value.upper() if value == "mixed Case" else value
        get_value = lambda: driver.native_value
        get_caret = lambda: driver.native_selection_value
        expected_caret = (len(caret_text.encode("utf-16-le")) // 2,) * 2
    else:
        runtime, driver, _field, model = _uia_tag_runtime(
            tmp_path,
            original,
            caret=len(caret_text),
        )
        model.normalize_input = lambda value: value.upper() if value == "mixed Case" else value
        get_value = lambda: model.value
        get_caret = lambda: model.selection
        expected_caret = (len(caret_text),) * 2
    captured = runtime.tag_context({"context_id": "ctx-1"})

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.tag_replace(
            {
                "context_id": "ctx-1",
                "target_token": captured["target_token"],
                "replacement": "mixed Case",
            }
        )

    assert exc_info.value.code == "A0_TAG_REPLACE_FAILED"
    assert get_value() == original
    assert get_caret() == expected_caret


def test_windows_a0_tag_release_expiry_noneditable_and_teardown(
    tmp_path: Path,
) -> None:
    runtime, driver, _window, _field = _native_tag_runtime(tmp_path, "@a0 summarize this")
    driver.native_is_editable = False
    captured = runtime.tag_context({"context_id": "ctx-1"})
    assert captured["replace_supported"] is False
    with pytest.raises(WindowsComputerUseError) as wrong:
        runtime.tag_replace(
            {"context_id": "ctx-1", "target_token": "wrong", "replacement": "answer"}
        )
    assert wrong.value.code == "A0_TAG_TARGET_EXPIRED"
    with pytest.raises(WindowsComputerUseError) as unsupported:
        runtime.tag_replace(
            {
                "context_id": "ctx-1",
                "target_token": captured["target_token"],
                "replacement": "answer",
            }
        )
    assert unsupported.value.code == "A0_TAG_REPLACE_UNSUPPORTED"
    assert runtime.tag_release({"context_id": "ctx-1", "target_token": "wrong"}) == {
        "released": False
    }
    assert runtime.tag_release(
        {"context_id": "ctx-1", "target_token": captured["target_token"]}
    ) == {"released": True}

    driver.native_is_editable = True
    captured = runtime.tag_context({"context_id": "ctx-1"})
    assert runtime._tag_target is not None
    runtime._tag_target.captured_at -= 16 * 60
    with pytest.raises(WindowsComputerUseError) as expired:
        runtime.tag_replace(
            {
                "context_id": "ctx-1",
                "target_token": captured["target_token"],
                "replacement": "answer",
            }
        )
    assert expired.value.code == "A0_TAG_TARGET_EXPIRED"
    assert runtime._tag_target is None

    runtime.tag_context({"context_id": "ctx-1"})
    runtime.stop_session({"context_id": "ctx-1"})
    assert runtime._tag_target is None


@pytest.mark.parametrize(
    ("text", "caret_text", "code"),
    [
        ("@a0   ", None, "A0_TAG_EMPTY_QUERY"),
        ("@a0 " + "x" * 2049, None, "A0_TAG_QUERY_TOO_LONG"),
        ("@a0 draft more text", "@a0 draft", "A0_TAG_CARET_POSITION"),
    ],
)
def test_windows_a0_tag_rejects_invalid_or_unbounded_invocations(
    tmp_path: Path,
    text: str,
    caret_text: str | None,
    code: str,
) -> None:
    caret = None if caret_text is None else len(caret_text.encode("utf-16-le")) // 2
    runtime, _driver, _window, _field = _native_tag_runtime(tmp_path, text, caret=caret)

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.tag_context({"context_id": "ctx-1"})

    assert exc_info.value.code == code


def test_windows_a0_tag_never_uses_desktop_capture_for_unverified_window_bounds(
    tmp_path: Path,
) -> None:
    runtime, driver, _window, _field = _native_tag_runtime(tmp_path, "@a0 summarize this")
    driver.active["bounds"] = (-20, 0, 640, 480)

    captured = runtime.tag_context({"context_id": "ctx-1"})

    assert captured["screenshot_status"] == "unavailable"
    assert "verified on-screen" in captured["screenshot_error"]
    assert "artifact" not in captured
    assert not any(call[0] in {"capture_png", "capture_window_png"} for call in driver.calls)


def test_windows_a0_tag_private_actions_do_not_leak_into_public_surface() -> None:
    from agent_zero_cli import computer_use as computer_use_mod

    assert {"tag_context", "tag_replace", "tag_release"}.isdisjoint(
        computer_use_mod._SUPPORTED_ACTIONS
    )


def test_windows_stdio_accepts_private_tag_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _driver, _window, _field = _native_tag_runtime(tmp_path, "@a0 summarize this")
    requests = "\n".join(
        [
            json.dumps({"request_id": "start-1", "action": "start_session", "context_id": "ctx-1"}),
            json.dumps({"request_id": "tag-1", "action": "tag_context", "context_id": "ctx-1"}),
            json.dumps({"request_id": "stop-1", "action": "shutdown"}),
            "",
        ]
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(requests))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    assert windows_runtime_mod.serve_stdio(runtime) == 0

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["ok"] is True
    assert responses[1]["ok"] is True
    assert responses[1]["result"]["query"] == "summarize this"


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop support probe is Windows-only")
def test_windows_support_probe_is_true_when_dependencies_exist() -> None:
    # This is a smoke check for the real Windows path; it stays skipped on Linux.
    assert WINDOWS_BACKEND_SPEC.detect() is True
