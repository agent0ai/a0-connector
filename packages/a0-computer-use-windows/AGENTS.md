# Windows Computer-Use DOX

## Purpose

- Own the Windows computer-use backend package.

## Ownership

- `pyproject.toml` and `src/a0_computer_use_windows/` are owned here.
- The package provides UI Automation tree/action support, Windows screen capture/input paths, runtime session state, and Windows-specific detection.

## Local Contracts

- `WINDOWS_BACKEND_SPEC` uses backend ID `windows`, family `windows`, `interpreter_strategy="current_python"`, and helper target `runtime.py`.
- Trust modes and shared feature constants live in `shared.py`; keep backend metadata, runtime metadata, and tests aligned.
- Runtime responses must include contract version and capabilities derived from the shared feature list.
- Session persistence must remain scoped by normalized context IDs and restore tokens.
- Capture debug output must stay opt-in and must not leak sensitive screen content unless explicitly requested for debugging.
- Advertise `a0-tag` only with the complete private focused-field contract.
  Reject UIA or native password controls before reading text, serializing the
  window tree, or capturing pixels. Bind each opaque target to the exact
  foreground HWND, PID, focused UIA runtime element, value/range, and caret.
  Native Edit controls use UTF-16 `EM_GETSEL`/`EM_SETSEL`/`EM_REPLACESEL`;
  HWND-less controls require bounded TextPattern selection plus ValuePattern
  verification and rollback. Preserve untouched text and caret exactly, and
  fail closed when an editor normalizes input.
- Tag screenshots are optional exact active-window crops. Require stable DWM
  bounds and foreground identity before and after capture; never substitute a
  full-desktop frame when those checks fail. Publish the private target only
  after context and optional-screenshot validation succeeds.

## Work Guidance

- Keep Windows-only imports isolated to this package/runtime path.
- Preserve background dispatch and foreground fallback semantics when changing UIA or input behavior.
- Normalize portable command/meta/super/Windows and modifier key names to
  pywinauto's native virtual-key tokens before global keyboard injection.
- Send ordinary `type` text as literal UTF-16 input; pywinauto key-sequence
  metacharacters are reserved for the explicit `key` action and optional submit.
- Prefer shared normalization helpers for action payloads, booleans, integers, context IDs, and restore tokens.

## Verification

- `./.venv/bin/python -m pytest tests/test_windows_computer_use_backend.py tests/test_computer_use_contract.py -v`

## Child DOX Index
