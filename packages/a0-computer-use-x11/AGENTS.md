# X11 Computer-Use DOX

## Purpose

- Own the Linux X11 computer-use backend package.

## Ownership

- `pyproject.toml`, `README.md`, and `src/a0_computer_use_x11/` are owned here.
- The package provides X11/XTest-oriented pointer, keyboard, and screenshot support.

## Local Contracts

- `X11_BACKEND_SPEC` uses backend ID `x11`, family `linux`, priority `90`, and helper target `computer_use_helper.py`.
- Trust modes are `interactive`, `persistent`, and `allow`.
- Feature metadata includes `focus-risk`; keep that risk visible while X11 actions can steal focus or move the real cursor.
- The shared CLI currently disables remote X11 backend selection. Do not enable it without an explicit product decision and matching tests.

## Work Guidance

- Keep X11 dependencies isolated to this package.
- Keep detection safe when no X server or XTest support is present.
- Avoid adding background-dispatch claims unless implementation and tests prove it.

## Verification

- `./.venv/bin/python -m pytest tests/test_x11_backend_package.py -v`

## Child DOX Index

No child DOX files.
