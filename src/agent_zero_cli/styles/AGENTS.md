# Styles DOX

## Purpose

- Own Textual CSS for the CLI in `app.tcss`.

## Ownership

- This directory owns visual layout, borders, colors, spacing, and screen/widget selectors for the Textual UI.
- Widget behavior and state transitions belong in Python files in sibling package scopes.

## Local Contracts

- Do not set a hard height on `#message-input`; `ChatInput` dynamically sizes itself with a border-aware height.
- Preserve `#message-input.progress-active` as the busy-state visual hook.
- Preserve `#message-input:focus` as the focused composer border.
- Do not reintroduce `#status-bar`; the status-bar surface was removed.
- Footer styling must not depend on duplicate command palette bindings.
- Visible composer bars and anchored popovers must not leave blank rows between adjoining surfaces.
- Keep UI text and controls from overlapping in browser preview and terminal-size snapshots.
- The model editor scrolls only its fields; keep the title, status, and Apply/Cancel outside that scroll area. Vision checkboxes use a visible checked-state accent and retain keyboard focus styling.

## Work Guidance

- Use restrained Textual styling that matches the existing dark CLI theme.
- Prefer stable dimensions and min/max bounds for fixed-format controls, bars, selectors, and modal panels.
- Reload the browser preview tab after TCSS edits; Textual serve does not hot-reload styles into an existing process.

## Verification

- `./.venv/bin/python -m pytest tests/test_app.py tests/test_chat_input.py -v`
- `./.venv/bin/python devtools/snapshot.py`
- For visual work, run `./.venv/bin/python devtools/serve.py` and inspect `http://localhost:8566`.

## Child DOX Index

No child DOX files.
