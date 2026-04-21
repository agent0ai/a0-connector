"""Multi-line chat input widget that grows up to 4 lines."""

from __future__ import annotations

from dataclasses import dataclass, field

from textual import events
from textual.message import Message
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme

from agent_zero_cli.attachments import AttachmentRef, attachment_label


_PLACEHOLDER = "Type a message... (/help for commands)"
_PROGRESS_CLASS = "progress-active"
# Same prefix as Agent Zero WebUI composer (see webui/components/chat/input/input-store.js).
_PROGRESS_PREFIX = "|>  "

# Minimal theme so the input blends with the app style.
_INPUT_THEME = TextAreaTheme(
    name="chat_input",
    syntax_styles={},
)

_MAX_CONTENT_LINES = 4


class ChatInput(TextArea):
    """A multi-line text input that auto-grows up to 4 lines.

    * **Enter** submits the message.
    * **Shift+Enter** / **Ctrl+J** inserts a newline.
    * Scrolls internally when content exceeds 4 lines.
    * While the agent is busy, progress appears as placeholder text inside the
      input (when it is empty), matching the core WebUI behavior.
    """

    @dataclass
    class Submitted(Message):
        """Posted when the user presses Enter to submit."""

        value: str
        input: ChatInput
        attachments: list[AttachmentRef] = field(default_factory=list)

    @dataclass
    class ValueChanged(Message):
        """Posted when the text content changes."""

        value: str
        input: ChatInput

    DEFAULT_CSS = """
    ChatInput {
        height: auto;
        min-height: 3;
        max-height: 6;
    }
    """

    def __init__(
        self,
        *,
        placeholder: str = _PLACEHOLDER,
        id: str | None = None,
    ) -> None:
        super().__init__(
            "",
            language=None,
            theme="css",
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            id=id,
            placeholder=placeholder,
        )
        self._base_placeholder = placeholder
        self._activity_active = False
        self._activity_label = ""
        self._activity_detail = ""
        self.attachments: list[AttachmentRef] = []

    def on_mount(self) -> None:
        self.register_theme(_INPUT_THEME)
        self.theme = "chat_input"
        self._update_height()

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, new: str) -> None:
        self.clear()
        if new:
            self.insert(new)
        self._update_height()

    # ---- key handling ------------------------------------------------

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"ctrl+v", "cmd+v"}:
            attach_clipboard_image = getattr(self.app, "attach_clipboard_image", None)
            if attach_clipboard_image is not None and await attach_clipboard_image():
                event.prevent_default()
                event.stop()
                return

        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text
            attachments = list(self.attachments)
            self.clear()
            self.clear_attachments()
            self._update_height()
            self.post_message(self.Submitted(value=text, input=self, attachments=attachments))
            return

        if event.key == "shift+enter" or event.key == "ctrl+j":
            # Insert a newline
            event.prevent_default()
            event.stop()
            self.insert("\n")
            self._update_height()
            return

        if event.key == "backspace" and not self.text and self.attachments:
            event.prevent_default()
            event.stop()
            self.remove_attachment(-1)
            return

    def _on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._update_height()
        self._sync_progress_placeholder()
        self.post_message(self.ValueChanged(value=self.text, input=self))

    # ---- in-input progress (WebUI-style) ----------------------------

    def _compose_activity_placeholder(self) -> str:
        detail = f" [{self._activity_detail}]" if self._activity_detail else ""
        return f"[dim]{_PROGRESS_PREFIX}{self._activity_label}{detail}[/dim]"

    def _compose_placeholder(self) -> str:
        prefix = f"{attachment_label(len(self.attachments))} " if self.attachments else ""
        if self._activity_active:
            return prefix + self._compose_activity_placeholder()
        return prefix + self._base_placeholder

    def _sync_progress_placeholder(self) -> None:
        if self.text:
            return
        self.placeholder = self._compose_placeholder()

    def set_activity(self, label: str, detail: str = "") -> None:
        """Show progress as the placeholder when the field is empty."""
        self._activity_label = label
        self._activity_detail = detail
        self._activity_active = True
        self.add_class(_PROGRESS_CLASS)
        self._sync_progress_placeholder()

    def set_idle(self) -> None:
        """Clear progress state and restore the normal placeholder."""
        self._activity_active = False
        self._activity_label = ""
        self._activity_detail = ""
        self.remove_class(_PROGRESS_CLASS)
        self.placeholder = self._compose_placeholder()

    # ---- attachments -------------------------------------------------

    def add_attachment(self, attachment: AttachmentRef) -> None:
        if any(existing.path == attachment.path for existing in self.attachments):
            return
        self.attachments.append(attachment)
        self._sync_progress_placeholder()

    def remove_attachment(self, index: int) -> None:
        if not self.attachments:
            return
        self.attachments.pop(index)
        self._sync_progress_placeholder()

    def clear_attachments(self) -> None:
        self.attachments = []
        self._sync_progress_placeholder()

    def set_attachments(self, attachments: list[AttachmentRef]) -> None:
        self.attachments = list(attachments)
        self._sync_progress_placeholder()

    # ---- dynamic height ---------------------------------------------

    def _update_height(self) -> None:
        line_count = self.document.line_count
        # Clamp between 1 and MAX_CONTENT_LINES
        visible = max(1, min(line_count, _MAX_CONTENT_LINES))
        new_h = visible + 2  # +2 for border
        self.styles.height = new_h

    # ---- disabled state ----------------------------------------------

    def watch_disabled(self, disabled: bool) -> None:
        self.read_only = disabled
