"""Textual application for read-only, Vim-style source viewing."""

from __future__ import annotations

from typing import Callable, Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static, TextArea
from textual.widgets.text_area import Selection

from .clipboard import ClipboardCopyError, copy_to_system_clipboard
from .files import LoadedTextFile


ClipboardWriter = Callable[[str], Optional[str]]


class VimTextArea(TextArea):
    """A read-only TextArea with a small, predictable Vim key set."""

    visual_mode = reactive(False)

    BINDINGS = [
        Binding("h,left", "vim_left", "Left", show=False),
        Binding("j,down", "vim_down", "Down", show=False),
        Binding("k,up", "vim_up", "Up", show=False),
        Binding("l,right", "vim_right", "Right", show=False),
        Binding("shift+left", "vim_left(True)", "Select left", show=False),
        Binding("shift+down", "vim_down(True)", "Select down", show=False),
        Binding("shift+up", "vim_up(True)", "Select up", show=False),
        Binding("shift+right", "vim_right(True)", "Select right", show=False),
        Binding("w,ctrl+right", "vim_word_right", "Next word", show=False),
        Binding("b,ctrl+left", "vim_word_left", "Previous word", show=False),
        Binding("0,home", "vim_line_start", "Line start", show=False),
        Binding("$,end", "vim_line_end", "Line end", show=False),
        Binding("g", "vim_document_start", "Document start", show=False),
        Binding("G,shift+g", "vim_document_end", "Document end", show=False),
        Binding(
            "pagedown,ctrl+f,ctrl+d", "vim_page_down", "Page down", show=False
        ),
        Binding("pageup,ctrl+b,ctrl+u", "vim_page_up", "Page up", show=False),
        Binding(
            "shift+pagedown", "vim_page_down(True)", "Select page down", show=False
        ),
        Binding(
            "shift+pageup", "vim_page_up(True)", "Select page up", show=False
        ),
        Binding("v", "toggle_visual", "Visual selection", show=False),
        Binding("escape", "cancel_visual", "Clear selection", show=False),
        Binding("y", "yank", "Copy selection", show=False),
        Binding(
            "ctrl+c,super+c", "copy_selection", "Copy selection", show=False
        ),
    ]

    class ModeChanged(Message):
        """Posted when the keyboard selection mode changes."""

    class CopyRequested(Message):
        """Request that the application copy selected text."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, text: str, *, soft_wrap: bool = True, id: str) -> None:
        super().__init__(
            text,
            read_only=True,
            show_cursor=True,
            show_line_numbers=True,
            soft_wrap=soft_wrap,
            highlight_cursor_line=True,
            id=id,
        )

    def _should_select(self, force_select: bool = False) -> bool:
        return force_select or self.visual_mode

    def action_vim_left(self, force_select: bool = False) -> None:
        self.action_cursor_left(self._should_select(force_select))

    def action_vim_right(self, force_select: bool = False) -> None:
        self.action_cursor_right(self._should_select(force_select))

    def action_vim_up(self, force_select: bool = False) -> None:
        self.action_cursor_up(self._should_select(force_select))

    def action_vim_down(self, force_select: bool = False) -> None:
        self.action_cursor_down(self._should_select(force_select))

    def action_vim_word_left(self) -> None:
        self.action_cursor_word_left(self.visual_mode)

    def action_vim_word_right(self) -> None:
        self.action_cursor_word_right(self.visual_mode)

    def action_vim_line_start(self) -> None:
        self.action_cursor_line_start(self.visual_mode)

    def action_vim_line_end(self) -> None:
        self.action_cursor_line_end(self.visual_mode)

    def action_vim_document_start(self) -> None:
        self.move_cursor((0, 0), select=self.visual_mode)

    def action_vim_document_end(self) -> None:
        self.move_cursor(self.document.end, select=self.visual_mode)

    def _move_page(self, *, down: bool, force_select: bool = False) -> None:
        selecting = self._should_select(force_select)
        anchor = self.selection.start
        if down:
            self.action_cursor_page_down()
        else:
            self.action_cursor_page_up()
        if selecting:
            self.selection = Selection(anchor, self.cursor_location)

    def action_vim_page_down(self, force_select: bool = False) -> None:
        self._move_page(down=True, force_select=force_select)

    def action_vim_page_up(self, force_select: bool = False) -> None:
        self._move_page(down=False, force_select=force_select)

    def action_toggle_visual(self) -> None:
        if self.visual_mode:
            self.action_cancel_visual()
            return
        self.selection = Selection.cursor(self.cursor_location)
        self.visual_mode = True
        self.post_message(self.ModeChanged())

    def action_cancel_visual(self) -> None:
        self.visual_mode = False
        self.selection = Selection.cursor(self.cursor_location)
        self.post_message(self.ModeChanged())

    def _request_copy(self, *, clear_after: bool) -> None:
        selected = self.selected_text
        self.post_message(self.CopyRequested(selected))
        if clear_after and selected:
            self.action_cancel_visual()

    def action_yank(self) -> None:
        self._request_copy(clear_after=True)

    def action_copy_selection(self) -> None:
        self._request_copy(clear_after=False)


class ViewerApp(App[None]):
    """Full-screen application that displays one text file."""

    TITLE = "Pure Text Viewer"
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = "#viewer"

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    #titlebar {
        height: 1;
        padding: 0 1;
        color: $text;
        background: $primary;
        text-overflow: ellipsis;
    }

    #viewer {
        height: 1fr;
        border: none;
        padding: 0;
        scrollbar-size: 1 1;
    }

    #viewer:focus {
        border: none;
    }

    #viewer .text-area--selection {
        color: $text;
        background: $accent;
    }

    #status {
        height: 1;
        padding: 0 1;
        color: $text;
        background: $boost;
        text-overflow: ellipsis;
    }

    #help {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $panel;
        text-overflow: ellipsis;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        source: LoadedTextFile,
        *,
        soft_wrap: bool = True,
        clipboard_writer: ClipboardWriter = copy_to_system_clipboard,
    ) -> None:
        super().__init__()
        self.source = source
        self.soft_wrap = soft_wrap
        self._clipboard_writer = clipboard_writer
        self.last_copy_backend: Optional[str] = None

    def compose(self) -> ComposeResult:
        size = self._format_size(self.source.size_bytes)
        top_line = f"{self.source.path}  |  {self.source.line_count} lines  |  {size}"
        yield Static(top_line, markup=False, id="titlebar")
        yield VimTextArea(self.source.text, soft_wrap=self.soft_wrap, id="viewer")
        yield Static("", markup=False, id="status")
        yield Static(
            "h/j/k/l move  Ctrl-F/B page  v select  y copy  q quit",
            markup=False,
            id="help",
        )

    def on_mount(self) -> None:
        self.query_one("#viewer", VimTextArea).focus()
        self._refresh_status()

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KiB"
        return f"{size_bytes / (1024 * 1024):.1f} MiB"

    def _refresh_status(self) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        row, column = viewer.cursor_location
        selected = viewer.selected_text
        mode = "VISUAL" if viewer.visual_mode else "NORMAL"
        selection_info = f"  |  {len(selected)} chars selected" if selected else ""
        wrap_info = "wrap" if viewer.soft_wrap else "no wrap"
        self.query_one("#status", Static).update(
            f"{mode}  |  Ln {row + 1}, Col {column + 1}  |  {wrap_info}{selection_info}"
        )

    @on(TextArea.SelectionChanged, "#viewer")
    def on_viewer_selection_changed(self, _event: TextArea.SelectionChanged) -> None:
        self._refresh_status()

    @on(VimTextArea.ModeChanged)
    def on_viewer_mode_changed(self, _event: VimTextArea.ModeChanged) -> None:
        self._refresh_status()

    @on(VimTextArea.CopyRequested)
    def on_copy_requested(self, event: VimTextArea.CopyRequested) -> None:
        if not event.text:
            self.notify("No text selected", severity="warning", timeout=1.5)
            return

        self.copy_to_clipboard(event.text)
        try:
            self.last_copy_backend = self._clipboard_writer(event.text)
        except ClipboardCopyError as error:
            self.last_copy_backend = None
            self.notify(str(error), severity="warning", timeout=2.0)
        self.notify(f"Copied {len(event.text)} characters", timeout=1.5)

