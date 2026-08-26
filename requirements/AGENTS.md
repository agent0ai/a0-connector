# Requirements DOX

## Purpose

- Own human-edited dependency input files for release locking.

## Ownership

- `a0-runtime.in` owns runtime dependency intent.
- `a0-build.in` owns build dependency intent.
- Generated lock outputs are owned by `constraints/AGENTS.md`.
- Top-level `requirements.txt` is root-owned as a compatibility pointer to the runtime lock.

## Local Contracts

- Edit `.in` files for dependency intent; regenerate locks through `devtools/lock_dependencies.py`.
- Keep platform-specific dependencies guarded with environment markers.
- New dependencies require user approval before installation and must be justified by project needs.
- Dependency changes must keep `pyproject.toml`, `requirements/`, and `constraints/` coherent.
- Terminal images require unconditional `pillow>=10.3.0` plus the two mutually exclusive Python-marked `textual-image` requirements: `>=0.8.5,<0.9` below Python 3.12 and `>=0.13.2,<0.14` on Python 3.12 and later.
- Preserve these conditional markers when changing image dependencies so Python
  3.10 through 3.13 can resolve a compatible renderer without importing it in
  headless or gateway mode.

## Work Guidance

- Prefer the smallest dependency surface that solves the problem.
- Keep minimum-version constraints broad enough for locking but narrow enough to express real compatibility.

## Verification

- With `uv` available: `./.venv/bin/python devtools/lock_dependencies.py --check`
- When image dependency markers change, run the lock check and validate every
  conditional branch represented by the supported Python versions.

## Child DOX Index
