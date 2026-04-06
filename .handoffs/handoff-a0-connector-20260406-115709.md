# Workflow Handoff: a0-connector

> **Session**: 2026-04-06T11:57:09.6632517Z
> **Agent**: Codex (GPT-5)
> **Handoff Reason**: checkpoint

---

## Ξ State Vector

**Objective**: Finish the splash-first Agent Zero shell polish so the branded splash is consistent before and after login, and splash-stage errors are shown once without duplicate body copy.

**Phase**: Splash Polish | Progress: 98% | Status: paused

**Current Focus**:
The larger splash-first shell, slash-command registry, capability-gated modal flows, and real `ChatInput` typing fix are already landed on `dev2`. Current uncommitted work is narrowly scoped to the splash surface: the pre-login splash now reuses the same Agent Zero banner as the chat intro, and error-state detail is single-sourced in the upper splash summary while the lower panel keeps only the retry affordance. Working tree is dirty only in four TUI files: `src/agent_zero_cli/widgets/chat_log.py`, `src/agent_zero_cli/widgets/splash_view.py`, `src/agent_zero_cli/styles/app.tcss`, and `tests/test_app.py`.

**Blocker** *(if any)*:
- No functional blocker; all tests are green.
- Resolution path: next session should do live visual verification against the browser/terminal UI and decide whether any final cleanup or spacing polish is still needed.

---

## Δ Context Frame

### Decisions Log
| ID | Decision | Rationale | Reversible |
|----|----------|-----------|------------|
| D1 | Keep the splash as the empty-context home and switch to chat only after a real user/assistant message. | Preserves a calm setup surface and keeps the composer/footer stable before the first chat event. | yes |
| D2 | Use the same Agent Zero banner renderable in both splash and chat views. | Removes brand drift between pre-login and post-login states while keeping chat scrolling behavior unchanged. | yes |
| D3 | Make splash error copy single-sourced in the upper section and keep the lower status panel as retry affordance only. | The duplicated error title/detail looked noisy and visually redundant in error state. | yes |
| D4 | Keep `SplashStatusPanel` verbose for `connecting` but minimal for `error`. | Connecting benefits from inline progress context; error already has stage/message/detail above. | yes |
| D5 | Retain the real-widget `ChatInput` regression test added after the TextArea event collision fix. | Fake-widget tests alone were insufficient to catch live typing regressions. | no |
| D6 | Treat `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector` as the only live plugin source of truth. | Dockerized Agent Zero runs from dockervolume, not the repo mirror. | no |

### Constraints Active
- Windows / PowerShell workflow; use `.\.venv\Scripts\python.exe` for local commands.
- The live backend/plugin source of truth is `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector`.
- `ctrl+p` must remain `show=False` in the footer binding.
- Splash semantics remain strict: empty context shows splash; first visible user/assistant message flips to chat; `F5` clear must not resurrect splash for a message-bearing context.
- `src/agent_zero_cli/styles/splash.md` is no longer driving the splash hero; editing it will not change the current splash UI.

### Patterns In Use
- **Splash-first shell**: `ContentSwitcher` owns splash vs chat body state, with splash stages driven from app state → See `src/agent_zero_cli/app.py:106-127`, `src/agent_zero_cli/app.py:231-333`, `src/agent_zero_cli/app.py:475-729`
- **Shared banner primitive**: splash and chat both render from the same helper instead of separate hero implementations → See `src/agent_zero_cli/widgets/chat_log.py:36-43`, `src/agent_zero_cli/widgets/chat_log.py:134-139`, `src/agent_zero_cli/widgets/splash_view.py:292-312`
- **Single-source splash error narrative**: upper splash summary owns `stage/message/detail`; lower status panel becomes retry-only in error state → See `src/agent_zero_cli/widgets/splash_view.py:179-193`, `src/agent_zero_cli/widgets/splash_view.py:371-392`
- **Capability-gated UX**: welcome actions and slash commands still derive from live connector features, not static assumptions → See `src/agent_zero_cli/app.py:145-217`, `src/agent_zero_cli/app.py:267-278`
- **Hybrid regression safety net**: app orchestration uses fakes, while composer typing uses a real widget harness → See `tests/test_app.py:161-181`, `tests/test_chat_input.py:12-53`

### Mental Models Required
Shared splash/banner
: The same Agent Zero banner renderable appears on the splash before login and in the chat log before the first real message, but only the chat-log instance participates in scroll history.

Splash summary ownership
: In splash stages, the upper `stage/message/detail` area is the authoritative narrative; lower panels are affordances (host/login/progress/retry), not duplicate copy blocks.

Empty-context home
: A connected empty chat still lives on the splash surface; the chat log only appears after a real message event marks the context as message-bearing.

Real-widget coverage
: `tests/test_app.py` covers orchestration with fakes; `tests/test_chat_input.py` is the guardrail for Textual-level typing contracts.

---

## Φ Code Map

### Modified This Session
| File | Lines | Change Summary |
|------|-------|----------------|
| `src/agent_zero_cli/widgets/chat_log.py` | 36-43, 134-139 | Reused a shared banner builder for chat intro and extracted the banner widget helper for splash/chat parity. |
| `src/agent_zero_cli/widgets/splash_view.py` | 155-193, 292-312, 371-392 | Replaced markdown hero with shared banner widget and made error-state status panel hide duplicate title/detail copy. |
| `src/agent_zero_cli/styles/app.tcss` | 22-24, 156-266 | Renamed banner styling to shared `.agent-zero-banner` and preserved splash panel styling around the new shared hero/error behavior. |
| `tests/test_app.py` | 200-217 | Added regressions for shared splash banner usage and retry-only error panel behavior. |

### Reference Anchors
| File | Lines | Relevance |
|------|-------|-----------|
| `src/agent_zero_cli/app.py` | 106-127 | Main composition: `ConnectionStatus`, `ContentSwitcher`, `SplashView`, `ChatLog`, slash menu, composer, footer. |
| `src/agent_zero_cli/app.py` | 231-333 | Splash state syncing, body switching, splash notices, and chat intro reveal logic. |
| `src/agent_zero_cli/app.py` | 475-729 | Startup/login/connection/error/ready flow that feeds splash stages. |
| `src/agent_zero_cli/widgets/chat_input.py` | 37-56, 106-136 | Current `ValueChanged` event model and slash/submit behavior after the typing-crash fix. |
| `tests/test_chat_input.py` | 12-53 | Real Textual widget coverage for live typing and Enter-submit behavior. |
| `tests/test_app.py` | 220-460 | Splash lifecycle, welcome/chat switching, slash UX, and intro-banner orchestration via fakes. |
| `docs/architecture.md` | 19-35 | Connector HTTP discovery/auth/connect contract. |
| `docs/architecture.md` | 52-72 | Connector WebSocket snapshot/event/complete contract. |
| `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector\api\v1\capabilities.py` | 11-77 | Live backend feature advertisement that still gates UI availability. |
| `.handoffs/handoff-a0-connector-20260404-094943.md` | 1-162 | Earlier checkpoint covering the broad splash-first rework and connector-backed modal flows. |
| `.handoffs/handoff-a0-connector-20260404-111553.md` | 1-172 | Earlier checkpoint documenting the `ChatInput.Changed` crash and the rationale for real widget coverage. |

### Entry Points
- **Primary**: `src/agent_zero_cli/app.py:513` — staged connection flow that drives splash `host/login/connecting/ready/error`.
- **Splash UI**: `src/agent_zero_cli/widgets/splash_view.py:292` — splash composition rooted at shared banner + stage summary + stage panels.
- **Chat Intro Banner**: `src/agent_zero_cli/widgets/chat_log.py:36` — one-time chat-log banner mount before the first rendered message.
- **Test Suite**: `tests/test_app.py` — splash lifecycle and UX orchestration; `tests/test_chat_input.py` — real composer typing contract.

---

## Ψ Knowledge Prerequisites

### Documentation Sections
- [ ] `docs/architecture.md` § HTTP routes (`19-35`) — connector discovery/auth/connect expectations still shape startup and splash error flows.
- [ ] `docs/architecture.md` § WebSocket events (`52-72`) — explains why splash remains home until snapshot/event activity reveals chat.
- [ ] `.handoffs/handoff-a0-connector-20260404-094943.md` § full checkpoint — broad context for the splash-first rework and connector-backed modals.
- [ ] `.handoffs/handoff-a0-connector-20260404-111553.md` § runtime regression checkpoint — context for the committed `ChatInput` fix and real-widget backstop.

### Modules to Explore
- [ ] `src/agent_zero_cli/app.py` — understand splash orchestration, body switching, and capability-gated commands.
- [ ] `src/agent_zero_cli/widgets/splash_view.py` — understand stage rendering, shared banner usage, and retry-only error panel behavior.
- [ ] `src/agent_zero_cli/widgets/chat_log.py` — understand shared banner helper vs intro-banner mounting semantics.
- [ ] `src/agent_zero_cli/widgets/chat_input.py` — understand current composer event contract and why the real-widget test exists.
- [ ] `tests/test_app.py` — understand the fake-widget seam and the new splash polish regressions.
- [ ] `tests/test_chat_input.py` — understand the real widget typing/submission safety net.

### External References *(optional)*
- Live plugin root: `C:\Users\3CLYP50\Documents\GitHub\dockervolume\usr\plugins\a0_connector`

---

## Ω Forward Vector

### Next Actions *(priority order)*
1. **Verify**: Run the live browser/terminal UI against a reachable connector and visually confirm the unified splash banner plus single-source error layout → `src/agent_zero_cli/app.py:673`, `src/agent_zero_cli/widgets/splash_view.py:187`
2. **Decide**: Choose whether to keep the retry-only error panel at current size or reduce its visual weight now that it contains no duplicate copy → `src/agent_zero_cli/styles/app.tcss:202-249`
3. **Clean**: Decide whether to remove or repurpose the now-unused `src/agent_zero_cli/styles/splash.md` file so future edits do not target dead UI.
4. **Stage**: Review and stage only the four current worktree files on `dev2`; no backend/plugin changes are pending in this checkpoint.

### Open Questions
- [ ] Should the retry-only error panel become visually smaller or more compact now that it is button-only?
- [ ] Do we want an explicit visual/snapshot regression for splash error state, or are the current unit-style regressions enough?
- [ ] Should `src/agent_zero_cli/styles/splash.md` be deleted, or kept as a future content artifact for a different surface?

### Success Criteria
- [ ] Pre-login splash and first-chat intro use the same Agent Zero banner styling.
- [ ] Splash-stage errors display title/detail once in the upper section, not twice.
- [ ] Chat reveal and scroll behavior remain unchanged after the shared-banner refactor.
- [ ] `.\.venv\Scripts\python.exe -m pytest tests -v` stays green.

### Hazards / Watch Points
- ⚠️ `src/agent_zero_cli/styles/splash.md` is now stale; editing it will not affect the splash unless the splash implementation changes again.
- ⚠️ The banner source contains block characters and appears mojibaked in raw file reads; verify real UTF-8 rendering if the banner text itself is touched.
- ⚠️ App-level splash tests still use fakes; they verify orchestration, not full rendered spacing in a live session.
- ⚠️ Editing only the repo copy of plugin code will not affect the live Dockerized backend.

---

## Glossary *(session-specific terms)*
| Term | Definition |
|------|------------|
| splash-first shell | App-shell pattern where empty-context setup and ready states live on the splash until a real message appears. |
| shared banner primitive | `build_agent_zero_banner_widget()` helper used by both splash and chat intro surfaces. |
| single-source splash error | Error presentation pattern where the upper splash summary owns narrative copy and the lower panel is retry affordance only. |
| fake-widget seam | `tests/test_app.py` strategy that swaps real widgets for fakes to test orchestration without a live Textual loop. |
| real-widget backstop | `tests/test_chat_input.py` harness that exercises actual Textual input behavior to catch event-contract regressions. |
