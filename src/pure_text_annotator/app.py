"""Textual application for Vim-style source viewing and sidecar notes."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterable, Optional

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.geometry import Offset, Size
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
    PdfAnchor,
    create_record,
    draft_from_selection,
    location_to_offset,
    parse_annotation_links,
)
from .clipboard import ClipboardCopyError, copy_to_system_clipboard
from .demo import DemoCase, DemoCaseError, load_demo_case, reset_demo_case
from .files import LoadedTextFile, TextFileError, load_source_file, save_text_file
from .rematch import (
    RematchCandidate,
    RematchChoice,
    RematchItem,
    RematchReport,
    RematchStatus,
    apply_rematches,
    confidence_summary,
    make_manual_candidate,
    scan_rematches,
    text_sha256,
    validate_applied_rematches,
)


ClipboardWriter = Callable[[str], Optional[str]]
SourceWriter = Callable[[LoadedTextFile, str], LoadedTextFile]
RematchPreviewCallback = Callable[
    [RematchItem, Optional[RematchCandidate]], None
]


class SyncMode(str, Enum):
    """Automatic cross-pane alignment policy."""

    NORMAL = "Normal"
    SYNC = "Sync"
    OFF = "Off"


def _vim_word_kind(character: str) -> Optional[bool]:
    """Classify a character using Vim's lowercase-word boundaries."""

    if character.isspace():
        return None
    return character.isalnum() or character == "_"


def _vim_word_end_location(text_area: TextArea) -> tuple[int, int]:
    """Return the target of Vim's ``e`` motion for a text area."""

    text = text_area.text
    cursor_index = text_area.document.get_index_from_location(
        text_area.cursor_location
    )
    if cursor_index >= len(text):
        return text_area.cursor_location

    cursor_kind = _vim_word_kind(text[cursor_index])
    if cursor_kind is not None:
        word_end = cursor_index
        while (
            word_end + 1 < len(text)
            and _vim_word_kind(text[word_end + 1]) == cursor_kind
        ):
            word_end += 1
        if word_end > cursor_index:
            return text_area.document.get_location_from_index(word_end)
        search_index = cursor_index + 1
    else:
        search_index = cursor_index

    while search_index < len(text) and text[search_index].isspace():
        search_index += 1
    if search_index >= len(text):
        return text_area.cursor_location

    word_kind = _vim_word_kind(text[search_index])
    word_end = search_index
    while (
        word_end + 1 < len(text)
        and _vim_word_kind(text[word_end + 1]) == word_kind
    ):
        word_end += 1
    return text_area.document.get_location_from_index(word_end)


def _vim_word_start_location(text_area: TextArea) -> tuple[int, int]:
    """Return the start of the next Vim word, skipping whitespace."""

    text = text_area.text
    cursor_index = text_area.document.get_index_from_location(
        text_area.cursor_location
    )
    for search_index in range(cursor_index + 1, len(text)):
        word_kind = _vim_word_kind(text[search_index])
        if word_kind is None:
            continue
        if _vim_word_kind(text[search_index - 1]) != word_kind:
            return text_area.document.get_location_from_index(search_index)
    return text_area.cursor_location


def _vim_word_previous_location(text_area: TextArea) -> tuple[int, int]:
    """Return the start of the previous Vim word, including symbol words."""

    text = text_area.text
    cursor_index = text_area.document.get_index_from_location(
        text_area.cursor_location
    )
    if cursor_index <= 0:
        return text_area.cursor_location

    search_index = cursor_index - 1
    while search_index >= 0 and text[search_index].isspace():
        search_index -= 1
    if search_index < 0:
        return text_area.document.get_location_from_index(0)

    word_kind = _vim_word_kind(text[search_index])
    while (
        search_index > 0
        and _vim_word_kind(text[search_index - 1]) == word_kind
    ):
        search_index -= 1
    return text_area.document.get_location_from_index(search_index)


def _vim_scroll_line(
    text_area: TextArea, direction: int, *, select: bool = False
) -> None:
    """Scroll one visual line and keep the cursor inside the viewport."""

    cursor_offset = text_area.wrapped_document.location_to_offset(
        text_area.cursor_location
    )
    viewport_height = max(1, text_area.scrollable_content_region.height)
    scroll_y = round(text_area.scroll_y)
    target_y = max(0, min(scroll_y + direction, text_area.max_scroll_y))
    if target_y == scroll_y:
        return

    text_area.scroll_to(
        y=target_y,
        animate=False,
        immediate=True,
    )
    visible_top = target_y
    visible_bottom = target_y + viewport_height - 1
    if cursor_offset.y < visible_top:
        cursor_offset = Offset(cursor_offset.x, visible_top)
    elif cursor_offset.y > visible_bottom:
        cursor_offset = Offset(cursor_offset.x, visible_bottom)
    else:
        return
    text_area.move_cursor(
        text_area.wrapped_document.offset_to_location(cursor_offset),
        select=select,
    )


def _position_cursor_line(text_area: TextArea, placement: str) -> None:
    """Place the cursor's visual line at the requested viewport position."""

    cursor_y = text_area.wrapped_document.location_to_offset(
        text_area.cursor_location
    ).y
    viewport_height = max(1, text_area.scrollable_content_region.height)
    if placement == "top":
        target_y = cursor_y
    elif placement == "middle":
        target_y = cursor_y - (viewport_height - 1) // 2
    else:
        target_y = cursor_y - viewport_height + 1
    text_area.scroll_to(
        y=target_y,
        animate=False,
        immediate=True,
    )


class HalfPageBottomScrollTextArea(TextArea):
    """A text area with enough trailing space to center its final line."""

    def _refresh_size(self) -> None:
        if self.soft_wrap:
            base_size = Size(0, self.wrapped_document.height)
        else:
            width, height = self.document.get_size(self.indent_width)
            base_size = Size(width + self.gutter_width + 1, height)
        trailing_height = self.scrollable_content_region.height // 2
        self.virtual_size = Size(
            base_size.width,
            base_size.height + trailing_height,
        )
        self._refresh_scrollbars()


class VimTextArea(HalfPageBottomScrollTextArea):
    """A guarded editable TextArea with a small, predictable Vim key set."""

    COMPONENT_CLASSES = TextArea.COMPONENT_CLASSES | {
        "text-area--annotation",
        "text-area--active-annotation",
        "text-area--rematch-candidate",
        "text-area--search-match",
    }

    visual_mode = reactive(False)
    edit_mode = reactive(False)

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
        Binding("e", "vim_word_end", "End of word", show=False),
        Binding("0,home", "vim_line_start", "Line start", show=False),
        Binding("$,end", "vim_line_end", "Line end", show=False),
        Binding("g", "vim_document_start", "Document start", show=False),
        Binding("G,shift+g", "vim_document_end", "Document end", show=False),
        Binding(
            "pagedown,ctrl+f,ctrl+d", "vim_page_down", "Page down", show=False
        ),
        Binding("pageup,ctrl+b,ctrl+u", "vim_page_up", "Page up", show=False),
        Binding(
            "ctrl+e", "vim_scroll_down_line", "Scroll down", show=False
        ),
        Binding("ctrl+y", "vim_scroll_up_line", "Scroll up", show=False),
        Binding(
            "shift+pagedown", "vim_page_down(True)", "Select page down", show=False
        ),
        Binding(
            "shift+pageup", "vim_page_up(True)", "Select page up", show=False
        ),
        Binding("v", "toggle_visual", "Visual selection", show=False),
        Binding("a", "annotate", "Add annotation", show=False),
        Binding("i", "request_edit", "Edit source", show=False),
        Binding("u", "vim_undo", "Undo change", show=False),
        Binding("ctrl+r", "vim_redo", "Redo change", show=False),
        Binding(
            "escape,ctrl+left_square_bracket",
            "exit_edit",
            "Return to Normal mode",
            show=False,
        ),
        Binding("ctrl+h", "delete_left", "Delete left", show=False),
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

    class EditRequested(Message):
        """Request confirmation before editing the original source file."""

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
        self._rematch_candidate_range: Optional[AnnotationRange] = None
        self._rematch_candidate_spans: dict[
            int, tuple[tuple[int, int], ...]
        ] = {}
        self._search_spans: dict[int, tuple[tuple[int, int], ...]] = {}
        self._normal_history_pending = False
        self.set_annotation_ranges(annotation_ranges)

    @property
    def annotation_ranges(self) -> tuple[AnnotationRange, ...]:
        """The valid persisted ranges currently highlighted in the source."""

        return self._annotation_ranges

    @property
    def active_annotation_range(self) -> Optional[AnnotationRange]:
        """The source range synchronized with the focused sidecar entry."""

        return self._active_annotation_range

    @property
    def rematch_candidate_range(self) -> Optional[AnnotationRange]:
        """The proposal currently previewed by the rematch review."""

        return self._rematch_candidate_range

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

    def set_rematch_candidate_range(
        self, candidate_range: Optional[AnnotationRange]
    ) -> None:
        """Highlight a proposed range independently of saved annotations."""

        valid_range = (
            candidate_range
            if candidate_range is not None
            and 0 <= candidate_range.start < candidate_range.end <= len(self.text)
            else None
        )
        if valid_range == self._rematch_candidate_range:
            return
        self._rematch_candidate_range = valid_range
        self._rematch_candidate_spans = self._build_annotation_spans(
            self.text,
            () if valid_range is None else (valid_range,),
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
        rematch_style = self.get_component_rich_style(
            "text-area--rematch-candidate"
        )
        for start, end in self._rematch_candidate_spans.get(line_index, ()):
            line.stylize(rematch_style, start, end)
        search_style = self.get_component_rich_style("text-area--search-match")
        for start, end in self._search_spans.get(line_index, ()):
            line.stylize(search_style, start, end)
        return line

    def _should_select(self, force_select: bool = False) -> bool:
        return force_select or self.visual_mode

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        normal_mode_actions = {
            "vim_left",
            "vim_right",
            "vim_up",
            "vim_down",
            "vim_word_left",
            "vim_word_right",
            "vim_word_end",
            "vim_line_start",
            "vim_line_end",
            "vim_document_start",
            "vim_document_end",
            "vim_page_down",
            "vim_page_up",
            "vim_scroll_down_line",
            "vim_scroll_up_line",
            "toggle_visual",
            "annotate",
            "request_edit",
            "vim_undo",
            "vim_redo",
            "yank",
        }
        if self.edit_mode and action in normal_mode_actions:
            return False
        if not self.edit_mode and action in {"delete_left", "delete_word_left"}:
            return False
        return super().check_action(action, parameters)

    def action_vim_left(self, force_select: bool = False) -> None:
        self.action_cursor_left(self._should_select(force_select))

    def action_vim_right(self, force_select: bool = False) -> None:
        self.action_cursor_right(self._should_select(force_select))

    def action_vim_up(self, force_select: bool = False) -> None:
        self.action_cursor_up(self._should_select(force_select))

    def action_vim_down(self, force_select: bool = False) -> None:
        self.action_cursor_down(self._should_select(force_select))

    def action_vim_word_left(self) -> None:
        self.move_cursor(
            _vim_word_previous_location(self), select=self.visual_mode
        )

    def action_vim_word_right(self) -> None:
        self.move_cursor(
            _vim_word_start_location(self), select=self.visual_mode
        )

    def action_vim_word_end(self) -> None:
        self.move_cursor(
            _vim_word_end_location(self), select=self.visual_mode
        )

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

    def action_vim_scroll_down_line(self) -> None:
        _vim_scroll_line(self, 1, select=self.visual_mode)

    def action_vim_scroll_up_line(self) -> None:
        _vim_scroll_line(self, -1, select=self.visual_mode)

    def action_toggle_visual(self) -> None:
        if self.visual_mode:
            self.action_cancel_visual()
            return
        self.selection = Selection.cursor(self.cursor_location)
        self.visual_mode = True
        self.post_message(self.ModeChanged())

    def action_annotate(self) -> None:
        self.post_message(self.AnnotationRequested(self.selection))

    def action_request_edit(self) -> None:
        self.post_message(self.EditRequested())

    def action_vim_undo(self) -> None:
        previous_text = self.text
        self._normal_history_pending = True
        self.undo()
        if self.text == previous_text:
            self._normal_history_pending = False

    def action_vim_redo(self) -> None:
        previous_text = self.text
        self._normal_history_pending = True
        self.redo()
        if self.text == previous_text:
            self._normal_history_pending = False

    def enter_edit_mode(self) -> None:
        """Make the source buffer editable after external confirmation."""

        if self.visual_mode:
            self.action_cancel_visual()
        self.history.checkpoint()
        self.selection = Selection.cursor(self.cursor_location)
        self.read_only = False
        self.edit_mode = True
        self.post_message(self.ModeChanged())

    def leave_edit_mode(self) -> None:
        """Return the source buffer to guarded Normal mode."""

        if not self.edit_mode:
            return
        self.read_only = True
        self.edit_mode = False
        self.history.checkpoint()
        self.selection = Selection.cursor(self.cursor_location)
        self.post_message(self.ModeChanged())

    def action_exit_edit(self) -> None:
        if self.edit_mode:
            self.leave_edit_mode()
        else:
            self.action_cancel_visual()

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


@dataclass(frozen=True)
class MetadataFold:
    """One collapsible metadata region in canonical sidecar line numbers."""

    key: str
    start_line: int
    end_line: int
    label: str


class AnnotationTextArea(HalfPageBottomScrollTextArea):
    """Guarded sidecar editor whose cursor tracks annotation entries."""

    COMPONENT_CLASSES = TextArea.COMPONENT_CLASSES | {
        "text-area--active-annotation",
        "text-area--annotation-quote",
        "text-area--annotation-meta",
        "text-area--inactive-annotation",
        "text-area--search-match",
    }

    edit_mode = reactive(False)

    BINDINGS = [
        Binding("h,left", "cursor_left", "Left", show=False),
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("l,right", "cursor_right", "Right", show=False),
        Binding("w,ctrl+right", "vim_word_right", "Next word", show=False),
        Binding("b,ctrl+left", "vim_word_left", "Previous word", show=False),
        Binding("e", "vim_word_end", "End of word", show=False),
        Binding("i", "request_edit", "Edit annotations", show=False),
        Binding("u", "vim_undo", "Undo change", show=False),
        Binding("ctrl+r", "vim_redo", "Redo change", show=False),
        Binding(
            "escape,ctrl+left_square_bracket",
            "exit_edit",
            "Return to Normal mode",
            show=False,
        ),
        Binding("ctrl+h", "delete_left", "Delete left", show=False),
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
        Binding(
            "ctrl+e", "vim_scroll_down_line", "Scroll down", show=False
        ),
        Binding("ctrl+y", "vim_scroll_up_line", "Scroll up", show=False),
    ]

    class ModeChanged(Message):
        """Posted when annotation Edit mode changes."""

    class EditRequested(Message):
        """Request immediate Edit mode for the annotation sidecar."""

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
            show_line_numbers=True,
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
        self._normal_history_pending = False
        self._canonical_text = text
        self.folding_enabled = True
        self._closed_fold_keys: set[str] = set()
        self._fold_regions: tuple[MetadataFold, ...] = ()
        line_count = len(text.split("\n"))
        self._display_to_full_lines = tuple(range(line_count))
        self._full_to_display_lines = tuple(range(line_count))
        self._display_is_projection = False
        self.set_annotation_links(annotation_links)

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        normal_mode_actions = {
            "cursor_left",
            "cursor_right",
            "cursor_up",
            "cursor_down",
            "vim_word_left",
            "vim_word_right",
            "vim_word_end",
            "cursor_line_start",
            "cursor_line_end",
            "document_start",
            "document_end",
            "cursor_page_down",
            "cursor_page_up",
            "vim_scroll_down_line",
            "vim_scroll_up_line",
            "request_edit",
            "vim_undo",
            "vim_redo",
        }
        if self.edit_mode and action in normal_mode_actions:
            return False
        if not self.edit_mode and action in {"delete_left", "delete_word_left"}:
            return False
        return super().check_action(action, parameters)

    @property
    def active_link(self) -> Optional[AnnotationLink]:
        """The entry currently synchronized with the source pane."""

        return self._active_link

    @property
    def canonical_text(self) -> str:
        """Return the complete sidecar, independent of its folded projection."""

        return self._canonical_text if self._display_is_projection else self.text

    @property
    def metadata_folds(self) -> tuple[MetadataFold, ...]:
        """The foldable metadata regions in canonical line coordinates."""

        return self._fold_regions

    def full_line_for_display(self, display_line: int) -> int:
        """Map a visible row back to its canonical sidecar row."""

        if not self._display_to_full_lines:
            return 0
        clamped = max(0, min(display_line, len(self._display_to_full_lines) - 1))
        return self._display_to_full_lines[clamped]

    def _display_line_for_full(self, full_line: int) -> int:
        if not self._full_to_display_lines:
            return 0
        clamped = max(0, min(full_line, len(self._full_to_display_lines) - 1))
        return self._full_to_display_lines[clamped]

    def _full_cursor_location(self) -> tuple[int, int]:
        row, column = self.cursor_location
        return self.full_line_for_display(row), column

    def _cursor_viewport_position(self) -> tuple[float, float]:
        """Capture horizontal scroll and the cursor's visual viewport row."""

        cursor_y = self.wrapped_document.location_to_offset(
            self.cursor_location
        ).y
        return float(self.scroll_x), float(cursor_y) - float(self.scroll_y)

    def _restore_cursor_viewport_position(
        self, position: tuple[float, float]
    ) -> None:
        """Keep the cursor stationary on screen after fold projection changes."""

        scroll_x, viewport_row = position
        cursor_y = self.wrapped_document.location_to_offset(
            self.cursor_location
        ).y
        self.scroll_to(
            x=scroll_x,
            y=float(cursor_y) - viewport_row,
            animate=False,
            immediate=True,
        )

    def _set_display_document(self, text: str) -> None:
        """Replace only the rendered document without touching edit history."""

        self._set_document(text, self.language)
        self._search_spans = {}
        self._line_cache.clear()

    def _build_fold_regions(self) -> None:
        lines = self._canonical_text.split("\n")
        old_keys = {region.key for region in self._fold_regions}
        regions: list[MetadataFold] = []

        if lines and lines[0].strip() == "---":
            for line_number in range(1, len(lines)):
                if lines[line_number].strip() == "---":
                    if line_number > 0:
                        regions.append(
                            MetadataFold(
                                "document",
                                0,
                                line_number,
                                "document metadata",
                            )
                        )
                    break

        for ordinal, link in enumerate(self._annotation_links, start=1):
            upper = min(link.sidecar_metadata_end_line, len(lines) - 1)
            yaml_start = next(
                (
                    line_number
                    for line_number in range(
                        max(0, link.sidecar_start_line), upper + 1
                    )
                    if lines[line_number].strip() == "```yaml"
                ),
                None,
            )
            if yaml_start is not None and yaml_start < upper:
                regions.append(
                    MetadataFold(
                        f"annotation:{ordinal}",
                        yaml_start,
                        upper,
                        f"Annotation {ordinal} metadata",
                    )
                )

        self._fold_regions = tuple(regions)
        new_keys = {region.key for region in regions}
        self._closed_fold_keys.intersection_update(new_keys)
        if self.folding_enabled:
            self._closed_fold_keys.update(new_keys - old_keys)

    def _rebuild_semantic_line_maps(self) -> None:
        links_by_line: dict[int, AnnotationLink] = {}
        metadata_lines: set[int] = set()
        quote_lines: set[int] = set()
        canonical_lines = self._canonical_text.split("\n")

        full_links_by_line: dict[int, AnnotationLink] = {}
        full_metadata_lines: set[int] = set()
        full_quote_lines: set[int] = set()

        if canonical_lines and canonical_lines[0].strip() == "---":
            for line_number in range(1, len(canonical_lines)):
                if canonical_lines[line_number].strip() == "---":
                    full_metadata_lines.update(range(line_number + 1))
                    break

        for link in self._annotation_links:
            full_links_by_line.update(
                (line_number, link)
                for line_number in range(
                    link.sidecar_start_line,
                    link.sidecar_end_line + 1,
                )
            )
            full_metadata_lines.update(
                line_number
                for line_number in range(
                    link.sidecar_start_line,
                    link.sidecar_metadata_end_line + 1,
                )
                if 0 <= line_number < len(canonical_lines)
                and not canonical_lines[line_number].startswith(
                    "## Annotation "
                )
            )
            full_metadata_lines.add(link.sidecar_end_line)
            if (
                link.sidecar_quote_start_line is not None
                and link.sidecar_quote_end_line is not None
            ):
                full_quote_lines.update(
                    range(
                        link.sidecar_quote_start_line,
                        link.sidecar_quote_end_line + 1,
                    )
                )

        for display_line, full_line in enumerate(self._display_to_full_lines):
            link = full_links_by_line.get(full_line)
            if link is not None:
                links_by_line[display_line] = link
            if full_line in full_metadata_lines:
                metadata_lines.add(display_line)
            if full_line in full_quote_lines:
                quote_lines.add(display_line)

        self._annotation_links_by_line = links_by_line
        self._metadata_lines = frozenset(metadata_lines)
        self._quote_lines = frozenset(quote_lines)
        self._line_cache.clear()
        self.refresh()

    def _apply_fold_projection(
        self, full_cursor: Optional[tuple[int, int]] = None
    ) -> None:
        if full_cursor is None:
            full_cursor = self._full_cursor_location()

        lines = self._canonical_text.split("\n")
        closed_regions = {
            region.start_line: region
            for region in self._fold_regions
            if self.folding_enabled
            and not self.edit_mode
            and region.key in self._closed_fold_keys
        }
        display_lines: list[str] = []
        display_to_full: list[int] = []
        full_to_display = [0] * len(lines)

        full_line = 0
        while full_line < len(lines):
            region = closed_regions.get(full_line)
            if region is None:
                display_row = len(display_lines)
                display_lines.append(lines[full_line])
                display_to_full.append(full_line)
                full_to_display[full_line] = display_row
                full_line += 1
                continue

            display_row = len(display_lines)
            line_count = region.end_line - region.start_line + 1
            display_lines.append(
                f"⏵ {region.label} · {line_count} lines · zo opens"
            )
            display_to_full.append(region.start_line)
            for hidden_line in range(
                region.start_line, min(region.end_line + 1, len(lines))
            ):
                full_to_display[hidden_line] = display_row
            full_line = region.end_line + 1

        projection = "\n".join(display_lines)
        display_is_projection = projection != self._canonical_text
        if projection != self.text:
            self._set_display_document(projection)
        self._display_to_full_lines = tuple(display_to_full)
        self._full_to_display_lines = tuple(full_to_display)
        self._display_is_projection = display_is_projection

        target_row = self._display_line_for_full(full_cursor[0])
        target_line = display_lines[target_row] if display_lines else ""
        target_column = min(full_cursor[1], len(target_line))
        target_location = (target_row, target_column)
        if self.cursor_location != target_location:
            self.move_cursor(target_location)
        self._rebuild_semantic_line_maps()

    def load_annotation_text(self, text: str) -> None:
        """Load a new canonical sidecar and reset its edit history."""

        self._canonical_text = text
        self._fold_regions = ()
        self._display_is_projection = False
        line_count = len(text.split("\n"))
        self._display_to_full_lines = tuple(range(line_count))
        self._full_to_display_lines = tuple(range(line_count))
        super().load_text(text)

    def sync_canonical_from_editor(self) -> None:
        """Capture the current full editor document as canonical text."""

        if not self._display_is_projection:
            self._canonical_text = self.text

    def toggle_metadata_folds(self) -> str:
        """Implement Vim's ``zi`` for the metadata fold projection."""

        full_cursor = self._full_cursor_location()
        viewport_position = self._cursor_viewport_position()
        self.folding_enabled = not self.folding_enabled
        if self.folding_enabled and not self._closed_fold_keys:
            self._closed_fold_keys.update(
                region.key for region in self._fold_regions
            )
        self._apply_fold_projection(full_cursor)
        self._restore_cursor_viewport_position(viewport_position)
        return (
            "Metadata folds enabled"
            if self.folding_enabled
            else "Metadata folds disabled"
        )

    def _metadata_fold_at_cursor(self) -> Optional[MetadataFold]:
        full_line = self.full_line_for_display(self.cursor_location[0])
        return next(
            (
                region
                for region in self._fold_regions
                if region.start_line <= full_line <= region.end_line
            ),
            None,
        )

    def open_metadata_fold(self) -> str:
        """Implement Vim's ``zo`` for the metadata fold at the cursor."""

        region = self._metadata_fold_at_cursor()
        if region is None:
            return "No metadata fold at cursor"
        if not self.folding_enabled or region.key not in self._closed_fold_keys:
            return "Metadata fold is already open"
        full_cursor = self._full_cursor_location()
        viewport_position = self._cursor_viewport_position()
        self._closed_fold_keys.remove(region.key)
        self._apply_fold_projection(full_cursor)
        self._restore_cursor_viewport_position(viewport_position)
        return f"Opened {region.label}"

    def close_metadata_fold(self) -> str:
        """Implement Vim's ``zc`` for the metadata fold at the cursor."""

        region = self._metadata_fold_at_cursor()
        if region is None:
            return "No metadata fold at cursor"
        full_cursor = self._full_cursor_location()
        viewport_position = self._cursor_viewport_position()
        self.folding_enabled = True
        self._closed_fold_keys.add(region.key)
        self._apply_fold_projection(full_cursor)
        self._restore_cursor_viewport_position(viewport_position)
        return f"Closed {region.label}"

    def set_annotation_links(
        self, annotation_links: Iterable[AnnotationLink]
    ) -> None:
        """Replace the semantic sidecar ranges used for line styling."""

        full_cursor = self._full_cursor_location()
        self._annotation_links = tuple(annotation_links)
        if self._active_link not in self._annotation_links:
            self._active_link = None
        self._build_fold_regions()
        self._apply_fold_projection(full_cursor)

    def show_link(self, link: AnnotationLink, *, move_cursor: bool) -> None:
        """Mark an annotation block and optionally center its first line."""

        changed = link != self._active_link
        self._active_link = link
        if changed:
            self._line_cache.clear()
            self.refresh()
        target_line = self._display_line_for_full(link.sidecar_note_line)
        if move_cursor and self.cursor_location[0] != target_line:
            self.move_cursor((target_line, 0), center=True)

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
        full_line = self.full_line_for_display(line_index)
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
                <= full_line
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

    def action_vim_word_end(self) -> None:
        self.move_cursor(_vim_word_end_location(self))

    def action_vim_word_right(self) -> None:
        self.move_cursor(_vim_word_start_location(self))

    def action_vim_word_left(self) -> None:
        self.move_cursor(_vim_word_previous_location(self))

    def action_vim_scroll_down_line(self) -> None:
        _vim_scroll_line(self, 1)

    def action_vim_scroll_up_line(self) -> None:
        _vim_scroll_line(self, -1)

    def action_request_edit(self) -> None:
        self.post_message(self.EditRequested())

    def action_vim_undo(self) -> None:
        previous_text = self.canonical_text
        full_cursor = self._full_cursor_location()
        if self._display_is_projection:
            self._set_display_document(self._canonical_text)
            line_count = len(self._canonical_text.split("\n"))
            self._display_to_full_lines = tuple(range(line_count))
            self._full_to_display_lines = tuple(range(line_count))
            self._display_is_projection = False
            self.move_cursor(full_cursor)
        self._normal_history_pending = True
        self.undo()
        if self.text == previous_text:
            self._normal_history_pending = False
        else:
            self._canonical_text = self.text
        self.set_annotation_links(parse_annotation_links(self._canonical_text))

    def action_vim_redo(self) -> None:
        previous_text = self.canonical_text
        full_cursor = self._full_cursor_location()
        if self._display_is_projection:
            self._set_display_document(self._canonical_text)
            line_count = len(self._canonical_text.split("\n"))
            self._display_to_full_lines = tuple(range(line_count))
            self._full_to_display_lines = tuple(range(line_count))
            self._display_is_projection = False
            self.move_cursor(full_cursor)
        self._normal_history_pending = True
        self.redo()
        if self.text == previous_text:
            self._normal_history_pending = False
        else:
            self._canonical_text = self.text
        self.set_annotation_links(parse_annotation_links(self._canonical_text))

    def enter_edit_mode(self) -> None:
        full_cursor = self._full_cursor_location()
        viewport_position = self._cursor_viewport_position()
        if self._display_is_projection:
            self._set_display_document(self._canonical_text)
            line_count = len(self._canonical_text.split("\n"))
            self._display_to_full_lines = tuple(range(line_count))
            self._full_to_display_lines = tuple(range(line_count))
            self._display_is_projection = False
            self.move_cursor(full_cursor)
            self._rebuild_semantic_line_maps()
        self.history.checkpoint()
        self.selection = Selection.cursor(self.cursor_location)
        self.read_only = False
        self.edit_mode = True
        self._restore_cursor_viewport_position(viewport_position)
        self.post_message(self.ModeChanged())

    def leave_edit_mode(self) -> None:
        if not self.edit_mode:
            return
        viewport_position = self._cursor_viewport_position()
        self._canonical_text = self.text
        full_cursor = self.cursor_location
        self.read_only = True
        self.edit_mode = False
        self.history.checkpoint()
        self._build_fold_regions()
        self._apply_fold_projection(full_cursor)
        self.selection = Selection.cursor(self.cursor_location)
        self._restore_cursor_viewport_position(viewport_position)
        self.post_message(self.ModeChanged())

    def action_exit_edit(self) -> None:
        self.leave_edit_mode()


class SearchInput(Input):
    """Compact Vim-style search prompt with explicit cancellation."""

    BINDINGS = [
        Binding("escape", "cancel_search", "Cancel search", show=False),
    ]

    class Cancelled(Message):
        """Posted when the user dismisses the search prompt."""

    def action_cancel_search(self) -> None:
        self.post_message(self.Cancelled())


@dataclass(frozen=True)
class RematchPreviewState:
    """Pane state restored after the temporary rematch preview closes."""

    viewer_cursor: tuple[int, int]
    viewer_selection: Selection
    viewer_visual_mode: bool
    viewer_scroll: tuple[float, float]
    annotator_cursor: tuple[int, int]
    annotator_selection: Selection
    annotator_scroll: tuple[float, float]
    annotator_active_link: Optional[AnnotationLink]
    focused_pane: str


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


class EditWarningModal(ModalScreen[bool]):
    """Require an explicit confirmation before editing the source file."""

    CSS = """
    EditWarningModal {
        align: center bottom;
        background: $background 10%;
    }

    #edit-warning-dialog {
        width: 96%;
        max-width: 120;
        height: 12;
        margin-bottom: 1;
        padding: 0 1;
        border: round $warning;
        background: $panel;
    }

    #edit-warning-title {
        height: 1;
        text-style: bold;
        color: $warning;
        text-overflow: ellipsis;
    }

    #edit-warning-message,
    #edit-warning-path,
    #edit-warning-annotations {
        height: 1;
        text-overflow: ellipsis;
    }

    #edit-warning-path,
    #edit-warning-annotations {
        color: $text-muted;
    }

    #edit-confirmation {
        height: 3;
        border: solid $warning;
    }

    #edit-warning-buttons {
        height: 3;
        align-horizontal: right;
    }

    #edit-warning-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, source_path: str) -> None:
        super().__init__()
        self.source_path = source_path

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-warning-dialog"):
            yield Static(
                "Warning: edit original source",
                markup=False,
                id="edit-warning-title",
            )
            yield Static(
                "Edit mode changes the original text buffer.",
                markup=False,
                id="edit-warning-message",
            )
            yield Static(
                self.source_path,
                markup=False,
                id="edit-warning-path",
            )
            yield Static(
                "Press Ctrl+S to save, or choose Save or Discard when quitting.",
                markup=False,
                id="edit-warning-annotations",
            )
            yield Input(
                placeholder='Type "Yes" exactly to proceed',
                id="edit-confirmation",
            )
            with Horizontal(id="edit-warning-buttons"):
                yield Button("Cancel", id="cancel-edit")
                yield Button(
                    "Edit original",
                    variant="warning",
                    id="confirm-edit",
                )

    def on_mount(self) -> None:
        self.query_one("#edit-confirmation", Input).focus()

    def action_confirm(self) -> None:
        confirmation = self.query_one("#edit-confirmation", Input).value
        if confirmation != "Yes":
            self.notify(
                'Type "Yes" exactly to enter Edit mode',
                severity="warning",
                timeout=1.75,
            )
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Input.Submitted, "#edit-confirmation")
    def on_confirmation_submitted(self, _event: Input.Submitted) -> None:
        self.action_confirm()

    @on(Button.Pressed, "#confirm-edit")
    def on_confirm_pressed(self, _event: Button.Pressed) -> None:
        self.action_confirm()

    @on(Button.Pressed, "#cancel-edit")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.action_cancel()


class QuitChangesModal(ModalScreen[Optional[str]]):
    """Choose whether dirty text buffers should be saved before quitting."""

    CSS = """
    QuitChangesModal {
        align: center middle;
    }

    #quit-dialog {
        width: 68%;
        max-width: 72;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $panel;
    }

    #quit-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
        color: $warning;
    }

    #quit-message,
    #quit-dirty-panes,
    #quit-hint {
        height: auto;
        margin-bottom: 1;
    }

    #quit-dirty-panes,
    #quit-hint {
        color: $text-muted;
    }

    #quit-buttons {
        height: 3;
        align-horizontal: right;
    }

    #quit-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("s", "save", "Save and quit", priority=True),
        Binding("d", "discard", "Discard and quit", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, dirty_panes: tuple[str, ...]) -> None:
        super().__init__()
        self.dirty_panes = dirty_panes

    def compose(self) -> ComposeResult:
        pane_names = " and ".join(self.dirty_panes)
        with Vertical(id="quit-dialog"):
            yield Static(
                "Unsaved changes",
                markup=False,
                id="quit-title",
            )
            yield Static(
                "Save changes before quitting?",
                markup=False,
                id="quit-message",
            )
            yield Static(
                f"Modified: {pane_names}",
                markup=False,
                id="quit-dirty-panes",
            )
            yield Static(
                "S saves · D discards · Escape cancels",
                markup=False,
                id="quit-hint",
            )
            with Horizontal(id="quit-buttons"):
                yield Button("Cancel", id="cancel-quit")
                yield Button(
                    "Discard",
                    variant="error",
                    id="discard-quit",
                )
                yield Button(
                    "Save",
                    variant="primary",
                    id="save-quit",
                )

    def action_save(self) -> None:
        self.dismiss("save")

    def action_discard(self) -> None:
        self.dismiss("discard")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save-quit")
    def on_save_pressed(self, _event: Button.Pressed) -> None:
        self.action_save()

    @on(Button.Pressed, "#discard-quit")
    def on_discard_pressed(self, _event: Button.Pressed) -> None:
        self.action_discard()

    @on(Button.Pressed, "#cancel-quit")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.action_cancel()


class DemoResetModal(ModalScreen[bool]):
    """Warn before restoring a marked rematch demonstration baseline."""

    CSS = """
    DemoResetModal {
        align: center middle;
    }

    #demo-reset-dialog {
        width: 76%;
        max-width: 84;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $panel;
    }

    #demo-reset-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
        color: $warning;
    }

    #demo-reset-message,
    #demo-reset-description,
    #demo-reset-hint {
        height: auto;
        margin-bottom: 1;
    }

    #demo-reset-description,
    #demo-reset-hint {
        color: $text-muted;
    }

    #demo-reset-buttons {
        height: 3;
        align-horizontal: right;
    }

    #demo-reset-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(self, demo: DemoCase) -> None:
        super().__init__()
        self.demo = demo

    def compose(self) -> ComposeResult:
        with Vertical(id="demo-reset-dialog"):
            yield Static(
                "Reset rematch demo case?",
                markup=False,
                id="demo-reset-title",
            )
            yield Static(
                f"{self.demo.name}\n\n"
                "This is a marked sample. Before its first rematch scan, R "
                "restores both the source and annotation sidecar to the "
                "deliberately stale default case. Any changes in these two "
                "demo files will be discarded.",
                markup=False,
                id="demo-reset-message",
            )
            yield Static(
                self.demo.description,
                markup=False,
                id="demo-reset-description",
            )
            yield Static(
                "After resetting, press R again to inspect the rematch proposals.",
                markup=False,
                id="demo-reset-hint",
            )
            with Horizontal(id="demo-reset-buttons"):
                yield Button("Cancel", id="cancel-demo-reset")
                yield Button(
                    "Reset demo",
                    variant="warning",
                    id="confirm-demo-reset",
                )

    def on_mount(self) -> None:
        self.query_one("#cancel-demo-reset", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#cancel-demo-reset")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-demo-reset")
    def on_confirm_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(True)


class RematchReviewModal(ModalScreen[Optional[tuple[RematchChoice, ...]]]):
    """Review read-only rematch proposals before requesting confirmation."""

    CSS = """
    RematchReviewModal {
        align: center bottom;
        background: $background 10%;
    }

    #rematch-review-dialog {
        width: 96%;
        max-width: 120;
        height: 10;
        margin-bottom: 1;
        padding: 0 1;
        border: round $accent;
        background: $panel;
    }

    #rematch-review-title {
        height: 1;
        text-style: bold;
        color: $accent;
    }

    #rematch-review-detail {
        height: 1fr;
        padding: 0 1;
        background: $surface;
    }

    #rematch-review-hint {
        height: 1;
        color: $text-muted;
        text-overflow: ellipsis;
    }

    #rematch-review-buttons {
        height: 3;
        grid-size: 6 1;
        grid-columns: 1fr 1fr 1fr 1fr 1fr 1fr;
        grid-rows: 3;
        grid-gutter: 0 1;
    }

    #rematch-review-buttons Button {
        width: 1fr;
        min-width: 0;
    }
    """

    BINDINGS = [
        Binding("j,down", "next_item", "Next", priority=True, show=False),
        Binding("k,up", "previous_item", "Previous", priority=True, show=False),
        Binding("space", "toggle_choice", "Accept/skip", priority=True, show=False),
        Binding("c", "cycle_candidate", "Next candidate", priority=True, show=False),
        Binding(
            "shift+c",
            "previous_candidate",
            "Previous candidate",
            priority=True,
            show=False,
        ),
        Binding("m", "use_manual_selection", "Use source selection", priority=True, show=False),
        Binding("ctrl+s", "continue_to_confirmation", "Apply selected", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(
        self,
        report: RematchReport,
        source_text: str,
        manual_source_range: Optional[AnnotationRange] = None,
        initial_choices: Iterable[RematchChoice] = (),
        preview_callback: Optional[RematchPreviewCallback] = None,
    ) -> None:
        super().__init__()
        self.report = report
        self.source_text = source_text
        self.manual_source_range = manual_source_range
        self.preview_callback = preview_callback
        self.items = report.stale_items
        self.current_index = 0
        initial_by_id = {
            choice.annotation_id: choice for choice in initial_choices
        }
        self.candidates_by_id: dict[str, tuple] = {}
        self.candidate_indices: dict[str, int] = {}
        self.selected_ids: set[str] = set()
        for item in self.items:
            annotation_id = item.annotation.annotation_id
            candidates = list(item.candidates)
            if manual_source_range is not None and not item.annotation.errors:
                try:
                    manual_candidate = make_manual_candidate(
                        source_text,
                        item.annotation,
                        manual_source_range,
                    )
                except ValueError:
                    manual_candidate = None
                if (
                    manual_candidate is not None
                    and all(
                        candidate.source_range != manual_candidate.source_range
                        for candidate in candidates
                    )
                ):
                    candidates.append(manual_candidate)
            self.candidates_by_id[annotation_id] = tuple(candidates)
            initial = initial_by_id.get(annotation_id)
            candidate_index = 0
            if initial is not None:
                for index, candidate in enumerate(candidates):
                    if candidate.source_range == initial.candidate.source_range:
                        candidate_index = index
                        break
                self.selected_ids.add(annotation_id)
            elif (
                item.proposed is not None
                and item.proposed.confidence >= 90
                and item.status is not RematchStatus.AMBIGUOUS
            ):
                self.selected_ids.add(annotation_id)
            self.candidate_indices[annotation_id] = candidate_index

    def compose(self) -> ComposeResult:
        with Vertical(id="rematch-review-dialog"):
            yield Static(
                "Review annotation rematches",
                markup=False,
                id="rematch-review-title",
            )
            yield Static("", markup=False, id="rematch-review-detail")
            yield Static(
                "J/K item · Space decision · C candidates · M selection · "
                "Ctrl+S confirm",
                markup=False,
                id="rematch-review-hint",
            )
            with Grid(id="rematch-review-buttons"):
                yield Button("Cancel", id="cancel-rematch-review")
                yield Button("← Item", id="previous-rematch-item")
                yield Button("Item →", id="next-rematch-item")
                yield Button("Accept", id="toggle-rematch-choice")
                yield Button("Cand. →", id="cycle-rematch-candidate")
                yield Button(
                    "Confirm…",
                    variant="primary",
                    id="continue-rematch",
                )

    def on_mount(self) -> None:
        self._refresh_review()
        self.query_one("#cancel-rematch-review", Button).focus()

    def _source_position(self, offset: int) -> str:
        """Return a human-readable one-based source position."""

        offset = max(0, min(offset, len(self.source_text)))
        line = self.source_text.count("\n", 0, offset) + 1
        line_start = self.source_text.rfind("\n", 0, offset) + 1
        return f"line {line}, column {offset - line_start + 1}"

    def _current_item(self) -> Optional[RematchItem]:
        if not self.items:
            return None
        return self.items[self.current_index]

    def _current_candidate(self, item: RematchItem):
        candidates = self.candidates_by_id[item.annotation.annotation_id]
        if not candidates:
            return None
        index = self.candidate_indices[item.annotation.annotation_id]
        return candidates[index]

    def _refresh_review(self) -> None:
        counts = "  ".join(
            f"{status.value}: {self.report.count(status)}"
            for status in (
                RematchStatus.SHIFTED,
                RematchStatus.MODIFIED,
                RematchStatus.AMBIGUOUS,
                RematchStatus.ORPHANED,
                RematchStatus.MALFORMED,
            )
            if self.report.count(status)
        )
        self.query_one("#rematch-review-title", Static).update(
            f"Rematch review · item {self.current_index + 1}/"
            f"{len(self.items)} · {len(self.selected_ids)} selected · {counts}"
        )

        item = self._current_item()
        if item is None:
            self.query_one("#rematch-review-detail", Static).update(
                "Nothing needs review."
            )
            return
        record = item.annotation
        candidate = self._current_candidate(item)
        candidates = self.candidates_by_id[record.annotation_id]
        candidate_index = self.candidate_indices[record.annotation_id]
        old_range = (
            f"[{record.start}, {record.end})"
            if record.start is not None and record.end is not None
            else "unavailable"
        )
        decision = (
            "ACCEPTED"
            if record.annotation_id in self.selected_ids
            else "SKIPPED"
        )
        toggle_button = self.query_one("#toggle-rematch-choice", Button)
        toggle_button.label = "Skip" if decision == "ACCEPTED" else "Accept"
        toggle_button.disabled = not candidates
        self.query_one(
            "#cycle-rematch-candidate", Button
        ).disabled = len(candidates) < 2
        if candidate is not None:
            detail = "\n".join(
                (
                    f"#{record.ordinal} · {item.status.value} · "
                    f"{decision} · candidate {candidate_index + 1}/"
                    f"{len(candidates)} · ~{candidate.confidence}% · "
                    f"{candidate.method}",
                    f"Old {old_range} → proposed "
                    f"[{candidate.source_range.start}, "
                    f"{candidate.source_range.end}) · "
                    f"{self._source_position(candidate.source_range.start)} "
                    f"· evidence: "
                    + ("; ".join(candidate.evidence) or "(none)"),
                )
            )
        else:
            detail = "\n".join(
                (
                    f"#{record.ordinal} · {item.status.value} · {decision} · "
                    "no safe automatic candidate",
                    f"Old {old_range} · {item.explanation} · use M with a "
                    "source Visual selection",
                )
            )
        self.query_one("#rematch-review-detail", Static).update(detail)
        if self.preview_callback is not None:
            self.preview_callback(item, candidate)

    def action_next_item(self) -> None:
        if self.items:
            self.current_index = (self.current_index + 1) % len(self.items)
            self._refresh_review()

    def action_previous_item(self) -> None:
        if self.items:
            self.current_index = (self.current_index - 1) % len(self.items)
            self._refresh_review()

    def action_toggle_choice(self) -> None:
        item = self._current_item()
        if (
            item is None
            or not self.candidates_by_id[item.annotation.annotation_id]
        ):
            self.notify(
                "This annotation has no safe candidate",
                severity="warning",
                timeout=1.5,
            )
            return
        annotation_id = item.annotation.annotation_id
        if annotation_id in self.selected_ids:
            self.selected_ids.remove(annotation_id)
        else:
            self.selected_ids.add(annotation_id)
        self._refresh_review()

    def action_cycle_candidate(self) -> None:
        self._cycle_candidate(1)

    def action_previous_candidate(self) -> None:
        self._cycle_candidate(-1)

    def _cycle_candidate(self, direction: int) -> None:
        item = self._current_item()
        if item is None:
            return
        candidates = self.candidates_by_id[item.annotation.annotation_id]
        if len(candidates) < 2:
            self.notify("No alternative candidate", timeout=1.25)
            return
        annotation_id = item.annotation.annotation_id
        self.candidate_indices[annotation_id] = (
            self.candidate_indices[annotation_id] + direction
        ) % len(candidates)
        self._refresh_review()

    def action_use_manual_selection(self) -> None:
        item = self._current_item()
        if item is None or self.manual_source_range is None:
            self.notify(
                "Make a Visual selection in the source pane before pressing R",
                severity="warning",
                timeout=2.5,
            )
            return
        candidates = self.candidates_by_id[item.annotation.annotation_id]
        manual_index = next(
            (
                index
                for index, candidate in enumerate(candidates)
                if candidate.method == "manual-selection"
            ),
            None,
        )
        if manual_index is None:
            self.notify(
                "That selection is already the current automatic candidate or "
                "cannot be used for this record",
                severity="warning",
                timeout=2,
            )
            return
        annotation_id = item.annotation.annotation_id
        self.candidate_indices[annotation_id] = manual_index
        self.selected_ids.add(annotation_id)
        self._refresh_review()

    def _choices(self) -> tuple[RematchChoice, ...]:
        choices = []
        for item in self.items:
            annotation_id = item.annotation.annotation_id
            if annotation_id not in self.selected_ids:
                continue
            candidate = self._current_candidate(item)
            if candidate is not None:
                choices.append(RematchChoice(annotation_id, candidate))
        return tuple(choices)

    def action_continue_to_confirmation(self) -> None:
        choices = self._choices()
        if not choices:
            self.notify(
                "Select at least one proposal to apply",
                severity="warning",
                timeout=1.75,
            )
            return
        self.dismiss(choices)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#next-rematch-item")
    def on_next_pressed(self, _event: Button.Pressed) -> None:
        self.action_next_item()

    @on(Button.Pressed, "#previous-rematch-item")
    def on_previous_pressed(self, _event: Button.Pressed) -> None:
        self.action_previous_item()

    @on(Button.Pressed, "#toggle-rematch-choice")
    def on_toggle_pressed(self, _event: Button.Pressed) -> None:
        self.action_toggle_choice()

    @on(Button.Pressed, "#cycle-rematch-candidate")
    def on_cycle_pressed(self, _event: Button.Pressed) -> None:
        self.action_cycle_candidate()

    @on(Button.Pressed, "#continue-rematch")
    def on_continue_pressed(self, _event: Button.Pressed) -> None:
        self.action_continue_to_confirmation()

    @on(Button.Pressed, "#cancel-rematch-review")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.action_cancel()


class RematchConfirmModal(ModalScreen[Optional[str]]):
    """The single confirmation gate before a rematch changes a sidecar."""

    CSS = """
    RematchConfirmModal {
        align: center bottom;
        background: $background 10%;
    }

    #rematch-confirm-dialog {
        width: 96%;
        max-width: 120;
        height: 10;
        margin-bottom: 1;
        padding: 0 1;
        border: round $warning;
        background: $panel;
    }

    #rematch-confirm-title {
        height: 1;
        text-style: bold;
        color: $warning;
        text-overflow: ellipsis;
    }

    #rematch-confirm-message {
        height: 1fr;
        padding: 0 1;
        background: $surface;
    }

    #rematch-confirm-hint {
        height: 1;
        color: $text-muted;
        text-overflow: ellipsis;
    }

    #rematch-confirm-buttons {
        height: 3;
        grid-size: 5 1;
        grid-columns: 1fr 1fr 1fr 1fr 1fr;
        grid-rows: 3;
        grid-gutter: 0 1;
    }

    #rematch-confirm-buttons Button {
        width: 1fr;
        min-width: 0;
    }
    """

    BINDINGS = [
        Binding("j,down", "next_choice", "Next selected", priority=True, show=False),
        Binding("k,up", "previous_choice", "Previous selected", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(
        self,
        report: RematchReport,
        choices: tuple[RematchChoice, ...],
        preview_callback: Optional[RematchPreviewCallback] = None,
    ) -> None:
        super().__init__()
        self.report = report
        self.choices = choices
        self.preview_callback = preview_callback
        self.current_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="rematch-confirm-dialog"):
            yield Static("", markup=False, id="rematch-confirm-title")
            yield Static("", markup=False, id="rematch-confirm-message")
            yield Static(
                "J/K selected change · source stays unchanged · sidecar backup "
                "is created · Cancel is focused",
                markup=False,
                id="rematch-confirm-hint",
            )
            with Grid(id="rematch-confirm-buttons"):
                yield Button("Cancel", id="cancel-rematch-confirm")
                yield Button("← Selected", id="previous-rematch-choice")
                yield Button("Selected →", id="next-rematch-choice")
                yield Button("Review", id="review-rematch-again")
                yield Button(
                    "Apply",
                    variant="warning",
                    id="apply-rematch-confirm",
                )

    def _current_choice(self) -> Optional[RematchChoice]:
        if not self.choices:
            return None
        return self.choices[self.current_index]

    def _item_for_choice(
        self, choice: RematchChoice
    ) -> Optional[RematchItem]:
        return next(
            (
                item
                for item in self.report.items
                if item.annotation.annotation_id == choice.annotation_id
            ),
            None,
        )

    def _refresh_confirmation(self) -> None:
        average, lowest = confidence_summary(self.choices)
        selected_ids = {choice.annotation_id for choice in self.choices}
        high = sum(choice.candidate.confidence >= 90 for choice in self.choices)
        reviewed = len(self.choices) - high
        method_counts: dict[str, int] = {}
        for choice in self.choices:
            method_counts[choice.candidate.method] = (
                method_counts.get(choice.candidate.method, 0) + 1
            )
        evidence_summary = ", ".join(
            f"{method.replace('-', ' ')}: {count}"
            for method, count in sorted(method_counts.items())
        )
        strong_context = sum(
            choice.candidate.context_similarity >= 0.90
            for choice in self.choices
        )
        ambiguous_skipped = sum(
            item.status is RematchStatus.AMBIGUOUS
            and item.annotation.annotation_id not in selected_ids
            for item in self.report.items
        )
        unresolved = sum(
            item.status in {RematchStatus.ORPHANED, RematchStatus.MALFORMED}
            and item.annotation.annotation_id not in selected_ids
            for item in self.report.items
        )
        self.query_one("#rematch-confirm-title", Static).update(
            f"Apply rematch? · selected {self.current_index + 1}/"
            f"{len(self.choices)} · ~{average}% average · ~{lowest}% lowest"
        )
        choice = self._current_choice()
        item = None if choice is None else self._item_for_choice(choice)
        if choice is None or item is None:
            current_detail = "No selected rematch is available."
        else:
            candidate = choice.candidate
            current_detail = (
                f"#{item.annotation.ordinal} · {item.status.value} · "
                f"[{candidate.source_range.start}, "
                f"{candidate.source_range.end}) · "
                f"~{candidate.confidence}% · {candidate.method}\n"
                f"Current evidence: "
                f"{'; '.join(candidate.evidence) or '(none)'}"
            )
            if self.preview_callback is not None:
                self.preview_callback(item, candidate)
        batch_detail = (
            f"Estimated correctness: ~{average}% average · "
            f"Lowest selected estimate: ~{lowest}% · "
            f"Evidence: {evidence_summary} · strong context: {strong_context} · "
            f"high: {high} · reviewed: {reviewed} · "
            f"ambiguous skipped: {ambiguous_skipped} · unresolved: {unresolved}"
        )
        self.query_one("#rematch-confirm-message", Static).update(
            current_detail + "\n" + batch_detail
        )

    def on_mount(self) -> None:
        self._refresh_confirmation()
        self.query_one("#cancel-rematch-confirm", Button).focus()

    def action_next_choice(self) -> None:
        if self.choices:
            self.current_index = (self.current_index + 1) % len(self.choices)
            self._refresh_confirmation()

    def action_previous_choice(self) -> None:
        if self.choices:
            self.current_index = (self.current_index - 1) % len(self.choices)
            self._refresh_confirmation()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel-rematch-confirm")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#previous-rematch-choice")
    def on_previous_choice_pressed(self, _event: Button.Pressed) -> None:
        self.action_previous_choice()

    @on(Button.Pressed, "#next-rematch-choice")
    def on_next_choice_pressed(self, _event: Button.Pressed) -> None:
        self.action_next_choice()

    @on(Button.Pressed, "#review-rematch-again")
    def on_review_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss("review")

    @on(Button.Pressed, "#apply-rematch-confirm")
    def on_apply_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss("apply")


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

    #viewer .text-area--rematch-candidate {
        color: $text;
        background: $success 50%;
        text-style: bold underline;
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
        Binding("q", "request_quit", "Quit"),
        Binding(
            "ctrl+q",
            "request_quit_force",
            "Quit",
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+s",
            "save_changes",
            "Save changes",
            show=False,
            priority=True,
        ),
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
        Binding(
            "S,shift+s",
            "cycle_sync_mode",
            "Cycle alignment mode",
            show=False,
        ),
        Binding(
            "R,shift+r",
            "rematch_annotations",
            "Review stale annotations",
            show=False,
        ),
        Binding(".", "align_panes", "Align focused pane", show=False),
        Binding(
            "z",
            "scroll_position_prefix_or_middle",
            "Scroll-position prefix",
            show=False,
            priority=True,
        ),
        Binding(
            "t",
            "scroll_cursor_top",
            "Cursor line at top",
            show=False,
            priority=True,
        ),
        Binding(
            "b",
            "scroll_cursor_bottom",
            "Cursor line at bottom",
            show=False,
            priority=True,
        ),
        Binding(
            "i",
            "toggle_metadata_folds",
            "Toggle metadata folds",
            show=False,
            priority=True,
        ),
        Binding(
            "o",
            "open_metadata_fold",
            "Open metadata fold",
            show=False,
            priority=True,
        ),
        Binding(
            "c",
            "close_metadata_fold",
            "Close metadata fold",
            show=False,
            priority=True,
        ),
    ]

    def __init__(
        self,
        source: LoadedTextFile,
        *,
        soft_wrap: bool = True,
        clipboard_writer: ClipboardWriter = copy_to_system_clipboard,
        source_writer: SourceWriter = save_text_file,
        annotation_store: Optional[AnnotationStore] = None,
        initial_source_cursor: Optional[tuple[int, int]] = None,
    ) -> None:
        super().__init__()
        self.source = source
        self._saved_source = source
        self.soft_wrap = soft_wrap
        self._clipboard_writer = clipboard_writer
        self._source_writer = source_writer
        self.initial_source_cursor = initial_source_cursor
        self.exit_source_cursor: Optional[tuple[int, int]] = None
        self.quit_completed = False
        self.last_copy_backend: Optional[str] = None
        self._pane_switch_pending = False
        self._pane_switch_timer: Optional[Timer] = None
        self._scroll_position_pending = False
        self._scroll_position_timer: Optional[Timer] = None
        self._search_target_id: Optional[str] = None
        self._search_queries: dict[str, str] = {}
        self._search_matches: dict[str, AnnotationRange] = {}
        self._pending_rematch_report: Optional[RematchReport] = None
        self._pending_rematch_choices: tuple[RematchChoice, ...] = ()
        self._pending_manual_range: Optional[AnnotationRange] = None
        self._rematch_preview_state: Optional[RematchPreviewState] = None
        self._suspend_alignment = False
        self.demo_case: Optional[DemoCase] = None
        self._demo_case_error: Optional[str] = None
        self._demo_reset_complete = False
        try:
            self.demo_case = load_demo_case(source.path)
        except DemoCaseError as error:
            self._demo_case_error = str(error)
        self.sync_mode = SyncMode.NORMAL
        self.annotation_store = annotation_store or AnnotationStore(source.path)
        try:
            self.annotation_text = self.annotation_store.read_text()
        except AnnotationFileError as error:
            self.annotation_text = f"# Annotation file error\n\n{error}\n"
        self._saved_annotation_text = self.annotation_text
        self.annotation_links = parse_annotation_links(self.annotation_text)
        self.annotation_ranges = tuple(
            link.source_range for link in self.annotation_links
        )
        self._rebuild_source_line_starts()

    def _rebuild_source_line_starts(self) -> None:
        self._source_line_starts = [0]
        self._source_line_starts.extend(
            index + 1
            for index, character in enumerate(self.source.text)
            if character == "\n"
        )

    def compose(self) -> ComposeResult:
        yield Static(self._titlebar_text(), markup=False, id="titlebar")
        with Horizontal(id="workspace"):
            viewer = VimTextArea(
                self.source.text,
                soft_wrap=self.soft_wrap,
                annotation_ranges=self.annotation_ranges,
                id="viewer",
            )
            viewer.border_title = (
                "Extracted PDF text"
                if self.source.source_kind == "pdf"
                else "Source"
            )
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
            self._normal_help_text(),
            markup=False,
            id="help",
        )

    def on_mount(self) -> None:
        self.query_one("#viewer", VimTextArea).focus()
        self._refresh_status()
        self.call_after_refresh(self._restore_initial_source_cursor)
        if self._demo_case_error is not None:
            self.notify(
                self._demo_case_error,
                severity="warning",
                timeout=6,
            )

    def _restore_initial_source_cursor(self) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        if self.initial_source_cursor is not None:
            viewer.move_cursor(self.initial_source_cursor, center=True)
        self._align_annotation_to_source()

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KiB"
        return f"{size_bytes / (1024 * 1024):.1f} MiB"

    def _titlebar_text(self) -> str:
        size = self._format_size(self.source.size_bytes)
        pages = (
            f"  |  {self.source.pdf_document.page_count} PDF pages"
            if self.source.pdf_document is not None
            else ""
        )
        demo = (
            f"  |  DEMO: {self.demo_case.name}"
            if self.demo_case is not None
            else ""
        )
        return (
            f"{self.source.path}  |  {self.source.line_count} lines  |  {size}"
            f"{pages}  |  notes: {self.annotation_store.path.name}{demo}"
        )

    def _normal_help_text(self) -> str:
        edit_hint = "i edit" if self.source.editable else "PDF source read-only"
        return (
            f"Tab/Ctrl-W W panes  {edit_hint}  Ctrl-S save  R rematch  "
            "S sync  . align  zi/zo/zc folds  "
            "h/j/k/l move  Ctrl-E/Y scroll  zt/zz/zb place  "
            "u/Ctrl-R undo/redo  / search  n/N repeat  v select  "
            "a annotate  q quit"
        )

    @staticmethod
    def _edit_help_text() -> str:
        return (
            "EDIT MODE  type to insert  Ctrl-S save  Ctrl-W delete word  "
            "Ctrl-H delete char  Esc/Ctrl-[ normal  Ctrl-Q quit"
        )

    def _refresh_titlebar(self) -> None:
        self.query_one("#titlebar", Static).update(self._titlebar_text())

    def _dirty_panes(self) -> tuple[str, ...]:
        dirty = []
        if self.query_one("#viewer", VimTextArea).text != self._saved_source.text:
            dirty.append("source")
        if (
            self.query_one("#annotator", AnnotationTextArea).canonical_text
            != self._saved_annotation_text
        ):
            dirty.append("annotations")
        return tuple(dirty)

    def _valid_annotation_links(self) -> tuple[AnnotationLink, ...]:
        source_length = len(self.query_one("#viewer", VimTextArea).text)
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
        offset = location_to_offset(viewer.text, viewer.cursor_location)
        link = self._link_for_source_offset(offset)
        if link is None:
            annotator.clear_active_link()
            viewer.set_active_annotation_range(None)
            return
        annotator.show_link(link, move_cursor=True)
        viewer.set_active_annotation_range(link.source_range)

    def _focus_annotation_at_cursor(self) -> Optional[AnnotationLink]:
        """Update annotation-pane focus without moving the source cursor."""

        annotator = self.query_one("#annotator", AnnotationTextArea)
        link = self._link_for_sidecar_line(
            annotator.full_line_for_display(annotator.cursor_location[0])
        )
        if link is None:
            annotator.clear_active_link()
            return None

        annotator.show_link(link, move_cursor=False)
        return link

    def _align_source_to_annotation(self) -> None:
        link = self._focus_annotation_at_cursor()
        if link is None:
            return

        viewer = self.query_one("#viewer", VimTextArea)
        viewer.set_active_annotation_range(link.source_range)
        if viewer.visual_mode:
            viewer.action_cancel_visual()
        viewer.move_cursor(
            self._source_location_for_offset(link.source_range.start),
            center=True,
        )

    def _refresh_status(self) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        target: TextArea = annotator if annotator.has_focus else viewer
        edit_mode = (
            viewer.edit_mode
            if target is viewer
            else annotator.edit_mode
        )
        self.query_one("#help", Static).update(
            self._edit_help_text()
            if edit_mode
            else self._normal_help_text()
        )
        if self._pane_switch_pending:
            self.query_one("#status", Static).update(
                "CTRL-W  |  press w to switch panes"
            )
            return
        if self._scroll_position_pending:
            self.query_one("#status", Static).update(
                "Z  |  t/z/b place cursor · i toggle folds · "
                "o/c open/close metadata"
            )
            return

        row, column = target.cursor_location
        selected = target.selected_text
        mode = (
            "EDIT"
            if edit_mode
            else "VISUAL"
            if target is viewer and viewer.visual_mode
            else "NORMAL"
        )
        selection_info = f"  |  {len(selected)} chars selected" if selected else ""
        dirty_panes = self._dirty_panes()
        modified_info = (
            f"  |  MODIFIED: {', '.join(dirty_panes)}" if dirty_panes else ""
        )
        wrap_info = "wrap" if target.soft_wrap else "no wrap"
        self.query_one("#status", Static).update(
            f"{mode}  |  ALIGN {self.sync_mode.value.upper()}  |  "
            f"Ln {row + 1}, Col {column + 1}  |  {wrap_info}"
            f"{selection_info}{modified_info}"
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
        if self._scroll_position_pending:
            self._clear_scroll_position_prefix()
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

    def action_cycle_sync_mode(self) -> None:
        """Cycle Normal, Sync, and Off cross-pane alignment modes."""

        modes = (SyncMode.NORMAL, SyncMode.SYNC, SyncMode.OFF)
        current_index = modes.index(self.sync_mode)
        self.sync_mode = modes[(current_index + 1) % len(modes)]
        self._refresh_status()
        self.notify(f"Alignment mode: {self.sync_mode.value}", timeout=1.25)

    def action_align_panes(self) -> None:
        """Manually align the panes and center both cursor lines."""

        target = self._focused_search_target()
        if isinstance(target, VimTextArea):
            self._align_annotation_to_source()
            _position_cursor_line(target, "middle")
            annotator = self.query_one("#annotator", AnnotationTextArea)
            if annotator.active_link is not None:
                _position_cursor_line(annotator, "middle")
        elif isinstance(target, AnnotationTextArea):
            self._align_source_to_annotation()
            _position_cursor_line(target, "middle")
            viewer = self.query_one("#viewer", VimTextArea)
            if target.active_link is not None:
                _position_cursor_line(viewer, "middle")

    def _capture_rematch_preview_state(self) -> None:
        """Remember both panes before the review starts moving them."""

        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        self._rematch_preview_state = RematchPreviewState(
            viewer_cursor=viewer.cursor_location,
            viewer_selection=viewer.selection,
            viewer_visual_mode=viewer.visual_mode,
            viewer_scroll=(float(viewer.scroll_x), float(viewer.scroll_y)),
            annotator_cursor=annotator.cursor_location,
            annotator_selection=annotator.selection,
            annotator_scroll=(
                float(annotator.scroll_x),
                float(annotator.scroll_y),
            ),
            annotator_active_link=annotator.active_link,
            focused_pane="annotator" if annotator.has_focus else "viewer",
        )

    def _preview_rematch_candidate(
        self,
        item: RematchItem,
        candidate: Optional[RematchCandidate],
    ) -> None:
        """Reveal the current proposal in the panes behind the compact modal."""

        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        viewer.visual_mode = False
        viewer.selection = Selection.cursor(viewer.cursor_location)

        preview_range = (
            candidate.source_range if candidate is not None else None
        )
        viewer.set_rematch_candidate_range(preview_range)
        preview_offset: Optional[int] = None
        if preview_range is not None:
            preview_offset = preview_range.start
        else:
            record = item.annotation
            if (
                record.start is not None
                and 0 <= record.start < len(viewer.text)
            ):
                preview_offset = record.start
        if preview_offset is not None:
            viewer.move_cursor(
                self._source_location_for_offset(preview_offset)
            )
            _position_cursor_line(viewer, "top")

        record_line = annotator.canonical_text.count(
            "\n", 0, item.annotation.block_start
        )
        link = next(
            (
                annotation_link
                for annotation_link in self.annotation_links
                if annotation_link.sidecar_start_line == record_line
            ),
            None,
        )
        if link is None:
            annotator.clear_active_link()
            preview_line = record_line
        else:
            annotator.show_link(link, move_cursor=False)
            preview_line = (
                link.sidecar_quote_start_line
                if link.sidecar_quote_start_line is not None
                else link.sidecar_note_line
            )
        annotator.move_cursor(
            (annotator._display_line_for_full(preview_line), 0)
        )
        _position_cursor_line(annotator, "top")

    def _restore_rematch_preview(self) -> None:
        """Remove the proposal marker and restore the pre-review pane state."""

        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        self._suspend_alignment = True
        viewer.set_rematch_candidate_range(None)
        state = self._rematch_preview_state
        if state is not None:
            viewer.move_cursor(state.viewer_cursor)
            viewer.selection = state.viewer_selection
            viewer.visual_mode = state.viewer_visual_mode
            viewer.scroll_to(
                x=state.viewer_scroll[0],
                y=state.viewer_scroll[1],
                animate=False,
                immediate=True,
            )
            if state.annotator_active_link is None:
                annotator.clear_active_link()
            else:
                annotator.show_link(
                    state.annotator_active_link,
                    move_cursor=False,
                )
            annotator.move_cursor(state.annotator_cursor)
            annotator.selection = state.annotator_selection
            annotator.scroll_to(
                x=state.annotator_scroll[0],
                y=state.annotator_scroll[1],
                animate=False,
                immediate=True,
            )
            if state.focused_pane == "annotator":
                annotator.focus()
            else:
                viewer.focus()
        self.call_after_refresh(self._resume_alignment)

    def _resume_alignment(self) -> None:
        self._suspend_alignment = False

    def action_rematch_annotations(self) -> None:
        """Run a read-only scan and open review when stale records exist."""

        if self.demo_case is not None and not self._demo_reset_complete:
            self.push_screen(
                DemoResetModal(self.demo_case),
                callback=self._finish_demo_reset,
            )
            return
        if self._dirty_panes():
            self.notify(
                "Save or discard text edits before scanning for rematches",
                severity="warning",
                timeout=3,
            )
            return
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        report = scan_rematches(viewer.text, annotator.canonical_text)
        if not report.items:
            self.notify("No structured annotations to rematch", timeout=2)
            return
        if not report.stale_items:
            self.notify(
                f"All {len(report.items)} annotation(s) still match",
                timeout=2,
            )
            return
        self._pending_rematch_report = report
        self._pending_rematch_choices = ()
        if viewer.selected_text:
            selection_start = location_to_offset(
                viewer.text, viewer.selection.start
            )
            selection_end = location_to_offset(
                viewer.text, viewer.selection.end
            )
            start, end = sorted((selection_start, selection_end))
            self._pending_manual_range = AnnotationRange(start, end)
        else:
            self._pending_manual_range = None
        self._capture_rematch_preview_state()
        self.push_screen(
            RematchReviewModal(
                report,
                viewer.text,
                self._pending_manual_range,
                preview_callback=self._preview_rematch_candidate,
            ),
            callback=self._finish_rematch_review,
        )

    def _finish_demo_reset(self, confirmed: bool) -> None:
        demo = self.demo_case
        if not confirmed or demo is None:
            self._refresh_status()
            return
        try:
            saved_source, saved_annotations = reset_demo_case(
                self._saved_source,
                self.annotation_store,
                demo,
            )
        except DemoCaseError as error:
            self.notify(str(error), severity="error", timeout=6)
            return

        self.source = saved_source
        self._saved_source = saved_source
        self.annotation_text = saved_annotations
        self._saved_annotation_text = saved_annotations
        self.annotation_links = parse_annotation_links(saved_annotations)
        self.annotation_ranges = tuple(
            link.source_range for link in self.annotation_links
        )
        self._rebuild_source_line_starts()
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        viewer.leave_edit_mode()
        annotator.leave_edit_mode()
        viewer.load_text(saved_source.text)
        viewer.set_annotation_ranges(self.annotation_ranges)
        viewer.move_cursor((0, 0))
        annotator.load_annotation_text(saved_annotations)
        annotator.set_annotation_links(self.annotation_links)
        self._set_search_match("viewer", None)
        self._set_search_match("annotator", None)
        self._demo_reset_complete = True
        self._refresh_titlebar()
        viewer.focus()
        self._align_annotation_to_source()
        self._refresh_status()
        self.notify(
            "Demo restored to its default stale case. Press R again to scan.",
            timeout=4,
        )

    def _finish_rematch_review(
        self, choices: Optional[tuple[RematchChoice, ...]]
    ) -> None:
        if choices is None:
            self._restore_rematch_preview()
            self._pending_rematch_report = None
            self._pending_rematch_choices = ()
            self._pending_manual_range = None
            self._rematch_preview_state = None
            self._refresh_status()
            return
        report = self._pending_rematch_report
        if report is None:
            self._restore_rematch_preview()
            self._rematch_preview_state = None
            return
        self._pending_rematch_choices = choices
        self.push_screen(
            RematchConfirmModal(
                report,
                choices,
                preview_callback=self._preview_rematch_candidate,
            ),
            callback=self._finish_rematch_confirmation,
        )

    def _finish_rematch_confirmation(self, choice: Optional[str]) -> None:
        report = self._pending_rematch_report
        choices = self._pending_rematch_choices
        if report is None:
            return
        if choice == "review":
            self.push_screen(
                RematchReviewModal(
                    report,
                    self.query_one("#viewer", VimTextArea).text,
                    self._pending_manual_range,
                    choices,
                    preview_callback=self._preview_rematch_candidate,
                ),
                callback=self._finish_rematch_review,
            )
            return
        if choice != "apply":
            self._restore_rematch_preview()
            self._pending_rematch_report = None
            self._pending_rematch_choices = ()
            self._pending_manual_range = None
            self._rematch_preview_state = None
            self._refresh_status()
            return
        self._restore_rematch_preview()
        self._rematch_preview_state = None
        self._apply_confirmed_rematch(report, choices)

    def _apply_confirmed_rematch(
        self,
        report: RematchReport,
        choices: tuple[RematchChoice, ...],
    ) -> None:
        """Fingerprint, back up, write, validate, and refresh one approved batch."""

        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        if self._dirty_panes():
            self.notify(
                "Buffers changed after review; run R to scan again",
                severity="warning",
                timeout=4,
            )
            return
        if (
            text_sha256(viewer.text) != report.source_sha256
            or text_sha256(annotator.canonical_text) != report.sidecar_sha256
        ):
            self.notify(
                "Documents changed after the scan; run R to recalculate",
                severity="warning",
                timeout=4,
            )
            return
        try:
            disk_source = load_source_file(self.source.path)
            disk_sidecar = self.annotation_store.read_text()
        except (TextFileError, AnnotationFileError) as error:
            self.notify(str(error), severity="error", timeout=5)
            return
        if (
            text_sha256(disk_source.text) != report.source_sha256
            or text_sha256(disk_sidecar) != report.sidecar_sha256
        ):
            self.notify(
                "A file changed on disk after the scan; reload and run R again",
                severity="warning",
                timeout=5,
            )
            return

        now = datetime.now(timezone.utc)
        rematched_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        backup_label = "before-rematch-" + now.strftime("%Y%m%dT%H%M%SZ")
        try:
            updated = apply_rematches(
                disk_sidecar,
                disk_source.text,
                choices,
                rematched_at=rematched_at,
                pdf_document=disk_source.pdf_document,
            )
            saved, backup_path = self.annotation_store.transactional_write(
                updated,
                backup_label=backup_label,
                validate=lambda written: validate_applied_rematches(
                    disk_sidecar,
                    written,
                    disk_source.text,
                    choices,
                    pdf_document=disk_source.pdf_document,
                ),
            )
        except (AnnotationFileError, ValueError) as error:
            self.notify(str(error), severity="error", timeout=6)
            return

        self.annotation_text = saved
        self._saved_annotation_text = saved
        self.annotation_links = parse_annotation_links(saved)
        self.annotation_ranges = tuple(
            link.source_range for link in self.annotation_links
        )
        annotator.load_annotation_text(saved)
        annotator.set_annotation_links(self.annotation_links)
        viewer.set_annotation_ranges(self.annotation_ranges)
        self._set_search_match("annotator", None)
        self._pending_rematch_report = None
        self._pending_rematch_choices = ()
        self._pending_manual_range = None
        if self.demo_case is not None:
            self._demo_reset_complete = False
        viewer.focus()
        self._align_annotation_to_source()
        self._refresh_status()
        backup_message = (
            f" Backup: {backup_path.name}." if backup_path is not None else ""
        )
        self.notify(
            f"Rematched {len(choices)} annotation(s)."
            + (
                " PDF anchors refreshed."
                if disk_source.pdf_document is not None
                else ""
            )
            + backup_message,
            timeout=5,
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        search_input = self.query_one("#search-input", SearchInput)
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        editing_pane = (
            viewer.edit_mode
            and viewer.has_focus
            or annotator.edit_mode
            and annotator.has_focus
        )
        if editing_pane and action in {
            "request_quit",
            "start_pane_switch",
            "finish_pane_switch",
            "start_search",
            "search_next",
            "search_previous",
            "cycle_sync_mode",
            "rematch_annotations",
            "align_panes",
            "scroll_position_prefix_or_middle",
            "scroll_cursor_top",
            "scroll_cursor_bottom",
            "toggle_metadata_folds",
            "open_metadata_fold",
            "close_metadata_fold",
        }:
            return False
        modal_open = isinstance(self.screen, ModalScreen)
        if action == "save_changes" and (modal_open or search_input.has_focus):
            return False
        if action in {"request_quit", "request_quit_force"} and modal_open:
            return False
        if action in {
            "start_search",
            "search_next",
            "search_previous",
        }:
            if modal_open or search_input.has_focus:
                return False
        if action in {
            "cycle_sync_mode",
            "rematch_annotations",
            "align_panes",
            "scroll_position_prefix_or_middle",
            "scroll_cursor_top",
            "scroll_cursor_bottom",
            "toggle_metadata_folds",
            "open_metadata_fold",
            "close_metadata_fold",
        }:
            if modal_open or search_input.has_focus:
                return False
        if action in {"start_pane_switch", "finish_pane_switch"}:
            if modal_open or search_input.has_focus:
                return False
        if action == "finish_pane_switch":
            return self._pane_switch_pending
        if action in {
            "scroll_cursor_top",
            "scroll_cursor_bottom",
            "toggle_metadata_folds",
            "open_metadata_fold",
            "close_metadata_fold",
        }:
            return self._scroll_position_pending
        return super().check_action(action, parameters)

    def action_request_quit(self) -> None:
        self._request_quit()

    def action_request_quit_force(self) -> None:
        self._request_quit()

    def action_save_changes(self) -> None:
        """Persist dirty source and annotation buffers without quitting."""

        dirty_panes = self._dirty_panes()
        if not dirty_panes:
            self.notify("No changes to save", timeout=1.5)
            return
        if self._save_dirty_changes():
            self.notify(
                f"Saved {', '.join(dirty_panes)}",
                timeout=2,
            )

    def _request_quit(self) -> None:
        dirty_panes = self._dirty_panes()
        if not dirty_panes:
            self._complete_quit()
            return
        self.push_screen(
            QuitChangesModal(dirty_panes),
            callback=self._finish_quit_request,
        )

    def _finish_quit_request(self, choice: Optional[str]) -> None:
        if choice is None:
            self._refresh_status()
            return
        if choice == "discard":
            self._complete_quit()
            return
        if self._save_dirty_changes():
            self._complete_quit()

    def _complete_quit(self) -> None:
        """Capture resumable state only after the quit decision is complete."""

        viewer = self.query_one("#viewer", VimTextArea)
        self.exit_source_cursor = viewer.cursor_location
        self.quit_completed = True
        self.exit()

    def _save_dirty_changes(self) -> bool:
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)

        if viewer.text != self._saved_source.text:
            try:
                saved_source = self._source_writer(
                    self._saved_source, viewer.text
                )
            except TextFileError as error:
                self.notify(str(error), severity="error", timeout=5)
                return False
            self._saved_source = saved_source
            self.source = saved_source
            self._rebuild_source_line_starts()
            self._refresh_titlebar()

        if annotator.canonical_text != self._saved_annotation_text:
            try:
                saved_annotations = self.annotation_store.write(
                    annotator.canonical_text
                )
            except AnnotationFileError as error:
                self.notify(str(error), severity="error", timeout=5)
                self._refresh_status()
                return False
            self._saved_annotation_text = saved_annotations

        self._refresh_status()
        return True

    def _clear_pane_switch_prefix(self) -> None:
        self._pane_switch_pending = False
        self._pane_switch_timer = None
        self._refresh_status()

    def action_start_pane_switch(self) -> None:
        if self._scroll_position_pending:
            self._clear_scroll_position_prefix()
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

    def _clear_scroll_position_prefix(self) -> None:
        self._scroll_position_pending = False
        self._scroll_position_timer = None
        self._refresh_status()

    def action_scroll_position_prefix_or_middle(self) -> None:
        """Start a z command, or complete ``zz`` when already pending."""

        if self._scroll_position_pending:
            self._finish_scroll_position("middle")
            return
        if self._pane_switch_pending:
            self._clear_pane_switch_prefix()
        self._scroll_position_pending = True
        if self._scroll_position_timer is not None:
            self._scroll_position_timer.stop()
        self._scroll_position_timer = self.set_timer(
            1.5, self._clear_scroll_position_prefix
        )
        self._refresh_status()

    def _finish_scroll_position(self, placement: str) -> None:
        self._scroll_position_pending = False
        if self._scroll_position_timer is not None:
            self._scroll_position_timer.stop()
            self._scroll_position_timer = None
        target = self._focused_search_target()
        if target is not None:
            _position_cursor_line(target, placement)
        self._refresh_status()

    def action_scroll_cursor_top(self) -> None:
        self._finish_scroll_position("top")

    def action_scroll_cursor_bottom(self) -> None:
        self._finish_scroll_position("bottom")

    def _finish_metadata_fold_command(self, command: str) -> None:
        self._scroll_position_pending = False
        if self._scroll_position_timer is not None:
            self._scroll_position_timer.stop()
            self._scroll_position_timer = None

        annotator = self.query_one("#annotator", AnnotationTextArea)
        if not annotator.has_focus:
            self.notify(
                "Metadata folds are available in the annotation pane",
                severity="warning",
                timeout=2,
            )
            self._refresh_status()
            return

        if command == "toggle":
            message = annotator.toggle_metadata_folds()
        elif command == "open":
            message = annotator.open_metadata_fold()
        else:
            message = annotator.close_metadata_fold()
        self._set_search_match("annotator", None)
        self._refresh_status()
        self.notify(message, timeout=1.75)

    def action_toggle_metadata_folds(self) -> None:
        self._finish_metadata_fold_command("toggle")

    def action_open_metadata_fold(self) -> None:
        self._finish_metadata_fold_command("open")

    def action_close_metadata_fold(self) -> None:
        self._finish_metadata_fold_command("close")

    @on(VimTextArea.EditRequested)
    def on_edit_requested(self, event: VimTextArea.EditRequested) -> None:
        """Ask for an exact confirmation before enabling source edits."""

        event.stop()
        viewer = self.query_one("#viewer", VimTextArea)
        if not viewer.has_focus or viewer.edit_mode:
            return
        if not self.source.editable:
            self.notify(
                "PDF source text is an extracted read-only view; edit the "
                "annotation pane instead",
                severity="warning",
                timeout=4,
            )
            return
        if self._pane_switch_pending:
            self._clear_pane_switch_prefix()
        if self._scroll_position_pending:
            self._clear_scroll_position_prefix()
        self.push_screen(
            EditWarningModal(str(self.source.path)),
            callback=self._finish_edit_confirmation,
        )

    def _finish_edit_confirmation(self, confirmed: bool) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        viewer.focus()
        if confirmed:
            self.query_one(
                "#annotator", AnnotationTextArea
            ).leave_edit_mode()
            viewer.enter_edit_mode()

    @on(TextArea.Changed, "#viewer")
    def on_viewer_changed(self, event: TextArea.Changed) -> None:
        """Track an in-memory source edit and refresh source metadata."""

        viewer = event.text_area
        assert isinstance(viewer, VimTextArea)
        if not (viewer.edit_mode or viewer._normal_history_pending):
            return
        viewer._normal_history_pending = False
        text = viewer.text
        self.source = replace(
            self.source,
            text=text,
            size_bytes=len(text.encode("utf-8")),
            line_count=0 if not text else text.count("\n") + 1,
        )
        self._rebuild_source_line_starts()
        viewer.set_annotation_ranges(self.annotation_ranges)
        self._set_search_match("viewer", None)
        self._refresh_titlebar()
        self._refresh_status()

    @on(AnnotationTextArea.EditRequested)
    def on_annotation_edit_requested(
        self, event: AnnotationTextArea.EditRequested
    ) -> None:
        """Enter annotation Edit mode immediately, without a warning."""

        event.stop()
        annotator = self.query_one("#annotator", AnnotationTextArea)
        if not annotator.has_focus or annotator.edit_mode:
            return
        if self._pane_switch_pending:
            self._clear_pane_switch_prefix()
        if self._scroll_position_pending:
            self._clear_scroll_position_prefix()
        self.query_one("#viewer", VimTextArea).leave_edit_mode()
        annotator.enter_edit_mode()

    @on(TextArea.Changed, "#annotator")
    def on_annotator_changed(self, event: TextArea.Changed) -> None:
        """Track a sidecar edit and rebuild annotation relationships."""

        annotator = event.text_area
        assert isinstance(annotator, AnnotationTextArea)
        if not (annotator.edit_mode or annotator._normal_history_pending):
            return
        annotator._normal_history_pending = False
        annotator.sync_canonical_from_editor()
        self.annotation_text = annotator.canonical_text
        self.annotation_links = parse_annotation_links(self.annotation_text)
        self.annotation_ranges = tuple(
            link.source_range for link in self.annotation_links
        )
        annotator.set_annotation_links(self.annotation_links)
        self.query_one("#viewer", VimTextArea).set_annotation_ranges(
            self.annotation_ranges
        )
        self._set_search_match("annotator", None)
        self._refresh_status()

    @on(TextArea.SelectionChanged, "#viewer")
    def on_viewer_selection_changed(self, _event: TextArea.SelectionChanged) -> None:
        self._clear_stale_search_match("viewer")
        self._refresh_status()
        viewer = self.query_one("#viewer", VimTextArea)
        if (
            not self._suspend_alignment
            and viewer.has_focus
            and not viewer.edit_mode
            and self.sync_mode in {SyncMode.NORMAL, SyncMode.SYNC}
        ):
            self._align_annotation_to_source()

    @on(TextArea.SelectionChanged, "#annotator")
    def on_annotator_selection_changed(
        self, _event: TextArea.SelectionChanged
    ) -> None:
        self._clear_stale_search_match("annotator")
        self._refresh_status()
        annotator = self.query_one("#annotator", AnnotationTextArea)
        if (
            not self._suspend_alignment
            and annotator.has_focus
            and not annotator.edit_mode
        ):
            if self.sync_mode is SyncMode.SYNC:
                self._align_source_to_annotation()
            else:
                self._focus_annotation_at_cursor()

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
        viewer = self.query_one("#viewer", VimTextArea)
        if (
            viewer.has_focus
            and not viewer.edit_mode
            and self.sync_mode in {SyncMode.NORMAL, SyncMode.SYNC}
        ):
            self._align_annotation_to_source()

    @on(AnnotationTextArea.ModeChanged)
    def on_annotator_mode_changed(
        self, _event: AnnotationTextArea.ModeChanged
    ) -> None:
        self._refresh_status()
        annotator = self.query_one("#annotator", AnnotationTextArea)
        if annotator.has_focus and not annotator.edit_mode:
            if self.sync_mode is SyncMode.SYNC:
                self._align_source_to_annotation()
            else:
                self._focus_annotation_at_cursor()

    @on(VimTextArea.AnnotationRequested)
    def on_annotation_requested(self, event: VimTextArea.AnnotationRequested) -> None:
        if self._dirty_panes():
            self.notify(
                "Save or discard text edits before adding a note",
                severity="warning",
                timeout=3,
            )
            return
        try:
            draft = draft_from_selection(
                self.source.text,
                event.selection.start,
                event.selection.end,
            )
        except ValueError:
            self.notify(
                "Select source text before annotating",
                severity="warning",
                timeout=2,
            )
            return

        pdf_anchor: Optional[PdfAnchor] = None
        if self.source.pdf_document is not None:
            pdf_anchor = self.source.pdf_document.anchor_for_range(
                draft.start, draft.end
            )
            if pdf_anchor is None:
                self.notify(
                    "Select extracted PDF text, not only a page heading or spacing",
                    severity="warning",
                    timeout=3,
                )
                return

        self.push_screen(
            NoteModal(draft),
            callback=lambda note: self._finish_annotation(
                draft, note, pdf_anchor
            ),
        )

    def _finish_annotation(
        self,
        draft: AnnotationDraft,
        note: Optional[str],
        pdf_anchor: Optional[PdfAnchor] = None,
    ) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        if note is None:
            viewer.focus()
            return

        record = create_record(
            draft,
            note,
            source_text=self.source.text,
            pdf_anchor=pdf_anchor,
        )
        try:
            updated = self.annotation_store.append(record)
        except AnnotationFileError as error:
            self.notify(str(error), severity="error", timeout=4)
            viewer.focus()
            return

        self.annotation_text = updated
        self._saved_annotation_text = updated
        self.annotation_links = parse_annotation_links(updated)
        self.annotation_ranges = tuple(
            link.source_range for link in self.annotation_links
        )
        annotator = self.query_one("#annotator", AnnotationTextArea)
        annotator.load_annotation_text(updated)
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

