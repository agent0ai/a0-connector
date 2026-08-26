# Screens DOX

## Purpose

- Own Textual modal and screen classes for chat selection, compaction, installed plugins, model presets/runtime, profile editing, and project instructions.

## Ownership

- Screen classes collect user intent and return structured results.
- Applying returned results to connector state belongs in app or command modules.

## Local Contracts

- `ChatListScreen` returns a context ID string or `None`.
- Modal screens should expose clear cancel/apply paths, usually with Escape and an explicit Apply/Cancel action.
- Textual `Select` widgets may emit duplicate `Changed` events while overlays close or while options refresh. Treat selection as user intent only when the screen is not suppressing events, the widget is not busy, and the value differs from the last committed value.
- When refreshing `Select` state programmatically, update cached selection inside the suppression window to prevent render loops.
- Result dataclasses should remain stable and easy for tests to assert.
- `ModelPresetsScreen` shows Main, Utility, and Embedding model details and describes clearing a chat override as using the concrete preset from settings.
- `ProfileEditorScreen` returns only Easy-mode title, instructions, and selected
  tool IDs; profile validation and persistence remain in Agent Zero Core.
- `PermissionsScreen` returns sparse Tool/MCP and Skill policy intent; Core's
  Agent Editor remains the persistence and runtime-policy owner.

## Work Guidance

- Keep screen copy compact and operational.
- Prefer local coercion helpers for server payloads rather than trusting payload shape.
- Do not let modal code mutate unrelated app state directly when a returned result can express the intent.

## Verification

- `./.venv/bin/python -m pytest tests/test_model_presets.py tests/test_installed_plugins_screen.py tests/test_app.py -v`

## Child DOX Index
