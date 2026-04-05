# Workflow Handoff: a0-connector

> **Session**: 2026-04-04T11:15:53.6781896Z
> **Agent**: Codex (GPT-5)
> **Handoff Reason**: checkpoint

---

## Ξ State Vector

**Objective**: Land the splash-first Agent Zero CLI rework so login/startup, slash tools, and first-chat experience are stable against the live connector.

**Phase**: Runtime Input Regression | Progress: 85% | Status: blocked

**Current Focus**:
The splash/login/chat shell rework is implemented: staged welcome flow, placeholder-first host entry, capability-gated slash/modal commands, the composer layout fix, and the Agent Zero ASCII intro banner in the chat log. `tests/test_app.py` is currently green (`13 passed` via `.\.venv\Scripts\python.exe -m pytest tests/test_app.py -v`), but live typing is blocked: as soon as the user types into the composer, Textual raises `TypeError: ChatInput.Changed.__init__() missing 1 required positional argument: 'input'`. The failure is triggered from `.\.venv\Lib\site-packages\textual\widgets\_text_area.py:1568` when `TextArea.edit()` posts `self.Changed(self)`, which now collides with the custom nested `ChatInput.Changed` signature in `src/agent_zero_cli/widgets/chat_input.py:45-49`. Branch is `dev2`; the worktree is intentionally dirty, including deletion of the in-repo plugin mirror.

**Blocker**:
- Typing into `#message-input` crashes the live app before the composer can be used.
- Resolution path: first inspect `src/agent_zero_cli/widgets/chat_input.py:37-56,133-136` against `.\.venv\Lib\site-packages\textual\widgets\_text_area.py:1565-1568`; fix or rename the custom `Changed` message so it no longer shadows `TextArea.Changed`, then add real widget coverage.

---

## Δ Context Frame

### Decisions Log
| ID | Decision | Rationale | Reversible |
|----|----------|-----------|------------|
| D1 | Keep the splash as the empty-context home and only switch to chat after a real message arrives | The shell should stay calm/useful before conversation exists | yes |
| D2 | Reintroduce the Agent Zero ASCII art inside the chat log, not as the empty-state shell itself | Preserves the real Agent Zero brand splash without sacrificing connection/setup UX | yes |
| D3 | Use `http://127.0.0.1:5080` as placeholder/default, not hardcoded field content | Fix typing friction and avoid overwriting active edits | yes |
| D4 | Use deferred focus helpers (`call_after_refresh`) for splash inputs and the composer | Initial mount/stage changes were stealing focus | yes |
| D5 | Gate `/settings`, `/skills`, `/compact`, `/pause`, `/nudge` from live `capabilities.features` | UI must reflect backend reality instead of assumptions | no |
| D6 | Treat `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector` as the only live plugin source | User removed the repo mirror; runtime truth is dockervolume only | no |
| D7 | Do not fix the new typing crash in this handoff; document it as the immediate next task | User explicitly requested documentation-first, not implementation | yes |

### Constraints Active
- Windows / PowerShell workflow; use `.\.venv\Scripts\python.exe`, not POSIX venv paths.
- The deleted `plugin/a0_connector/**` tree in the repo is intentional; any backend edits must target `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector`.
- `ctrl+p` must remain `show=False` in the footer binding.
- Splash semantics are strict: empty context => splash-ready; first rendered user/assistant message => chat log; `clear_chat` must not resurrect splash for a message-bearing context.
- `tests/test_app.py` uses fakes and does not exercise Textual `TextArea` internals; live typing/render bugs can slip past it.

### Patterns In Use
- **Splash-first shell**: staged host/login/connecting/ready/error flow with `ContentSwitcher` body ownership → See `src/agent_zero_cli/app.py:106-127`, `src/agent_zero_cli/app.py:231-278`, `src/agent_zero_cli/widgets/splash_view.py:268-410`
- **Registry-driven commands**: typed command metadata feeds palette, slash suggestions, availability, and handlers → See `src/agent_zero_cli/app.py:129-223`, `src/agent_zero_cli/commands.py:10-33`
- **Modal-backed optional tools**: settings, skills, and compaction stay thin in `app.py` and push focused screens → See `src/agent_zero_cli/app.py:1127-1234`, `src/agent_zero_cli/screens/settings_modal.py:152-301`, `src/agent_zero_cli/screens/skills_modal.py:195-471`, `src/agent_zero_cli/screens/compact_modal.py:105-253`
- **Intro-banner-before-first-message**: chat log mounts the Agent Zero banner once per fresh context just before first user/assistant content → See `src/agent_zero_cli/widgets/chat_log.py:36-46`, `src/agent_zero_cli/app.py:328-332`, `src/agent_zero_cli/app.py:731-803`
- **Capability-gated connector integration**: client endpoints and live plugin features shape what the shell surfaces → See `src/agent_zero_cli/client.py:488-573`, `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector\api\v1\capabilities.py:11-77`

### Mental Models Required
Empty-context home vs chat intro
: The splash is the stateful home for a connected-but-empty chat; the ASCII banner is a one-time log artifact inserted above the first real message.

Layout ownership
: `#body-switcher` must own the flexible `1fr` height or the bottom composer gets crowded out by the scrollable body.

Capability gating
: Slash commands and welcome actions are not static; they are derived from the connector’s advertised features and current run state.

Fake-widget test gap
: `tests/test_app.py` validates app orchestration with fakes, so it won’t catch base `TextArea` event-contract collisions like the current `ChatInput.Changed` crash.

---

## Φ Code Map

### Modified This Session
| File | Lines | Change Summary |
|------|-------|----------------|
| `src/agent_zero_cli/app.py` | 106-223, 231-333, 513-803, 949-1269 | Splash-first layout, deferred focus, staged connection/login flow, welcome/chat switching, intro-banner reveal, slash dispatch, and modal-backed commands |
| `src/agent_zero_cli/widgets/splash_view.py` | 25-98, 268-467 | Streamlined hero copy, placeholder-first host input, staged panels, and deferred primary focus |
| `src/agent_zero_cli/widgets/chat_log.py` | 12-46, 128-135 | Agent Zero ASCII intro banner mounted above the first chat message and reset on clear |
| `src/agent_zero_cli/widgets/chat_input.py` | 37-56, 106-137, 173-183 | Custom input events, key handling, auto-grow, and the current `Changed`-event shadowing bug |
| `src/agent_zero_cli/styles/app.tcss` | 6-40, 156-245 | Flexible shell layout to keep the composer visible plus chat-intro and splash styling |
| `src/agent_zero_cli/styles/splash.md` | 1-3 | Reduced splash hero copy to a minimal shell introduction |
| `tests/test_app.py` | 20-62, 189-218, 299-439 | Placeholder-host, startup focus, welcome/chat switching, intro-banner, and slash-menu regressions |
| `src/agent_zero_cli/client.py` | 488-573 | Connector-backed settings, agents, skills, model presets, compaction, and chat metadata endpoints |
| `tests/test_plugin_backend.py` | 13-18, 217-404 | Dockervolume-backed plugin import path and capability/proxy coverage |

### Reference Anchors
| File | Lines | Relevance |
|------|-------|-----------|
| `src/agent_zero_cli/commands.py` | 10-33 | Shared command availability/spec types consumed by palette + slash UX |
| `src/agent_zero_cli/widgets/slash_menu.py` | 17-172 | Suggestion menu behavior, highlighting, insertion, and disabled-command rendering |
| `src/agent_zero_cli/screens/settings_modal.py` | 152-301 | Curated settings flow and save validation |
| `src/agent_zero_cli/screens/skills_modal.py` | 195-471 | Skills filters, detail pane, refresh, and delete flow |
| `src/agent_zero_cli/screens/compact_modal.py` | 105-253 | Compaction confirmation flow and gating |
| `tests/test_client.py` | 478-648 | HTTP contract coverage for settings/skills/presets/compaction/chat metadata |
| `docs/architecture.md` | 19-35 | Connector HTTP discovery/auth/connect contract |
| `docs/architecture.md` | 52-72 | Connector WebSocket snapshot/event/complete contract |
| `docs/tui-frontend.md` | 26-28 | File map only; UI composition prose may be stale |
| `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector\api\v1\capabilities.py` | 11-77 | Live feature advertisement and optional-feature gating |
| `.\.venv\Lib\site-packages\textual\widgets\_text_area.py` | 1568 | Base TextArea edit path that posts `self.Changed(self)` and surfaces the current typing crash |

### Entry Points
- **Primary**: `src/agent_zero_cli/widgets/chat_input.py:45` — custom `Changed` message currently collides with `TextArea.Changed` during live typing
- **Startup / State Orchestration**: `src/agent_zero_cli/app.py:513` — staged connection flow into ready splash or error/login paths
- **Chat Reveal / Banner**: `src/agent_zero_cli/app.py:731` — snapshot/event handling that flips splash to chat and reveals the intro banner
- **Test Suite**: `tests/test_app.py` — covers splash lifecycle, welcome switching, intro-banner behavior, and slash UX, but not real TextArea typing internals

---

## Ψ Knowledge Prerequisites

### Documentation Sections
- [ ] `docs/architecture.md` § HTTP routes (`19-35`) — understand capability discovery, auth, and connector POST shape
- [ ] `docs/architecture.md` § WebSocket events (`52-72`) — understand snapshot/event/complete semantics that drive welcome-to-chat switching
- [ ] `docs/tui-frontend.md` § file map (`26-28`) — use for package orientation only; treat UI-composition prose as potentially stale

### Modules to Explore
- [ ] `src/agent_zero_cli/widgets/chat_input.py:37-56,106-137` — understand the custom message classes and why live typing crashes
- [ ] `.\.venv\Lib\site-packages\textual\widgets\_text_area.py:1565-1568` — confirm the upstream `TextArea.edit()` message contract
- [ ] `src/agent_zero_cli/app.py:231-333,513-803,949-1269` — splash state, connection staging, chat reveal, and slash/modal dispatch
- [ ] `src/agent_zero_cli/widgets/chat_log.py:20-135` — intro-banner mount/reset behavior
- [ ] `src/agent_zero_cli/widgets/splash_view.py:55-98,268-467` — placeholder-first host field and deferred focus
- [ ] `src/agent_zero_cli/styles/app.tcss:6-40,156-245` — layout/composer visibility and splash styles
- [ ] `src/agent_zero_cli/client.py:488-573` — connector-backed optional endpoints used by welcome actions/commands

### External References *(optional)*
- Live plugin source of truth: `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector`
- Prior handoff artifact: `.handoffs/handoff-a0-connector-20260404-094943.md`

---

## Ω Forward Vector

### Next Actions *(priority order)*
1. **Diagnose**: Fix the live typing crash by reconciling `ChatInput.Changed` with Textual’s `TextArea.Changed` contract → `src/agent_zero_cli/widgets/chat_input.py:45-49`, `src/agent_zero_cli/widgets/chat_input.py:133-136`, `.\.venv\Lib\site-packages\textual\widgets\_text_area.py:1568`
2. **Verify**: Re-run the browser/terminal UI against the live backend and confirm the composer is visible/focusable and the ASCII banner appears above the first message → `src/agent_zero_cli/styles/app.tcss:6-40`, `src/agent_zero_cli/app.py:717-729`, `src/agent_zero_cli/widgets/chat_log.py:36-46`
3. **Backstop**: Add at least one real widget/render test for `ChatInput` so TextArea-level event regressions are caught before release → `tests/test_app.py`, potentially a new focused test file for `ChatInput`
4. **Stage Carefully**: Review and stage only the intended TUI files on `dev2`; do not restore the deleted in-repo plugin mirror and route any backend work to dockervolume only

### Open Questions
- [ ] Should the custom chat-input change event be renamed entirely instead of shadowing `TextArea.Changed`, or can it safely mirror the base signature?
- [ ] After the typing crash is fixed, does the composer/banner behavior hold in the user’s real logged-in session without additional layout tweaks?
- [ ] Do we want dedicated live-render coverage for the ASCII banner’s block-character encoding as well as its insertion logic?

### Success Criteria
- [ ] Typing into `#message-input` no longer throws `TypeError` in the Textual console
- [ ] The composer remains visible and focusable after login, after agent responses, and after context switches
- [ ] The Agent Zero ASCII banner appears once above the first real message in each fresh chat context
- [ ] Splash remains minimal/placeholder-first and capability-gated commands still behave correctly
- [ ] `.\.venv\Scripts\python.exe -m pytest tests/test_app.py -v` stays green, and any backend edits target dockervolume only

### Hazards / Watch Points
- ⚠️ `tests/test_app.py` is green but currently blind to the live `TextArea` event collision; don’t mistake fake-widget coverage for interactive safety.
- ⚠️ The worktree is intentionally dirty and includes deletion of `plugin/a0_connector/**`; avoid reverting or staging those changes accidentally.
- ⚠️ The banner uses block characters; if any future edits touch encoding/save format, verify UTF-8 rendering in the real Textual session.

---

## Glossary *(session-specific terms)*
| Term | Definition |
|------|------------|
| ready splash | Connected empty-context home state shown before a chat has any visible messages |
| chat intro banner | The one-time Agent Zero ASCII art mounted at the top of `ChatLog` before the first real message |
| connector features | Optional capabilities advertised by `capabilities` and used to gate slash/welcome actions |
| dockervolume plugin | The live backend plugin under `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector` |
| fake-widget gap | The testing blind spot created when `tests/test_app.py` replaces real widgets with fakes instead of exercising Textual internals |
