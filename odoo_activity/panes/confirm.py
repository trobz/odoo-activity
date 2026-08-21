"""ConfirmScreen — Yes/No popup, shared by the app shell and any pane that
needs to confirm a mutating action (start/stop/restart, kill, signals).

PromptScreen is its text-input sibling, for an action that needs a value
from the user rather than a plain go/no-go (e.g. Mail's Send test mail,
which needs a recipient address)."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No popup. Dismisses with the chosen bool."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 50; height: auto;
        border: round $accent; background: $surface;
        padding: 1;
    }
    #confirm-msg { margin-bottom: 1; text-align: center; }
    #confirm-buttons { height: 3; align: center middle; }
    #confirm-buttons Button { margin: 0 1; }
    """

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", id="confirm-yes", variant="error")
                yield Button("No", id="confirm-no", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class PromptScreen(ModalScreen[str | None]):
    """Text-input popup. Dismisses with the entered text (stripped), or None
    on Cancel/escape/empty -- so a caller can treat "nothing typed" the same
    as "cancelled" without checking both."""

    DEFAULT_CSS = """
    PromptScreen { align: center middle; }
    #prompt-box {
        width: 60; height: auto;
        border: round $accent; background: $surface;
        padding: 1;
    }
    #prompt-msg { margin-bottom: 1; }
    #prompt-input { margin-bottom: 1; }
    #prompt-buttons { height: 3; align: center middle; }
    #prompt-buttons Button { margin: 0 1; }
    """

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]

    def __init__(self, message: str, placeholder: str = "") -> None:
        super().__init__()
        self._message = message
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Static(self._message, id="prompt-msg")
            yield Input(placeholder=self._placeholder, id="prompt-input")
            with Horizontal(id="prompt-buttons"):
                yield Button("Send", id="prompt-ok", variant="error")
                yield Button("Cancel", id="prompt-cancel", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prompt-ok":
            self._submit()
        else:
            self.dismiss(None)

    def _submit(self) -> None:
        self.dismiss(self.query_one("#prompt-input", Input).value.strip() or None)
