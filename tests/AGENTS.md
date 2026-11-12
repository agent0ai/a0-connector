# Tests DOX

## Purpose

- Own the pytest suite, fixtures, fake widgets/clients, and local regression coverage.

## Ownership

- All files under `tests/` are owned here, including self-signed certificate fixtures.
- Tests may import package sources and package backend `src/` directories directly when validating local package contracts.
- `test_computer_use_contract.py` checks macOS and Windows backend imports and boolean payload behavior in fresh interpreters, both with the current CLI and with `agent_zero_cli.text_utils` unavailable as in older releases.
- `test_plugin_backend.py` exercises Core's first-action session startup through the shared `ComputerUseManager` with fake platform helpers: chat isolation/reuse, no input before startup, existing silent approval, screenshot delivery, and bounded failures without permission retries.
- `test_composer_controls.py` covers generic optional controls, two unrelated descriptors, older Core, stale host/chat/revision work, CSRF/base paths, and anchored choice popovers using fake clients and Textual pilots. Fixtures must not depend on any particular optional plugin.
- `test_chat_log.py` covers layout-supplied rendering widths, resize/border integrity, repaint-only status animation with cached details, and coalesced bottom-follow callbacks. Assert work counts and lifecycle behavior rather than machine-dependent timing thresholds.

## Local Contracts

- Async tests use pytest/anyio with asyncio-compatible fixtures; many files set `pytestmark = pytest.mark.anyio`.
- Prefer `tmp_path`, `monkeypatch`, and local fake classes over real user config or live services.
- `tests/test_app.py` fake widgets mirror the widget API used by `AgentZeroCLI`. When app code calls a new widget method, update the fake.
- Connection-header coverage checks initial host selection and manual entry stay quiet, while connection attempts, errors, and connected chats restore the status row.
- Splash layout tests load the real stylesheet and echo host-state messages like the app. Check complete rendered button labels, heading/Refresh alignment and spacing, the bottom manual-URL toggle, absence of splash workspace paths, visible connection controls at compact sizes, stable selection/focus, and manual-input preservation across resize.
- `test_banner.py` checks the shared splash/chat logo at width boundaries and through resize, including full lettering in short wide windows and the symbol-only fallback in narrow ones.
- Chat-tab regression coverage clicks `+` and returns to the original tab using real widgets, checking one banner per transcript with both live and replayed greetings.
- `test_chat_list.py` covers repeated F6 during loading/selection, immediate dismissal before a slow chat switch, mouse/keyboard selection, cancellation, and long-list navigation without per-chat widget trees.
- Chat search coverage retains literal highlighted snippets, local title hits, candidate scope/order, keyboard focus/selection, pending-result guards, stale cancellation, empty/error recovery, and short-terminal bounds. `test_client.py` checks the query request and older/malformed Core replies; Core owns search validation and matching tests.
- Image-rendering tests use `ImageRenderer.for_test()` or a fake renderer; they must not probe a developer terminal, import native terminal backends for headless/gateway tests, or require a live Agent Zero image endpoint.
- Connector/plugin tests should not require a live Agent Zero server unless a test is explicitly designed as live/integration coverage.
- Instance-discovery scenarios must stub every competing runtime path they do
  not exercise, including Docker CLI, local sockets, HTTP APIs, and WSL, so a
  developer's running containers cannot change expected results.
- Browser-extension CLI tests prove exact command routing, fail-closed catalog/companion absence, exact JSON contract acceptance, stable exit meanings, browser target validation, private-output redaction, capped stdout/stderr with overflow termination, authenticated/CSRF remote status independent of companion availability, and explicit terminal-only Chrome pairing handoff. JSON/redirected output must create no pairing secret; creation is one authenticated CSRF POST with exact host/identity/expiry validation. Child credential environment variables remain removed.
- `test_browser_extension_bootstrap.py` uses only synthetic never-executed
  archives and test-only compiled pins. Check private retained final bytes,
  digest and archive rejection, redirected download rejection, empty-policy
  no-network behavior, and exact bootstrap-to-native invocation/cleanup order.
  Static provisioned-release assertions additionally bind the actual reviewed
  Mac 2.12.3 source pins and retained 2.12.0/2.12.1 r2 and 2.12.2 pins
  (archive versus executable versus catalog), exact
  immutable download URL and production origin; newest compatible selection
  is independent of registry order.
  These pins do not raise the independently enforced 2.12.0 secure floor.
  Other platforms select
  no artifact and perform no download. These tests never fetch, execute or
  install the provisioned companion and are not platform acceptance evidence.
- `test_browser_extension_release.py` builds only temporary synthetic native
  install-state trees and monkeypatches compiled pins inside tests. Cover exact
  release discovery without process execution; reject changed bytes/size/key,
  symlinks/hardlinks, nonprivate state/root, duplicate or unknown JSON fields,
  unapproved/old releases, pending transactions, and forged manifest binding
  even when its local digest is updated. Empty production pins remain unable to
  trust PATH or an installed program's self-report; diagnostic errors are fixed
  and pathless. Fixture pins must never enter distributable registry source.
  Create the synthetic manifest directory with explicit safe permissions,
  independent of the test runner's umask, and separately reject writable parents.
- `test_browser_extension_windows.py` exercises native private ACLs, retained
  handles, hardlink/share-mode rejection, install locks and bootstrap cleanup.
  Synthetic installed-state discovery substitutes only the registry read;
  real HKCU and Credential Manager effects belong to separately approved native
  opt-in tests. Skip Unix permission fixtures on Windows and Windows-only
  imports elsewhere; neither skip set establishes the other platform's acceptance.
- `test_browser_extension_linux_packaging.py` reads the candidate builder's
  reproducible one-file archive through the independent CLI bootstrap extractor.
  Use synthetic never-executed bytes; reject empty payloads and source symlinks.
  The Linux bootstrap delegates arguments to a fake CLI and refuses root;
  tests do not download a CLI or register a native host.
  Real Secret Service and Chromium tests remain explicit opt-in host checks.
  `test_browser_extension_cli.py` also rejects incomplete successful native
  install results; accepting an installed result does not establish discovery
  trust or relax independent final-byte pins.
  Lifecycle receipt cases bind repair/local-uninstall operation and exit code,
  reject paths and forged completion, and preserve pending credential cleanup.
  Credential-inventory pending receipts must not imply registration retirement,
  zero registrations, completed cleanup, or successful uninstall.
- `tests/test_plugin_backend.py` may resolve a plugin root from `A0_CONNECTOR_PLUGIN_ROOT`, a local `plugin/`, or a sibling Agent Zero checkout. Keep fake Agent Zero helper modules isolated and reset between tests.
- Its legacy CLI fixtures expose no restricted principal and mirror Core's
  boolean argument coercion; do not substitute the broader CLI flag parser.
  The tool-policy stub supplies an allowed decision for transport tests. Core
  owns verification of policy denial.
- Certificate fixtures are test assets only; do not replace them with real secrets.
- Transcript image widget tests use fake renderers backed by ordinary Textual widgets; focused tests must not instantiate native terminal-image controls or emit terminal graphics sequences.

## Work Guidance

- Development companion tests cover explicit source selection, confirmation
  before process launch, native-only registration delegation, strict channel
  and identity readback, malformed/duplicate result rejection, and separation
  from production command routing. They do not certify the selected program's
  provenance or imply a paired browser-control runtime.
  Development update coverage must prove explicit native-only delegation,
  confirmation before execution, no browser-target replacement, and exact
  action readback; filesystem transactions remain the native test boundary.

- Add focused regression tests near the behavior changed.
- Preview tests must assert forced half-cell mode and usable explicit widget construction, while native TGP/Sixel visual acceptance remains a separately recorded capable-terminal check.
- Keep test names behavior-oriented.
- Mark tests for platform-only modules so a full suite on another supported OS skips them before importing unavailable platform internals.
- Avoid sleeps and timing assumptions unless there is no better signal.
- Use full suite verification for shared protocol, backend contract, or UI orchestration changes.
- Gateway coverage must include its parser/JSONL contract, tools-only connection without chat creation, authentication and capability failures, no-context reconnect, all four tool families, five permission scopes, scope dependencies, result-before-metadata ordering, correlated command success/failure, and complete shutdown cleanup. macOS Computer Use coverage must assert staged Accessibility then Screen Recording setup, one prompt per service, fresh helper polling, bounded timeout, and resumption of the original start request. macOS A0 Tag coverage must separately prove that its private session still requires Accessibility but can return text-only context without Screen Recording, while verified native-window screenshots remain fail-closed.
- Windows A0 Tag coverage must exercise both HWND-backed native Edit ranges and HWND-less UIA TextPattern fields, protected-field ordering, exact Unicode value/range/caret revalidation, normalization rollback, target lifecycle, failed-context target disposal, stdio privacy, and refusal to replace failed active-window crops with desktop screenshots. Windows ordinary input coverage must prove that printable text, including pywinauto metacharacters and Unicode surrogate pairs, remains literal while explicit key chords keep their native virtual-key mapping.
- Large-payload boundary, binary, cancellation, and reconnect regressions belong in the default suite. The 16/32/64 MiB transport soak is marked `large_payload_soak` and remains skipped unless `A0_RUN_LARGE_PAYLOAD_SOAK=1`; this keeps ordinary CI fast while exposing one deterministic nightly command.

## Verification

- Full suite: `./.venv/bin/python -m pytest tests/ -v`.
- Async fallback: `./.venv/bin/python -m pytest tests/ -v -p anyio --anyio-backends=asyncio`.
- Large-payload soak: `A0_RUN_LARGE_PAYLOAD_SOAK=1 ./.venv/bin/python -m pytest tests/test_client.py -m large_payload_soak -v`.

## Child DOX Index

No child DOX files.
