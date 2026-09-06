# Tests DOX

## Purpose

- Own the pytest suite, fixtures, fake widgets/clients, and local regression coverage.

## Ownership

- All files under `tests/` are owned here, including self-signed certificate fixtures.
- Tests may import package sources and package backend `src/` directories directly when validating local package contracts.

## Local Contracts

- Async tests use pytest/anyio with asyncio-compatible fixtures; many files set `pytestmark = pytest.mark.anyio`.
- Prefer `tmp_path`, `monkeypatch`, and local fake classes over real user config or live services.
- `tests/test_app.py` fake widgets mirror the widget API used by `AgentZeroCLI`. When app code calls a new widget method, update the fake.
- Image-rendering tests use `ImageRenderer.for_test()` or a fake renderer; they
  must not probe a developer terminal, import native terminal backends for
  headless/gateway tests, or require a live Agent Zero image endpoint.
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
  `test_browser_extension_cli.py` also rejects incomplete successful native
  install results; accepting an installed result does not establish discovery
  trust or relax independent final-byte pins.
  Lifecycle receipt cases bind repair/local-uninstall operation and exit code,
  reject paths and forged completion, and preserve pending credential cleanup.
  Credential-inventory pending receipts must not imply registration retirement,
  zero registrations, completed cleanup, or successful uninstall.
- `tests/test_plugin_backend.py` may resolve a plugin root from `A0_CONNECTOR_PLUGIN_ROOT`, a local `plugin/`, or a sibling Agent Zero checkout. Keep fake Agent Zero helper modules isolated and reset between tests.
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
- Preview tests must assert forced half-cell mode and usable explicit widget
  construction, while native TGP/Sixel visual acceptance remains a separately
  recorded capable-terminal check.
- Keep test names behavior-oriented.
- Avoid sleeps and timing assumptions unless there is no better signal.
- Use full suite verification for shared protocol, backend contract, or UI orchestration changes.
- Gateway coverage must include its parser/JSONL contract, tools-only connection
  without chat creation, authentication and capability failures, no-context
  reconnect, all four tool families, five permission scopes, scope dependencies,
  result-before-metadata ordering, correlated command success/failure, and
  complete shutdown cleanup. macOS Computer Use coverage must assert staged
  Accessibility then Screen Recording setup, one prompt per service, fresh
  helper polling, bounded timeout, and resumption of the original start request.

## Verification

- Full suite: `./.venv/bin/python -m pytest tests/ -v`.
- Async fallback: `./.venv/bin/python -m pytest tests/ -v -p anyio --anyio-backends=asyncio`.

## Child DOX Index
