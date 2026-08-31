from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


PACKAGE_SRC = Path(__file__).resolve().parents[1] / "packages/a0-computer-use-wayland/src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from a0_computer_use_wayland import WAYLAND_BACKEND_SPEC, get_backend_spec
from a0_computer_use_wayland import detection as wayland_detection
from a0_computer_use_wayland import paths as wayland_paths


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAYLAND_HELPER_FILES = [
    PROJECT_ROOT / "src" / "agent_zero_cli" / "computer_use_helper.py",
    PROJECT_ROOT
    / "packages"
    / "a0-computer-use-wayland"
    / "src"
    / "a0_computer_use_wayland"
    / "computer_use_helper.py",
]


class _FakeInt(int):
    def __new__(cls, value: object = 0, *args: object, **kwargs: object) -> "_FakeInt":
        del args, kwargs
        return int.__new__(cls, int(value))


class _FakeFloat(float):
    def __new__(cls, value: object = 0.0, *args: object, **kwargs: object) -> "_FakeFloat":
        del args, kwargs
        return float.__new__(cls, float(value))


class _FakeStr(str):
    def __new__(cls, value: object = "", *args: object, **kwargs: object) -> "_FakeStr":
        del args, kwargs
        return str.__new__(cls, str(value))


class _FakeArray(list):
    def __init__(self, value: object = (), *args: object, **kwargs: object) -> None:
        del args, kwargs
        super().__init__(value)


class _FakeDictionary(dict):
    def __init__(self, value: object = (), *args: object, **kwargs: object) -> None:
        del args, kwargs
        super().__init__(value)


class _FakeStruct(tuple):
    def __new__(cls, value: object = (), *args: object, **kwargs: object) -> "_FakeStruct":
        del args, kwargs
        return tuple.__new__(cls, value)


class _FakeGdk:
    _KEYVALS = {
        "Alt_L": 0xFFE9,
        "BackSpace": 0xFF08,
        "Control_L": 0xFFE3,
        "Delete": 0xFFFF,
        "Down": 0xFF54,
        "Escape": 0xFF1B,
        "Left": 0xFF51,
        "Page_Down": 0xFF56,
        "Page_Up": 0xFF55,
        "Return": 0xFF0D,
        "Right": 0xFF53,
        "Shift_L": 0xFFE1,
        "Super_L": 0xFFEB,
        "Tab": 0xFF09,
        "Up": 0xFF52,
        "XF86AudioMute": 0x1008FF12,
        "space": 0x20,
    }

    @staticmethod
    def unicode_to_keyval(value: int) -> int:
        return value

    @classmethod
    def keyval_from_name(cls, name: str) -> int:
        return cls._KEYVALS.get(name, 0)


class _FakeRemoteDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    def NotifyKeyboardKeycode(
        self,
        session_handle: object,
        options: object,
        keycode: object,
        state: object,
    ) -> None:
        del options
        self.calls.append(("keycode", str(session_handle), int(keycode), int(state)))

    def NotifyKeyboardKeysym(
        self,
        session_handle: object,
        options: object,
        keysym: object,
        state: object,
    ) -> None:
        del options
        self.calls.append(("keysym", str(session_handle), int(keysym), int(state)))


class _FakeAtspiExtents:
    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class _FakeAtspiStateSet:
    def __init__(self, states: set[str]) -> None:
        self._states = states

    def contains(self, state: str) -> bool:
        return state in self._states


class _FakeAtspiAccessible:
    def __init__(
        self,
        *,
        role: str,
        name: str = "",
        description: str = "",
        children: list["_FakeAtspiAccessible"] | None = None,
        actions: list[str] | None = None,
        states: set[str] | None = None,
        frame: tuple[int, int, int, int] | None = None,
        text: str = "",
        caret_offset: int | None = None,
        value: float | None = None,
        pid: int = 123,
    ) -> None:
        self.role = role
        self.name = name
        self.description = description
        self.children = children or []
        self.actions = actions or []
        self.states = states or {"VISIBLE", "SHOWING", "ENABLED"}
        self.frame = frame
        self.text = text
        self.caret_offset = len(text) if caret_offset is None else caret_offset
        self.value = value
        self.pid = pid
        self.performed_actions: list[int] = []
        self.focused = False
        self.insert_text_lengths: list[int] = []
        self.set_text_values: list[str] = []
        self.set_numeric_values: list[float] = []

    def get_name(self) -> str:
        return self.name

    def get_role_name(self) -> str:
        return self.role

    def get_description(self) -> str:
        return self.description

    def get_process_id(self) -> int:
        return self.pid

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int) -> "_FakeAtspiAccessible":
        return self.children[index]

    def get_extents(self, coord_type: object) -> _FakeAtspiExtents | None:
        del coord_type
        if self.frame is None:
            return None
        return _FakeAtspiExtents(*self.frame)

    def get_state_set(self) -> _FakeAtspiStateSet:
        return _FakeAtspiStateSet(self.states)

    def get_n_actions(self) -> int:
        return len(self.actions)

    def get_action_name(self, index: int) -> str:
        return self.actions[index]

    def get_action_description(self, index: int) -> str:
        return f"{self.actions[index]} action"

    def get_key_binding(self, index: int) -> str:
        del index
        return ""

    def do_action(self, index: int) -> bool:
        self.performed_actions.append(index)
        return True

    def grab_focus(self) -> bool:
        self.focused = True
        self.states.add("FOCUSED")
        return True

    def get_character_count(self) -> int:
        return len(self.text)

    def get_text(self, start: int, end: int) -> str:
        return self.text[start:end]

    def get_caret_offset(self) -> int:
        return self.caret_offset

    def delete_text(self, start: int, end: int) -> bool:
        self.text = self.text[:start] + self.text[end:]
        self.caret_offset = start
        return True

    def insert_text(self, position: int, text: str, length: int) -> bool:
        self.insert_text_lengths.append(length)
        value = text.encode("utf-8")[:length].decode("utf-8", errors="ignore")
        self.text = self.text[:position] + value + self.text[position:]
        self.caret_offset = position + len(value)
        return True

    def set_text_contents(self, value: str) -> bool:
        self.set_text_values.append(value)
        self.text = value
        return True

    def get_current_value(self) -> float | None:
        return self.value

    def set_current_value(self, value: float) -> bool:
        self.set_numeric_values.append(value)
        self.value = value
        return True


class _FakeAtspi:
    CoordType = types.SimpleNamespace(SCREEN="screen")
    StateType = types.SimpleNamespace(
        ACTIVE="ACTIVE",
        CHECKED="CHECKED",
        EDITABLE="EDITABLE",
        ENABLED="ENABLED",
        EXPANDED="EXPANDED",
        FOCUSED="FOCUSED",
        FOCUSABLE="FOCUSABLE",
        PRESSED="PRESSED",
        PROTECTED="PROTECTED",
        SELECTED="SELECTED",
        SHOWING="SHOWING",
        VISIBLE="VISIBLE",
    )
    desktop: _FakeAtspiAccessible | None = None

    @classmethod
    def get_desktop(cls, index: int) -> _FakeAtspiAccessible:
        assert index == 0
        assert cls.desktop is not None
        return cls.desktop


def _install_wayland_helper_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAtspi.desktop = None
    dbus_mod = types.ModuleType("dbus")
    for name in (
        "Boolean",
        "Byte",
        "Int16",
        "Int32",
        "Int64",
        "UInt16",
        "UInt32",
        "UInt64",
    ):
        setattr(dbus_mod, name, _FakeInt)
    dbus_mod.Double = _FakeFloat
    dbus_mod.String = _FakeStr
    dbus_mod.ObjectPath = _FakeStr
    dbus_mod.Signature = _FakeStr
    dbus_mod.Array = _FakeArray
    dbus_mod.Dictionary = _FakeDictionary
    dbus_mod.Struct = _FakeStruct
    dbus_mod.Interface = lambda obj, _iface=None: obj
    dbus_mod.SessionBus = object

    dbus_mainloop_mod = types.ModuleType("dbus.mainloop")
    dbus_glib_mod = types.ModuleType("dbus.mainloop.glib")
    dbus_glib_mod.DBusGMainLoop = lambda *args, **kwargs: None

    gi_mod = types.ModuleType("gi")
    gi_mod.require_version = lambda *args, **kwargs: None
    gi_repository_mod = types.ModuleType("gi.repository")
    gi_repository_mod.Gdk = _FakeGdk
    gi_repository_mod.GLib = types.SimpleNamespace(MainLoop=object)
    gi_repository_mod.Gst = types.SimpleNamespace(init=lambda *args, **kwargs: None)
    gi_repository_mod.Atspi = _FakeAtspi

    monkeypatch.setitem(sys.modules, "dbus", dbus_mod)
    monkeypatch.setitem(sys.modules, "dbus.mainloop", dbus_mainloop_mod)
    monkeypatch.setitem(sys.modules, "dbus.mainloop.glib", dbus_glib_mod)
    monkeypatch.setitem(sys.modules, "gi", gi_mod)
    monkeypatch.setitem(sys.modules, "gi.repository", gi_repository_mod)


@pytest.fixture(params=WAYLAND_HELPER_FILES, ids=("cli-helper", "package-helper"))
def wayland_helper_module(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    _install_wayland_helper_stubs(monkeypatch)
    helper_path = Path(request.param)
    module_name = f"_a0_test_wayland_helper_{abs(hash(helper_path))}"
    spec = importlib.util.spec_from_file_location(module_name, helper_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _portal_helper(module):
    helper = module.PortalComputerUseHelper.__new__(module.PortalComputerUseHelper)
    remote_desktop = _FakeRemoteDesktop()
    helper._remote_desktop = remote_desktop
    helper._element_index_cache = {}
    helper._tag_target = None
    helper._session = module.PortalSession(
        context_id="ctx-1",
        trust_mode="persistent",
        session_id="sess-1",
        session_handle="/org/freedesktop/portal/desktop/session/a0/test",
        stream_id=1,
        width=1920,
        height=1080,
        devices=3,
        restore_token="",
        capture_stream=None,
    )
    return helper, remote_desktop


class _FakeCaptureStream:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, tuple[int, int, int, int] | None]] = []

    def capture_png(
        self,
        output_path: str | None = None,
        *,
        crop: tuple[int, int, int, int] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.calls.append((output_path, crop))
        return {"capture_path": output_path or "", "width": 800, "height": 600}


def test_wayland_backend_spec_exposes_expected_metadata() -> None:
    spec = get_backend_spec()

    assert spec is WAYLAND_BACKEND_SPEC
    assert spec.backend_id == "wayland"
    assert spec.backend_family == "linux"
    assert spec.priority == 100
    assert spec.interpreter_strategy == "system_python"
    assert spec.helper_target == str(wayland_paths.HELPER_SCRIPT)
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("allow") is True
    assert "portal-remote-desktop" in spec.features
    assert "inline-png-capture" in spec.features
    assert "fresh-frame-capture" in spec.features
    assert "global-pixel-actions" in spec.features
    assert "atspi-tree-snapshot" in spec.features
    assert "atspi-structural-targeting" in spec.features
    assert "native-window-list" in spec.features
    assert "window-state" in spec.features
    assert "window-scoped-tree-snapshot" in spec.features
    assert "verified-window-focus" in spec.features
    assert "target-verified-keyboard-input" in spec.features
    assert "element-index-targeting" in spec.features
    assert "background-dispatch" in spec.features
    assert "foreground-dispatch-fallback" in spec.features
    assert "real-cursor-may-move" in spec.features
    assert "a0-tag" in spec.features


def test_wayland_a0_tag_captures_and_replaces_exact_focused_span(
    wayland_helper_module,
    tmp_path: Path,
) -> None:
    field = _FakeAtspiAccessible(
        role="text",
        name="Message",
        states={"VISIBLE", "SHOWING", "ENABLED", "FOCUSABLE", "FOCUSED", "EDITABLE"},
        frame=(30, 80, 500, 80),
        text="🙂 intro\n  @A0.developer draft a reply",
    )
    window = _FakeAtspiAccessible(
        role="frame",
        name="Notes",
        states={"VISIBLE", "SHOWING", "ENABLED", "ACTIVE"},
        frame=(10, 20, 800, 600),
        children=[field],
    )
    app = _FakeAtspiAccessible(role="application", name="Text Editor", children=[window])
    _FakeAtspi.desktop = _FakeAtspiAccessible(role="desktop", children=[app])
    helper, _remote_desktop = _portal_helper(wayland_helper_module)
    capture_stream = _FakeCaptureStream()
    helper._session.capture_stream = capture_stream
    capture_path = str(tmp_path / "tag.png")

    captured = helper.tag_context({"session_id": "sess-1", "capture_path": capture_path})

    assert captured["query"] == "draft a reply"
    assert captured["profile_override"] == "developer"
    assert captured["tag_text"] == "@A0.developer draft a reply"
    assert captured["app_name"] == "Text Editor"
    assert captured["window_title"] == "Notes"
    assert captured["replace_supported"] is True
    assert captured["screenshot_status"] == "unavailable"
    assert "verified active-window bounds" in captured["screenshot_error"]
    assert capture_stream.calls == []

    replaced = helper.tag_replace(
        {
            "session_id": "sess-1",
            "target_token": captured["target_token"],
            "replacement": "Réponse concise 🌟",
        }
    )

    assert replaced["replaced"] is True
    assert field.text == "🙂 intro\n  Réponse concise 🌟"
    assert field.insert_text_lengths == [len("Réponse concise 🌟".encode("utf-8"))]
    assert helper._tag_target is None


def test_wayland_a0_tag_ignores_stale_focus_outside_the_active_window(
    wayland_helper_module,
) -> None:
    stale = _FakeAtspiAccessible(
        role="section",
        states={"VISIBLE", "SHOWING", "FOCUSED"},
    )
    stale_window = _FakeAtspiAccessible(
        role="frame",
        name="Inactive app",
        children=[stale],
    )
    field = _FakeAtspiAccessible(
        role="text",
        states={"VISIBLE", "SHOWING", "FOCUSED", "EDITABLE"},
        text="@a0 use the active field",
    )
    active_window = _FakeAtspiAccessible(
        role="frame",
        name="Active app",
        states={"VISIBLE", "SHOWING", "ACTIVE"},
        children=[field],
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[
            _FakeAtspiAccessible(role="application", name="Stale", children=[stale_window]),
            _FakeAtspiAccessible(role="application", name="Current", children=[active_window]),
        ],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)
    helper._session.capture_stream = _FakeCaptureStream()

    captured = helper.tag_context({"session_id": "sess-1"})

    assert captured["query"] == "use the active field"
    assert captured["window_title"] == "Active app"


def test_wayland_a0_tag_fails_closed_for_changed_or_protected_fields(
    wayland_helper_module,
) -> None:
    field = _FakeAtspiAccessible(
        role="text",
        states={"VISIBLE", "SHOWING", "ENABLED", "FOCUSED", "EDITABLE"},
        text="@a0 summarize this",
    )
    window = _FakeAtspiAccessible(
        role="frame",
        name="Editor",
        states={"VISIBLE", "SHOWING", "ENABLED", "ACTIVE"},
        frame=(0, 0, 800, 600),
        children=[field],
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="App", children=[window])],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)
    helper._session.capture_stream = _FakeCaptureStream()
    captured = helper.tag_context({"session_id": "sess-1"})
    field.text = "@a0 changed by the user"

    with pytest.raises(wayland_helper_module.PortalError) as changed:
        helper.tag_replace(
            {
                "session_id": "sess-1",
                "target_token": captured["target_token"],
                "replacement": "unsafe",
            }
        )
    assert changed.value.code == "A0_TAG_TARGET_CHANGED"

    field.text = "@a0 reveal this"
    field.caret_offset = len(field.text)
    field.states.add("PROTECTED")
    with pytest.raises(wayland_helper_module.PortalError) as protected:
        helper.tag_context({"session_id": "sess-1"})
    assert protected.value.code == "A0_TAG_PROTECTED_FIELD"


def test_wayland_a0_tag_revalidates_active_window_process_identity(
    wayland_helper_module,
) -> None:
    field = _FakeAtspiAccessible(
        role="text",
        states={"VISIBLE", "SHOWING", "FOCUSED", "EDITABLE"},
        text="@a0 keep the exact target",
    )
    window = _FakeAtspiAccessible(
        role="frame",
        name="Editor",
        states={"VISIBLE", "SHOWING", "ACTIVE"},
        frame=(0, 0, 800, 600),
        children=[field],
        pid=123,
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="App", children=[window])],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)
    helper._session.capture_stream = _FakeCaptureStream()
    captured = helper.tag_context({"session_id": "sess-1"})
    window.pid = 456

    with pytest.raises(wayland_helper_module.PortalError) as changed:
        helper.tag_replace(
            {
                "session_id": "sess-1",
                "target_token": captured["target_token"],
                "replacement": "unsafe",
            }
        )

    assert changed.value.code == "A0_TAG_TARGET_CHANGED"
    assert field.text == "@a0 keep the exact target"


def test_wayland_a0_tag_restores_original_when_editor_normalizes_replacement(
    wayland_helper_module,
) -> None:
    original = "@a0 preserve café — 🌟"
    field = _FakeAtspiAccessible(
        role="text",
        states={"VISIBLE", "SHOWING", "FOCUSED", "EDITABLE"},
        text=original,
    )
    window = _FakeAtspiAccessible(
        role="frame",
        name="Editor",
        states={"VISIBLE", "SHOWING", "ACTIVE"},
        children=[field],
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="App", children=[window])],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)
    helper._session.capture_stream = _FakeCaptureStream()
    captured = helper.tag_context({"session_id": "sess-1"})
    insert_text = field.insert_text

    def normalize_once(position: int, text: str, length: int) -> bool:
        value = text.upper() if text == "mixed Case" else text
        return insert_text(position, value, length)

    field.insert_text = normalize_once  # type: ignore[method-assign]
    with pytest.raises(wayland_helper_module.PortalError) as changed:
        helper.tag_replace(
            {
                "session_id": "sess-1",
                "target_token": captured["target_token"],
                "replacement": "mixed Case",
            }
        )

    assert changed.value.code == "A0_TAG_REPLACE_FAILED"
    assert field.text == original
    assert field.insert_text_lengths[-1] == len(original.encode("utf-8"))


def test_wayland_detection_and_support_reason_are_additive_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wayland_detection, "SYSTEM_PYTHON", sys.executable)

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert wayland_detection.detect_wayland_support() is True
    assert wayland_detection.wayland_support_reason() == "Wayland portal backend is available."

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert wayland_detection.detect_wayland_support() is False
    assert "not supported by the Wayland portal backend" in wayland_detection.wayland_support_reason()

    monkeypatch.setattr(wayland_detection, "SYSTEM_PYTHON", str(Path(sys.executable).with_name("definitely-missing-python")))
    assert wayland_detection.detect_wayland_support() is False
    assert "Required system Python interpreter not found" in wayland_detection.wayland_support_reason()


def test_wayland_shortcut_dispatch_uses_evdev_keycodes_for_ctrl_t(
    wayland_helper_module,
) -> None:
    helper, remote_desktop = _portal_helper(wayland_helper_module)

    result = helper.key({"session_id": "sess-1", "keys": ["ctrl", "T"]})

    assert result["keys"] == ["ctrl", "T"]
    assert remote_desktop.calls == [
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", 29, 1),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", 20, 1),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", 20, 0),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", 29, 0),
    ]


@pytest.mark.parametrize(
    ("keys", "codes"),
    [
        (["Super", "H"], [125, 35]),
        (["alt", "F9"], [56, 67]),
    ],
)
def test_wayland_shortcut_dispatch_covers_super_alt_and_function_keys(
    wayland_helper_module,
    keys: list[str],
    codes: list[int],
) -> None:
    helper, remote_desktop = _portal_helper(wayland_helper_module)

    helper.key({"session_id": "sess-1", "keys": keys})

    assert remote_desktop.calls == [
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", codes[0], 1),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", codes[1], 1),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", codes[1], 0),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", codes[0], 0),
    ]


def test_wayland_shortcut_dispatch_falls_back_to_keysyms_for_unknown_keys(
    wayland_helper_module,
) -> None:
    helper, remote_desktop = _portal_helper(wayland_helper_module)

    helper.key({"session_id": "sess-1", "keys": ["XF86AudioMute"]})

    assert remote_desktop.calls == [
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", 0x1008FF12, 1),
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", 0x1008FF12, 0),
    ]


def test_wayland_text_dispatch_still_uses_keysyms(
    wayland_helper_module,
) -> None:
    window = _FakeAtspiAccessible(
        role="frame",
        name="Focused App",
        states={"VISIBLE", "SHOWING", "ENABLED", "ACTIVE"},
        frame=(0, 0, 800, 600),
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="Fake App", children=[window])],
    )
    helper, remote_desktop = _portal_helper(wayland_helper_module)
    window_id = helper.list_windows({"session_id": "sess-1"})["windows"][0]["window_id"]

    result = helper.type_text(
        {"session_id": "sess-1", "window_id": window_id, "text": "T", "submit": True}
    )

    assert remote_desktop.calls == [
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", ord("T"), 1),
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", ord("T"), 0),
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", 0xFF0D, 1),
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", 0xFF0D, 0),
    ]
    assert result["window_id"] == window_id
    assert result["focus_verified"] is True


def test_wayland_ax_snapshot_returns_linux_atspi_tree(
    wayland_helper_module,
) -> None:
    button = _FakeAtspiAccessible(
        role="push button",
        name="Open",
        actions=["press"],
        frame=(100, 200, 80, 30),
    )
    text_field = _FakeAtspiAccessible(
        role="text",
        name="Search",
        states={"VISIBLE", "SHOWING", "ENABLED", "FOCUSABLE", "EDITABLE"},
        frame=(20, 40, 300, 36),
        text="",
    )
    app = _FakeAtspiAccessible(
        role="application",
        name="Fake App",
        children=[button, text_field],
        frame=(0, 0, 800, 600),
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(role="desktop", children=[app])
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    result = helper.ax_snapshot({"session_id": "sess-1", "max_depth": 3, "max_nodes": 20})

    assert result["app"] == {"name": "Linux desktop", "backend": "at-spi"}
    assert result["node_count"] == 4
    assert result["truncated"] is False
    root = result["tree"]
    assert root["role"] == "Desktop"
    assert root["children"][0]["path"] == [0]
    assert root["children"][0]["title"] == "Fake App"
    assert root["children"][0]["children"][0]["path"] == [0, 0]
    assert root["children"][0]["children"][0]["actions"][0]["name"] == "press"
    assert root["children"][0]["children"][1]["states"] == [
        "editable",
        "enabled",
        "focusable",
        "showing",
        "visible",
    ]


def test_wayland_ax_action_presses_element_by_path(
    wayland_helper_module,
) -> None:
    button = _FakeAtspiAccessible(
        role="push button",
        name="Open",
        actions=["press"],
        frame=(100, 200, 80, 30),
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="Fake App", children=[button])],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    result = helper.ax_action({"session_id": "sess-1", "path": [0, 0], "operation": "press"})

    assert button.performed_actions == [0]
    assert result["operation"] == "press"
    assert result["target"]["path"] == [0, 0]
    assert result["target"]["role"] == "push button"
    assert result["target"]["title"] == "Open"


def test_wayland_window_state_indexes_elements_for_background_actions(
    wayland_helper_module,
) -> None:
    button = _FakeAtspiAccessible(
        role="push button",
        name="Open",
        actions=["press"],
        frame=(100, 200, 80, 30),
    )
    text_field = _FakeAtspiAccessible(
        role="text",
        name="Search",
        states={"VISIBLE", "SHOWING", "ENABLED", "FOCUSABLE", "EDITABLE"},
        frame=(20, 40, 300, 36),
    )
    app = _FakeAtspiAccessible(
        role="application",
        name="Fake App",
        children=[button, text_field],
        frame=(0, 0, 800, 600),
        pid=321,
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(role="desktop", children=[app])
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    windows = helper.list_windows({"session_id": "sess-1"})
    state = helper.get_window_state(
        {
            "session_id": "sess-1",
            "window_id": windows["windows"][0]["window_id"],
            "max_depth": 2,
        }
    )
    button_index = state["tree"]["children"][0]["element_index"]
    text_index = state["tree"]["children"][1]["element_index"]
    press = helper.element_action(
        {
            "session_id": "sess-1",
            "window_id": state["window_id"],
            "element_index": button_index,
            "operation": "press",
            "dispatch": "background",
        }
    )
    typed = helper.element_action(
        {
            "session_id": "sess-1",
            "window_id": state["window_id"],
            "element_index": text_index,
            "operation": "set_value",
            "dispatch": "background",
            "value": "hello",
        }
    )

    assert windows["windows"][0]["window_id"] == "atspi-pid:321:path:0"
    assert state["tree"]["element_index"] == 0
    assert button_index == 1
    assert text_index == 2
    assert button.performed_actions == [0]
    assert text_field.set_text_values == ["hello"]
    assert press["actual_dispatch"] == "background"
    assert typed["actual_dispatch"] == "background"


def test_wayland_window_list_returns_frames_and_skips_services(
    wayland_helper_module,
) -> None:
    frame = _FakeAtspiAccessible(
        role="frame",
        name="#general | Agent Zero - Discord",
        states={"VISIBLE", "SHOWING", "ENABLED", "ACTIVE"},
        frame=(69, 50, 1851, 1030),
        pid=57929,
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[
            _FakeAtspiAccessible(role="application", name="gsd-color", pid=3554),
            _FakeAtspiAccessible(role="application", name="Discord", children=[frame], pid=57929),
        ],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    result = helper.list_windows({"session_id": "sess-1"})

    assert result["count"] == 1
    assert result["windows"] == [
        {
            "window_id": "atspi-pid:57929:path:1.0",
            "pid": 57929,
            "app_name": "Discord",
            "title": "#general | Agent Zero - Discord",
            "role": "frame",
            "frame": {
                "x": 69,
                "y": 50,
                "width": 1851,
                "height": 1030,
                "normalized_x": 69 / 1920,
                "normalized_y": 50 / 1080,
                "normalized_width": 1851 / 1920,
                "normalized_height": 1030 / 1080,
            },
            "active": True,
            "focused": False,
            "visible": True,
            "path": [1, 0],
        }
    ]


def test_wayland_press_rejects_unrecognized_action_instead_of_using_index_zero(
    wayland_helper_module,
) -> None:
    button = _FakeAtspiAccessible(
        role="push button",
        name="Mystery",
        actions=["doDefault"],
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="Fake App", children=[button])],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    with pytest.raises(wayland_helper_module.PortalError) as exc_info:
        helper.ax_action({"session_id": "sess-1", "path": [0, 0], "operation": "press"})

    assert exc_info.value.code == "COMPUTER_USE_AX_ACTION_UNAVAILABLE"
    assert button.performed_actions == []


def test_wayland_press_rejects_window_activation(wayland_helper_module) -> None:
    window = _FakeAtspiAccessible(
        role="frame",
        name="Discord",
        actions=["activate"],
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="Discord", children=[window])],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    with pytest.raises(wayland_helper_module.PortalError) as exc_info:
        helper.ax_action({"session_id": "sess-1", "path": [0, 0], "operation": "press"})

    assert exc_info.value.code == "COMPUTER_USE_WINDOW_ACTIVATION_REQUIRED"
    assert window.performed_actions == []


def test_wayland_ax_snapshot_can_be_scoped_to_one_window(
    wayland_helper_module,
) -> None:
    noise = _FakeAtspiAccessible(
        role="frame",
        name="GNOME Shell",
        children=[_FakeAtspiAccessible(role="panel", name=f"Panel {index}") for index in range(20)],
        frame=(0, 0, 1920, 1080),
        pid=3350,
    )
    composer = _FakeAtspiAccessible(
        role="text",
        name="Message #general",
        states={"VISIBLE", "SHOWING", "ENABLED", "FOCUSABLE", "EDITABLE"},
    )
    discord = _FakeAtspiAccessible(
        role="frame",
        name="#general | Agent Zero - Discord",
        children=[composer],
        frame=(69, 50, 1851, 1030),
        pid=57929,
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[
            _FakeAtspiAccessible(role="application", name="gnome-shell", children=[noise], pid=3350),
            _FakeAtspiAccessible(role="application", name="Discord", children=[discord], pid=57929),
        ],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)
    window_id = helper.list_windows({"session_id": "sess-1"})["windows"][1]["window_id"]

    result = helper.ax_snapshot(
        {"session_id": "sess-1", "window_id": window_id, "max_depth": 3, "max_nodes": 5}
    )

    assert result["scoped"] is True
    assert result["window_id"] == window_id
    assert result["node_count"] == 2
    assert result["tree"]["title"] == "#general | Agent Zero - Discord"
    assert result["tree"]["children"][0]["title"] == "Message #general"


def test_wayland_background_focus_reports_unavailable_and_auto_falls_back(
    wayland_helper_module,
) -> None:
    text_field = _FakeAtspiAccessible(
        role="text",
        name="Search",
        states={"VISIBLE", "SHOWING", "ENABLED", "FOCUSABLE", "EDITABLE"},
        frame=(20, 40, 300, 36),
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[
            _FakeAtspiAccessible(
                role="application",
                name="Fake App",
                children=[text_field],
                frame=(0, 0, 800, 600),
            )
        ],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)
    state = helper.get_window_state({"session_id": "sess-1", "max_depth": 2})
    text_index = state["tree"]["children"][0]["element_index"]

    background_only = helper.element_action(
        {
            "session_id": "sess-1",
            "element_index": text_index,
            "operation": "focus",
            "dispatch": "background",
        }
    )
    assert background_only["background_unavailable"] is True
    assert text_field.focused is False

    auto = helper.element_action(
        {
            "session_id": "sess-1",
            "element_index": text_index,
            "operation": "focus",
            "dispatch": "auto",
        }
    )

    assert text_field.focused is True
    assert auto["actual_dispatch"] == "foreground"
    assert auto["foreground_fallback_used"] is True
    assert auto["focus_verified"] is True


def test_wayland_foreground_focus_can_target_window_id(
    wayland_helper_module,
) -> None:
    window = _FakeAtspiAccessible(
        role="frame",
        name="#general | Agent Zero - Discord",
        frame=(69, 50, 1851, 1030),
        pid=57929,
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[
            _FakeAtspiAccessible(
                role="application",
                name="Discord",
                children=[window],
                pid=57929,
            )
        ],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)
    window_id = helper.list_windows({"session_id": "sess-1"})["windows"][0]["window_id"]

    result = helper.element_action(
        {
            "session_id": "sess-1",
            "window_id": window_id,
            "operation": "focus",
            "dispatch": "foreground",
        }
    )

    assert window.focused is True
    assert result["target"]["window_id"] == window_id
    assert result["target"]["role"] == "frame"
    assert result["focus_verified"] is True
    assert result["actual_dispatch"] == "foreground"


def test_wayland_window_focus_falls_back_to_verified_wmctrl_activation(
    wayland_helper_module,
    monkeypatch,
) -> None:
    window = _FakeAtspiAccessible(
        role="frame",
        name="#general | Agent Zero - Discord",
        frame=(69, 50, 1851, 1030),
        pid=57929,
    )
    window.grab_focus = lambda: False  # type: ignore[method-assign]
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[
            _FakeAtspiAccessible(
                role="application",
                name="Discord",
                children=[window],
                pid=57929,
            )
        ],
    )
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> types.SimpleNamespace:
        del kwargs
        calls.append(args)
        if args == ["wmctrl", "-lp"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout="0x01e0000a  0 57929 host #general | Agent Zero - Discord\n",
            )
        window.states.add("ACTIVE")
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(wayland_helper_module.subprocess, "run", fake_run)
    helper, _remote_desktop = _portal_helper(wayland_helper_module)
    window_id = helper.list_windows({"session_id": "sess-1"})["windows"][0]["window_id"]

    result = helper.element_action(
        {
            "session_id": "sess-1",
            "window_id": window_id,
            "operation": "focus",
            "dispatch": "foreground",
        }
    )

    assert calls == [["wmctrl", "-lp"], ["wmctrl", "-ia", "0x01e0000a"]]
    assert result["focus_verified"] is True
    assert result["actual_dispatch"] == "foreground"


def test_wayland_type_fails_closed_without_verified_target_focus(
    wayland_helper_module,
) -> None:
    window = _FakeAtspiAccessible(
        role="frame",
        name="Inactive App",
        frame=(0, 0, 800, 600),
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="Fake App", children=[window])],
    )
    helper, remote_desktop = _portal_helper(wayland_helper_module)
    window_id = helper.list_windows({"session_id": "sess-1"})["windows"][0]["window_id"]

    with pytest.raises(wayland_helper_module.PortalError) as missing:
        helper.type_text({"session_id": "sess-1", "text": "wrong target"})
    with pytest.raises(wayland_helper_module.PortalError) as unfocused:
        helper.type_text({"session_id": "sess-1", "window_id": window_id, "text": "wrong target"})

    assert missing.value.code == "COMPUTER_USE_WINDOW_REQUIRED"
    assert unfocused.value.code == "COMPUTER_USE_TARGET_NOT_FOCUSED"
    assert remote_desktop.calls == []


def test_wayland_ax_action_sets_text_by_semantic_target(
    wayland_helper_module,
) -> None:
    text_field = _FakeAtspiAccessible(
        role="text",
        name="Search",
        states={"VISIBLE", "SHOWING", "ENABLED", "FOCUSABLE", "EDITABLE"},
        frame=(20, 40, 300, 36),
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="Fake App", children=[text_field])],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    result = helper.ax_action(
        {
            "session_id": "sess-1",
            "target": {"role": "text", "title": "Search"},
            "operation": "set_value",
            "value": "hello",
        }
    )

    assert text_field.set_text_values == ["hello"]
    assert result["operation"] == "set_value"
    assert result["target"]["path"] == [0, 0]
    assert result["target"]["text"] == "hello"
