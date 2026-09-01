# macOS Computer-Use DOX

## Purpose

- Own the macOS computer-use backend package.

## Ownership

- `pyproject.toml` and `src/a0_computer_use_macos/` are owned here.
- The package provides Accessibility tree/action support, CoreGraphics capture/input paths, runtime session state, and macOS-specific detection.

## Local Contracts

- `MACOS_BACKEND_SPEC` uses backend ID `macos`, family `macos`, `interpreter_strategy="current_python"`, and helper target `runtime.py`.
- Trust modes and shared feature constants live in `shared.py`; keep backend metadata, runtime metadata, and tests aligned.
- Runtime responses must include contract version and capabilities derived from the shared feature list.
- Debug logging must stay opt-in through environment flags and must not leak secrets.
- The runtime exposes non-prompting `permission_status`, explicit
  `request_accessibility`, and explicit `request_screen_recording` internal
  operations. Screen status/request uses Core Graphics preflight/request APIs;
  Accessibility uses the ApplicationServices trust APIs.
- The backend advertises `a0-tag` only with the complete private capture,
  replace, and release contract. Capture resolves the frontmost app, focused
  window, and focused non-protected field through Accessibility; text ranges use
  native UTF-16 offsets. Replacement must revalidate the same app/window/field,
  caret, exact original range, editability, and protection state, then verify
  the exact write and restore the original tag best-effort on failure.
- A private `launcher-tag` session requires Accessibility but skips the ordinary
  full-display capture probe. Its optional screenshot requires Screen Recording
  plus one CoreGraphics window matching the Accessibility PID and finite native
  bounds; otherwise capture continues with an explicit unavailable reason.

## Work Guidance

- Keep macOS framework imports isolated to this package/runtime path.
- Preserve user permission semantics around Accessibility and screen capture.
- Permission orchestration belongs to the parent connector manager: this helper
  must return promptly after one status or request operation so the manager can
  close it and poll TCC through a fresh process. Do not add an indefinite prompt
  wait inside the helper.
- Keep session restore-token handling normalized through shared helpers.

## Verification

- `./.venv/bin/python -m pytest tests/test_macos_backend_package.py tests/test_macos_computer_use_backend.py tests/test_computer_use_contract.py -v`

## Child DOX Index
