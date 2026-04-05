# Agent Zero CLI — Dev Tools

Tools for web-like development of the Textual TUI.

## Prerequisites

```bash
# Windows
.\.venv\Scripts\python -m pip install textual-serve

# Linux / macOS
./.venv/bin/python -m pip install textual-serve
```

---

## 1. Browser Preview (`serve.py`)

Runs the full TUI inside a browser tab — the closest thing to "inspect element"
for a terminal app. Works with any browser; the AI assistant can take
screenshots of it just like a web page.

```bash
# Windows
.\.venv\Scripts\python devtools/serve.py                 # http://localhost:8566
.\.venv\Scripts\python devtools/serve.py --port 9000     # custom port
.\.venv\Scripts\python devtools/serve.py --debug         # enable Textual devtools

# Linux / macOS
./.venv/bin/python devtools/serve.py                     # http://localhost:8566
./.venv/bin/python devtools/serve.py --port 9000         # custom port
./.venv/bin/python devtools/serve.py --debug             # enable Textual devtools
```

> **Tip:** Append `?fontsize=14` to the URL to tweak the rendered font size.

## 2. SVG Snapshot (`snapshot.py`)

Captures a pixel-perfect SVG of the TUI's initial screen (no live backend
needed). Great for quick layout checks, CI diffing, or sharing.

```bash
# Windows
.\.venv\Scripts\python devtools/snapshot.py
.\.venv\Scripts\python devtools/snapshot.py -o devtools\snapshots\footer_check.svg --width 100 --height 30

# Linux / macOS
./.venv/bin/python devtools/snapshot.py
./.venv/bin/python devtools/snapshot.py -o /tmp/footer_check.svg --width 100 --height 30
```

Output lands in `devtools/snapshots/` by default.

## 3. Activity Demo (`activity_demo.py`)

Simulates agent progress states (idle → busy → idle) and captures three
SVG snapshots to verify the in-input progress indicator visually matches the
core WebUI.

```bash
# Windows
.\.venv\Scripts\python devtools/activity_demo.py

# Linux / macOS
./.venv/bin/python devtools/activity_demo.py
```

Produces:
| File | Shows |
|------|-------|
| `snapshots/tui_idle.svg` | Normal placeholder |
| `snapshots/tui_activity.svg` | Spinner + "Using tool [web_search]" |
| `snapshots/tui_reset.svg` | Placeholder restored after reset |

---

## Typical Workflow

1. **Making CSS / layout changes** → run `serve.py`, open browser, iterate live.
2. **Verifying a specific state** → run `activity_demo.py`, check SVGs.
3. **Quick smoke test** → run `snapshot.py`, compare SVGs before/after.
4. **AI-assisted review** → start `serve.py`, let the assistant take browser
   screenshots and give visual feedback.

## Files

```
devtools/
├── README.md            # This file
├── serve.py             # Browser preview server
├── snapshot.py          # SVG snapshot capture
├── activity_demo.py     # Progress state demo + captures
└── snapshots/           # Generated SVGs (gitignored)
```
