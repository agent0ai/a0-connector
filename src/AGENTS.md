# Source Tree DOX

## Purpose

- Own Python source code under `src/`.
- Route CLI package work to the narrower `agent_zero_cli` contract.

## Ownership

- `src/agent_zero_cli/` is the only durable package in this tree and has its own child DOX.
- This doc owns source-tree level packaging assumptions and any future sibling packages under `src/`.

## Local Contracts

- The root `pyproject.toml` builds `src/agent_zero_cli` into the `a0` distribution.
- Keep CLI package imports compatible with Python 3.11+.
- Do not add platform-specific desktop libraries to source-tree imports unless guarded so other platforms can still import the CLI.

## Work Guidance

- Prefer package-relative imports matching the current `agent_zero_cli` style.
- Keep implementation details in the package subtree rather than adding source-tree globals.

## Verification

- `./.venv/bin/python -m pytest tests/ -v`

## Child DOX Index

- `agent_zero_cli/AGENTS.md` - Main Textual CLI, connector client, commands, remote tools, and local UI contracts.
