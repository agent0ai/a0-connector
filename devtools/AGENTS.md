# Devtools DOX

## Purpose

- Own local development tools for Textual preview, snapshots, preview process launching, and dependency lock regeneration.

## Ownership

- `serve.py` owns the browser preview server.
- `preview_launcher.py` owns subprocess launch behavior for the served TUI.
- `snapshot.py` owns static SVG snapshot generation.
- `lock_dependencies.py` owns release lock regeneration and pyproject dependency pin sync.
- `README.md` explains these tools.

## Local Contracts

- Browser preview runs with `./.venv/bin/python devtools/serve.py` and serves `http://localhost:8566` by default.
- Browser and SVG captures remove inherited `NO_COLOR` before constructing the app so documentation keeps the full-colour theme. Ordinary CLI sessions still respect `NO_COLOR`; `textual-serve` supplies the browser's true-colour capability.
- `preview_launcher.py` must set `A0_CLI_IMAGE_MODE=halfcell` immediately before exec, and `snapshot.py` must inject a usable real `initialize_image_renderer(force_halfcell=True)` fallback without native protocol probes. xterm.js/SVG preview is deterministic fallback layout evidence, not native TGP/Sixel protocol or cleanup acceptance.
- Textual serve does not hot-reload a running TUI process; reload the browser tab after code or TCSS edits.
- Browser automation against served Textual must target the xterm helper textarea, not normal DOM inputs.
- Public documentation captures must run from a neutral working directory such as `/tmp/a0-cli-demo`, use demo chat state, and exclude personal paths and browser debug URLs. Inspect captures before copying identical assets to Core and website docs.
- General CLI documentation shows the vanilla Core controls; disable community composer additions such as Reasoning Effort in the capture instance.
- Snapshot output defaults to `devtools/snapshots/`, which is generated output.
- `lock_dependencies.py` uses `uv pip compile` with Python 3.10 as the universal-resolution floor, writes `constraints/a0-runtime.txt` and `constraints/a0-build.txt`, and syncs pinned runtime/build dependencies into `pyproject.toml`.
- Lock regeneration preserves compatible existing pins; `--upgrade` explicitly refreshes all dependencies. `--check` seeds temporary outputs from the saved locks and checks consistency without changing tracked files.

## Work Guidance

- Keep devtools commands Linux-first unless a section is explicitly platform-specific.
- Do not add heavyweight runtime dependencies to devtools without updating dependency inputs and locks.
- Ensure preview subprocess cleanup remains robust on Linux.

## Verification

- `./.venv/bin/python -m pytest tests/test_devtools.py -v`
- `./.venv/bin/python devtools/snapshot.py`
- For dependency changes with `uv` available: `./.venv/bin/python devtools/lock_dependencies.py --check`

## Child DOX Index

No child DOX files.
