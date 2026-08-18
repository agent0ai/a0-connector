# Widgets DOX

## Purpose

- Own reusable Textual widgets used by the CLI shell: composer, chat log, splash view, command palette, footer, context tabs, status bars, banners, popovers, goal controls, and model/message queue controls.

## Ownership

- Files in this directory implement widget behavior and rendering only.
- `image_entry.py` owns focusable inline transcript image state, thumbnail and
  expanded presentation, stable placeholders, and renderer-widget cleanup.
- App-level orchestration remains in `src/agent_zero_cli/app.py` and command modules.
- Visual constants that require layout styling belong in `src/agent_zero_cli/styles/app.tcss`.

## Local Contracts

- `ChatInput` is the single source for composer behavior: Enter submits, `Ctrl+J` inserts a newline, `Ctrl+A` selects the full draft, history is scoped by chat context, `@` opens reference completion, and content grows to four lines before internal scrolling.
- Reference completion inserts prompt text only: `@[./path]`, `@[./folder/]`, `@[/container/path]`, `@[agent/profile]`, `@[skill/name]`, or `@[mcp/server]`. It must not activate skills or delegate work.
- Discovered host rows use Up/Down to move the active selection and Enter or Space to connect.
- In-input activity must use the WebUI-style `|>  ` placeholder prefix, add the `progress-active` class, and escape detail text before putting it into Rich/Textual markup.
- `ChatInput.set_idle()` must clear activity state and restore the normal placeholder without losing attachment or queue placeholder state.
- Do not reintroduce `ActivityBar` or `#status-bar`. Activity belongs in `#message-input`.
- `ChatLog` status metadata must stay concise and must redact or summarize large/sensitive fields such as code, prompt text, stdout, stderr, markdown, HTML, and raw content. Media-bearing keys consumed by image extraction and direct `img://`, `data:image/`, or `/api/image_get` values must never render or enter transcript copy text.
- Transcript renderable caches must use A0-owned attribute names and must not
  shadow Textual's internal widget render cache.
- Footer/command palette behavior must not duplicate the command palette entry. The `ctrl+p` binding remains `show=False` in `app.py`.
- The compact model switcher bar shows the effective model pill without a `Main` role prefix. Its preset selector shows the effective preset and uses `Use preset from settings (<name>)` when a chat override can be cleared.
- Programmatic model-switcher refreshes must ignore queued stale `Select.Changed` events so they cannot reverse a user's preset choice.
- `ProfileMenuPopover` may report `default` as current status but never renders
  it as a selectable or editable row. Its Create action remains available when
  no selectable profiles remain.
- `ImageEntry` is a focusable, expandable rendering surface only. It posts load requests but performs no network I/O, copies the semantic `ImageReference.copy_text` placeholder rather than terminal output, and handles Textual prune before descendant removal so renderer controls are cleaned before its owned Pillow asset closes. Releasing a loading or rendered entry invalidates that generation before returning it to pending.
- `TranscriptEntry` owns the primary text, status, or code widget and all image references for exactly one connector sequence. Replacing its primary never removes its image children; copying combines the primary and semantic image placeholders.
- `ChatLog` coalesces image-load scans to one callback per refresh, after Textual's inherited scroll watcher updates viewport state. It requests entries within one viewport of visible content, invalidates still-loading entries farther than two viewports away, and retains rendered surfaces for eight viewports so several expanded siblings survive normal focus/autofollow movement without making long-history memory unbounded. Visible Sixel surfaces redraw together after scroll or layout refresh because Sixel pixels are not terminal-retained. Geometry is screen-relative; zero-size entries remain unscheduled until layout makes them measurable. Asynchronous image surface changes restore the bottom only while auto-follow is active and never resume it for a user reading older content.
- Image entries begin expanded and toggle on click, `Enter`, and `Space`; their
  36-by-12 thumbnail and 96-by-32 expanded boxes preserve aspect ratio and state
  across updates and resize. Expanded sizing must use the transcript entry's
  available width rather than the shrink-wrapped current surface. They must
  delegate all fetches to app-owned state and release native surfaces when
  removed.

## Work Guidance

- Use Textual and Rich renderables instead of ad hoc ANSI strings when practical.
- Keep widget APIs small and mirrored in test fakes when `app.py` calls them.
- Avoid widget methods that perform network calls; pass state in from the app or command layer.
- Keep text and controls stable across narrow terminal/browser-preview widths.

## Verification

- `./.venv/bin/python -m pytest tests/test_chat_input.py tests/test_app.py tests/test_splash_view.py -v`

## Child DOX Index
