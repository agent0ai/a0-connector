# Documentation DOX

## Purpose

- Own durable project documentation under `docs/`.

## Ownership

- `README.md` in this folder, architecture, configuration, development, headless mode, and TUI frontend docs are owned here.
- Root `README.md` remains root-owned, but docs here must stay consistent with it.

## Local Contracts

- Documentation must reflect the current CLI, connector protocol, and builtin plugin model.
- Plugin/backend docs must state that `_a0_connector` is builtin in Agent Zero Core and not vendored in this repo.
- Use Linux command examples by default. Include macOS or Windows examples only where the documented workflow is platform-specific.
- Architecture docs own protocol descriptions, HTTP routes, WebSocket events, event bridge notes, and plugin/runtime boundaries.
- TUI docs own Textual layout, UI development loop, and browser-preview guidance.

## Work Guidance

- Keep docs concise, operational, and current.
- Remove stale notes instead of explaining history.
- Prefer direct links or file names over duplicated architecture detail when another doc already owns the explanation.

## Verification

- Confirm every file linked from `docs/README.md` exists and matches the root `README.md` claims.

## Child DOX Index
