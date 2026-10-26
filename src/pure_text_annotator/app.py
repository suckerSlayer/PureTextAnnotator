"""Textual application for Vim-style source viewing and sidecar notes."""

from __future__ import annotations

from bisect import bisect_right
from typing import Callable, Iterable, Optional

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Input, Static, TextArea
from textual.widgets.text_area import Selection

from .annotations import (
    AnnotationDraft,
    AnnotationFileError,
    AnnotationLink,
    AnnotationRange,
    AnnotationStore,
    create_record,
    draft_from_selection,
    location_to_offset,
    parse_annotation_links,
)
from .clipboard import ClipboardCopyError, copy_to_system_clipboard
from .files import LoadedTextFile


ClipboardWriter = Callable[[str], Optional[str]]


class VimTextArea(TextArea):
    """A read-only TextArea with a small, predictable Vim key set."""

    COMPONENT_CLASSES = TextArea.COMPONENT_CLASSES | {
        "text-area--annotation",
        "text-area--active-annotation",
        "text-area--search-match",
    }

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
        Binding("a", "annotate", "Add annotation", show=False),
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

    class AnnotationRequested(Message):
        """Request a note for the widget's current selection."""

        def __init__(self, selection: Selection) -> None:
            super().__init__()
            self.selection = selection

    def __init__(
        self,
        text: str,
        *,
        soft_wrap: bool = True,
        annotation_ranges: Iterable[AnnotationRange] = (),
        id: str,
    ) -> None:
        super().__init__(
            text,
            read_only=True,
            show_cursor=True,
            show_line_numbers=True,
            soft_wrap=soft_wrap,
            highlight_cursor_line=False,
            id=id,
        )
        self._annotation_ranges: tuple[AnnotationRange, ...] = ()
        self._annotation_spans: dict[int, tuple[tuple[int, int], ...]] = {}
        self._active_annotation_range: Optional[AnnotationRange] = None
        self._active_annotation_spans: dict[
            int, tuple[tuple[int, int], ...]
        ] = {}
        self._search_spans: dict[int, tuple[tuple[int, int], ...]] = {}
        self.set_annotation_ranges(annotation_ranges)

    @property
    def annotation_ranges(self) -> tuple[AnnotationRange, ...]:
        """The valid persisted ranges currently highlighted in the source."""

        return self._annotation_ranges

    @property
    def active_annotation_range(self) -> Optional[AnnotationRange]:
        """The source range synchronized with the focused sidecar entry."""

        return self._active_annotation_range

    def set_annotation_ranges(
        self, annotation_ranges: Iterable[AnnotationRange]
    ) -> None:
        """Replace source highlights using global code-point offsets."""

        text = self.text
        valid_ranges = tuple(
            annotation_range
            for annotation_range in annotation_ranges
            if 0 <= annotation_range.start < annotation_range.end <= len(text)
        )
        self._annotation_ranges = valid_ranges
        if self._active_annotation_range not in valid_ranges:
            self._active_annotation_range = None
        self._rebuild_annotation_spans()
        self._line_cache.clear()
        self.refresh()

    def set_active_annotation_range(
        self, annotation_range: Optional[AnnotationRange]
    ) -> None:
        """Highlight one synchronized range while leaving the others yellow."""

        active_range = (
            annotation_range
            if annotation_range in self._annotation_ranges
            else None
        )
        if active_range == self._active_annotation_range:
            return
        self._active_annotation_range = active_range
        self._rebuild_annotation_spans()
        self._line_cache.clear()
        self.refresh()

    def _rebuild_annotation_spans(self) -> None:
        """Rebuild normal and active highlights from the persisted ranges."""

        active_range = self._active_annotation_range
        inactive_ranges = tuple(
            annotation_range
            for annotation_range in self._annotation_ranges
            if annotation_range != active_range
        )
        self._annotation_spans = self._build_annotation_spans(
            self.text, inactive_ranges
        )
        self._active_annotation_spans = self._build_annotation_spans(
            self.text,
            () if active_range is None else (active_range,),
        )

    def set_search_match(
        self, search_match: Optional[AnnotationRange]
    ) -> None:
        """Highlight the current search result without selecting its text."""

        valid_match = (
            search_match
            if search_match is not None
            and 0 <= search_match.start < search_match.end <= len(self.text)
            else None
        )
        self._search_spans = self._build_annotation_spans(
            self.text,
            () if valid_match is None else (valid_match,),
        )
        self._line_cache.clear()
        self.refresh()

    @staticmethod
    def _build_annotation_spans(
        text: str, annotation_ranges: Iterable[AnnotationRange]
    ) -> dict[int, tuple[tuple[int, int], ...]]:
        lines = text.split("\n")
        line_starts = [0]
        line_starts.extend(
            index + 1
            for index, character in enumerate(text)
            if character == "\n"
        )
        spans_by_line: dict[int, list[tuple[int, int]]] = {}

        for annotation_range in annotation_ranges:
            first_row = bisect_right(line_starts, annotation_range.start) - 1
            last_row = bisect_right(line_starts, annotation_range.end - 1) - 1
            for row in range(first_row, last_row + 1):
                line_start = line_starts[row]
                line_end = line_start + len(lines[row])
                span_start = max(annotation_range.start, line_start)
                span_end = min(annotation_range.end, line_end)
                if span_start < span_end:
                    spans_by_line.setdefault(row, []).append(
                        (span_start - line_start, span_end - line_start)
                    )

        merged_by_line = {}
        for row, spans in spans_by_line.items():
            merged = []
            for span_start, span_end in sorted(spans):
                if merged and span_start <= merged[-1][1]:
                    previous_start, previous_end = merged[-1]
                    merged[-1] = (previous_start, max(previous_end, span_end))
                else:
                    merged.append((span_start, span_end))
            merged_by_line[row] = tuple(merged)
        return merged_by_line

    def get_line(self, line_index: int) -> Text:
        """Return one source line with persisted annotation ranges styled."""

        line = super().get_line(line_index)
        annotation_style = self.get_component_rich_style("text-area--annotation")
        for start, end in self._annotation_spans.get(line_index, ()):
            line.stylize(annotation_style, start, end)
        active_style = self.get_component_rich_style(
            "text-area--active-annotation"
        )
        for start, end in self._active_annotation_spans.get(line_index, ()):
            line.stylize(active_style, start, end)
        search_style = self.get_component_rich_style("text-area--search-match")
        for start, end in self._search_spans.get(line_index, ()):
            line.stylize(search_style, start, end)
        return line

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

    def action_annotate(self) -> None:
        self.post_message(self.AnnotationRequested(self.selection))

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


class AnnotationTextArea(TextArea):
    """Read-only sidecar view whose cursor tracks annotation entries."""

    COMPONENT_CLASSES = TextArea.COMPONENT_CLASSES | {
        "text-area--active-annotation",
        "text-area--annotation-quote",
        "text-area--annotation-meta",
        "text-area--inactive-annotation",
        "text-area--search-match",
    }

    BINDINGS = [
        Binding("h,left", "cursor_left", "Left", show=False),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("l,right", "cursor_right", "Right", show=False),
        Binding("0,home", "cursor_line_start", "Line start", show=False),
        Binding("$,end", "cursor_line_end", "Line end", show=False),
        Binding("g", "document_start", "Document start", show=False),
        Binding("G,shift+g", "document_end", "Document end", show=False),
        Binding(
            "pagedown,ctrl+f,ctrl+d", "cursor_page_down", "Page down", show=False
        ),
        Binding(
            "pageup,ctrl+b,ctrl+u", "cursor_page_up", "Page up", show=False
        ),
    ]

    def __init__(
        self,
        text: str,
        *,
        annotation_links: Iterable[AnnotationLink] = (),
        id: str,
    ) -> None:
        super().__init__(
            text,
            read_only=True,
            show_cursor=True,
            show_line_numbers=False,
            soft_wrap=True,
            highlight_cursor_line=False,
            id=id,
        )
        self._active_link: Optional[AnnotationLink] = None
        self._annotation_links: tuple[AnnotationLink, ...] = ()
        self._annotation_links_by_line: dict[int, AnnotationLink] = {}
        self._metadata_lines: frozenset[int] = frozenset()
        self._quote_lines: frozenset[int] = frozenset()
        self._search_spans: dict[int, tuple[tuple[int, int], ...]] = {}
        self.set_annotation_links(annotation_links)

    @property
    def active_link(self) -> Optional[AnnotationLink]:
        """The entry currently synchronized with the source pane."""

        return self._active_link

    def set_annotation_links(
        self, annotation_links: Iterable[AnnotationLink]
    ) -> None:
        """Replace the semantic sidecar ranges used for line styling."""

        links = tuple(annotation_links)
        links_by_line: dict[int, AnnotationLink] = {}
        metadata_lines: set[int] = set()
        quote_lines: set[int] = set()

        lines = self.text.splitlines()
        if lines and lines[0].strip() == "---":
            for line_number in range(1, len(lines)):
                if lines[line_number].strip() == "---":
                    metadata_lines.update(range(line_number + 1))
                    break

        for link in links:
            links_by_line.update(
                (line_number, link)
                for line_number in range(
                    link.sidecar_start_line,
                    link.sidecar_end_line + 1,
                )
            )
            metadata_lines.update(
                line_number
                for line_number in range(
                    link.sidecar_start_line,
                    link.sidecar_metadata_end_line + 1,
                )
                if not lines[line_number].startswith("## Annotation ")
            )
            metadata_lines.add(link.sidecar_end_line)
            if (
                link.sidecar_quote_start_line is not None
                and link.sidecar_quote_end_line is not None
            ):
                quote_lines.update(
                    range(
                        link.sidecar_quote_start_line,
                        link.sidecar_quote_end_line + 1,
                    )
                )

        self._annotation_links = links
        self._annotation_links_by_line = links_by_line
        self._metadata_lines = frozenset(metadata_lines)
        self._quote_lines = frozenset(quote_lines)
        if self._active_link not in links:
            self._active_link = None
        self._line_cache.clear()
        self.refresh()

    def show_link(self, link: AnnotationLink, *, move_cursor: bool) -> None:
        """Mark an annotation block and optionally center its first line."""

        changed = link != self._active_link
        self._active_link = link
        if changed:
            self._line_cache.clear()
            self.refresh()
        if move_cursor and self.cursor_location[0] != link.sidecar_note_line:
            self.move_cursor((link.sidecar_note_line, 0), center=True)

    def clear_active_link(self) -> None:
        """Remove the active-entry marker when no valid annotations exist."""

        if self._active_link is not None:
            self._active_link = None
            self._line_cache.clear()
            self.refresh()

    def set_search_match(
        self, search_match: Optional[AnnotationRange]
    ) -> None:
        """Highlight the current search result without selecting its text."""

        valid_match = (
            search_match
            if search_match is not None
            and 0 <= search_match.start < search_match.end <= len(self.text)
            else None
        )
        self._search_spans = VimTextArea._build_annotation_spans(
            self.text,
            () if valid_match is None else (valid_match,),
        )
        self._line_cache.clear()
        self.refresh()

    def get_line(self, line_index: int) -> Text:
        """Return a sidecar line with metadata, quote, and note styling."""

        line = super().get_line(line_index)
        line_link = self._annotation_links_by_line.get(line_index)
        if line_link is not None and line_link != self._active_link:
            line.stylize(
                self.get_component_rich_style("text-area--inactive-annotation")
            )
        else:
            if line_index in self._metadata_lines:
                line.stylize(
                    self.get_component_rich_style("text-area--annotation-meta")
                )
            if line_index in self._quote_lines:
                line.stylize(
                    self.get_component_rich_style("text-area--annotation-quote")
                )
            link = self._active_link
            if (
                link is not None
                and link.sidecar_note_start_line is not None
                and link.sidecar_note_end_line is not None
                and link.sidecar_note_start_line
                <= line_index
                <= link.sidecar_note_end_line
            ):
                line.stylize(
                    self.get_component_rich_style(
                        "text-area--active-annotation"
                    )
                )
        search_style = self.get_component_rich_style("text-area--search-match")
        for start, end in self._search_spans.get(line_index, ()):
            line.stylize(search_style, start, end)
        return line

    def action_document_start(self) -> None:
        self.move_cursor((0, 0))

    def action_document_end(self) -> None:
        self.move_cursor(self.document.end)


class SearchInput(Input):
    """Compact Vim-style search prompt with explicit cancellation."""

    BINDINGS = [
        Binding("escape", "cancel_search", "Cancel search", show=False),
    ]

    class Cancelled(Message):
        """Posted when the user dismisses the search prompt."""

    def action_cancel_search(self) -> None:
        self.post_message(self.Cancelled())


class NoteModal(ModalScreen[Optional[str]]):
    """Collect a Markdown note for one selected source range."""

    CSS = """
    NoteModal {
        align: center middle;
    }

    #note-dialog {
        width: 78%;
        max-width: 90;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }

    #note-title {
        height: 1;
        text-style: bold;
        color: $text;
    }

    #note-range {
        height: 1;
        margin-bottom: 1;
        color: $text-muted;
    }

    #quote-preview {
        height: auto;
        max-height: 5;
        padding: 0 1;
        margin-bottom: 1;
        border: solid $primary;
        overflow-y: auto;
    }

    #note-input {
        height: 8;
        margin-bottom: 1;
        border: solid $primary;
    }

    #note-buttons {
        height: 3;
        align-horizontal: right;
    }

    #note-buttons Button {
        margin-left: 1;
    }

    #note-hint {
        height: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save note", priority=True),
        Binding("ctrl+enter", "save", "Save note", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, draft: AnnotationDraft) -> None:
        super().__init__()
        self.draft = draft

    def compose(self) -> ComposeResult:
        preview = self.draft.quote
        if len(preview) > 500:
            preview = preview[:499] + "…"
        with Vertical(id="note-dialog"):
            yield Static("Add annotation", markup=False, id="note-title")
            yield Static(
                f"Source range [{self.draft.start}, {self.draft.end})",
                markup=False,
                id="note-range",
            )
            yield Static(preview, markup=False, id="quote-preview")
            yield TextArea(
                "",
                soft_wrap=True,
                show_line_numbers=False,
                placeholder="Write the note in Markdown…",
                id="note-input",
            )
            yield Static(
                "Ctrl+S or Ctrl+Enter saves · Escape cancels",
                markup=False,
                id="note-hint",
            )
            with Horizontal(id="note-buttons"):
                yield Button("Cancel", id="cancel-note")
                yield Button("Add note", variant="primary", id="save-note")

    def on_mount(self) -> None:
        self.query_one("#note-input", TextArea).focus()

    def action_save(self) -> None:
        note = self.query_one("#note-input", TextArea).text.rstrip()
        if not note.strip():
            self.notify("Write a note before saving", severity="warning", timeout=1.5)
            return
        self.dismiss(note)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save-note")
    def on_save_pressed(self, _event: Button.Pressed) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel-note")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.action_cancel()


class ViewerApp(App[None]):
    """Display one source file beside its persistent annotation sidecar."""

    TITLE = "Pure Text Annotator"
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

    #workspace {
        height: 1fr;
    }

    #viewer, #annotator {
        width: 1fr;
        height: 1fr;
        padding: 0;
        scrollbar-size: 1 1;
        border: round $primary;
    }

    #viewer:focus {
        border: round $accent;
    }

    #annotator:focus {
        border: round $secondary;
    }

    #viewer .text-area--selection, #annotator .text-area--selection {
        color: $text;
        background: $accent;
    }

    #viewer .text-area--annotation {
        color: $text;
        background: $warning 35%;
        text-style: underline;
    }

    #annotator .text-area--active-annotation {
        color: $text;
        background: $accent 18%;
    }

    #viewer .text-area--active-annotation,
    #annotator .text-area--annotation-quote {
        color: $text;
        background: $secondary 22%;
    }

    #viewer .text-area--active-annotation {
        text-style: underline;
    }

    #annotator .text-area--annotation-meta {
        color: $text-muted;
        text-style: dim;
    }

    #annotator .text-area--inactive-annotation {
        color: $text-muted;
        text-style: dim;
    }

    #viewer .text-area--search-match,
    #annotator .text-area--search-match {
        color: $text;
        background: $success 45%;
        text-style: bold;
    }

    #status {
        height: 1;
        padding: 0 1;
        color: $text;
        background: $boost;
        text-overflow: ellipsis;
    }

    #search-input {
        display: none;
        height: 1;
        padding: 0 1;
        border: none;
        color: $text;
        background: $boost;
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
        Binding(
            "ctrl+w",
            "start_pane_switch",
            "Pane switch prefix",
            show=False,
            priority=True,
        ),
        Binding(
            "w",
            "finish_pane_switch",
            "Switch pane",
            show=False,
            priority=True,
        ),
        Binding("/", "start_search", "Search", show=False),
        Binding("n", "search_next", "Next search result", show=False),
        Binding(
            "N,shift+n",
            "search_previous",
            "Previous search result",
            show=False,
        ),
    ]

    def __init__(
        self,
        source: LoadedTextFile,
        *,
        soft_wrap: bool = True,
        clipboard_writer: ClipboardWriter = copy_to_system_clipboard,
        annotation_store: Optional[AnnotationStore] = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.soft_wrap = soft_wrap
        self._clipboard_writer = clipboard_writer
        self.last_copy_backend: Optional[str] = None
        self._pane_switch_pending = False
        self._pane_switch_timer: Optional[Timer] = None
        self._search_target_id: Optional[str] = None
        self._search_queries: dict[str, str] = {}
        self._search_matches: dict[str, AnnotationRange] = {}
        self.annotation_store = annotation_store or AnnotationStore(source.path)
        try:
            self.annotation_text = self.annotation_store.read_text()
        except AnnotationFileError as error:
            self.annotation_text = f"# Annotation file error\n\n{error}\n"
        self.annotation_links = parse_annotation_links(self.annotation_text)
        self.annotation_ranges = tuple(
            link.source_range for link in self.annotation_links
        )
        self._source_line_starts = [0]
        self._source_line_starts.extend(
            index + 1
            for index, character in enumerate(self.source.text)
            if character == "\n"
        )

    def compose(self) -> ComposeResult:
        size = self._format_size(self.source.size_bytes)
        top_line = (
            f"{self.source.path}  |  {self.source.line_count} lines  |  {size}"
            f"  |  notes: {self.annotation_store.path.name}"
        )
        yield Static(top_line, markup=False, id="titlebar")
        with Horizontal(id="workspace"):
            viewer = VimTextArea(
                self.source.text,
                soft_wrap=self.soft_wrap,
                annotation_ranges=self.annotation_ranges,
                id="viewer",
            )
            viewer.border_title = "Source"
            yield viewer
            annotator = AnnotationTextArea(
                self.annotation_text,
                annotation_links=self.annotation_links,
                id="annotator",
            )
            annotator.border_title = "Annotation file"
            yield annotator
        yield Static("", markup=False, id="status")
        yield SearchInput(
            placeholder="/ Search",
            compact=True,
            select_on_focus=False,
            id="search-input",
        )
        yield Static(
            "Tab/Ctrl-W W panes  h/j/k/l move  Ctrl-F/B page  "
            "/ search  n/N repeat  v select  a annotate  q quit",
            markup=False,
            id="help",
        )

    def on_mount(self) -> None:
        self.query_one("#viewer", VimTextArea).focus()
        self._refresh_status()
        self.call_after_refresh(self._align_annotation_to_source)

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KiB"
        return f"{size_bytes / (1024 * 1024):.1f} MiB"

    def _valid_annotation_links(self) -> tuple[AnnotationLink, ...]:
        source_length = len(self.source.text)
        return tuple(
            link
            for link in self.annotation_links
            if 0
            <= link.source_range.start
            < link.source_range.end
            <= source_length
        )

    def _link_for_source_offset(self, offset: int) -> Optional[AnnotationLink]:
        links = self._valid_annotation_links()
        if not links:
            return None

        annotator = self.query_one("#annotator", AnnotationTextArea)
        active_link = annotator.active_link
        containing = tuple(
            link
            for link in links
            if link.source_range.start <= offset < link.source_range.end
        )
        if active_link in containing:
            return active_link
        if containing:
            return containing[0]

        def distance_from_range(link: AnnotationLink) -> int:
            source_range = link.source_range
            if offset < source_range.start:
                return source_range.start - offset
            return offset - source_range.end

        nearest_distance = min(distance_from_range(link) for link in links)
        nearest = tuple(
            link
            for link in links
            if distance_from_range(link) == nearest_distance
        )
        if active_link in nearest:
            return active_link
        return min(
            nearest,
            key=lambda link: (
                link.source_range.start,
                link.source_range.end,
                link.sidecar_start_line,
            ),
        )

    def _link_for_sidecar_line(self, line: int) -> Optional[AnnotationLink]:
        links = self._valid_annotation_links()
        if not links:
            return None

        for link in links:
            if link.sidecar_start_line <= line <= link.sidecar_end_line:
                return link

        preceding = tuple(
            link for link in links if link.sidecar_start_line <= line
        )
        if preceding:
            return max(preceding, key=lambda link: link.sidecar_start_line)
        return min(links, key=lambda link: link.sidecar_start_line)

    def _source_location_for_offset(self, offset: int) -> tuple[int, int]:
        row = bisect_right(self._source_line_starts, offset) - 1
        return row, offset - self._source_line_starts[row]

    def _align_annotation_to_source(self) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        offset = location_to_offset(self.source.text, viewer.cursor_location)
        link = self._link_for_source_offset(offset)
        if link is None:
            annotator.clear_active_link()
            viewer.set_active_annotation_range(None)
            return
        annotator.show_link(link, move_cursor=True)
        viewer.set_active_annotation_range(link.source_range)

    def _align_source_to_annotation(self) -> None:
        annotator = self.query_one("#annotator", AnnotationTextArea)
        link = self._link_for_sidecar_line(annotator.cursor_location[0])
        if link is None:
            annotator.clear_active_link()
            return

        annotator.show_link(link, move_cursor=False)
        viewer = self.query_one("#viewer", VimTextArea)
        viewer.set_active_annotation_range(link.source_range)
        if viewer.visual_mode:
            viewer.action_cancel_visual()
        viewer.move_cursor(
            self._source_location_for_offset(link.source_range.start),
            center=True,
        )

    def _refresh_status(self) -> None:
        if self._pane_switch_pending:
            self.query_one("#status", Static).update(
                "CTRL-W  |  press w to switch panes"
            )
            return

        viewer = self.query_one("#viewer", VimTextArea)
        row, column = viewer.cursor_location
        selected = viewer.selected_text
        mode = "VISUAL" if viewer.visual_mode else "NORMAL"
        selection_info = f"  |  {len(selected)} chars selected" if selected else ""
        wrap_info = "wrap" if viewer.soft_wrap else "no wrap"
        self.query_one("#status", Static).update(
            f"{mode}  |  Ln {row + 1}, Col {column + 1}  |  {wrap_info}{selection_info}"
        )

    def _focused_search_target(self) -> Optional[TextArea]:
        """Return the searchable pane that currently owns keyboard focus."""

        viewer = self.query_one("#viewer", VimTextArea)
        if viewer.has_focus:
            return viewer
        annotator = self.query_one("#annotator", AnnotationTextArea)
        if annotator.has_focus:
            return annotator
        return None

    def _search_target(self, target_id: str) -> TextArea:
        """Resolve a stored pane identifier to its text area."""

        if target_id == "viewer":
            return self.query_one("#viewer", VimTextArea)
        return self.query_one("#annotator", AnnotationTextArea)

    @staticmethod
    def _location_for_text_offset(text: str, offset: int) -> tuple[int, int]:
        """Convert a code-point offset to a TextArea row and column."""

        row = text.count("\n", 0, offset)
        line_start = text.rfind("\n", 0, offset) + 1
        return row, offset - line_start

    def _set_search_match(
        self, target_id: str, search_match: Optional[AnnotationRange]
    ) -> None:
        """Update a pane's current-match overlay and tracked range."""

        target = self._search_target(target_id)
        if isinstance(target, VimTextArea):
            target.set_search_match(search_match)
        else:
            assert isinstance(target, AnnotationTextArea)
            target.set_search_match(search_match)
        if search_match is None:
            self._search_matches.pop(target_id, None)
        else:
            self._search_matches[target_id] = search_match

    def _clear_stale_search_match(self, target_id: str) -> None:
        """Clear a result highlight after the cursor moves away from it."""

        search_match = self._search_matches.get(target_id)
        if search_match is None:
            return
        target = self._search_target(target_id)
        offset = location_to_offset(target.text, target.cursor_location)
        if not search_match.start <= offset < search_match.end:
            self._set_search_match(target_id, None)

    def _close_search_prompt(self) -> Optional[str]:
        """Hide the prompt, restore the status row, and refocus its pane."""

        target_id = self._search_target_id
        search_input = self.query_one("#search-input", SearchInput)
        search_input.display = False
        self.query_one("#status", Static).display = True
        self._search_target_id = None
        if target_id is not None:
            self._search_target(target_id).focus()
        self._refresh_status()
        return target_id

    def _perform_search(
        self, target_id: str, query: str, *, forward: bool
    ) -> bool:
        """Move to one literal match, wrapping at document boundaries."""

        target = self._search_target(target_id)
        text = target.text
        cursor_offset = location_to_offset(text, target.cursor_location)
        wrapped = False

        if forward:
            match_start = text.find(query, min(cursor_offset + 1, len(text)))
            if match_start < 0:
                match_start = text.find(query)
                wrapped = match_start >= 0
        else:
            match_start = text.rfind(query, 0, cursor_offset)
            if match_start < 0:
                match_start = text.rfind(query)
                wrapped = match_start >= 0

        if match_start < 0:
            self._set_search_match(target_id, None)
            self.notify(
                f"Pattern not found: {query}",
                severity="warning",
                timeout=2,
            )
            return False

        match = AnnotationRange(match_start, match_start + len(query))
        if isinstance(target, VimTextArea) and target.visual_mode:
            target.action_cancel_visual()
        target.move_cursor(
            self._location_for_text_offset(text, match_start),
            center=True,
        )
        self._set_search_match(target_id, match)
        if wrapped:
            boundary = "top" if forward else "bottom"
            self.notify(f"Search wrapped to {boundary}", timeout=1)
        return True

    def action_start_search(self) -> None:
        """Open a Vim-style search prompt for the focused pane."""

        target = self._focused_search_target()
        if target is None or target.id is None:
            return
        if self._pane_switch_pending:
            self._clear_pane_switch_prefix()
        if isinstance(target, VimTextArea) and target.visual_mode:
            target.action_cancel_visual()

        self._search_target_id = target.id
        search_input = self.query_one("#search-input", SearchInput)
        pane_name = "source" if target.id == "viewer" else "annotations"
        search_input.value = "/"
        search_input.cursor_position = 1
        search_input.placeholder = f"Search {pane_name}"
        self.query_one("#status", Static).display = False
        search_input.display = True
        search_input.focus()

    def action_search_next(self) -> None:
        """Repeat the current pane's last search forward."""

        target = self._focused_search_target()
        if target is None or target.id is None:
            return
        query = self._search_queries.get(target.id)
        if query is None:
            self.notify("No previous search in this pane", timeout=1.5)
            return
        self._perform_search(target.id, query, forward=True)

    def action_search_previous(self) -> None:
        """Repeat the current pane's last search backward."""

        target = self._focused_search_target()
        if target is None or target.id is None:
            return
        query = self._search_queries.get(target.id)
        if query is None:
            self.notify("No previous search in this pane", timeout=1.5)
            return
        self._perform_search(target.id, query, forward=False)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        search_input = self.query_one("#search-input", SearchInput)
        if action in {
            "start_search",
            "search_next",
            "search_previous",
        }:
            if isinstance(self.screen, NoteModal) or search_input.has_focus:
                return False
        if action in {"start_pane_switch", "finish_pane_switch"}:
            if isinstance(self.screen, NoteModal) or search_input.has_focus:
                return False
        if action == "finish_pane_switch":
            return self._pane_switch_pending
        return super().check_action(action, parameters)

    def _clear_pane_switch_prefix(self) -> None:
        self._pane_switch_pending = False
        self._pane_switch_timer = None
        self._refresh_status()

    def action_start_pane_switch(self) -> None:
        self._pane_switch_pending = True
        if self._pane_switch_timer is not None:
            self._pane_switch_timer.stop()
        self._pane_switch_timer = self.set_timer(
            1.5, self._clear_pane_switch_prefix
        )
        self._refresh_status()

    def action_finish_pane_switch(self) -> None:
        self._pane_switch_pending = False
        if self._pane_switch_timer is not None:
            self._pane_switch_timer.stop()
            self._pane_switch_timer = None

        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        if viewer.has_focus:
            annotator.focus()
        else:
            viewer.focus()
        self._refresh_status()

    @on(TextArea.SelectionChanged, "#viewer")
    def on_viewer_selection_changed(self, _event: TextArea.SelectionChanged) -> None:
        self._clear_stale_search_match("viewer")
        self._refresh_status()
        if self.query_one("#viewer", VimTextArea).has_focus:
            self._align_annotation_to_source()

    @on(TextArea.SelectionChanged, "#annotator")
    def on_annotator_selection_changed(
        self, _event: TextArea.SelectionChanged
    ) -> None:
        self._clear_stale_search_match("annotator")
        if self.query_one("#annotator", AnnotationTextArea).has_focus:
            self._align_source_to_annotation()

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        """Run a search submitted from the bottom prompt."""

        event.stop()
        target_id = self._search_target_id
        if target_id is None:
            self._close_search_prompt()
            return

        entered_query = (
            event.value[1:] if event.value.startswith("/") else event.value
        )
        query = entered_query or self._search_queries.get(target_id, "")
        if not query:
            self.notify("Enter text to search for", severity="warning", timeout=1.5)
            return

        self._search_queries[target_id] = query
        self._close_search_prompt()
        self._perform_search(target_id, query, forward=True)

    @on(SearchInput.Cancelled)
    def on_search_cancelled(self, event: SearchInput.Cancelled) -> None:
        """Dismiss the prompt without changing the current search."""

        event.stop()
        self._close_search_prompt()

    @on(VimTextArea.ModeChanged)
    def on_viewer_mode_changed(self, _event: VimTextArea.ModeChanged) -> None:
        self._refresh_status()

    @on(VimTextArea.AnnotationRequested)
    def on_annotation_requested(self, event: VimTextArea.AnnotationRequested) -> None:
        try:
            draft = draft_from_selection(
                self.source.text,
                event.selection.start,
                event.selection.end,
            )
        except ValueError:
            self.notify("Select source text before annotating", severity="warning", timeout=2)
            return

        self.push_screen(
            NoteModal(draft),
            callback=lambda note: self._finish_annotation(draft, note),
        )

    def _finish_annotation(
        self, draft: AnnotationDraft, note: Optional[str]
    ) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        if note is None:
            viewer.focus()
            return

        record = create_record(draft, note)
        try:
            updated = self.annotation_store.append(record)
        except AnnotationFileError as error:
            self.notify(str(error), severity="error", timeout=4)
            viewer.focus()
            return

        self.annotation_text = updated
        self.annotation_links = parse_annotation_links(updated)
        self.annotation_ranges = tuple(
            link.source_range for link in self.annotation_links
        )
        annotator = self.query_one("#annotator", AnnotationTextArea)
        annotator.load_text(updated)
        annotator.set_annotation_links(self.annotation_links)
        self._set_search_match("annotator", None)
        viewer.set_annotation_ranges(self.annotation_ranges)
        if self.annotation_links:
            newest_link = self.annotation_links[-1]
            annotator.show_link(newest_link, move_cursor=True)
            viewer.set_active_annotation_range(newest_link.source_range)
        viewer.action_cancel_visual()
        viewer.focus()
        self.notify(
            f"Saved annotation for range [{draft.start}, {draft.end})",
            timeout=2,
        )

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

