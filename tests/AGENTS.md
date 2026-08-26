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
- `tests/test_plugin_backend.py` may resolve a plugin root from `A0_CONNECTOR_PLUGIN_ROOT`, a local `plugin/`, or a sibling Agent Zero checkout. Keep fake Agent Zero helper modules isolated and reset between tests.
- Certificate fixtures are test assets only; do not replace them with real secrets.
- Transcript image widget tests use fake renderers backed by ordinary Textual widgets; focused tests must not instantiate native terminal-image controls or emit terminal graphics sequences.

## Work Guidance

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
