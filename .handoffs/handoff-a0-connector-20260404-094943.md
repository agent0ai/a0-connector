# Workflow Handoff: a0-connector

> **Session**: 2026-04-04T09:49:43Z
> **Agent**: Codex (GPT-5)
> **Handoff Reason**: checkpoint

---

## Ξ State Vector

**Objective**: Finish and preserve the TUI rework that adds a persistent splash-first shell, central slash-command registry, and connector-backed settings, skills, and compact flows, while syncing the live plugin surface back into the repo mirror.

**Phase**: Post-Implementation Checkpoint | Progress: 96% | Status: paused

**Current Focus**:
Branch is `dev2`. The requested product work is implemented and tests passed; the remaining work is mainly commit preparation, optional cleanup, and any follow-up polish. The repo now contains both the CLI/TUI rework and a mirrored copy of the updated connector plugin under `plugin/a0_connector`, while the live runtime plugin remains the dockervolume copy.

**Blocker** *(if any)*:
- No functional blocker on the feature set. One tooling watch point remains: `devtools/snapshot.py` writes the SVG successfully but the command exits non-zero on Windows because console output includes a Unicode arrow that `cp1252` cannot encode.
- Resolution path: If snapshot automation matters in the next session, patch the printed output to ASCII or force UTF-8 console encoding; otherwise treat the existing SVG artifact as valid and move on.

---

## Δ Context Frame

### Decisions Log
| ID | Decision | Rationale | Reversible |
|----|----------|-----------|------------|
| D1 | Startup/login moved into one staged splash inside the main app shell. | The splash is a persistent empty-state home, not a transient bootstrap modal. It must stay visible after connect until the current context has visible messages. | yes |
| D2 | A central command registry (`CommandSpec` + availability predicate + handler) drives slash UX, `/help`, and command palette population. | Prevents drift between help text, slash suggestions, and available actions. | yes |
| D3 | `/settings`, `/skills`, and `/compact` use connector-backed `A0Client` endpoints only. | The CLI must not call raw Agent Zero core routes directly for these flows. | no |
| D4 | The authoritative backend/plugin source remains `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector`; `plugin/a0_connector` is now only a mirrored in-repo copy. | Dockerized Agent Zero loads the dockervolume plugin at runtime. Editing only the repo mirror will not affect the live backend. | no |
| D5 | `Pause` and `Nudge` remain surfaced but can render disabled explanations when unavailable. No new pause proxy was added in this pass. | Honest UI is better than pretending a connector feature exists. | yes |
| D6 | The repo mirror keeps copied `__pycache__` and `.pyc` files on disk for now, but `.gitignore` excludes them. | Policy/tooling blocked safe binary cleanup during the copy-back pass; ignoring them unblocks commit hygiene. | yes |

### Constraints Active
- Windows / PowerShell workflow. Use `.\.venv\Scripts\python` for local commands.
- Live backend edits must continue in `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector`, then be mirrored back into `plugin/a0_connector` only when needed.
- Do not change `ctrl+p` footer binding semantics.
- Do not import Agent Zero core modules at plugin module scope.
- The splash returns for a brand-new empty chat, not for `F5` clear.
- Optional commands and flows must gate off advertised connector capabilities.

### Patterns In Use
- **Splash-first shell**: app owns `host | login | connecting | ready | error` state and switches between welcome/chat body modes → See `src/agent_zero_cli/app.py:230-271`, `src/agent_zero_cli/widgets/splash_view.py:37-489`
- **Typed command registry**: each command has canonical name, aliases, description, availability, and handler → See `src/agent_zero_cli/commands.py:11-31`, `src/agent_zero_cli/app.py:144-216`
- **Modal flow orchestration**: app fetches connector data, opens focused screens, and applies results on return → See `src/agent_zero_cli/app.py:1101-1254`, `src/agent_zero_cli/screens/settings_modal.py:152-301`, `src/agent_zero_cli/screens/skills_modal.py:195-471`, `src/agent_zero_cli/screens/compact_modal.py:105-253`
- **Thin connector proxies**: plugin handlers stay aligned to existing core payloads with minimal translation → See `plugin/a0_connector/api/v1/settings_get.py:1-12`, `plugin/a0_connector/api/v1/settings_set.py:1-22`, `plugin/a0_connector/api/v1/skills_list.py:1-55`, `plugin/a0_connector/api/v1/compact_chat.py:1-75`

### Mental Models Required
Empty-context home
: Successful connect does not automatically switch to the chat log. The splash remains until the active context has a visible user/assistant message.

Capability-gated UX
: Command availability is a runtime contract derived from connector `capabilities.features`; unsupported flows should hide or disable themselves instead of failing later.

Authoritative live plugin vs repo mirror
: Runtime truth lives in dockervolume. The in-repo `plugin/a0_connector` copy is for repo visibility, review, and commit history after sync.

Slash-menu semantics
: Menu opens when the first token starts with `/`, filters as the token changes, closes on empty input, whitespace after the command token, or `Escape`, and supports `Up/Down`, `Tab`, and zero-arg `Enter`.

---

## Φ Code Map

### Modified This Session
| File | Lines | Change Summary |
|------|-------|----------------|
| `src/agent_zero_cli/app.py` | 144-216, 230-271, 435-708, 877-1268 | Added typed command registry, splash/body-mode orchestration, capability-aware welcome actions, slash-menu routing, and `/settings` `/skills` `/compact` command flows. |
| `src/agent_zero_cli/client.py` | 467-589 | Added connector-backed methods for chat fetch, settings get/set, agents, skills, model presets, compaction stats, and compact execution. |
| `src/agent_zero_cli/commands.py` | 1-33 | Introduced shared command metadata types used by help, slash UX, and palette. |
| `src/agent_zero_cli/widgets/chat_input.py` | 45-168 | Added change/slash navigation messages and slash-menu activity state routing from the input widget. |
| `src/agent_zero_cli/widgets/slash_menu.py` | 1-172 | Added reusable slash suggestion menu widget with highlight and selection messages. |
| `src/agent_zero_cli/widgets/splash_view.py` | 37-489 | Added staged splash view, host/login/status panels, and welcome action deck. |
| `src/agent_zero_cli/screens/settings_modal.py` | 18-301 | Added curated settings modal limited to Agent, Workdir, Authentication, and Runtime sections. |
| `src/agent_zero_cli/screens/skills_modal.py` | 17-471 | Added skills modal with filters, details pane, refresh, and delete confirmation. |
| `src/agent_zero_cli/screens/compact_modal.py` | 16-253 | Added compaction confirmation modal with stats, model selection, preset selection, and disabled-state gating. |
| `src/agent_zero_cli/styles/app.tcss` | 145-306 | Added splash, welcome-action, and slash-menu styling. |
| `src/agent_zero_cli/styles/splash.md` | 1-25 | Normalized hero markdown content rendered by the splash. |
| `src/agent_zero_cli/widgets/connection_status.py` | 23-39 | Fixed connection status rendering path while leaving existing mojibake bullet glyphs untouched. |
| `src/agent_zero_cli/widgets/__init__.py` | 1-10 | Exported new splash and slash-menu widgets. |
| `tests/test_app.py` | 64-388 | Added coverage for staged startup, welcome/chat transitions, slash menu behavior, and registry-driven help. |
| `tests/test_client.py` | 478-633 | Added client coverage for the new connector endpoints. |
| `tests/test_plugin_backend.py` | 217-404 | Added capability/proxy tests using the authoritative dockervolume plugin import path. |
| `plugin/a0_connector/api/v1/capabilities.py` | 11-78 | Mirrored the live plugin capability advertisement including optional feature detection. |
| `plugin/a0_connector/api/v1/settings_get.py` | 1-12 | Mirrored thin settings read proxy. |
| `plugin/a0_connector/api/v1/settings_set.py` | 1-22 | Mirrored thin settings write proxy. |
| `plugin/a0_connector/api/v1/agents_list.py` | 1-15 | Mirrored agents-list proxy. |
| `plugin/a0_connector/api/v1/skills_list.py` | 1-55 | Mirrored skills-list proxy with filtering support. |
| `plugin/a0_connector/api/v1/skills_delete.py` | 1-26 | Mirrored skill-delete proxy. |
| `plugin/a0_connector/api/v1/model_presets.py` | 1-29 | Mirrored model-presets proxy. |
| `plugin/a0_connector/api/v1/compact_chat.py` | 1-75 | Mirrored compact-chat proxy and stats/submit behavior. |
| `.gitignore` | 198-199 | Ignored mirrored plugin `__pycache__` directories and `.pyc` files. |

### Reference Anchors
| File | Lines | Relevance |
|------|-------|-----------|
| `docs/architecture.md` | 19-35, 52-72 | HTTP and WebSocket connector contract the CLI and plugin must honor. |
| `docs/tui-frontend.md` | 26-33 | High-level file map for the TUI/tests; useful for orientation, but parts of the UI composition description are now stale after the splash-first rework. |
| `devtools/snapshot.py` | 41-60 | Snapshot generation entry point; relevant to the Windows `cp1252` console-output hazard. |
| `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector\api\v1\capabilities.py` | 1-78 | Live runtime plugin capability source; future backend edits should start here, not in the repo mirror. |

### Entry Points
- **Primary**: `src/agent_zero_cli/app.py:454` — startup path that probes capabilities, drives splash stages, and establishes the initial UI state.
- **Slash Routing**: `src/agent_zero_cli/app.py:877` — slash token parsing, matching, and dispatch.
- **Connector Client**: `src/agent_zero_cli/client.py:467` — new HTTP surface for settings, skills, presets, and compaction.
- **Test Suite**: `tests/test_app.py` — covers splash lifecycle, welcome/chat switching, and slash UX; `tests/test_client.py` covers new endpoints; `tests/test_plugin_backend.py` covers mirrored plugin behavior.

---

## Ψ Knowledge Prerequisites

### Documentation Sections
- [ ] `docs/architecture.md` § HTTP routes and WebSocket event contract — needed to preserve connector compatibility.
- [ ] `docs/architecture.md` § Authentication Notes — clarifies public capability/login endpoints vs API-key-protected handlers.
- [ ] `docs/tui-frontend.md` § Key files table — useful for orientation only; do not trust the old main-screen composition text without checking code.

### Modules to Explore
- [ ] `src/agent_zero_cli/app.py` — understand splash state, command registry wiring, and modal orchestration.
- [ ] `src/agent_zero_cli/widgets/splash_view.py` — understand staged splash rendering and action dispatch.
- [ ] `src/agent_zero_cli/widgets/slash_menu.py` — understand slash menu selection/highlight behavior.
- [ ] `src/agent_zero_cli/client.py` — understand the connector endpoint contract now expected by the UI flows.
- [ ] `plugin/a0_connector/api/v1/capabilities.py` and sibling proxy handlers — understand which features are advertised and how the thin proxies map to Agent Zero helpers.

### External References *(optional)*
- Live plugin root: `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector`

---

## Ω Forward Vector

### Next Actions *(priority order)*
1. **Review**: Inspect `git status` and stage only the intended TUI/plugin mirror files on branch `dev2` → `src/agent_zero_cli/app.py:144`, `plugin/a0_connector/api/v1/capabilities.py:11`
2. **Decide**: Confirm whether the final commit should include the repo mirror under `plugin/a0_connector` together with the CLI rework, or split the mirror into a separate commit.
3. **Polish**: If future snapshot automation matters, patch `devtools/snapshot.py` output to avoid the Windows `cp1252` Unicode failure → `devtools/snapshot.py:41`
4. **Clean**: Optionally remove on-disk `plugin/a0_connector/__pycache__` artifacts with a binary-safe shell command; git already ignores them via `.gitignore:198-199`.
5. **Extend**: If product scope grows, decide whether `Pause` and `Nudge` should get real connector proxies or remain disabled/help-only states.

### Open Questions
- [ ] Should the commit keep the repo plugin mirror sync in the same changeset as the TUI rework, or separate source/runtime sync from UI behavior?
- [ ] Should we patch the mojibake bullet glyphs in `src/agent_zero_cli/widgets/connection_status.py:28-39`, or leave that for a later encoding cleanup pass?
- [ ] Should `docs/tui-frontend.md` be updated now that the shell is no longer chat-log-only?

### Success Criteria
- [ ] Clean commit prepared without accidental inclusion of unrelated pre-existing changes such as `devtools/README.md`, `devtools/serve.py`, or `devtools/snapshots/tui_snapshot.svg`.
- [ ] Repo mirror under `plugin/a0_connector` matches the live dockervolume plugin surface used by the new client methods.
- [ ] Tests remain green for app, client, and plugin backend coverage.
- [ ] Any next-session work starts from the dockervolume plugin as runtime truth, not from the repo mirror alone.

### Hazards / Watch Points
- ⚠️ Editing only `plugin/a0_connector` will not change the live Dockerized Agent Zero backend.
- ⚠️ `plugin/a0_connector` still contains copied `__pycache__`/`.pyc` files on disk; they are ignored by git but can confuse manual file operations.
- ⚠️ `docs/tui-frontend.md` is useful as a map, but some prose predates the splash-first shell and modal flow additions.

---

## Glossary *(session-specific terms)*
| Term | Definition |
|------|------------|
| Splash-first shell | App-shell pattern where the welcome surface is the empty-state home and remains visible until the active context has visible messages. |
| Command registry | Shared `CommandSpec` metadata used to drive help text, slash suggestions, palette entries, and runtime availability checks. |
| Repo mirror | The in-repo `plugin/a0_connector` copy synced from the authoritative live plugin in dockervolume. |
| Capability gate | UI/command availability rule derived from `capabilities.features` rather than optimistic hard-coding. |
