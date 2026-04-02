# TUI frontend file map

This note lists files that define the **Textual** terminal UI (layout, widgets, styles, and modal screens) for the Agent Zero CLI under `src/agent_zero_cli/`.

## IDE embedded terminal

When you run the CLI inside Cursor or VS Code, it appears in the **integrated terminal** at the bottom of the window. You get the same full-screen TUI as in an external terminal: chat log, status line above the input (`Waiting for input` when idle), multiline input, and a footer with shortcuts (for example `f6` Chats, `f7` Nudge, `f5` Clear, `f8` Pause, `^p` palette).

## Files that are mainly “frontend”

| Path | Role |
|------|------|
| `src/agent_zero_cli/styles/app.tcss` | Global TUI styling (colors, borders, `#chat-log`, `#status-bar`, `#message-input`, modal screens, footer). |
| `src/agent_zero_cli/widgets/activity_bar.py` | Status line above the input (spinner + text while busy, idle text otherwise). |
| `src/agent_zero_cli/widgets/chat_input.py` | Multiline input (Enter to send, grows up to a few lines). |
| `src/agent_zero_cli/widgets/__init__.py` | Re-exports widgets (small; part of the UI package). |
| `src/agent_zero_cli/screens/host_input.py` | Host URL setup screen (TUI overlay). |
| `src/agent_zero_cli/screens/login.py` | Login screen (TUI overlay). |
| `src/agent_zero_cli/screens/chat_list.py` | Chat list picker (TUI overlay). |

## Where UI meets logic

These are not “layout only,” but they drive or support what you see:

| Path | Role |
|------|------|
| `src/agent_zero_cli/app.py` | Main `App`: composes the main screen (`RichLog`, `ActivityBar`, `ChatInput`, `Footer`) and owns WebSocket handling, commands, and most state. |
| `src/agent_zero_cli/__main__.py` | Entry point that starts the app. |
| `src/agent_zero_cli/client.py` | HTTP/WebSocket client (no widgets). |
| `src/agent_zero_cli/config.py` | Configuration and env (no widgets). |

## Tests

`tests/test_app.py` exercises application behavior (including UI-adjacent flows), not the `.tcss` file directly.
