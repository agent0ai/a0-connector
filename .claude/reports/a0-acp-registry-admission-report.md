# Implementation Report — A0 ACP Registry Admission

**Plan**: `.claude/plans/a0-acp-registry-admission.md`
**Branch**: `codex/acp-registry-admission-clean`
**Status**: COMPLETE

## Summary

A0 now conditionally advertises ACP terminal authentication and provides the
`a0 acp --login` command required by that method. The command reuses a verified
remembered session when available; otherwise it accepts ephemeral credentials
or prompts, and only persists the existing protected session-cookie store after
successful authentication. The package is release-ready as `a0` version 2.11.0
with a GitHub Release to PyPI trusted-publishing workflow.

## Tasks completed

- ACP terminal authentication → `src/agent_zero_cli/acp.py` (UPDATE)
- ACP login command routing → `src/agent_zero_cli/__main__.py` (UPDATE)
- Regression coverage → `tests/test_acp.py`, `tests/test_entrypoint.py` (UPDATE)
- Release metadata and documentation → `pyproject.toml`, `src/agent_zero_cli/__init__.py`, `README.md` (UPDATE)
- Trusted publishing → `.github/workflows/publish-pypi.yml` (CREATE)

## Tests added

- Terminal authentication is only advertised to terminal-auth-capable ACP clients.
- Saved-session reuse, successful credential login, and failed login persistence behavior.
- `a0 acp --login` parser routing.

## Validation results

- `PYTHONPATH="src:$A0_SITE" /Users/lazy/a0-ready-test/.venv/bin/python -m pytest tests/test_acp.py tests/test_entrypoint.py -v` — PASS, 17 tests.
- `uv build --offline` — PASS: built `a0-2.11.0.tar.gz` and `a0-2.11.0-py3-none-any.whl`.
- Wheel metadata inspection — PASS: `Name: a0`, `Version: 2.11.0`.
- ACP stdio initialize handshake — PASS: a terminal-capable client receives the
  `a0-web-login` terminal method with `--login`.
- `git diff --check` — PASS.

## Deviations from the plan

None. The local checkout had no project virtual environment, so focused tests
used the already-installed A0 runtime packages with the existing
`/Users/lazy/a0-ready-test` test runner; no dependencies were installed.

## Issues encountered

- The PyPI project does not yet exist and the upstream release has not been
  merged/tagged. The workflow is ready, but publishing requires the maintainer
  to configure PyPI Trusted Publishing for `agent0ai/a0-connector` and publish
  the release. The ACP Registry entry should be submitted only after that
  artifact is publicly resolvable.
