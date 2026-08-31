# Wayland Computer-Use DOX

## Purpose

- Own the Linux Wayland computer-use backend package.

## Ownership

- `pyproject.toml`, `README.md`, and `src/a0_computer_use_wayland/` are owned here.
- The package provides Wayland portal remote desktop/screencast control, AT-SPI accessibility tree/action support, and native window metadata.

## Local Contracts

- `WAYLAND_BACKEND_SPEC` uses backend ID `wayland`, family `linux`, priority `100`, `interpreter_strategy="system_python"`, and helper target `computer_use_helper.py`.
- Trust modes are `interactive`, `persistent`, and `allow`.
- Preserve the feature contract for portal capture/control, inline PNG capture, fresh-frame capture, normalized coordinates, pointer/keyboard injection, AT-SPI tree/action/set-value, native window listing/state, element index targeting, background dispatch, and foreground fallback.
- The `a0-tag` feature provides Launcher-only focused tag capture, protected
  field rejection, an opaque short-lived native target, and exact AT-SPI range
  replacement with best-effort rollback. GNOME Wayland AT-SPI can report a
  native top-level window at `(0, 0)` regardless of its compositor position, so
  tag capture must report screenshot unavailable instead of cropping the monitor
  stream until a compositor-verified active-window capture source exists.
  Keep these helper actions out of the agent-visible remote action contract and
  behavior-aligned in the builtin compatibility helper. Resolve the target
  inside an AT-SPI active top-level window and prefer its deepest readable
  focused descendant; inactive applications can retain stale focused states and
  must not win capture or replacement revalidation. Reject an AT-SPI protected
  state or password role before reading text or capturing a frame, bind apply to
  the same process/window identity, and reject a logical line whose end cannot
  be bounded safely.
- Keep AT-SPI ranges and post-insertion checks in Unicode character offsets,
  but pass UTF-8 byte lengths to `EditableText.insert_text` for both replacement
  and rollback text.
- Native window IDs identify AT-SPI frame/window/dialog nodes, not application wrappers. Window-scoped snapshots must stay within that node; foreground focus may target that window ID directly and must be observed after activation; keyboard text injection requires that same window to be active or focused.
- If a top-level XWayland window rejects AT-SPI `grab_focus`, use its unambiguous PID/title match through `wmctrl` and still require the same AT-SPI node to report active or focused before claiming success.
- Never choose an arbitrary AT-SPI action by index. Press only a recognized press/click action, and require the focus operation for application or window activation.
- Keep DBus, GI, GStreamer, Pillow, and AT-SPI imports inside this package/helper path, not in shared CLI import paths.

## Work Guidance

- Prefer defensive handling around portal permission/session state; user approval is part of the runtime contract.
- Keep helper output machine-readable and avoid noisy stdout that would corrupt stdio JSON.
- Treat coordinate-space changes as protocol changes and update tests/docs accordingly.

## Verification

- `./.venv/bin/python -m pytest tests/test_wayland_backend_package.py tests/test_computer_use_contract.py -v`

## Child DOX Index
