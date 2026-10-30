"""Textual application for Vim-style source viewing and sidecar notes."""

from __future__ import annotations

from bisect import bisect_right
from enum import Enum
from typing import Callable, Iterable, Optional

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Offset
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
from .files import LoadedTextFile, TextFileError, save_text_file


ClipboardWriter = Callable[[str], Optional[str]]
SourceWriter = Callable[[LoadedTextFile, str], LoadedTextFile]


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


class VimTextArea(TextArea):
    """A guarded editable TextArea with a small, predictable Vim key set."""

    COMPONENT_CLASSES = TextArea.COMPONENT_CLASSES | {
        "text-area--annotation",
        "text-area--active-annotation",
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
        self._search_spans: dict[int, tuple[tuple[int, int], ...]] = {}
        self._normal_undo_pending = False
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
        self.action_cursor_word_left(self.visual_mode)

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
        self._normal_undo_pending = True
        self.undo()
        if self.text == previous_text:
            self._normal_undo_pending = False

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


class AnnotationTextArea(TextArea):
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
        Binding("e", "vim_word_end", "End of word", show=False),
        Binding("i", "request_edit", "Edit annotations", show=False),
        Binding("u", "vim_undo", "Undo change", show=False),
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
        self._normal_undo_pending = False
        self.set_annotation_links(annotation_links)

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        normal_mode_actions = {
            "cursor_left",
            "cursor_right",
            "cursor_up",
            "cursor_down",
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

    def action_vim_word_end(self) -> None:
        self.move_cursor(_vim_word_end_location(self))

    def action_vim_word_right(self) -> None:
        self.move_cursor(_vim_word_start_location(self))

    def action_vim_scroll_down_line(self) -> None:
        _vim_scroll_line(self, 1)

    def action_vim_scroll_up_line(self) -> None:
        _vim_scroll_line(self, -1)

    def action_request_edit(self) -> None:
        self.post_message(self.EditRequested())

    def action_vim_undo(self) -> None:
        previous_text = self.text
        self._normal_undo_pending = True
        self.undo()
        if self.text == previous_text:
            self._normal_undo_pending = False

    def enter_edit_mode(self) -> None:
        self.history.checkpoint()
        self.selection = Selection.cursor(self.cursor_location)
        self.read_only = False
        self.edit_mode = True
        self.post_message(self.ModeChanged())

    def leave_edit_mode(self) -> None:
        if not self.edit_mode:
            return
        self.read_only = True
        self.edit_mode = False
        self.history.checkpoint()
        self.selection = Selection.cursor(self.cursor_location)
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
        align: center middle;
    }

    #edit-warning-dialog {
        width: 72%;
        max-width: 78;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $panel;
    }

    #edit-warning-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
        color: $warning;
    }

    #edit-warning-message,
    #edit-warning-path,
    #edit-warning-annotations {
        height: auto;
        margin-bottom: 1;
    }

    #edit-warning-path,
    #edit-warning-annotations {
        color: $text-muted;
    }

    #edit-confirmation {
        height: 3;
        margin-bottom: 1;
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
                "Edit mode writes every change directly to the original file.",
                markup=False,
                id="edit-warning-message",
            )
            yield Static(
                self.source_path,
                markup=False,
                id="edit-warning-path",
            )
            yield Static(
                "Existing annotation ranges are not rewritten automatically.",
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
        Binding(
            "ctrl+q",
            "force_quit",
            "Quit",
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
    ]

    def __init__(
        self,
        source: LoadedTextFile,
        *,
        soft_wrap: bool = True,
        clipboard_writer: ClipboardWriter = copy_to_system_clipboard,
        source_writer: SourceWriter = save_text_file,
        annotation_store: Optional[AnnotationStore] = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.soft_wrap = soft_wrap
        self._clipboard_writer = clipboard_writer
        self._source_writer = source_writer
        self.last_copy_backend: Optional[str] = None
        self._pane_switch_pending = False
        self._pane_switch_timer: Optional[Timer] = None
        self._scroll_position_pending = False
        self._scroll_position_timer: Optional[Timer] = None
        self._search_target_id: Optional[str] = None
        self._search_queries: dict[str, str] = {}
        self._search_matches: dict[str, AnnotationRange] = {}
        self.sync_mode = SyncMode.NORMAL
        self.annotation_store = annotation_store or AnnotationStore(source.path)
        try:
            self.annotation_text = self.annotation_store.read_text()
        except AnnotationFileError as error:
            self.annotation_text = f"# Annotation file error\n\n{error}\n"
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
            self._normal_help_text(),
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

    def _titlebar_text(self) -> str:
        size = self._format_size(self.source.size_bytes)
        return (
            f"{self.source.path}  |  {self.source.line_count} lines  |  {size}"
            f"  |  notes: {self.annotation_store.path.name}"
        )

    @staticmethod
    def _normal_help_text() -> str:
        return (
            "Tab/Ctrl-W W panes  i edit  S sync mode  . align  "
            "h/j/k/l move  Ctrl-E/Y scroll  zt/zz/zb place  "
            "u undo  / search  n/N repeat  v select  a annotate  q quit"
        )

    @staticmethod
    def _edit_help_text() -> str:
        return (
            "EDIT MODE  type to insert  Ctrl-W delete word  "
            "Ctrl-H delete char  Esc/Ctrl-[ normal  Ctrl-Q quit"
        )

    def _refresh_titlebar(self) -> None:
        self.query_one("#titlebar", Static).update(self._titlebar_text())

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

    def _focus_annotation_at_cursor(self) -> Optional[AnnotationLink]:
        """Update annotation-pane focus without moving the source cursor."""

        annotator = self.query_one("#annotator", AnnotationTextArea)
        link = self._link_for_sidecar_line(annotator.cursor_location[0])
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
                "Z  |  press t, z, or b to place the cursor line"
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
        wrap_info = "wrap" if target.soft_wrap else "no wrap"
        self.query_one("#status", Static).update(
            f"{mode}  |  ALIGN {self.sync_mode.value.upper()}  |  "
            f"Ln {row + 1}, Col {column + 1}  |  {wrap_info}{selection_info}"
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
        """Manually align the other pane from the focused pane."""

        target = self._focused_search_target()
        if isinstance(target, VimTextArea):
            self._align_annotation_to_source()
        elif isinstance(target, AnnotationTextArea):
            self._align_source_to_annotation()

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
            "quit",
            "start_pane_switch",
            "finish_pane_switch",
            "start_search",
            "search_next",
            "search_previous",
            "cycle_sync_mode",
            "align_panes",
            "scroll_position_prefix_or_middle",
            "scroll_cursor_top",
            "scroll_cursor_bottom",
        }:
            return False
        modal_open = isinstance(self.screen, ModalScreen)
        if action in {
            "start_search",
            "search_next",
            "search_previous",
        }:
            if modal_open or search_input.has_focus:
                return False
        if action in {
            "cycle_sync_mode",
            "align_panes",
            "scroll_position_prefix_or_middle",
            "scroll_cursor_top",
            "scroll_cursor_bottom",
        }:
            if modal_open or search_input.has_focus:
                return False
        if action in {"start_pane_switch", "finish_pane_switch"}:
            if modal_open or search_input.has_focus:
                return False
        if action == "finish_pane_switch":
            return self._pane_switch_pending
        if action in {"scroll_cursor_top", "scroll_cursor_bottom"}:
            return self._scroll_position_pending
        return super().check_action(action, parameters)

    def action_force_quit(self) -> None:
        self.exit()

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

    @on(VimTextArea.EditRequested)
    def on_edit_requested(self, event: VimTextArea.EditRequested) -> None:
        """Ask for an exact confirmation before enabling source edits."""

        event.stop()
        viewer = self.query_one("#viewer", VimTextArea)
        if not viewer.has_focus or viewer.edit_mode:
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
        """Persist each source edit atomically and refresh source metadata."""

        viewer = event.text_area
        assert isinstance(viewer, VimTextArea)
        if not (viewer.edit_mode or viewer._normal_undo_pending):
            return
        viewer._normal_undo_pending = False

        try:
            saved_source = self._source_writer(self.source, viewer.text)
        except TextFileError as error:
            cursor_row, cursor_column = viewer.cursor_location
            viewer.leave_edit_mode()
            viewer.load_text(self.source.text)
            lines = self.source.text.split("\n")
            cursor_row = min(cursor_row, len(lines) - 1)
            cursor_column = min(cursor_column, len(lines[cursor_row]))
            viewer.move_cursor((cursor_row, cursor_column))
            viewer.set_annotation_ranges(self.annotation_ranges)
            self.notify(
                f"{error}; the unsaved edit was reverted",
                severity="error",
                timeout=5,
            )
            return

        self.source = saved_source
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
        """Persist sidecar edits and rebuild annotation relationships."""

        annotator = event.text_area
        assert isinstance(annotator, AnnotationTextArea)
        if not (annotator.edit_mode or annotator._normal_undo_pending):
            return
        annotator._normal_undo_pending = False

        try:
            updated = self.annotation_store.write(annotator.text)
        except AnnotationFileError as error:
            cursor_row, cursor_column = annotator.cursor_location
            annotator.leave_edit_mode()
            annotator.load_text(self.annotation_text)
            lines = self.annotation_text.split("\n")
            cursor_row = min(cursor_row, len(lines) - 1)
            cursor_column = min(cursor_column, len(lines[cursor_row]))
            annotator.move_cursor((cursor_row, cursor_column))
            annotator.set_annotation_links(self.annotation_links)
            self.notify(
                f"{error}; the unsaved edit was reverted",
                severity="error",
                timeout=5,
            )
            return

        self.annotation_text = updated
        self.annotation_links = parse_annotation_links(updated)
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
            viewer.has_focus
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
        if annotator.has_focus and not annotator.edit_mode:
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

