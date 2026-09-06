# Agent Zero CLI — Dev Tools

Tools for web-like development of the Textual TUI.

## Local A0 wheel build (no GitHub Actions)

The normal package build includes the browser-extension CLI, explicit
development install/update commands, and the read-only stable release resolver.
It needs only Python, `uv`, and the pinned build requirements; runtime packages
are not bundled or installed by this command:

```bash
uv build --wheel --no-python-downloads \
  --build-constraints constraints/a0-build.txt --require-hashes \
  --out-dir /absolute/new/output-directory --no-create-gitignore
```

After that first build has cached the pinned backend, add `--offline` to rebuild
without network access. Use a new output directory rather than overwriting an
already handed-off artifact. Inspect the wheel's `METADATA`, entry points and
`agent_zero_cli/browser_extension*.py` members before installation.

Installing the wheel is a separate explicit operation. For an existing verified
A0 environment whose runtime dependencies are already present, install the exact
wheel with `uv pip install --offline --no-deps --reinstall --python
/absolute/a0-environment/bin/python /absolute/a0-version-py3-none-any.whl`.
This preserves runtime dependency versions; it does not install/update the
separately registered native companion or modify its paired identity. Fresh
environments still need the package's pinned runtime dependencies.

An A0 wheel is not a signed native release. Its Mac bootstrap independently pins
the signed/notarized r2 companion, while Chrome pairing still requires the
matching Agent Zero server. The explicit-source development commands remain
separate and never substitute for production release trust.

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

The launcher deliberately sets `A0_CLI_IMAGE_MODE=halfcell` for its child.
xterm.js previews the portable, bounded fallback and do not exercise TGP/Sixel
protocol bytes or native-surface cleanup. Use a separately verified capable
terminal for native TGP/Sixel acceptance; do not assume Apple Terminal supports
either protocol.

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

Snapshots initialize and inject the same forced half-cell renderer, so capture
is deterministic and library-free. SVG output is layout evidence only; it does
not prove native terminal-protocol rendering or cleanup.

## 3. AI Agent Runbook: Send Text Through the Textual Wrapper

When the app is served via `devtools/serve.py`, Textual is rendered through an
`xterm.js` wrapper. This means widget IDs like `#splash-host-input` are not
normal browser DOM inputs. For automation, send keystrokes through the hidden
terminal helper textarea:

- Selector: `#terminal .xterm-helper-textarea`
- Model: click/focus terminal helper -> type keys -> press Enter
- Multiline composer input: use `Ctrl+J` to insert a newline. Some
  browser/xterm paths collapse `Shift+Enter` into plain `Enter`.

### Why this matters

- `document.querySelector("input")` may return nothing useful for app widgets.
- Direct `fill()` calls on Textual widget IDs usually do not work in browser
  automation.
- Typing often fails if the helper textarea is not focused first.
- `/copy` copies the currently visible transcript text through the TUI clipboard
  path, which is more reliable than drag-selecting the xterm canvas.

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
