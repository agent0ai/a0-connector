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
- Plugin composer labels share the provider/model chip's text colour and keyboard-focus styling.
- Keep UI text and controls from overlapping in browser preview and terminal-size snapshots.
- Style chat-selector rows through `#chat-list`'s `OptionList` component classes; keep the compact title/metadata/preview layout and a visible keyboard highlight without per-chat widgets.
- The chat intro logo and workspace line share a two-cell horizontal inset and one blank row above each; keep this spacing scoped to the intro so transcript messages retain their width.
- Splash content is top-aligned in a centered 82-cell column. Host rows reserve three cells without an enclosing border; keep their list-height calculation in sync. Refresh sits at the right of the heading, with a blank row below the heading even in short viewports. The primary action follows its target, narrow windows stack the remember/connect row, and the manual-URL toggle sits below the keyboard hint in place of the removed workspace path. Short viewports tighten other form spacing while preserving the logo chosen by width.
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
