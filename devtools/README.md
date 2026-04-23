# Agent Zero CLI — Dev Tools

Tools for web-like development of the Textual TUI.

## Prerequisites

Use the project venv. `textual-serve` is part of the workspace dependencies now.
If an older venv is missing it, refresh the environment:

```bash
# Windows
.\.venv\Scripts\python -m pip install -e .

# Linux / macOS
./.venv/bin/python -m pip install -e .
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

On Linux, the preview launcher now arms a parent-death signal so browser-preview
CLI sessions shut down with the serving process instead of lingering under
`systemd --user`.

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

## 3. AI Agent Runbook: Send Text Through the Textual Wrapper

When the app is served via `devtools/serve.py`, Textual is rendered through an
`xterm.js` wrapper. This means widget IDs like `#splash-host-input` are not
normal browser DOM inputs. For automation, send keystrokes through the hidden
terminal helper textarea:

- Selector: `#terminal .xterm-helper-textarea`
- Model: click/focus terminal helper -> type keys -> press Enter

### Why this matters

- `document.querySelector("input")` may return nothing useful for app widgets.
- Direct `fill()` calls on Textual widget IDs usually do not work in browser
  automation.
- Typing often fails if the helper textarea is not focused first.

### Minimal Playwright Example (Linux)

```bash
mkdir -p /tmp/a0-pw
cd /tmp/a0-pw
npm init -y
npm install playwright
npx playwright install chromium
```

```bash
cd /tmp/a0-pw
node <<'NODE'
import { chromium } from "playwright";

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
const page = await context.newPage();

await page.goto("http://localhost:8566/", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(2500);

const helper = page.locator("#terminal .xterm-helper-textarea");
await helper.click();
await page.waitForTimeout(120);

// Clear current field value in the focused Textual input.
await page.keyboard.press("Home");
for (let i = 0; i < 45; i++) await page.keyboard.press("Delete");

// insertText is more reliable than type() for the first character.
await page.keyboard.insertText("http://localhost:32081");
await page.keyboard.press("Enter");

await page.waitForTimeout(1500);
await page.screenshot({ path: "/tmp/a0-connect-result.png", type: "png" });

await browser.close();
NODE
```

### Operational Notes for DevOps Automation

- Start the preview server first:
  `./.venv/bin/python devtools/serve.py --debug`
- Verify the endpoint before automation:
  `curl -I http://localhost:8566/`
- If the first typed character is missing, add:
  `helper.click()` + a short wait + `keyboard.insertText(...)`.
- Use screenshots as ground truth for state transitions, since rendered Textual
  content is on xterm canvas layers.

---

## Typical Workflow

1. **Making CSS / layout changes** → run `serve.py`, open browser, iterate live.
2. **Quick smoke test** → run `snapshot.py`, compare SVGs before/after.
3. **AI-assisted review** → start `serve.py`, let the assistant take browser
   screenshots and give visual feedback.
4. **AI/DevOps scripted input** → drive `#terminal .xterm-helper-textarea` for
   reproducible login/host-entry flows in CI or local diagnostics.

## Files

```
devtools/
├── README.md            # This file
├── serve.py             # Browser preview server
├── snapshot.py          # SVG snapshot capture
└── snapshots/           # Generated SVGs (gitignored)
```
