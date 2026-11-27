"""Textual application for Vim-style source viewing and sidecar notes."""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, TypeVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.document._wrapped_document import WrappedDocument
from textual.geometry import Offset, Size
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import Button, Input, LoadingIndicator, Static, TextArea
from textual.widgets.text_area import Selection

from .annotations import (
    AnnotationDraft,
    AnnotationFileError,
    AnnotationLink,
    AnnotationRange,
    AnnotationStore,
    AnnotationType,
    PdfAnchor,
    SourceBinding,
    annotation_note_text,
    create_record,
    delete_annotation_block,
    draft_from_selection,
    inspect_source_binding,
    location_to_offset,
    parse_annotation_links,
    rewrite_source_binding,
    update_annotation_block,
)
from .clipboard import ClipboardCopyError, copy_to_system_clipboard
from .demo import DemoCase, DemoCaseError, load_demo_case, reset_demo_case
from .files import LoadedTextFile, TextFileError, load_source_file, save_text_file
from .file_safety import (
    BufferChangeState,
    ConcurrentFileChangeError,
    DiskFingerprint,
    DiskFingerprintError,
    classify_buffer_change,
    fingerprint_bytes,
    fingerprint_path,
    write_conflict_copy,
)
from .pdf_import import (
    PdfAnnotationConflict,
    PdfConflictDecision,
    PdfConflictKind,
    PdfConflictReport,
    PdfConflictResolution,
    PdfImportConflictError,
    PdfImportError,
    import_pdf_text_markups,
    resolve_pdf_annotation_conflicts,
)
from .rematch import (
    AnnotationDocumentValidationError,
    RematchCandidate,
    RematchChoice,
    RematchItem,
    RematchReport,
    RematchStatus,
    apply_rematches,
    confidence_summary,
    make_manual_candidate,
    parse_annotation_records,
    scan_rematches,
    text_sha256,
    validate_annotation_document,
    validate_applied_rematches,
)


ClipboardWriter = Callable[[str], Optional[str]]
SourceWriter = Callable[[LoadedTextFile, str], LoadedTextFile]
RematchPreviewCallback = Callable[
    [RematchItem, Optional[RematchCandidate]], None
]
PdfConflictPreviewCallback = Callable[[PdfAnnotationConflict], None]
ProcessingResult = TypeVar("ProcessingResult")


KEYBINDING_REFERENCE = """PURE TEXT ANNOTATOR KEYBINDINGS

Movement
  h/j/k/l or arrows       move cursor
  w / b / e               next word / previous word / word end
  0 / $                   line start / line end
  gg / G                  document start / document end
  [count]gg               jump to the numbered line
  Ctrl-F / Ctrl-B         page down / page up
  Ctrl-E / Ctrl-Y         scroll down / up one line
  zt / zz / zb            place cursor at top / middle / bottom
  Ctrl-O / Ctrl-I (Tab)   older / newer pane-local jump

Annotations
  Ctrl-N / Ctrl-P         next / previous annotation
  v                       visual selection in source
  a                       add note to selected source text
  m                       add types / filter / annotation tools
  R                       review PDF conflicts / stale rematches
  .                       align and center both panes manually
  S                       cycle Normal / Sync / Off alignment
  zi / zo / zc            toggle / open / close metadata folds

Search, editing, and files
  /                       search focused pane
  n / N                   next / previous search result
  i                       enter Edit mode
  Esc or Ctrl-[           leave Edit mode or clear selection
  u / Ctrl-R              undo / redo
  Ctrl-W / Ctrl-H         delete previous word / character in Edit mode
  Ctrl-S                  save changes
  m, then L               reload source and annotations from disk
  m, then P               rebind a moved sidecar to this source
  y or Ctrl-C / Cmd-C     copy selection
  Ctrl-W w                switch panes
  q or Ctrl-Q             quit
"""


class SyncMode(str, Enum):
    """Automatic cross-pane alignment policy."""

    NORMAL = "Normal"
    SYNC = "Sync"
    OFF = "Off"


@dataclass(frozen=True)
class TrackedFileState:
    """Current memory/disk relationship for one editor pane."""

    pane: str
    path: Path
    baseline: DiskFingerprint
    current: DiskFingerprint
    change_state: BufferChangeState

    @property
    def memory_changed(self) -> bool:
        return self.change_state in {
            BufferChangeState.MEMORY_ONLY,
            BufferChangeState.BOTH,
        }

    @property
    def disk_changed(self) -> bool:
        return self.change_state in {
            BufferChangeState.DISK_ONLY,
            BufferChangeState.BOTH,
        }


@dataclass(frozen=True)
class ReloadPayload:
    """Disk data read off the UI thread before a live-buffer replacement."""

    source: Optional[LoadedTextFile]
    source_fingerprint: Optional[DiskFingerprint]
    annotation_text: Optional[str]
    annotation_fingerprint: Optional[DiskFingerprint]


@dataclass(frozen=True)
class SourceRebindAssessment:
    """Evidence shown before a moved sidecar is bound to this source."""

    binding: SourceBinding
    confidence: int
    summary: str
    exact_ranges: int
    recoverable_quotes: int
    record_count: int


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


class _ConstantTimeHeightWrappedDocument(WrappedDocument):
    """Expose the already-materialized wrapped height in constant time."""

    @property
    def height(self) -> int:
        return len(self._offset_to_line_info)


class HalfPageBottomScrollTextArea(TextArea):
    """A text area with enough trailing space to center its final line."""

    class CommandStateChanged(Message):
        """Posted when a Vim count or ``g`` prefix changes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._jump_back_stack: list[tuple[int, int]] = []
        self._jump_forward_stack: list[tuple[int, int]] = []
        self._vim_count_digits = ""
        self._vim_g_pending = False

    def _set_document(self, text: str, language: Optional[str]) -> None:
        """Use Textual's wrapped layout with an O(1) height lookup."""

        super()._set_document(text, language)
        self.wrapped_document.__class__ = _ConstantTimeHeightWrappedDocument

    def render_line(self, y: int) -> Strip:
        """Render without rebuilding the whole document for every screen row.

        Textual 8.2.8 reads ``self.text`` while rendering each row and uses the
        complete selection as a soft-wrap cache key. Both choices make cursor
        movement and scrolling scale with the full document. The document is
        already split into lines, and only the logical lines touched by a
        selection, cursor, or bracket match need a distinct cache entry.
        """

        if self.document.line_count == 1 and self.document.end == (0, 0):
            return super().render_line(y)

        scroll_x, scroll_y = self.scroll_offset
        absolute_y = scroll_y + y
        wrapped_document = self.wrapped_document
        try:
            line_index = wrapped_document._offset_to_line_info[absolute_y][0]
        except IndexError:
            line_index = None

        selection = self.selection
        selection_start, selection_end = selection
        cursor_row, cursor_column = selection_end
        selection_key: object = None
        if selection_start == selection_end:
            if self._has_cursor and line_index == cursor_row:
                selection_key = ("cursor", cursor_column)
        else:
            selection_top, selection_bottom = sorted(selection)
            top_row, top_column = selection_top
            bottom_row, bottom_column = selection_bottom
            if (
                line_index is not None
                and top_row <= line_index <= bottom_row
            ):
                if top_row == bottom_row:
                    selection_key = (
                        "selection",
                        top_column,
                        bottom_column,
                    )
                elif line_index == top_row:
                    selection_key = ("selection-start", top_column)
                elif line_index == bottom_row:
                    selection_key = ("selection-end", bottom_column)
                else:
                    selection_key = ("selection-middle",)
                if self._has_cursor and line_index == cursor_row:
                    selection_key = (
                        selection_key,
                        "cursor",
                        cursor_column,
                    )

        _, cursor_y = self._cursor_offset
        matching_bracket = self._matching_bracket_location
        matching_bracket_key = (
            matching_bracket
            if matching_bracket is not None
            and line_index in {cursor_row, matching_bracket[0]}
            else None
        )
        cache_key = (
            self.size,
            scroll_x,
            absolute_y,
            selection_key,
            (
                selection_end
                if (
                    self._cursor_visible
                    and self.cursor_blink
                    and absolute_y == cursor_y
                )
                else None
            ),
            self.theme,
            matching_bracket_key,
            self.match_cursor_bracket,
            self.soft_wrap,
            self.show_line_numbers,
            self.read_only,
            self.show_cursor,
            self.suggestion if line_index == cursor_row else None,
        )
        if (cached_line := self._line_cache.get(cache_key)) is not None:
            return cached_line
        line = self._render_line(y)
        self._line_cache[cache_key] = line
        return line

    @property
    def vim_command_text(self) -> str:
        """Return the pending count and ``g`` prefix for status display."""

        return self._vim_count_digits + ("g" if self._vim_g_pending else "")

    @property
    def jump_back_stack(self) -> tuple[tuple[int, int], ...]:
        """Expose older jump positions for diagnostics and tests."""

        return tuple(self._jump_back_stack)

    @property
    def jump_forward_stack(self) -> tuple[tuple[int, int], ...]:
        """Expose newer jump positions for diagnostics and tests."""

        return tuple(self._jump_forward_stack)

    def _jump_history_location(self) -> tuple[int, int]:
        """Capture one pane-local position for the jumplist."""

        return self.cursor_location

    def _restore_jump_history_location(
        self, location: tuple[int, int]
    ) -> None:
        """Restore a pane-local jumplist position and center it."""

        self.move_cursor(location, center=True)

    def _append_jump_position(
        self,
        stack: list[tuple[int, int]],
        location: tuple[int, int],
    ) -> None:
        if not stack or stack[-1] != location:
            stack.append(location)
            del stack[:-100]

    def move_cursor_with_jump(
        self,
        location: tuple[int, int],
        *,
        center: bool = True,
        select: bool = False,
    ) -> bool:
        """Move to ``location`` while recording the position being left."""

        if location == self.cursor_location:
            return False
        self._append_jump_position(
            self._jump_back_stack,
            self._jump_history_location(),
        )
        self._jump_forward_stack.clear()
        self.move_cursor(location, center=center, select=select)
        return True

    def jump_back(self) -> bool:
        """Move to the previous position in this pane's independent jumplist."""

        current = self._jump_history_location()
        while self._jump_back_stack:
            destination = self._jump_back_stack.pop()
            if destination == current:
                continue
            self._append_jump_position(self._jump_forward_stack, current)
            self._restore_jump_history_location(destination)
            return True
        return False

    def jump_forward(self) -> bool:
        """Move to the next position in this pane's independent jumplist."""

        current = self._jump_history_location()
        while self._jump_forward_stack:
            destination = self._jump_forward_stack.pop()
            if destination == current:
                continue
            self._append_jump_position(self._jump_back_stack, current)
            self._restore_jump_history_location(destination)
            return True
        return False

    def clear_jump_history(self) -> None:
        """Discard both directions of this pane's jumplist."""

        self._jump_back_stack.clear()
        self._jump_forward_stack.clear()

    def cancel_vim_command(self) -> None:
        """Clear a pending count or incomplete ``g`` command."""

        if not self.vim_command_text:
            return
        self._vim_count_digits = ""
        self._vim_g_pending = False
        self.post_message(self.CommandStateChanged())

    def action_vim_count_digit(self, digit: int) -> None:
        """Accumulate a Normal-mode line count before ``gg``."""

        if self._vim_g_pending:
            self.cancel_vim_command()
        if len(self._vim_count_digits) < 9:
            self._vim_count_digits += str(digit)
            self.post_message(self.CommandStateChanged())

    def action_vim_zero_or_line_start(self) -> None:
        """Append zero to a count, or perform Vim's standalone ``0`` motion."""

        if self._vim_count_digits and not self._vim_g_pending:
            if len(self._vim_count_digits) < 9:
                self._vim_count_digits += "0"
                self.post_message(self.CommandStateChanged())
            return
        self.cancel_vim_command()
        self.action_cursor_line_start(
            bool(getattr(self, "visual_mode", False))
        )

    def action_vim_goto_prefix(self) -> None:
        """Wait for a second ``g``, then jump to line one or a counted line."""

        if not self._vim_g_pending:
            self._vim_g_pending = True
            self.post_message(self.CommandStateChanged())
            return
        line_number = int(self._vim_count_digits or "1")
        self._vim_count_digits = ""
        self._vim_g_pending = False
        self.post_message(self.CommandStateChanged())
        row = max(0, min(line_number - 1, self.document.line_count - 1))
        self.move_cursor_with_jump(
            (row, 0),
            select=bool(getattr(self, "visual_mode", False)),
        )

    def action_vim_document_end(self) -> None:
        """Jump to the document end, or to a counted line like Vim's ``G``."""

        count = int(self._vim_count_digits) if self._vim_count_digits else None
        self.cancel_vim_command()
        destination = self.document.end
        if count is not None:
            row = max(0, min(count - 1, self.document.line_count - 1))
            destination = (row, 0)
        self.move_cursor_with_jump(
            destination,
            select=bool(getattr(self, "visual_mode", False)),
        )

    def action_vim_line_start(self) -> None:
        self.cancel_vim_command()
        self.action_cursor_line_start(
            bool(getattr(self, "visual_mode", False))
        )

    def action_vim_line_end(self) -> None:
        self.cancel_vim_command()
        self.action_cursor_line_end(
            bool(getattr(self, "visual_mode", False))
        )

    def _refresh_size(self) -> None:
        if self.soft_wrap:
            base_size = Size(
                0,
                len(self.wrapped_document._offset_to_line_info),
            )
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
        *[
            Binding(
                str(digit),
                f"vim_count_digit({digit})",
                "Line count",
                show=False,
            )
            for digit in range(1, 10)
        ],
        Binding("0", "vim_zero_or_line_start", "Count or line start", show=False),
        Binding("home", "vim_line_start", "Line start", show=False),
        Binding("$,end", "vim_line_end", "Line end", show=False),
        Binding("g", "vim_goto_prefix", "Go-to prefix", show=False),
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
        Binding("a", "annotate", "Add note", show=False),
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
        self._span_layout_text = ""
        self._span_layout_lines: list[str] = []
        self._span_layout_starts: list[int] = [0]
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
        self._annotation_spans = self._build_cached_annotation_spans(
            inactive_ranges
        )
        self._active_annotation_spans = self._build_cached_annotation_spans(
            () if active_range is None else (active_range,)
        )

    def _ensure_span_layout(self) -> None:
        """Cache line boundaries shared by every source highlight layer."""

        text = self.text
        if text == self._span_layout_text and self._span_layout_lines:
            return
        self._span_layout_text = text
        self._span_layout_lines = text.split("\n")
        self._span_layout_starts = [0]
        self._span_layout_starts.extend(
            index + 1
            for index, character in enumerate(text)
            if character == "\n"
        )

    def _build_cached_annotation_spans(
        self, annotation_ranges: Iterable[AnnotationRange]
    ) -> dict[int, tuple[tuple[int, int], ...]]:
        self._ensure_span_layout()
        return self._build_spans_for_layout(
            self._span_layout_lines,
            self._span_layout_starts,
            annotation_ranges,
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
        self._search_spans = self._build_cached_annotation_spans(
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
        self._rematch_candidate_spans = self._build_cached_annotation_spans(
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
        return VimTextArea._build_spans_for_layout(
            lines,
            line_starts,
            annotation_ranges,
        )

    @staticmethod
    def _build_spans_for_layout(
        lines: Sequence[str],
        line_starts: Sequence[int],
        annotation_ranges: Iterable[AnnotationRange],
    ) -> dict[int, tuple[tuple[int, int], ...]]:
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
            "vim_count_digit",
            "vim_zero_or_line_start",
            "vim_goto_prefix",
            "vim_line_start",
            "vim_line_end",
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
        self.cancel_vim_command()
        self.action_cursor_left(self._should_select(force_select))

    def action_vim_right(self, force_select: bool = False) -> None:
        self.cancel_vim_command()
        self.action_cursor_right(self._should_select(force_select))

    def action_vim_up(self, force_select: bool = False) -> None:
        self.cancel_vim_command()
        self.action_cursor_up(self._should_select(force_select))

    def action_vim_down(self, force_select: bool = False) -> None:
        self.cancel_vim_command()
        self.action_cursor_down(self._should_select(force_select))

    def action_vim_word_left(self) -> None:
        self.cancel_vim_command()
        self.move_cursor(
            _vim_word_previous_location(self), select=self.visual_mode
        )

    def action_vim_word_right(self) -> None:
        self.cancel_vim_command()
        self.move_cursor(
            _vim_word_start_location(self), select=self.visual_mode
        )

    def action_vim_word_end(self) -> None:
        self.cancel_vim_command()
        self.move_cursor(
            _vim_word_end_location(self), select=self.visual_mode
        )

    def action_vim_line_start(self) -> None:
        super().action_vim_line_start()

    def action_vim_line_end(self) -> None:
        super().action_vim_line_end()

    def _move_page(self, *, down: bool, force_select: bool = False) -> None:
        self.cancel_vim_command()
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
        self.cancel_vim_command()
        _vim_scroll_line(self, 1, select=self.visual_mode)

    def action_vim_scroll_up_line(self) -> None:
        self.cancel_vim_command()
        _vim_scroll_line(self, -1, select=self.visual_mode)

    def action_toggle_visual(self) -> None:
        self.cancel_vim_command()
        if self.visual_mode:
            self.action_cancel_visual()
            return
        self.selection = Selection.cursor(self.cursor_location)
        self.visual_mode = True
        self.post_message(self.ModeChanged())

    def action_annotate(self) -> None:
        self.cancel_vim_command()
        self.post_message(self.AnnotationRequested(self.selection))

    def action_request_edit(self) -> None:
        self.cancel_vim_command()
        self.post_message(self.EditRequested())

    def action_vim_undo(self) -> None:
        self.cancel_vim_command()
        previous_text = self.text
        self._normal_history_pending = True
        self.undo()
        if self.text == previous_text:
            self._normal_history_pending = False

    def action_vim_redo(self) -> None:
        self.cancel_vim_command()
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
        self.cancel_vim_command()
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
        self.cancel_vim_command()
        self._request_copy(clear_after=True)

    def action_copy_selection(self) -> None:
        self.cancel_vim_command()
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
        "text-area--annotation-subtitle",
        "text-area--inactive-annotation",
        "text-area--search-match",
    }

    edit_mode = reactive(False)

    BINDINGS = [
        Binding("h,left", "vim_left", "Left", show=False),
        Binding("j,down", "vim_down", "Down", show=False),
        Binding("k,up", "vim_up", "Up", show=False),
        Binding("l,right", "vim_right", "Right", show=False),
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
        *[
            Binding(
                str(digit),
                f"vim_count_digit({digit})",
                "Line count",
                show=False,
            )
            for digit in range(1, 10)
        ],
        Binding("0", "vim_zero_or_line_start", "Count or line start", show=False),
        Binding("home", "vim_line_start", "Line start", show=False),
        Binding("$,end", "vim_line_end", "Line end", show=False),
        Binding("g", "vim_goto_prefix", "Go-to prefix", show=False),
        Binding("G,shift+g", "vim_document_end", "Document end", show=False),
        Binding(
            "pagedown,ctrl+f,ctrl+d", "vim_page_down", "Page down", show=False
        ),
        Binding(
            "pageup,ctrl+b,ctrl+u", "vim_page_up", "Page up", show=False
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
        self._annotation_type_filter: Optional[AnnotationType] = None
        self._annotation_links_by_line: dict[int, AnnotationLink] = {}
        self._metadata_lines: frozenset[int] = frozenset()
        self._quote_lines: frozenset[int] = frozenset()
        self._subtitle_lines: frozenset[int] = frozenset()
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
            "vim_left",
            "vim_right",
            "vim_up",
            "vim_down",
            "vim_word_left",
            "vim_word_right",
            "vim_word_end",
            "vim_count_digit",
            "vim_zero_or_line_start",
            "vim_goto_prefix",
            "vim_line_start",
            "vim_line_end",
            "vim_document_end",
            "vim_page_down",
            "vim_page_up",
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

    @property
    def annotation_type_filter(self) -> Optional[AnnotationType]:
        """The annotation kind currently visible, or ``None`` for all."""

        return self._annotation_type_filter

    @property
    def visible_annotation_links(self) -> tuple[AnnotationLink, ...]:
        """Return links admitted by the current type filter."""

        selected = self._annotation_type_filter
        return tuple(
            link
            for link in self._annotation_links
            if selected is None or link.annotation_type is selected
        )

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

    def _jump_history_location(self) -> tuple[int, int]:
        """Store canonical sidecar rows so folds do not corrupt history."""

        return self._full_cursor_location()

    def _restore_jump_history_location(
        self, location: tuple[int, int]
    ) -> None:
        full_row, column = location
        display_row = self._display_line_for_full(full_row)
        line = self.text.split("\n")[display_row] if self.text else ""
        self.move_cursor(
            (display_row, min(column, len(line))),
            center=True,
        )

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
                        f"Annotation {ordinal} {link.annotation_type.label} metadata",
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
        subtitle_lines: set[int] = set()
        canonical_lines = self._canonical_text.split("\n")

        full_links_by_line: dict[int, AnnotationLink] = {}
        full_metadata_lines: set[int] = set()
        full_quote_lines: set[int] = set()
        full_subtitle_lines: set[int] = set()

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
            full_subtitle_lines.update(
                line_number
                for line_number in range(
                    link.sidecar_start_line,
                    link.sidecar_end_line + 1,
                )
                if 0 <= line_number < len(canonical_lines)
                and canonical_lines[line_number].strip()
                in {"### Original text", "### Note"}
            )

        for display_line, full_line in enumerate(self._display_to_full_lines):
            link = full_links_by_line.get(full_line)
            if link is not None:
                links_by_line[display_line] = link
            if full_line in full_metadata_lines:
                metadata_lines.add(display_line)
            if full_line in full_quote_lines:
                quote_lines.add(display_line)
            if full_line in full_subtitle_lines:
                subtitle_lines.add(display_line)

        self._annotation_links_by_line = links_by_line
        self._metadata_lines = frozenset(metadata_lines)
        self._quote_lines = frozenset(quote_lines)
        self._subtitle_lines = frozenset(subtitle_lines)
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
        hidden_annotations = {
            link.sidecar_start_line: link
            for link in self._annotation_links
            if not self.edit_mode
            and self._annotation_type_filter is not None
            and link.annotation_type is not self._annotation_type_filter
        }
        display_lines: list[str] = []
        display_to_full: list[int] = []
        full_to_display = [0] * len(lines)

        full_line = 0
        while full_line < len(lines):
            hidden_link = hidden_annotations.get(full_line)
            if hidden_link is not None:
                display_row = max(0, len(display_lines) - 1)
                for hidden_line in range(
                    hidden_link.sidecar_start_line,
                    min(hidden_link.sidecar_end_line + 1, len(lines)),
                ):
                    full_to_display[hidden_line] = display_row
                full_line = hidden_link.sidecar_end_line + 1
                continue
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

        self.clear_jump_history()
        self.cancel_vim_command()
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

    def set_annotation_type_filter(
        self, annotation_type: Optional[AnnotationType]
    ) -> None:
        """Project only one annotation kind while retaining canonical text."""

        selected = (
            None if annotation_type is None else AnnotationType(annotation_type)
        )
        if selected is self._annotation_type_filter:
            return
        full_cursor = self._full_cursor_location()
        viewport_position = self._cursor_viewport_position()
        self._annotation_type_filter = selected
        if (
            self._active_link is not None
            and selected is not None
            and self._active_link.annotation_type is not selected
        ):
            self._active_link = None
        self._apply_fold_projection(full_cursor)
        self._restore_cursor_viewport_position(viewport_position)

    def show_link(
        self,
        link: AnnotationLink,
        *,
        move_cursor: bool,
        record_jump: bool = False,
    ) -> None:
        """Mark an annotation block and optionally center its first line."""

        changed = link != self._active_link
        self._active_link = link
        if changed:
            self._line_cache.clear()
            self.refresh()
        target_line = self._display_line_for_full(link.sidecar_note_line)
        if move_cursor and self.cursor_location[0] != target_line:
            if record_jump:
                self.move_cursor_with_jump((target_line, 0), center=True)
            else:
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
            if line_index in self._subtitle_lines:
                line.stylize(
                    self.get_component_rich_style(
                        "text-area--annotation-subtitle"
                    )
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

    def action_vim_left(self) -> None:
        self.cancel_vim_command()
        self.action_cursor_left()

    def action_vim_right(self) -> None:
        self.cancel_vim_command()
        self.action_cursor_right()

    def action_vim_up(self) -> None:
        self.cancel_vim_command()
        self.action_cursor_up()

    def action_vim_down(self) -> None:
        self.cancel_vim_command()
        self.action_cursor_down()

    def action_vim_word_end(self) -> None:
        self.cancel_vim_command()
        self.move_cursor(_vim_word_end_location(self))

    def action_vim_word_right(self) -> None:
        self.cancel_vim_command()
        self.move_cursor(_vim_word_start_location(self))

    def action_vim_word_left(self) -> None:
        self.cancel_vim_command()
        self.move_cursor(_vim_word_previous_location(self))

    def action_vim_page_down(self) -> None:
        self.cancel_vim_command()
        self.action_cursor_page_down()

    def action_vim_page_up(self) -> None:
        self.cancel_vim_command()
        self.action_cursor_page_up()

    def action_vim_scroll_down_line(self) -> None:
        self.cancel_vim_command()
        _vim_scroll_line(self, 1)

    def action_vim_scroll_up_line(self) -> None:
        self.cancel_vim_command()
        _vim_scroll_line(self, -1)

    def action_request_edit(self) -> None:
        self.cancel_vim_command()
        self.post_message(self.EditRequested())

    def action_vim_undo(self) -> None:
        self.cancel_vim_command()
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
        self.cancel_vim_command()
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
        self.cancel_vim_command()
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
            yield Static("Add note", markup=False, id="note-title")
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


class ExternalFileChangeModal(ModalScreen[Optional[str]]):
    """Resolve external edits without ever replacing them silently."""

    CSS = """
    ExternalFileChangeModal {
        align: center middle;
        background: $background 10%;
    }

    #external-change-dialog {
        width: 84%;
        max-width: 92;
        height: auto;
        max-height: 18;
        padding: 1 2;
        border: round $warning;
        background: $panel;
    }

    #external-change-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
        color: $warning;
    }

    #external-change-message,
    #external-change-files,
    #external-change-hint {
        height: auto;
        margin-bottom: 1;
    }

    #external-change-files,
    #external-change-hint {
        color: $text-muted;
    }

    #external-change-buttons {
        height: 3;
        align-horizontal: right;
    }

    #external-change-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("r", "reload", "Reload disk", priority=True),
        Binding("c", "copy", "Preserve conflict copy", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(
        self,
        states: tuple[TrackedFileState, ...],
        *,
        manual_reload: bool = False,
    ) -> None:
        super().__init__()
        self.states = states
        self.manual_reload = manual_reload
        self.can_copy = any(
            state.memory_changed
            and (manual_reload or state.disk_changed)
            for state in states
        )

    def compose(self) -> ComposeResult:
        title = (
            "Reload files from disk"
            if self.manual_reload
            else "Files changed outside Pure Text Annotator"
        )
        rows = "\n".join(
            f"{state.pane}: {state.change_state.label} · {state.path.name}"
            for state in self.states
        )
        if any(
            state.change_state is BufferChangeState.BOTH
            for state in self.states
        ):
            message = (
                "Saving now would overwrite external edits. Reload discards "
                "the conflicting PTA buffer; Conflict copy preserves it first."
            )
        elif self.manual_reload and self.can_copy:
            message = (
                "Reload discards unsaved PTA buffers. Conflict copy preserves "
                "them beside the originals before reloading."
            )
        else:
            message = "Reload the changed disk content before continuing."
        with Vertical(id="external-change-dialog"):
            yield Static(title, markup=False, id="external-change-title")
            yield Static(
                message,
                markup=False,
                id="external-change-message",
            )
            yield Static(rows, markup=False, id="external-change-files")
            yield Static(
                "R reloads · C saves conflict copy then reloads · Escape cancels",
                markup=False,
                id="external-change-hint",
            )
            with Horizontal(id="external-change-buttons"):
                yield Button("Cancel", id="cancel-external-change")
                yield Button(
                    "Conflict copy + reload",
                    id="copy-external-change",
                    variant="warning",
                    disabled=not self.can_copy,
                )
                yield Button(
                    "Reload disk",
                    id="reload-external-change",
                    variant="primary",
                )

    def action_reload(self) -> None:
        self.dismiss("reload")

    def action_copy(self) -> None:
        if self.can_copy:
            self.dismiss("copy")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#reload-external-change")
    def on_reload_pressed(self, _event: Button.Pressed) -> None:
        self.action_reload()

    @on(Button.Pressed, "#copy-external-change")
    def on_copy_pressed(self, _event: Button.Pressed) -> None:
        self.action_copy()

    @on(Button.Pressed, "#cancel-external-change")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.action_cancel()


class SourceRebindModal(ModalScreen[bool]):
    """Confirm a moved or renamed source without obscuring both panes."""

    CSS = """
    SourceRebindModal {
        align: center middle;
        background: $background 10%;
    }

    #source-rebind-dialog {
        width: 88%;
        max-width: 100;
        height: auto;
        max-height: 17;
        padding: 1 2;
        border: round $warning;
        background: $panel;
    }

    #source-rebind-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
        color: $warning;
    }

    #source-rebind-message,
    #source-rebind-paths,
    #source-rebind-evidence,
    #source-rebind-hint {
        height: auto;
        margin-bottom: 1;
    }

    #source-rebind-paths,
    #source-rebind-evidence,
    #source-rebind-hint {
        color: $text-muted;
    }

    #source-rebind-buttons {
        height: 3;
        align-horizontal: right;
    }

    #source-rebind-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("r", "rebind", "Rebind source", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, assessment: SourceRebindAssessment) -> None:
        super().__init__()
        self.assessment = assessment

    def compose(self) -> ComposeResult:
        binding = self.assessment.binding
        recorded = (
            str(binding.recorded_path)
            if binding.recorded_path is not None
            else binding.recorded_reference or "(unknown)"
        )
        with Vertical(id="source-rebind-dialog"):
            yield Static(
                "Source moved or renamed",
                markup=False,
                id="source-rebind-title",
            )
            yield Static(
                "This sidecar names a different source. Rebinding changes only "
                "the source path in its header; annotation ranges are untouched.",
                markup=False,
                id="source-rebind-message",
            )
            yield Static(
                f"Recorded: {recorded}\nCurrent:  {binding.current_path}",
                markup=False,
                id="source-rebind-paths",
            )
            yield Static(
                f"Compatibility estimate: {self.assessment.confidence}% · "
                f"{self.assessment.summary}\n"
                f"Exact ranges {self.assessment.exact_ranges}/"
                f"{self.assessment.record_count} · quotations found "
                f"{self.assessment.recoverable_quotes}/"
                f"{self.assessment.record_count}",
                markup=False,
                id="source-rebind-evidence",
            )
            yield Static(
                "R confirms · Escape keeps the sidecar read-only · use R rematch "
                "after binding if the source content changed",
                markup=False,
                id="source-rebind-hint",
            )
            with Horizontal(id="source-rebind-buttons"):
                yield Button("Cancel", id="cancel-source-rebind")
                yield Button(
                    "Rebind source",
                    id="confirm-source-rebind",
                    variant="warning",
                )

    def action_rebind(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-source-rebind")
    def on_rebind_pressed(self, _event: Button.Pressed) -> None:
        self.action_rebind()

    @on(Button.Pressed, "#cancel-source-rebind")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.action_cancel()


class AnnotationMenuModal(ModalScreen[Optional[str]]):
    """Offer annotation operations without hiding the two text panes."""

    CSS = """
    AnnotationMenuModal {
        align: center bottom;
        background: $background 10%;
    }

    #annotation-menu-dialog {
        width: 96%;
        max-width: 120;
        height: 17;
        margin-bottom: 1;
        padding: 0 1;
        border: round $accent;
        background: $panel;
    }

    #annotation-menu-title {
        height: 1;
        text-style: bold;
        color: $accent;
    }

    #annotation-menu-current,
    #annotation-menu-hint {
        height: 1;
        color: $text-muted;
        text-overflow: ellipsis;
    }

    #annotation-menu-buttons {
        height: 11;
        grid-size: 5 3;
        grid-columns: 1fr 1fr 1fr 1fr 1fr;
        grid-rows: 3 3 3;
        grid-gutter: 1 1;
        margin-top: 1;
    }

    #annotation-menu-buttons Button {
        width: 1fr;
        min-width: 0;
    }
    """

    BINDINGS = [
        Binding("a", "add", "Add note", priority=True, show=False),
        Binding("h", "highlight", "Add highlight", priority=True, show=False),
        Binding("b", "bookmark", "Add bookmark", priority=True, show=False),
        Binding("f", "filter", "Filter annotations", priority=True, show=False),
        Binding("x", "edit_current", "Edit current", priority=True, show=False),
        Binding("i", "edit", "Enter Edit mode", priority=True, show=False),
        Binding("s", "sync", "Switch Sync mode", priority=True, show=False),
        Binding("d", "delete", "Delete current", priority=True, show=False),
        Binding("r", "rematch", "Rematch", priority=True, show=False),
        Binding("k", "keybindings", "Keybindings", priority=True, show=False),
        Binding("e", "embed", "Embed PDF", priority=True, show=False),
        Binding("l", "reload", "Reload files", priority=True, show=False),
        Binding("p", "rebind", "Rebind source", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(
        self,
        current_label: Optional[str],
        *,
        is_pdf: bool,
        sync_mode: SyncMode,
        annotation_filter: Optional[AnnotationType],
        needs_rebind: bool,
    ) -> None:
        super().__init__()
        self.current_label = current_label
        self.is_pdf = is_pdf
        self.sync_mode = sync_mode
        self.annotation_filter = annotation_filter
        self.needs_rebind = needs_rebind

    def compose(self) -> ComposeResult:
        current = self.current_label or "No valid current annotation"
        with Vertical(id="annotation-menu-dialog"):
            yield Static(
                "Annotation actions",
                markup=False,
                id="annotation-menu-title",
            )
            yield Static(
                current,
                markup=False,
                id="annotation-menu-current",
            )
            with Grid(id="annotation-menu-buttons"):
                yield Button(
                    "A  Add note",
                    id="menu-add-annotation",
                )
                yield Button(
                    "H  Add highlight",
                    id="menu-add-highlight",
                )
                yield Button(
                    "B  Add bookmark",
                    id="menu-add-bookmark",
                )
                filter_label = (
                    self.annotation_filter.label
                    if self.annotation_filter is not None
                    else "All"
                )
                yield Button(
                    f"F  Filter · {filter_label}",
                    id="menu-filter-annotations",
                )
                yield Button(
                    "X  Edit current annotation",
                    id="menu-edit-current-annotation",
                    disabled=self.current_label is None,
                )
                yield Button(
                    "I  Enter Edit mode",
                    id="menu-enter-edit-mode",
                )
                yield Button(
                    f"S  Switch Sync mode · {self.sync_mode.value}",
                    id="menu-switch-sync-mode",
                )
                yield Button(
                    "D  Delete current annotation",
                    id="menu-delete-annotation",
                    disabled=self.current_label is None,
                )
                yield Button(
                    "R  Rematch annotations",
                    id="menu-rematch-annotations",
                )
                yield Button(
                    "K  Show keybindings",
                    id="menu-show-keybindings",
                )
                yield Button(
                    "E  Synchronize annotations into source PDF",
                    id="menu-embed-pdf",
                    disabled=not self.is_pdf,
                )
                yield Button(
                    "L  Reload files from disk",
                    id="menu-reload-files",
                )
                yield Button(
                    "P  Rebind source path",
                    id="menu-rebind-source",
                    disabled=not self.needs_rebind,
                )
            yield Static(
                "A/H/B/F/X/I/S/D/R/K/E/L/P chooses · Escape cancels · writes are "
                "verified and atomic",
                markup=False,
                id="annotation-menu-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#menu-add-annotation", Button).focus()

    def action_delete(self) -> None:
        if self.current_label is not None:
            self.dismiss("delete")

    def action_add(self) -> None:
        self.dismiss("add")

    def action_edit(self) -> None:
        self.dismiss("edit")

    def action_highlight(self) -> None:
        self.dismiss("highlight")

    def action_bookmark(self) -> None:
        self.dismiss("bookmark")

    def action_filter(self) -> None:
        self.dismiss("filter")

    def action_edit_current(self) -> None:
        if self.current_label is not None:
            self.dismiss("edit-current")

    def action_sync(self) -> None:
        self.dismiss("sync")

    def action_rematch(self) -> None:
        self.dismiss("rematch")

    def action_keybindings(self) -> None:
        self.dismiss("keybindings")

    def action_embed(self) -> None:
        if self.is_pdf:
            self.dismiss("embed")

    def action_reload(self) -> None:
        self.dismiss("reload")

    def action_rebind(self) -> None:
        if self.needs_rebind:
            self.dismiss("rebind")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#menu-delete-annotation")
    def on_delete_pressed(self, _event: Button.Pressed) -> None:
        self.action_delete()

    @on(Button.Pressed, "#menu-add-annotation")
    def on_add_pressed(self, _event: Button.Pressed) -> None:
        self.action_add()

    @on(Button.Pressed, "#menu-add-highlight")
    def on_highlight_pressed(self, _event: Button.Pressed) -> None:
        self.action_highlight()

    @on(Button.Pressed, "#menu-add-bookmark")
    def on_bookmark_pressed(self, _event: Button.Pressed) -> None:
        self.action_bookmark()

    @on(Button.Pressed, "#menu-filter-annotations")
    def on_filter_pressed(self, _event: Button.Pressed) -> None:
        self.action_filter()

    @on(Button.Pressed, "#menu-edit-current-annotation")
    def on_edit_current_pressed(self, _event: Button.Pressed) -> None:
        self.action_edit_current()

    @on(Button.Pressed, "#menu-enter-edit-mode")
    def on_edit_pressed(self, _event: Button.Pressed) -> None:
        self.action_edit()

    @on(Button.Pressed, "#menu-switch-sync-mode")
    def on_sync_pressed(self, _event: Button.Pressed) -> None:
        self.action_sync()

    @on(Button.Pressed, "#menu-rematch-annotations")
    def on_rematch_pressed(self, _event: Button.Pressed) -> None:
        self.action_rematch()

    @on(Button.Pressed, "#menu-show-keybindings")
    def on_keybindings_pressed(self, _event: Button.Pressed) -> None:
        self.action_keybindings()

    @on(Button.Pressed, "#menu-embed-pdf")
    def on_embed_pressed(self, _event: Button.Pressed) -> None:
        self.action_embed()

    @on(Button.Pressed, "#menu-reload-files")
    def on_reload_pressed(self, _event: Button.Pressed) -> None:
        self.action_reload()

    @on(Button.Pressed, "#menu-rebind-source")
    def on_rebind_pressed(self, _event: Button.Pressed) -> None:
        self.action_rebind()


class AnnotationFilterModal(ModalScreen[Optional[str]]):
    """Choose the annotation kind projected in the sidecar pane."""

    CSS = """
    AnnotationFilterModal {
        align: center bottom;
        background: $background 10%;
    }

    #annotation-filter-dialog {
        width: 96%;
        max-width: 100;
        height: 9;
        margin-bottom: 1;
        padding: 0 1;
        border: round $accent;
        background: $panel;
    }

    #annotation-filter-title,
    #annotation-filter-hint {
        height: 1;
    }

    #annotation-filter-title {
        text-style: bold;
        color: $accent;
    }

    #annotation-filter-hint {
        color: $text-muted;
    }

    #annotation-filter-buttons {
        height: 3;
        margin-top: 1;
        align-horizontal: center;
    }

    #annotation-filter-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("a", "choose('all')", "All", priority=True, show=False),
        Binding("n", "choose('note')", "Notes", priority=True, show=False),
        Binding("h", "choose('highlight')", "Highlights", priority=True, show=False),
        Binding("b", "choose('bookmark')", "Bookmarks", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(self, current: Optional[AnnotationType]) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        label = self.current.label if self.current is not None else "All"
        with Vertical(id="annotation-filter-dialog"):
            yield Static(
                f"Filter annotations by type · current: {label}",
                markup=False,
                id="annotation-filter-title",
            )
            with Horizontal(id="annotation-filter-buttons"):
                yield Button("A  All", id="filter-all", variant="primary")
                yield Button("N  Notes", id="filter-note")
                yield Button("H  Highlights", id="filter-highlight")
                yield Button("B  Bookmarks", id="filter-bookmark")
            yield Static(
                "The sidecar file is unchanged; search and Ctrl-N/P use visible entries",
                markup=False,
                id="annotation-filter-hint",
            )

    def on_mount(self) -> None:
        selected = "all" if self.current is None else self.current.value
        self.query_one(f"#filter-{selected}", Button).focus()

    def action_choose(self, value: str) -> None:
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        prefix = "filter-"
        if event.button.id and event.button.id.startswith(prefix):
            self.action_choose(event.button.id[len(prefix) :])


class EditAnnotationModal(
    ModalScreen[Optional[tuple[AnnotationType, str]]]
):
    """Edit one annotation's structured type and optional Markdown body."""

    CSS = """
    EditAnnotationModal {
        align: center bottom;
        background: $background 10%;
    }

    #edit-annotation-dialog {
        width: 96%;
        max-width: 110;
        height: 17;
        margin-bottom: 1;
        padding: 0 1;
        border: round $accent;
        background: $panel;
    }

    #edit-annotation-title {
        height: 1;
        text-style: bold;
        color: $accent;
    }

    #edit-annotation-label,
    #edit-annotation-type,
    #edit-annotation-hint {
        height: 1;
        color: $text-muted;
        text-overflow: ellipsis;
    }

    #edit-annotation-types,
    #edit-annotation-actions {
        height: 3;
        align-horizontal: center;
    }

    #edit-annotation-types Button,
    #edit-annotation-actions Button {
        margin: 0 1;
    }

    #edit-annotation-body {
        height: 6;
        border: solid $primary;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save annotation", priority=True),
        Binding("ctrl+enter", "save", "Save annotation", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(
        self,
        label: str,
        annotation_type: AnnotationType,
        note: str,
    ) -> None:
        super().__init__()
        self.label = label
        self.annotation_type = annotation_type
        self.note = note

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-annotation-dialog"):
            yield Static(
                "Edit current annotation",
                markup=False,
                id="edit-annotation-title",
            )
            yield Static(self.label, markup=False, id="edit-annotation-label")
            yield Static(
                f"Type: {self.annotation_type.label}",
                markup=False,
                id="edit-annotation-type",
            )
            with Horizontal(id="edit-annotation-types"):
                yield Button("Note", id="edit-type-note")
                yield Button("Highlight", id="edit-type-highlight")
                yield Button("Bookmark", id="edit-type-bookmark")
            yield TextArea(
                self.note,
                soft_wrap=True,
                show_line_numbers=False,
                placeholder="Markdown body (required only for Note)",
                id="edit-annotation-body",
            )
            yield Static(
                "Choose a type, edit the body, then Ctrl+S · Escape cancels",
                markup=False,
                id="edit-annotation-hint",
            )
            with Horizontal(id="edit-annotation-actions"):
                yield Button("Cancel", id="cancel-edit-annotation")
                yield Button(
                    "Save annotation",
                    variant="primary",
                    id="save-edit-annotation",
                )

    def on_mount(self) -> None:
        self.query_one("#edit-annotation-body", TextArea).focus()

    def _select_type(self, annotation_type: AnnotationType) -> None:
        self.annotation_type = annotation_type
        self.query_one("#edit-annotation-type", Static).update(
            f"Type: {annotation_type.label}"
        )

    def action_save(self) -> None:
        note = self.query_one("#edit-annotation-body", TextArea).text.rstrip()
        if self.annotation_type is AnnotationType.NOTE and not note.strip():
            self.notify(
                "A Note requires body text",
                severity="warning",
                timeout=1.5,
            )
            return
        self.dismiss((self.annotation_type, note))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "save-edit-annotation":
            self.action_save()
        elif button_id == "cancel-edit-annotation":
            self.action_cancel()
        elif button_id and button_id.startswith("edit-type-"):
            self._select_type(AnnotationType(button_id.removeprefix("edit-type-")))


class DeleteAnnotationModal(ModalScreen[bool]):
    """Confirm one structured annotation deletion."""

    CSS = """
    DeleteAnnotationModal {
        align: center bottom;
        background: $background 10%;
    }

    #delete-annotation-dialog {
        width: 96%;
        max-width: 120;
        height: 10;
        margin-bottom: 1;
        padding: 0 1;
        border: round $error;
        background: $panel;
    }

    #delete-annotation-title {
        height: 1;
        text-style: bold;
        color: $error;
    }

    #delete-annotation-label,
    #delete-annotation-preview,
    #delete-annotation-hint {
        height: 1;
        text-overflow: ellipsis;
    }

    #delete-annotation-preview,
    #delete-annotation-hint {
        color: $text-muted;
    }

    #delete-annotation-buttons {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    #delete-annotation-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("d", "confirm", "Delete", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(self, label: str, quote_preview: str) -> None:
        super().__init__()
        self.label = label
        self.quote_preview = quote_preview

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-annotation-dialog"):
            yield Static(
                "Delete current annotation?",
                markup=False,
                id="delete-annotation-title",
            )
            yield Static(
                self.label,
                markup=False,
                id="delete-annotation-label",
            )
            yield Static(
                self.quote_preview,
                markup=False,
                id="delete-annotation-preview",
            )
            yield Static(
                "The source text is unchanged · D confirms · Escape cancels",
                markup=False,
                id="delete-annotation-hint",
            )
            with Horizontal(id="delete-annotation-buttons"):
                yield Button("Cancel", id="cancel-delete-annotation")
                yield Button(
                    "Delete annotation",
                    variant="error",
                    id="confirm-delete-annotation",
                )

    def on_mount(self) -> None:
        self.query_one("#cancel-delete-annotation", Button).focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-delete-annotation")
    def on_confirm_pressed(self, _event: Button.Pressed) -> None:
        self.action_confirm()

    @on(Button.Pressed, "#cancel-delete-annotation")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.action_cancel()


class KeybindingsModal(ModalScreen[None]):
    """Display a scrollable reference for the application's keybindings."""

    CSS = """
    KeybindingsModal {
        align: center middle;
        background: $background 15%;
    }

    #keybindings-dialog {
        width: 88%;
        max-width: 92;
        height: 88%;
        max-height: 34;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }

    #keybindings-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
        color: $accent;
    }

    #keybindings-text {
        height: 1fr;
        border: solid $primary;
    }

    #keybindings-hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q,escape", "close", "Close", priority=True, show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="keybindings-dialog"):
            yield Static(
                "Keybinding reference",
                markup=False,
                id="keybindings-title",
            )
            yield TextArea(
                KEYBINDING_REFERENCE,
                read_only=True,
                show_cursor=False,
                show_line_numbers=False,
                soft_wrap=False,
                id="keybindings-text",
            )
            yield Static(
                "Arrow/Page keys scroll · Q or Escape closes",
                markup=False,
                id="keybindings-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#keybindings-text", TextArea).focus()

    def action_close(self) -> None:
        self.dismiss(None)


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


def _compact_conflict_text(value: Optional[str], limit: int = 72) -> str:
    """Keep conflict evidence readable inside a compact overlay."""

    if value is None:
        return "(not present)"
    compact = " ".join(value.split()) or "(empty)"
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


class ProcessingModal(ModalScreen[None]):
    """Small blocking overlay whose indicator keeps animating during work."""

    CSS = """
    ProcessingModal {
        align: center middle;
        background: $background 20%;
    }

    #processing-dialog {
        width: 52;
        height: 7;
        padding: 1 2;
        border: round $accent;
        background: $panel;
        content-align: center middle;
    }

    #processing-message {
        width: 1fr;
        height: 1;
        text-align: center;
        text-style: bold;
    }

    #processing-indicator {
        width: 1fr;
        height: 1;
        margin-top: 1;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="processing-dialog"):
            yield Static(
                self.message,
                markup=False,
                id="processing-message",
            )
            yield LoadingIndicator(id="processing-indicator")


class PdfConflictReviewModal(
    ModalScreen[Optional[tuple[PdfConflictResolution, ...]]]
):
    """Collect one explicit PDF-or-Markdown decision for every conflict."""

    CSS = """
    PdfConflictReviewModal {
        align: center bottom;
        background: $background 10%;
    }

    #pdf-conflict-review-dialog {
        width: 96%;
        max-width: 120;
        height: 13;
        margin-bottom: 1;
        padding: 0 1;
        border: round $warning;
        background: $panel;
    }

    #pdf-conflict-review-title {
        height: 1;
        text-style: bold;
        color: $warning;
    }

    #pdf-conflict-review-detail {
        height: 1fr;
        padding: 0 1;
        background: $surface;
    }

    #pdf-conflict-review-hint {
        height: 1;
        color: $text-muted;
        text-overflow: ellipsis;
    }

    #pdf-conflict-review-buttons {
        height: 3;
        grid-size: 6 1;
        grid-columns: 1fr 1fr 1fr 1fr 1fr 1fr;
        grid-rows: 3;
        grid-gutter: 0 1;
    }

    #pdf-conflict-review-buttons Button {
        width: 1fr;
        min-width: 0;
    }
    """

    BINDINGS = [
        Binding("j,down", "next_conflict", "Next", priority=True, show=False),
        Binding("k,up", "previous_conflict", "Previous", priority=True, show=False),
        Binding("m", "keep_markdown", "Keep Markdown", priority=True, show=False),
        Binding("p", "keep_pdf", "Keep PDF", priority=True, show=False),
        Binding("ctrl+s", "continue_to_confirmation", "Confirm", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(
        self,
        report: PdfConflictReport,
        initial_resolutions: Iterable[PdfConflictResolution] = (),
        preview_callback: Optional[PdfConflictPreviewCallback] = None,
    ) -> None:
        super().__init__()
        self.report = report
        self.items = report.conflicts
        self.current_index = 0
        self.preview_callback = preview_callback
        self.decisions = {
            resolution.annotation_id: resolution.decision
            for resolution in initial_resolutions
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="pdf-conflict-review-dialog"):
            yield Static(
                "Review PDF/Markdown annotation conflicts",
                markup=False,
                id="pdf-conflict-review-title",
            )
            yield Static("", markup=False, id="pdf-conflict-review-detail")
            yield Static(
                "J/K conflict - M keep Markdown - P keep PDF - "
                "all conflicts need a choice",
                markup=False,
                id="pdf-conflict-review-hint",
            )
            with Grid(id="pdf-conflict-review-buttons"):
                yield Button("Cancel", id="cancel-pdf-conflict-review")
                yield Button("<- Conflict", id="previous-pdf-conflict")
                yield Button("Conflict ->", id="next-pdf-conflict")
                yield Button("Keep Markdown", id="keep-markdown-conflict")
                yield Button("Keep PDF", id="keep-pdf-conflict")
                yield Button(
                    "Confirm...",
                    variant="primary",
                    id="continue-pdf-conflicts",
                )

    def on_mount(self) -> None:
        self._refresh_review()
        self.query_one("#cancel-pdf-conflict-review", Button).focus()

    def _current_conflict(self) -> Optional[PdfAnnotationConflict]:
        if not self.items:
            return None
        return self.items[self.current_index]

    @staticmethod
    def _decision_label(
        conflict: PdfAnnotationConflict,
        decision: Optional[PdfConflictDecision],
    ) -> str:
        if decision is None:
            return "UNDECIDED"
        if conflict.kind is PdfConflictKind.DELETED_FROM_MARKDOWN:
            return (
                "DELETE FROM PDF"
                if decision is PdfConflictDecision.KEEP_MARKDOWN
                else "RESTORE INTO MARKDOWN"
            )
        if decision is PdfConflictDecision.KEEP_MARKDOWN:
            return (
                "RESTORE INTO PDF"
                if conflict.kind is PdfConflictKind.DELETED_FROM_PDF
                else "USE MARKDOWN"
            )
        return (
            "DELETE FROM MARKDOWN"
            if conflict.kind is PdfConflictKind.DELETED_FROM_PDF
            else "USE PDF"
        )

    def _refresh_review(self) -> None:
        conflict = self._current_conflict()
        self.query_one("#pdf-conflict-review-title", Static).update(
            f"PDF conflict review - item {self.current_index + 1}/"
            f"{len(self.items)} - {len(self.decisions)}/{len(self.items)} decided"
        )
        if conflict is None:
            self.query_one("#pdf-conflict-review-detail", Static).update(
                "No conflicts need review."
            )
            return
        decision = self.decisions.get(conflict.annotation_id)
        deleted_from_pdf = conflict.kind is PdfConflictKind.DELETED_FROM_PDF
        deleted_from_markdown = (
            conflict.kind is PdfConflictKind.DELETED_FROM_MARKDOWN
        )
        keep_markdown = self.query_one("#keep-markdown-conflict", Button)
        keep_pdf = self.query_one("#keep-pdf-conflict", Button)
        if deleted_from_pdf:
            keep_markdown.label = "Restore PDF"
            keep_pdf.label = "Delete MD"
        elif deleted_from_markdown:
            keep_markdown.label = "Delete PDF"
            keep_pdf.label = "Restore MD"
        else:
            keep_markdown.label = "Use Markdown"
            keep_pdf.label = "Use PDF"
        sidecar_range = (
            f"[{conflict.sidecar_start}, {conflict.sidecar_end})"
            if conflict.sidecar_start is not None
            and conflict.sidecar_end is not None
            else "deleted"
            if deleted_from_markdown
            else "unavailable"
        )
        pdf_range = (
            f"[{conflict.pdf_start}, {conflict.pdf_end})"
            if conflict.pdf_start is not None and conflict.pdf_end is not None
            else "deleted"
        )
        pdf_confidence = (
            f" - mapping ~{conflict.mapping_confidence}%"
            if conflict.mapping_confidence is not None
            else ""
        )
        ordinal_label = (
            f"#{conflict.ordinal}"
            if conflict.ordinal is not None
            else "PDF-only"
        )
        sidecar_type_label = (
            conflict.sidecar_type.value
            if conflict.sidecar_type is not None
            else "(deleted)"
        )
        pdf_type_label = (
            conflict.pdf_type.value
            if conflict.pdf_type is not None
            else "(deleted)"
        )
        detail = "\n".join(
            (
                f"{ordinal_label} - {conflict.kind.value} - "
                f"{self._decision_label(conflict, decision)} - "
                f"id {conflict.annotation_id}",
                f"Markdown {sidecar_range}: "
                f"type {sidecar_type_label} - "
                f"{_compact_conflict_text(conflict.sidecar_quote)} - note: "
                f"{_compact_conflict_text(conflict.sidecar_note)}",
                f"PDF {pdf_range}{pdf_confidence}: "
                f"type {pdf_type_label} - "
                f"{_compact_conflict_text(conflict.pdf_quote)} - note: "
                f"{_compact_conflict_text(conflict.pdf_note)}",
                (
                    "Restore PDF keeps this Markdown record and writes it back; "
                    "Delete MD accepts the PDF deletion."
                    if deleted_from_pdf
                    else "Delete PDF accepts the Markdown deletion; "
                    "Restore MD reimports the PDF annotation."
                    if deleted_from_markdown
                    else "Use Markdown writes its range/note into the PDF; "
                    "Use PDF replaces the Markdown record."
                ),
            )
        )
        self.query_one("#pdf-conflict-review-detail", Static).update(detail)
        if self.preview_callback is not None:
            self.preview_callback(conflict)

    def action_next_conflict(self) -> None:
        if self.items:
            self.current_index = (self.current_index + 1) % len(self.items)
            self._refresh_review()

    def action_previous_conflict(self) -> None:
        if self.items:
            self.current_index = (self.current_index - 1) % len(self.items)
            self._refresh_review()

    def _choose(self, decision: PdfConflictDecision) -> None:
        conflict = self._current_conflict()
        if conflict is None:
            return
        self.decisions[conflict.annotation_id] = decision
        self._refresh_review()

    def action_keep_markdown(self) -> None:
        self._choose(PdfConflictDecision.KEEP_MARKDOWN)

    def action_keep_pdf(self) -> None:
        self._choose(PdfConflictDecision.KEEP_PDF)

    def _resolutions(self) -> tuple[PdfConflictResolution, ...]:
        return tuple(
            PdfConflictResolution(
                conflict.annotation_id,
                self.decisions[conflict.annotation_id],
            )
            for conflict in self.items
            if conflict.annotation_id in self.decisions
        )

    def action_continue_to_confirmation(self) -> None:
        if len(self.decisions) != len(self.items):
            self.notify(
                "Choose the PDF or Markdown result for every conflict",
                severity="warning",
                timeout=2,
            )
            return
        self.dismiss(self._resolutions())

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#previous-pdf-conflict")
    def on_previous_pressed(self, _event: Button.Pressed) -> None:
        self.action_previous_conflict()

    @on(Button.Pressed, "#next-pdf-conflict")
    def on_next_pressed(self, _event: Button.Pressed) -> None:
        self.action_next_conflict()

    @on(Button.Pressed, "#keep-markdown-conflict")
    def on_keep_markdown_pressed(self, _event: Button.Pressed) -> None:
        self.action_keep_markdown()

    @on(Button.Pressed, "#keep-pdf-conflict")
    def on_keep_pdf_pressed(self, _event: Button.Pressed) -> None:
        self.action_keep_pdf()

    @on(Button.Pressed, "#continue-pdf-conflicts")
    def on_continue_pressed(self, _event: Button.Pressed) -> None:
        self.action_continue_to_confirmation()

    @on(Button.Pressed, "#cancel-pdf-conflict-review")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.action_cancel()


class PdfConflictConfirmModal(ModalScreen[Optional[str]]):
    """Final compact confirmation before applying reviewed PDF choices."""

    CSS = """
    PdfConflictConfirmModal {
        align: center bottom;
        background: $background 10%;
    }

    #pdf-conflict-confirm-dialog {
        width: 96%;
        max-width: 120;
        height: 11;
        margin-bottom: 1;
        padding: 0 1;
        border: round $warning;
        background: $panel;
    }

    #pdf-conflict-confirm-title {
        height: 1;
        text-style: bold;
        color: $warning;
    }

    #pdf-conflict-confirm-message {
        height: 1fr;
        padding: 0 1;
        background: $surface;
    }

    #pdf-conflict-confirm-hint {
        height: 1;
        color: $text-muted;
        text-overflow: ellipsis;
    }

    #pdf-conflict-confirm-buttons {
        height: 3;
        grid-size: 5 1;
        grid-columns: 1fr 1fr 1fr 1fr 1fr;
        grid-rows: 3;
        grid-gutter: 0 1;
    }

    #pdf-conflict-confirm-buttons Button {
        width: 1fr;
        min-width: 0;
    }
    """

    BINDINGS = [
        Binding("j,down", "next_resolution", "Next", priority=True, show=False),
        Binding("k,up", "previous_resolution", "Previous", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(
        self,
        report: PdfConflictReport,
        resolutions: tuple[PdfConflictResolution, ...],
        preview_callback: Optional[PdfConflictPreviewCallback] = None,
    ) -> None:
        super().__init__()
        self.report = report
        self.resolutions = resolutions
        self.preview_callback = preview_callback
        self.current_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="pdf-conflict-confirm-dialog"):
            yield Static("", markup=False, id="pdf-conflict-confirm-title")
            yield Static("", markup=False, id="pdf-conflict-confirm-message")
            yield Static(
                "A sidecar backup is created - files are changed only after Apply",
                markup=False,
                id="pdf-conflict-confirm-hint",
            )
            with Grid(id="pdf-conflict-confirm-buttons"):
                yield Button("Cancel", id="cancel-pdf-conflict-confirm")
                yield Button("<- Decision", id="previous-pdf-resolution")
                yield Button("Decision ->", id="next-pdf-resolution")
                yield Button("Review", id="review-pdf-conflicts-again")
                yield Button(
                    "Apply",
                    variant="warning",
                    id="apply-pdf-conflicts",
                )

    def on_mount(self) -> None:
        self._refresh_confirmation()
        self.query_one("#cancel-pdf-conflict-confirm", Button).focus()

    def _current_pair(
        self,
    ) -> tuple[Optional[PdfAnnotationConflict], Optional[PdfConflictResolution]]:
        if not self.resolutions:
            return None, None
        resolution = self.resolutions[self.current_index]
        conflict = next(
            (
                item
                for item in self.report.conflicts
                if item.annotation_id == resolution.annotation_id
            ),
            None,
        )
        return conflict, resolution

    def _refresh_confirmation(self) -> None:
        markdown_count = sum(
            resolution.decision is PdfConflictDecision.KEEP_MARKDOWN
            for resolution in self.resolutions
        )
        pdf_count = len(self.resolutions) - markdown_count
        self.query_one("#pdf-conflict-confirm-title", Static).update(
            f"Apply PDF conflict decisions? - {self.current_index + 1}/"
            f"{len(self.resolutions)} - Markdown wins: {markdown_count} - "
            f"PDF wins: {pdf_count}"
        )
        conflict, resolution = self._current_pair()
        if conflict is None or resolution is None:
            detail = "No conflict decision is available."
        else:
            deleted_from_pdf = (
                conflict.kind is PdfConflictKind.DELETED_FROM_PDF
            )
            deleted_from_markdown = (
                conflict.kind is PdfConflictKind.DELETED_FROM_MARKDOWN
            )
            if resolution.decision is PdfConflictDecision.KEEP_MARKDOWN:
                action = (
                    "restore annotation into PDF"
                    if deleted_from_pdf
                    else "delete annotation from PDF"
                    if deleted_from_markdown
                    else "write Markdown version into PDF"
                )
            else:
                action = (
                    "delete Markdown record"
                    if deleted_from_pdf
                    else "restore annotation into Markdown"
                    if deleted_from_markdown
                    else "replace Markdown with PDF version"
                )
            confidence = (
                f"PDF geometry mapping: ~{conflict.mapping_confidence}%."
                if conflict.mapping_confidence is not None
                else "PDF annotation is absent; this is a deletion decision."
            )
            ordinal_label = (
                f"#{conflict.ordinal}"
                if conflict.ordinal is not None
                else "PDF-only"
            )
            sidecar_type_label = (
                conflict.sidecar_type.value
                if conflict.sidecar_type is not None
                else "deleted"
            )
            pdf_type_label = (
                conflict.pdf_type.value
                if conflict.pdf_type is not None
                else "deleted"
            )
            detail = (
                f"{ordinal_label} - {conflict.annotation_id}\n"
                f"Decision: {action}. {confidence}\n"
                f"Markdown ({sidecar_type_label}) note: "
                f"{_compact_conflict_text(conflict.sidecar_note)}\n"
                f"PDF ({pdf_type_label}) "
                f"note: {_compact_conflict_text(conflict.pdf_note)}"
            )
            if self.preview_callback is not None:
                self.preview_callback(conflict)
        self.query_one("#pdf-conflict-confirm-message", Static).update(detail)

    def action_next_resolution(self) -> None:
        if self.resolutions:
            self.current_index = (self.current_index + 1) % len(self.resolutions)
            self._refresh_confirmation()

    def action_previous_resolution(self) -> None:
        if self.resolutions:
            self.current_index = (self.current_index - 1) % len(self.resolutions)
            self._refresh_confirmation()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#previous-pdf-resolution")
    def on_previous_pressed(self, _event: Button.Pressed) -> None:
        self.action_previous_resolution()

    @on(Button.Pressed, "#next-pdf-resolution")
    def on_next_pressed(self, _event: Button.Pressed) -> None:
        self.action_next_resolution()

    @on(Button.Pressed, "#review-pdf-conflicts-again")
    def on_review_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss("review")

    @on(Button.Pressed, "#apply-pdf-conflicts")
    def on_apply_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss("apply")

    @on(Button.Pressed, "#cancel-pdf-conflict-confirm")
    def on_cancel_pressed(self, _event: Button.Pressed) -> None:
        self.action_cancel()


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

    #annotator .text-area--annotation-subtitle {
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
            "ctrl+o",
            "jump_back",
            "Previous jump",
            show=False,
            priority=True,
        ),
        Binding(
            "tab,ctrl+i",
            "jump_forward",
            "Next jump",
            show=False,
            priority=True,
        ),
        Binding(
            "shift+tab",
            "ignore_tab",
            "No pane switching",
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
            "ctrl+n",
            "next_annotation",
            "Next annotation",
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+p",
            "previous_annotation",
            "Previous annotation",
            show=False,
            priority=True,
        ),
        Binding(
            "m",
            "open_annotation_menu",
            "Annotation actions",
            show=False,
            priority=True,
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

    def get_driver_class(self):
        """Use button-event mouse tracking on Linux and macOS terminals."""

        driver_class = super().get_driver_class()
        if driver_class.__module__ == "textual.drivers.linux_driver":
            from .terminal_driver import ButtonEventLinuxDriver

            return ButtonEventLinuxDriver
        return driver_class

    def __init__(
        self,
        source: LoadedTextFile,
        *,
        soft_wrap: bool = True,
        clipboard_writer: ClipboardWriter = copy_to_system_clipboard,
        source_writer: SourceWriter = save_text_file,
        annotation_store: Optional[AnnotationStore] = None,
        initial_source_cursor: Optional[tuple[int, int]] = None,
        embed_on_annotation_save: bool = True,
    ) -> None:
        super().__init__()
        self.source = source
        self._saved_source = source
        self.soft_wrap = soft_wrap
        self._clipboard_writer = clipboard_writer
        self._source_writer = source_writer
        self.embed_on_annotation_save = embed_on_annotation_save
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
        self._pending_pdf_conflict_report: Optional[PdfConflictReport] = None
        self._pending_pdf_conflict_resolutions: tuple[
            PdfConflictResolution, ...
        ] = ()
        self._pending_manual_range: Optional[AnnotationRange] = None
        self._rematch_preview_state: Optional[RematchPreviewState] = None
        self._pending_menu_link: Optional[AnnotationLink] = None
        self._menu_focus_pane = "viewer"
        self._suspend_alignment = False
        self._processing = False
        self._pending_save_intent: Optional[str] = None
        self._pending_external_states: tuple[TrackedFileState, ...] = ()
        self._pending_external_manual_reload = False
        self._annotation_load_error: Optional[str] = None
        self._disk_tracking_error: Optional[str] = None
        self._source_rebind_required = False
        self._source_binding_error: Optional[str] = None
        self.demo_case: Optional[DemoCase] = None
        self._demo_case_error: Optional[str] = None
        self._initial_pdf_sync_error: Optional[str] = None
        self._initial_pdf_conflict_report: Optional[
            PdfConflictReport
        ] = None
        self._demo_reset_complete = False
        self.annotation_type_filter: Optional[AnnotationType] = None
        try:
            self.demo_case = load_demo_case(source.path)
        except DemoCaseError as error:
            self._demo_case_error = str(error)
        self.sync_mode = SyncMode.NORMAL
        self.annotation_store = annotation_store or AnnotationStore(source.path)
        try:
            self._source_disk_fingerprint = (
                source.disk_fingerprint or fingerprint_path(source.path)
            )
        except DiskFingerprintError as error:
            self._source_disk_fingerprint = DiskFingerprint.missing()
            self._disk_tracking_error = str(error)
        try:
            (
                self.annotation_text,
                self._annotation_disk_fingerprint,
            ) = self.annotation_store.read_snapshot()
        except AnnotationFileError as error:
            self._annotation_load_error = str(error)
            self.annotation_text = f"# Annotation file error\n\n{error}\n"
            try:
                self._annotation_disk_fingerprint = fingerprint_path(
                    self.annotation_store.path
                )
            except DiskFingerprintError as fingerprint_error:
                self._annotation_disk_fingerprint = DiskFingerprint.missing()
                self._disk_tracking_error = str(fingerprint_error)
        self._source_binding = inspect_source_binding(
            self.annotation_text,
            sidecar_path=self.annotation_store.path,
            current_source_path=source.path,
        )
        self._source_rebind_required = (
            self._source_binding.managed
            and self._source_binding.valid
            and not self._source_binding.matches
        )
        if self._source_binding.managed and not self._source_binding.valid:
            self._source_binding_error = self._source_binding.error

        if (
            source.source_kind == "pdf"
            and not self._source_rebind_required
            and self._source_binding_error is None
        ):
            try:
                import_pdf_text_markups(
                    source.path,
                    annotations_path=self.annotation_store.path,
                    loaded_source=source,
                )
                (
                    self.annotation_text,
                    self._annotation_disk_fingerprint,
                ) = self.annotation_store.read_snapshot()
                self._source_binding = inspect_source_binding(
                    self.annotation_text,
                    sidecar_path=self.annotation_store.path,
                    current_source_path=source.path,
                )
            except PdfImportConflictError as error:
                self._initial_pdf_conflict_report = error.report
                self._initial_pdf_sync_error = (
                    f"{error}. Press R to review the conflicts."
                )
            except (PdfImportError, AnnotationFileError) as error:
                self._initial_pdf_sync_error = str(error)
        self._refresh_source_binding()
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
        if self._source_rebind_required:
            self.call_after_refresh(self._open_source_rebind)
        elif self._initial_pdf_conflict_report is not None:
            self.call_after_refresh(self._open_initial_pdf_conflict_review)
        if self._source_binding_error is not None:
            self.notify(
                "Invalid annotation source binding: "
                + self._source_binding_error
                + ". The pair is read-only until the header is fixed and reloaded.",
                severity="error",
                timeout=7,
            )
        if self._demo_case_error is not None:
            self.notify(
                self._demo_case_error,
                severity="warning",
                timeout=6,
            )
        if (
            self._initial_pdf_sync_error is not None
            and self._initial_pdf_conflict_report is None
        ):
            self.notify(
                "Could not reconcile PDF annotations on open: "
                + self._initial_pdf_sync_error,
                severity="warning",
                timeout=6,
            )
        if self._disk_tracking_error is not None:
            self.notify(
                "File change tracking is unavailable: "
                + self._disk_tracking_error,
                severity="error",
                timeout=6,
            )

    async def _run_with_processing(
        self,
        message: str,
        operation: Callable[[], ProcessingResult],
    ) -> ProcessingResult:
        """Run blocking work off the UI thread behind an animated overlay."""

        if self._processing:
            raise RuntimeError("another operation is already in progress")
        self._processing = True
        resume_alignment = not self._suspend_alignment
        self._suspend_alignment = True
        modal = ProcessingModal(message)
        await self.push_screen(modal)
        try:
            return await asyncio.to_thread(operation)
        finally:
            if self.screen is modal:
                await self.pop_screen()
            self._processing = False
            if resume_alignment:
                self.call_after_refresh(self._resume_alignment)

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
            f"Ctrl-W W panes  {edit_hint}  Ctrl-S save  R rematch  "
            "Ctrl-N/P annotations  m types/filter  S sync  . align  zi/zo/zc folds  "
            "h/j/k/l move  gg/G/countgg jump  Ctrl-O/I history  "
            "Ctrl-E/Y scroll  zt/zz/zb place  "
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
        if self.source.text != self._saved_source.text:
            dirty.append("source")
        if (
            self.query_one("#annotator", AnnotationTextArea).canonical_text
            != self._saved_annotation_text
        ):
            dirty.append("annotations")
        return tuple(dirty)

    def _tracked_file_states(self) -> tuple[TrackedFileState, ...]:
        """Compare exact current disk bytes with both loaded baselines."""

        dirty = set(self._dirty_panes())
        source_current = fingerprint_path(self.source.path)
        annotation_current = fingerprint_path(self.annotation_store.path)
        return (
            TrackedFileState(
                pane="source",
                path=self.source.path,
                baseline=self._source_disk_fingerprint,
                current=source_current,
                change_state=classify_buffer_change(
                    memory_changed="source" in dirty,
                    disk_changed=(
                        source_current != self._source_disk_fingerprint
                    ),
                ),
            ),
            TrackedFileState(
                pane="annotations",
                path=self.annotation_store.path,
                baseline=self._annotation_disk_fingerprint,
                current=annotation_current,
                change_state=classify_buffer_change(
                    memory_changed="annotations" in dirty,
                    disk_changed=(
                        annotation_current
                        != self._annotation_disk_fingerprint
                    ),
                ),
            ),
        )

    def _buffer_text_for_pane(self, pane: str) -> str:
        if pane == "source":
            return self.query_one("#viewer", VimTextArea).text
        if pane == "annotations":
            return self.query_one(
                "#annotator", AnnotationTextArea
            ).canonical_text
        raise ValueError(f"unknown tracked pane: {pane}")

    def _refresh_disk_baselines(self) -> None:
        """Accept the current pair after a verified external transaction."""

        self._source_disk_fingerprint = fingerprint_path(self.source.path)
        self._annotation_disk_fingerprint = fingerprint_path(
            self.annotation_store.path
        )

    def _refresh_source_binding(self) -> SourceBinding:
        """Re-evaluate which source the currently loaded sidecar declares."""

        binding = inspect_source_binding(
            self.annotation_text,
            sidecar_path=self.annotation_store.path,
            current_source_path=self.source.path,
        )
        self._source_binding = binding
        self._source_rebind_required = (
            binding.managed and binding.valid and not binding.matches
        )
        self._source_binding_error = (
            binding.error if binding.managed and not binding.valid else None
        )
        return binding

    def _assess_source_binding(self) -> SourceRebindAssessment:
        """Estimate how well the loaded anchors fit the proposed source path."""

        binding = self._refresh_source_binding()
        records = parse_annotation_records(self.annotation_text)
        record_count = len(records)
        current_text = self.source.text
        current_hash = text_sha256(current_text)
        exact_ranges = 0
        recoverable_quotes = 0
        scores: list[int] = []
        matching_document_hash = False

        for record in records:
            if record.source_sha256 == current_hash:
                matching_document_hash = True
            if (
                record.start is None
                or record.end is None
                or record.quote is None
                or record.errors
            ):
                scores.append(0)
                continue

            exact = (
                0 <= record.start < record.end <= len(current_text)
                and current_text[record.start : record.end] == record.quote
            )
            if exact:
                exact_ranges += 1
                recoverable_quotes += 1
                scores.append(96)
                continue

            first = current_text.find(record.quote)
            if first < 0:
                scores.append(15)
                continue
            recoverable_quotes += 1
            repeated = current_text.find(record.quote, first + 1) >= 0
            scores.append(65 if repeated else 85)

        if matching_document_hash:
            confidence = 100
            summary = "a stored whole-document hash matches"
        elif not records:
            confidence = 50
            summary = "no annotation anchors are available to compare"
        else:
            confidence = round(sum(scores) / record_count)
            if exact_ranges == record_count:
                summary = "every recorded range still matches exactly"
            elif recoverable_quotes == record_count:
                summary = "all quotations remain present, but some ranges moved"
            elif recoverable_quotes:
                summary = "only some quotations can be located in this source"
            else:
                summary = "no recorded quotation was found in this source"

        return SourceRebindAssessment(
            binding=binding,
            confidence=confidence,
            summary=summary,
            exact_ranges=exact_ranges,
            recoverable_quotes=recoverable_quotes,
            record_count=record_count,
        )

    def _open_source_rebind(self) -> None:
        """Open the compact, explicit path-rebinding confirmation."""

        binding = self._refresh_source_binding()
        if self._source_binding_error is not None:
            self.notify(
                "Cannot rebind this sidecar: " + self._source_binding_error,
                severity="error",
                timeout=6,
            )
            return
        if not binding.managed:
            self.notify(
                "This annotation file has no PTA source binding",
                severity="warning",
                timeout=3,
            )
            return
        if not self._source_rebind_required:
            self.notify(
                "The annotation sidecar is already bound to this source",
                timeout=2,
            )
            return
        if isinstance(self.screen, SourceRebindModal):
            return
        self.push_screen(
            SourceRebindModal(self._assess_source_binding()),
            callback=self._finish_source_rebind,
        )

    def _require_confirmed_source_binding(self, action: str) -> bool:
        """Keep both documents read-only until a path mismatch is reviewed."""

        self._refresh_source_binding()
        if self._source_binding_error is not None:
            self.notify(
                "Cannot "
                + action
                + ": the sidecar source header is invalid ("
                + self._source_binding_error
                + ")",
                severity="error",
                timeout=6,
            )
            return False
        if not self._source_rebind_required:
            return True
        self.notify(
            f"Rebind this sidecar before you {action}",
            severity="warning",
            timeout=3,
        )
        self._open_source_rebind()
        return False

    def _finish_source_rebind(self, confirmed: bool) -> None:
        """Atomically rewrite only the managed front matter source field."""

        if not confirmed:
            self._refresh_status()
            self.notify(
                "Rebinding cancelled; source and annotations remain read-only",
                severity="warning",
                timeout=3,
            )
            return
        if self._dirty_panes():
            self.notify(
                "Reload or discard in-memory edits before rebinding",
                severity="warning",
                timeout=3,
            )
            return

        try:
            states = self._tracked_file_states()
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=6)
            return
        if any(state.disk_changed for state in states):
            self.notify(
                "A tracked file changed before rebinding; review and reload it",
                severity="warning",
                timeout=4,
            )
            self._pending_save_intent = None
            self._open_external_change_prompt(
                tuple(
                    state
                    for state in states
                    if state.change_state is not BufferChangeState.UNCHANGED
                ),
                manual_reload=True,
            )
            return

        try:
            updated = rewrite_source_binding(
                self.annotation_text,
                sidecar_path=self.annotation_store.path,
                current_source_path=self.source.path,
            )
            validate_annotation_document(updated)
            saved = self.annotation_store.write(
                updated,
                expected_fingerprint=self._annotation_disk_fingerprint,
            )
        except (
            AnnotationDocumentValidationError,
            AnnotationFileError,
            ConcurrentFileChangeError,
            DiskFingerprintError,
            ValueError,
        ) as error:
            self.notify(
                f"Could not rebind the source: {error}",
                severity="error",
                timeout=7,
            )
            return

        self._annotation_disk_fingerprint = fingerprint_bytes(
            saved.encode("utf-8")
        )
        self._replace_annotation_document(saved)
        self._refresh_source_binding()
        self._refresh_titlebar()
        self._refresh_status()
        self.notify(
            "Source path rebound; annotation ranges were not changed. "
            "Press R to review rematches if the content moved.",
            timeout=6,
        )

    def _valid_annotation_links(self) -> tuple[AnnotationLink, ...]:
        source_length = len(self.source.text)
        selected = self.annotation_type_filter
        return tuple(
            link
            for link in self.annotation_links
            if 0
            <= link.source_range.start
            < link.source_range.end
            <= source_length
            and (selected is None or link.annotation_type is selected)
        )

    def _current_annotation_link(self) -> Optional[AnnotationLink]:
        """Return the annotation addressed by the focused pane's cursor."""

        target = self._focused_search_target()
        if isinstance(target, VimTextArea):
            offset = self._source_offset_for_location(target.cursor_location)
            return self._link_for_source_offset(offset)
        if isinstance(target, AnnotationTextArea):
            full_line = target.full_line_for_display(target.cursor_location[0])
            return self._link_for_sidecar_line(full_line)
        return None

    def _annotation_label(self, link: AnnotationLink) -> str:
        try:
            ordinal = self.annotation_links.index(link) + 1
        except ValueError:
            ordinal = 0
        source_range = link.source_range
        prefix = f"Annotation {ordinal}" if ordinal else "Annotation"
        position = (
            f"position {link.bookmark_position}"
            if link.annotation_type is AnnotationType.BOOKMARK
            else f"source [{source_range.start}, {source_range.end})"
        )
        return f"{prefix} · {link.annotation_type.label} · {position}"

    def _navigate_annotation(self, direction: int) -> None:
        """Move to the next or previous annotation in the focused pane."""

        links = self._valid_annotation_links()
        if not links:
            self.notify("No valid annotations to navigate", timeout=1.75)
            return

        target = self._focused_search_target()
        if isinstance(target, VimTextArea):
            target.cancel_vim_command()
            ordered = tuple(
                sorted(
                    links,
                    key=lambda link: (
                        link.source_range.start,
                        link.source_range.end,
                        link.sidecar_start_line,
                    ),
                )
            )
            offset = self._source_offset_for_location(target.cursor_location)
            containing = tuple(
                link
                for link in ordered
                if link.source_range.start <= offset < link.source_range.end
            )
            active = self.query_one(
                "#annotator", AnnotationTextArea
            ).active_link
            current = active if active in containing else (
                containing[0] if containing else None
            )
            if current is not None:
                current_index = ordered.index(current)
                target_index = current_index + direction
                destination = (
                    ordered[target_index]
                    if 0 <= target_index < len(ordered)
                    else None
                )
            else:
                candidates = (
                    tuple(
                        link
                        for link in ordered
                        if link.source_range.start >= offset
                    )
                    if direction > 0
                    else tuple(
                        link
                        for link in ordered
                        if link.source_range.start < offset
                    )
                )
                if candidates:
                    destination = candidates[0] if direction > 0 else candidates[-1]
                else:
                    destination = None

            if destination is None:
                boundary = "last" if direction > 0 else "first"
                self.notify(f"Already at {boundary} annotation", timeout=1.25)
                return

            if target.visual_mode:
                target.action_cancel_visual()
            target.move_cursor_with_jump(
                self._source_location_for_offset(
                    self._source_offset_for_link(destination)
                ),
                center=True,
            )
            target.set_active_annotation_range(destination.source_range)
            if self.sync_mode in {SyncMode.NORMAL, SyncMode.SYNC}:
                self.query_one("#annotator", AnnotationTextArea).show_link(
                    destination,
                    move_cursor=True,
                )
        elif isinstance(target, AnnotationTextArea):
            target.cancel_vim_command()
            ordered = tuple(sorted(links, key=lambda link: link.sidecar_start_line))
            full_line = target.full_line_for_display(target.cursor_location[0])
            current = next(
                (
                    link
                    for link in ordered
                    if link.sidecar_start_line
                    <= full_line
                    <= link.sidecar_end_line
                ),
                None,
            )
            if current is not None:
                current_index = ordered.index(current)
                target_index = current_index + direction
                destination = (
                    ordered[target_index]
                    if 0 <= target_index < len(ordered)
                    else None
                )
            else:
                candidates = (
                    tuple(
                        link
                        for link in ordered
                        if link.sidecar_start_line > full_line
                    )
                    if direction > 0
                    else tuple(
                        link
                        for link in ordered
                        if link.sidecar_start_line < full_line
                    )
                )
                if candidates:
                    destination = candidates[0] if direction > 0 else candidates[-1]
                else:
                    destination = None

            if destination is None:
                boundary = "last" if direction > 0 else "first"
                self.notify(f"Already at {boundary} annotation", timeout=1.25)
                return

            target.show_link(
                destination,
                move_cursor=True,
                record_jump=True,
            )
            if self.sync_mode is SyncMode.SYNC:
                viewer = self.query_one("#viewer", VimTextArea)
                viewer.set_active_annotation_range(destination.source_range)
                viewer.move_cursor(
                    self._source_location_for_offset(
                        self._source_offset_for_link(destination)
                    ),
                    center=True,
                )
        else:
            return

        self._refresh_status()

    def action_next_annotation(self) -> None:
        self._navigate_annotation(1)

    def action_previous_annotation(self) -> None:
        self._navigate_annotation(-1)

    def action_open_annotation_menu(self) -> None:
        """Open the compact annotation action menu for the focused pane."""

        self._cancel_focused_vim_command()
        if self._pane_switch_pending:
            self._clear_pane_switch_prefix()
        if self._scroll_position_pending:
            self._clear_scroll_position_prefix()
        focused = self._focused_search_target()
        self._menu_focus_pane = (
            "annotator" if isinstance(focused, AnnotationTextArea) else "viewer"
        )
        self._pending_menu_link = self._current_annotation_link()
        current_label = (
            self._annotation_label(self._pending_menu_link)
            if self._pending_menu_link is not None
            else None
        )
        self.push_screen(
            AnnotationMenuModal(
                current_label,
                is_pdf=self.source.source_kind == "pdf",
                sync_mode=self.sync_mode,
                annotation_filter=self.annotation_type_filter,
                needs_rebind=self._source_rebind_required,
            ),
            callback=self._finish_annotation_menu,
        )

    def _finish_annotation_menu(self, choice: Optional[str]) -> None:
        link = self._pending_menu_link
        write_choices = {
            "add",
            "highlight",
            "bookmark",
            "edit-current",
            "edit",
            "delete",
            "rematch",
            "embed",
        }
        if choice in write_choices and not self._require_confirmed_source_binding(
            "modify or reconcile this document pair"
        ):
            self._pending_menu_link = None
            return
        if choice == "edit-current":
            if self._dirty_panes():
                self._pending_menu_link = None
                self.notify(
                    "Save or discard text edits before editing one annotation",
                    severity="warning",
                    timeout=3,
                )
                return
            if link is None or link not in self.annotation_links:
                self._pending_menu_link = None
                self.notify(
                    "The current annotation is no longer available",
                    severity="warning",
                    timeout=2,
                )
                return
            try:
                note = annotation_note_text(
                    self.query_one(
                        "#annotator", AnnotationTextArea
                    ).canonical_text,
                    link,
                )
            except ValueError as error:
                self._pending_menu_link = None
                self.notify(str(error), severity="error", timeout=4)
                return
            self._pending_menu_link = None
            self.push_screen(
                EditAnnotationModal(
                    self._annotation_label(link),
                    link.annotation_type,
                    note,
                ),
                callback=lambda result: self._finish_edit_annotation(
                    link, result
                ),
            )
            return
        if choice == "delete":
            if self._dirty_panes():
                self._pending_menu_link = None
                self.notify(
                    "Save or discard text edits before deleting an annotation",
                    severity="warning",
                    timeout=3,
                )
                return
            if link is None or link not in self.annotation_links:
                self._pending_menu_link = None
                self.notify(
                    "The current annotation is no longer available",
                    severity="warning",
                    timeout=2,
                )
                return
            quote = self.query_one("#viewer", VimTextArea).text[
                link.source_range.start : link.source_range.end
            ]
            quote_preview = " ".join(quote.split())
            if len(quote_preview) > 100:
                quote_preview = quote_preview[:99] + "…"
            self.push_screen(
                DeleteAnnotationModal(
                    self._annotation_label(link),
                    quote_preview or "(empty source quotation)",
                ),
                callback=self._finish_delete_annotation,
            )
            return

        self._pending_menu_link = None
        if choice == "add":
            viewer = self.query_one("#viewer", VimTextArea)
            viewer.action_annotate()
        elif choice == "highlight":
            self._start_typed_annotation(AnnotationType.HIGHLIGHT)
        elif choice == "bookmark":
            self._start_typed_annotation(AnnotationType.BOOKMARK)
        elif choice == "filter":
            self.push_screen(
                AnnotationFilterModal(self.annotation_type_filter),
                callback=self._finish_annotation_filter,
            )
        elif choice == "edit":
            target = (
                self.query_one("#annotator", AnnotationTextArea)
                if self._menu_focus_pane == "annotator"
                else self.query_one("#viewer", VimTextArea)
            )
            target.focus()
            target.action_request_edit()
        elif choice == "sync":
            self.action_cycle_sync_mode()
        elif choice == "rematch":
            self.call_next(self.action_rematch_annotations)
        elif choice == "keybindings":
            self.push_screen(KeybindingsModal())
        elif choice == "embed":
            self._embed_pdf_annotations_from_menu()
        elif choice == "reload":
            self.action_reload_files()
        elif choice == "rebind":
            self._open_source_rebind()
        else:
            self._refresh_status()

    def _finish_edit_annotation(
        self,
        link: AnnotationLink,
        result: Optional[tuple[AnnotationType, str]],
    ) -> None:
        """Persist one structured type/body edit and refresh both panes."""

        if result is None:
            self._refresh_status()
            return
        if not self._require_confirmed_source_binding(
            "edit annotations"
        ):
            return
        if self._dirty_panes() or link not in self.annotation_links:
            self.notify(
                "Documents changed before the edit was applied; reopen the menu",
                severity="warning",
                timeout=3,
            )
            return
        annotation_type, note = result
        annotation_id = link.annotation_id
        try:
            updated = update_annotation_block(
                self.query_one(
                    "#annotator", AnnotationTextArea
                ).canonical_text,
                link,
                annotation_type=annotation_type,
                note=note,
            )
        except ValueError as error:
            self.notify(str(error), severity="error", timeout=5)
            return

        self._replace_annotation_document(updated, mark_saved=False)
        edited_link = next(
            (
                candidate
                for candidate in self.annotation_links
                if candidate.annotation_id == annotation_id
            ),
            None,
        )
        annotator = self.query_one("#annotator", AnnotationTextArea)
        viewer = self.query_one("#viewer", VimTextArea)
        visible = self._valid_annotation_links()
        if edited_link is not None and edited_link in visible:
            annotator.show_link(edited_link, move_cursor=True)
            viewer.set_active_annotation_range(edited_link.source_range)
        else:
            annotator.clear_active_link()
            viewer.set_active_annotation_range(None)
        if self._menu_focus_pane == "annotator":
            annotator.focus()
        else:
            viewer.focus()
        self._request_save("save")
        if "annotations" not in self._dirty_panes():
            self.notify(
                f"Updated {annotation_type.value} annotation"
                + (
                    " · hidden by the current filter"
                    if edited_link is not None and edited_link not in visible
                    else ""
                ),
                timeout=2.5,
            )

    def _finish_annotation_filter(self, choice: Optional[str]) -> None:
        """Apply a non-destructive type projection to the annotation pane."""

        if choice is None:
            self._refresh_status()
            return
        selected = None if choice == "all" else AnnotationType(choice)
        self.annotation_type_filter = selected
        annotator = self.query_one("#annotator", AnnotationTextArea)
        annotator.set_annotation_type_filter(selected)
        visible = self._valid_annotation_links()
        viewer = self.query_one("#viewer", VimTextArea)
        if visible:
            destination = visible[0]
            annotator.show_link(destination, move_cursor=True)
            viewer.set_active_annotation_range(destination.source_range)
        else:
            annotator.clear_active_link()
            viewer.set_active_annotation_range(None)
        annotator.focus()
        self._set_search_match("annotator", None)
        self._refresh_status()
        label = selected.label if selected is not None else "All"
        self.notify(
            f"Annotation filter: {label} · {len(visible)} visible",
            timeout=2,
        )

    def _finish_delete_annotation(self, confirmed: bool) -> None:
        link = self._pending_menu_link
        self._pending_menu_link = None
        if not confirmed or link is None:
            self._refresh_status()
            return
        if not self._require_confirmed_source_binding(
            "delete annotations"
        ):
            return
        if self._dirty_panes() or link not in self.annotation_links:
            self.notify(
                "Documents changed before deletion; reopen the menu",
                severity="warning",
                timeout=3,
            )
            return

        old_links = self.annotation_links
        old_index = old_links.index(link)
        deleted_label = self._annotation_label(link)
        try:
            updated = delete_annotation_block(
                self.query_one("#annotator", AnnotationTextArea).canonical_text,
                link,
            )
        except ValueError as error:
            self.notify(str(error), severity="error", timeout=5)
            return

        self._replace_annotation_document(updated, mark_saved=False)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        viewer = self.query_one("#viewer", VimTextArea)

        if self.annotation_links:
            destination = self.annotation_links[
                min(old_index, len(self.annotation_links) - 1)
            ]
            annotator.show_link(destination, move_cursor=True)
            if self._menu_focus_pane == "viewer":
                viewer.set_active_annotation_range(destination.source_range)
                viewer.move_cursor(
                    self._source_location_for_offset(
                        self._source_offset_for_link(destination)
                    ),
                    center=True,
                )
                viewer.focus()
            else:
                annotator.focus()
                if self.sync_mode is SyncMode.SYNC:
                    viewer.set_active_annotation_range(
                        destination.source_range
                    )
                    viewer.move_cursor(
                        self._source_location_for_offset(
                            self._source_offset_for_link(destination)
                        ),
                        center=True,
                    )
        else:
            annotator.clear_active_link()
            viewer.set_active_annotation_range(None)
            (
                annotator.focus()
                if self._menu_focus_pane == "annotator"
                else viewer.focus()
            )

        self._request_save("save")
        if "annotations" not in self._dirty_panes():
            self.notify(
                f"Deleted {deleted_label}",
                timeout=2.5,
            )

    def _embed_pdf_annotations_from_menu(self) -> None:
        """Manually synchronize saved records into the source PDF."""

        if not self._require_confirmed_source_binding(
            "synchronize PDF annotations"
        ):
            return
        if self.source.source_kind != "pdf":
            self.notify(
                "PDF embedding is available only for PDF sources",
                severity="warning",
                timeout=2.5,
            )
            return
        if self._dirty_panes():
            self.notify(
                "Save or discard text edits before embedding annotations",
                severity="warning",
                timeout=3,
            )
            return
        try:
            states = self._tracked_file_states()
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=6)
            return
        if any(state.disk_changed for state in states):
            self._pending_save_intent = None
            self._open_external_change_prompt(
                tuple(
                    state
                    for state in states
                    if state.change_state is not BufferChangeState.UNCHANGED
                ),
                manual_reload=False,
            )
            return
        if not self.annotation_store.path.exists():
            self.notify(
                "No saved annotation file to embed",
                severity="warning",
                timeout=2.5,
            )
            return
        self._refresh_pdf_copy_after_annotation_save(
            source=self._saved_source,
            manual=True,
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

    def _source_offset_for_location(self, location: tuple[int, int]) -> int:
        """Map a source cursor to an offset without joining the document."""

        viewer = self.query_one("#viewer", VimTextArea)
        if viewer.edit_mode or viewer._normal_history_pending:
            return viewer.document.get_index_from_location(location)
        row, column = location
        return self._source_line_starts[row] + column

    @staticmethod
    def _source_offset_for_link(link: AnnotationLink) -> int:
        """Use a bookmark's exact cursor position, otherwise its range start."""

        return (
            link.bookmark_position
            if link.annotation_type is AnnotationType.BOOKMARK
            and link.bookmark_position is not None
            else link.source_range.start
        )

    def _align_annotation_to_source(self, *, record_jump: bool = False) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        offset = self._source_offset_for_location(viewer.cursor_location)
        link = self._link_for_source_offset(offset)
        if link is None:
            annotator.clear_active_link()
            viewer.set_active_annotation_range(None)
            return
        annotator.show_link(
            link,
            move_cursor=True,
            record_jump=record_jump,
        )
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

    def _align_source_to_annotation(self, *, record_jump: bool = False) -> None:
        link = self._focus_annotation_at_cursor()
        if link is None:
            return

        viewer = self.query_one("#viewer", VimTextArea)
        viewer.set_active_annotation_range(link.source_range)
        if viewer.visual_mode:
            viewer.action_cancel_visual()
        destination = self._source_location_for_offset(
            self._source_offset_for_link(link)
        )
        if record_jump:
            viewer.move_cursor_with_jump(destination, center=True)
        else:
            viewer.move_cursor(destination, center=True)

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
        command_info = (
            f"  |  CMD {target.vim_command_text}"
            if isinstance(target, HalfPageBottomScrollTextArea)
            and target.vim_command_text
            else ""
        )
        selection_info = f"  |  {len(selected)} chars selected" if selected else ""
        dirty_panes = self._dirty_panes()
        modified_info = (
            f"  |  MODIFIED: {', '.join(dirty_panes)}" if dirty_panes else ""
        )
        binding_info = (
            "  |  REBIND REQUIRED: m P"
            if self._source_rebind_required
            else "  |  INVALID SOURCE BINDING"
            if self._source_binding_error is not None
            else ""
        )
        wrap_info = "wrap" if target.soft_wrap else "no wrap"
        filter_info = (
            self.annotation_type_filter.value.upper()
            if self.annotation_type_filter is not None
            else "ALL"
        )
        self.query_one("#status", Static).update(
            f"{mode}  |  ALIGN {self.sync_mode.value.upper()}  |  "
            f"FILTER {filter_info}  |  Ln {row + 1}, Col {column + 1}  |  {wrap_info}"
            f"{command_info}{selection_info}{modified_info}{binding_info}"
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

    def _cancel_focused_vim_command(self) -> None:
        target = self._focused_search_target()
        if isinstance(target, HalfPageBottomScrollTextArea):
            target.cancel_vim_command()

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
        offset = (
            self._source_offset_for_location(target.cursor_location)
            if isinstance(target, VimTextArea)
            else target.document.get_index_from_location(target.cursor_location)
        )
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
        target.cancel_vim_command()
        target.move_cursor_with_jump(
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
        target.cancel_vim_command()
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

        self._cancel_focused_vim_command()
        modes = (SyncMode.NORMAL, SyncMode.SYNC, SyncMode.OFF)
        current_index = modes.index(self.sync_mode)
        self.sync_mode = modes[(current_index + 1) % len(modes)]
        self._refresh_status()
        self.notify(f"Alignment mode: {self.sync_mode.value}", timeout=1.25)

    def action_align_panes(self) -> None:
        """Manually align the panes and center both cursor lines."""

        target = self._focused_search_target()
        if isinstance(target, VimTextArea):
            target.cancel_vim_command()
            self._align_annotation_to_source(record_jump=True)
            _position_cursor_line(target, "middle")
            annotator = self.query_one("#annotator", AnnotationTextArea)
            if annotator.active_link is not None:
                _position_cursor_line(annotator, "middle")
        elif isinstance(target, AnnotationTextArea):
            target.cancel_vim_command()
            self._align_source_to_annotation(record_jump=True)
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

    def _open_initial_pdf_conflict_review(self) -> None:
        """Immediately expose conflicts detected while opening a PDF pair."""

        report = self._initial_pdf_conflict_report
        if report is None:
            return
        self._initial_pdf_conflict_report = None
        self._pending_pdf_conflict_report = report
        self._pending_pdf_conflict_resolutions = ()
        self._capture_rematch_preview_state()
        self.push_screen(
            PdfConflictReviewModal(
                report,
                preview_callback=self._preview_pdf_conflict,
            ),
            callback=self._finish_pdf_conflict_review,
        )

    def _preview_pdf_conflict(self, conflict: PdfAnnotationConflict) -> None:
        """Reveal a PDF/Markdown conflict in both panes behind its modal."""

        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        viewer.visual_mode = False
        viewer.selection = Selection.cursor(viewer.cursor_location)
        start = (
            conflict.pdf_start
            if conflict.pdf_start is not None
            else conflict.sidecar_start
        )
        end = (
            conflict.pdf_end
            if conflict.pdf_end is not None
            else conflict.sidecar_end
        )
        preview_range = (
            AnnotationRange(start, end)
            if start is not None
            and end is not None
            and 0 <= start < end <= len(viewer.text)
            else None
        )
        viewer.set_rematch_candidate_range(preview_range)
        if preview_range is not None:
            viewer.move_cursor(
                self._source_location_for_offset(preview_range.start)
            )
            _position_cursor_line(viewer, "top")

        record = next(
            (
                item
                for item in parse_annotation_records(annotator.canonical_text)
                if item.annotation_id == conflict.annotation_id
            ),
            None,
        )
        if record is None:
            annotator.clear_active_link()
            return
        record_line = annotator.canonical_text.count(
            "\n", 0, record.block_start
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

    def _replace_annotation_document(
        self, saved: str, *, mark_saved: bool = True
    ) -> None:
        """Load one sidecar document into the live annotation pane."""

        annotator = self.query_one("#annotator", AnnotationTextArea)
        viewer = self.query_one("#viewer", VimTextArea)
        full_cursor = annotator._full_cursor_location()
        viewport_position = annotator._cursor_viewport_position()
        self.annotation_text = saved
        if mark_saved:
            self._saved_annotation_text = saved
        self.annotation_links = parse_annotation_links(saved)
        self.annotation_ranges = tuple(
            link.source_range for link in self.annotation_links
        )
        annotator.load_annotation_text(saved)
        annotator.set_annotation_links(self.annotation_links)
        display_row = annotator._display_line_for_full(full_cursor[0])
        display_lines = annotator.text.split("\n")
        display_row = max(0, min(display_row, len(display_lines) - 1))
        display_line = display_lines[display_row] if display_lines else ""
        annotator.move_cursor(
            (display_row, min(full_cursor[1], len(display_line)))
        )
        annotator._restore_cursor_viewport_position(viewport_position)
        viewer.set_annotation_ranges(self.annotation_ranges)
        self._set_search_match("annotator", None)

    async def _review_pdf_conflicts_before_text_rematch(self) -> bool:
        """Open conflict review when PDF reconciliation needs user choices."""

        if self.source.source_kind != "pdf":
            return False
        viewer = self.query_one("#viewer", VimTextArea)
        viewer_text = viewer.text
        source_path = self.source.path
        annotations_path = self.annotation_store.path

        def reconcile_pdf_pair() -> tuple[LoadedTextFile, Optional[str]]:
            current_source = load_source_file(source_path)
            if current_source.text != viewer_text:
                return current_source, None
            import_pdf_text_markups(
                current_source.path,
                annotations_path=annotations_path,
                loaded_source=current_source,
            )
            return current_source, self.annotation_store.read_text()

        try:
            current_source, refreshed = await self._run_with_processing(
                "Checking PDF and Markdown annotations…",
                reconcile_pdf_pair,
            )
        except PdfImportConflictError as error:
            try:
                refreshed = self.annotation_store.read_text()
            except AnnotationFileError as read_error:
                self.notify(str(read_error), severity="error", timeout=5)
                return True
            annotator = self.query_one("#annotator", AnnotationTextArea)
            if refreshed != annotator.canonical_text:
                self._replace_annotation_document(refreshed)
            self._pending_pdf_conflict_report = error.report
            self._pending_pdf_conflict_resolutions = ()
            if self._rematch_preview_state is None:
                self._capture_rematch_preview_state()
            self.push_screen(
                PdfConflictReviewModal(
                    error.report,
                    preview_callback=self._preview_pdf_conflict,
                ),
                callback=self._finish_pdf_conflict_review,
            )
            return True
        except (PdfImportError, TextFileError, AnnotationFileError) as error:
            self.notify(str(error), severity="error", timeout=6)
            return True
        if refreshed is None:
            self.notify(
                "The PDF text changed while the viewer was open; reopen it "
                "before rematching",
                severity="warning",
                timeout=5,
            )
            return True

        self.source = current_source
        self._saved_source = current_source
        annotator = self.query_one("#annotator", AnnotationTextArea)
        if refreshed != annotator.canonical_text:
            self._replace_annotation_document(refreshed)
        self._initial_pdf_sync_error = None
        try:
            self._refresh_disk_baselines()
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=5)
            return True
        self._refresh_titlebar()
        return False

    def _finish_pdf_conflict_review(
        self,
        resolutions: Optional[tuple[PdfConflictResolution, ...]],
    ) -> None:
        report = self._pending_pdf_conflict_report
        if resolutions is None or report is None:
            self._restore_rematch_preview()
            self._pending_pdf_conflict_report = None
            self._pending_pdf_conflict_resolutions = ()
            self._rematch_preview_state = None
            self._refresh_status()
            return
        self._pending_pdf_conflict_resolutions = resolutions
        self.push_screen(
            PdfConflictConfirmModal(
                report,
                resolutions,
                preview_callback=self._preview_pdf_conflict,
            ),
            callback=self._finish_pdf_conflict_confirmation,
        )

    async def _finish_pdf_conflict_confirmation(
        self, choice: Optional[str]
    ) -> None:
        report = self._pending_pdf_conflict_report
        resolutions = self._pending_pdf_conflict_resolutions
        if report is None:
            return
        if choice == "review":
            self.push_screen(
                PdfConflictReviewModal(
                    report,
                    resolutions,
                    preview_callback=self._preview_pdf_conflict,
                ),
                callback=self._finish_pdf_conflict_review,
            )
            return
        if choice != "apply":
            self._restore_rematch_preview()
            self._pending_pdf_conflict_report = None
            self._pending_pdf_conflict_resolutions = ()
            self._rematch_preview_state = None
            self._refresh_status()
            return
        self._restore_rematch_preview()
        self._rematch_preview_state = None
        await self._apply_pdf_conflict_resolutions(report, resolutions)

    async def _apply_pdf_conflict_resolutions(
        self,
        report: PdfConflictReport,
        resolutions: tuple[PdfConflictResolution, ...],
    ) -> None:
        """Apply a confirmed conflict batch and reload the canonical pair."""

        if self._dirty_panes():
            self.notify(
                "Buffers changed after conflict review; press R to scan again",
                severity="warning",
                timeout=4,
            )
            return
        source_path = self.source.path
        annotations_path = self.annotation_store.path

        def apply_resolutions():
            result = resolve_pdf_annotation_conflicts(
                source_path,
                report,
                resolutions,
                annotations_path=annotations_path,
            )
            return (
                result,
                load_source_file(source_path),
                self.annotation_store.read_text(),
            )

        try:
            (
                result,
                refreshed_source,
                refreshed_annotations,
            ) = await self._run_with_processing(
                "Applying PDF/Markdown decisions…",
                apply_resolutions,
            )
        except (PdfImportError, TextFileError, AnnotationFileError) as error:
            self._pending_pdf_conflict_report = None
            self._pending_pdf_conflict_resolutions = ()
            self.notify(str(error), severity="error", timeout=7)
            return

        viewer = self.query_one("#viewer", VimTextArea)
        self.source = refreshed_source
        self._saved_source = refreshed_source
        if refreshed_source.text != viewer.text:
            viewer.load_text(refreshed_source.text)
            viewer.clear_jump_history()
            self._rebuild_source_line_starts()
        self._replace_annotation_document(refreshed_annotations)
        try:
            self._refresh_disk_baselines()
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=5)
            return
        self._pending_pdf_conflict_report = None
        self._pending_pdf_conflict_resolutions = ()
        self._initial_pdf_sync_error = None
        self._refresh_titlebar()
        self._refresh_status()
        backup = (
            f" Backup: {result.backup_path.name}."
            if result.backup_path is not None
            else ""
        )
        markdown_kept = sum(
            item.decision is PdfConflictDecision.KEEP_MARKDOWN
            for item in resolutions
        )
        pdf_kept = len(resolutions) - markdown_kept
        self.notify(
            f"Resolved {len(resolutions)} PDF annotation conflict(s): "
            f"Markdown kept {markdown_kept}, PDF kept {pdf_kept}."
            + backup
            + " Press R again for text rematches.",
            timeout=7,
        )

    async def action_rematch_annotations(self) -> None:
        """Run a read-only scan and open review when stale records exist."""

        self._cancel_focused_vim_command()
        if not self._require_confirmed_source_binding(
            "scan or apply annotation rematches"
        ):
            return
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
        self._capture_rematch_preview_state()
        if await self._review_pdf_conflicts_before_text_rematch():
            if self._pending_pdf_conflict_report is None:
                self._restore_rematch_preview()
                self._rematch_preview_state = None
            return
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        source_text = viewer.text
        sidecar_text = annotator.canonical_text
        report = await self._run_with_processing(
            "Scanning annotation anchors…",
            lambda: scan_rematches(source_text, sidecar_text),
        )
        if not report.items:
            self._restore_rematch_preview()
            self._rematch_preview_state = None
            self.notify("No structured annotations to rematch", timeout=2)
            return
        if not report.stale_items:
            self._restore_rematch_preview()
            self._rematch_preview_state = None
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
        try:
            self._refresh_disk_baselines()
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=5)
            return
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
        viewer.clear_jump_history()
        viewer.cancel_vim_command()
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
        self._refresh_pdf_copy_after_annotation_save(source=saved_source)
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
            (
                disk_sidecar,
                disk_sidecar_fingerprint,
            ) = self.annotation_store.read_snapshot()
        except (TextFileError, AnnotationFileError) as error:
            self.notify(str(error), severity="error", timeout=5)
            return
        try:
            disk_source_fingerprint = (
                disk_source.disk_fingerprint
                or fingerprint_path(disk_source.path)
            )
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=5)
            return
        if (
            disk_source_fingerprint != self._source_disk_fingerprint
            or disk_sidecar_fingerprint
            != self._annotation_disk_fingerprint
        ):
            self.notify(
                "A file changed on disk after it was loaded; reload and run R "
                "again. No file was overwritten.",
                severity="warning",
                timeout=6,
            )
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
                expected_fingerprint=disk_sidecar_fingerprint,
                validate=lambda written: validate_applied_rematches(
                    disk_sidecar,
                    written,
                    disk_source.text,
                    choices,
                    pdf_document=disk_source.pdf_document,
                ),
            )
        except (
            AnnotationFileError,
            ConcurrentFileChangeError,
            DiskFingerprintError,
            ValueError,
        ) as error:
            self.notify(str(error), severity="error", timeout=6)
            return

        self.annotation_text = saved
        self._saved_annotation_text = saved
        try:
            self._annotation_disk_fingerprint = fingerprint_bytes(
                saved.encode("utf-8")
            )
            self._source_disk_fingerprint = disk_source_fingerprint
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=5)
            return
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
        self._refresh_pdf_copy_after_annotation_save(source=disk_source)
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
            "next_annotation",
            "previous_annotation",
            "open_annotation_menu",
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
        if action in {"jump_back", "jump_forward", "ignore_tab"} and modal_open:
            return False
        if action in {"request_quit", "request_quit_force"} and modal_open:
            return False
        if action in {
            "start_search",
            "search_next",
            "search_previous",
            "next_annotation",
            "previous_annotation",
            "open_annotation_menu",
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

        self._cancel_focused_vim_command()
        self._request_save("save")

    def _validate_dirty_annotations(self, dirty_panes: tuple[str, ...]) -> bool:
        if "annotations" not in dirty_panes:
            return True
        if self._annotation_load_error is not None:
            self.notify(
                "The annotation file did not load safely and cannot be "
                "overwritten. Fix it externally or reload it first.",
                severity="error",
                timeout=6,
            )
            return False
        annotation_text = self.query_one(
            "#annotator", AnnotationTextArea
        ).canonical_text
        try:
            validate_annotation_document(annotation_text)
        except AnnotationDocumentValidationError as error:
            self.notify(str(error), severity="error", timeout=7)
            return False
        return True

    def _request_save(self, intent: str) -> None:
        """Validate, preflight disk state, then save or open conflict review."""

        if not self._require_confirmed_source_binding("save changes"):
            return
        dirty_panes = self._dirty_panes()
        try:
            states = self._tracked_file_states()
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=6)
            return

        external = tuple(state for state in states if state.disk_changed)
        if external:
            relevant = tuple(
                state
                for state in states
                if state.change_state is not BufferChangeState.UNCHANGED
            )
            self._pending_save_intent = intent
            self._open_external_change_prompt(relevant, manual_reload=False)
            return

        if not self._validate_dirty_annotations(dirty_panes):
            return

        if not dirty_panes:
            if intent == "quit":
                self._complete_quit()
            else:
                self.notify("No changes to save", timeout=1.5)
            return

        try:
            saved = self._save_dirty_changes()
        except (ConcurrentFileChangeError, DiskFingerprintError):
            # A file changed in the narrow interval after preflight. Re-run
            # classification and present the same recoverable conflict flow.
            self._pending_save_intent = intent
            try:
                raced_states = self._tracked_file_states()
            except DiskFingerprintError as error:
                self.notify(str(error), severity="error", timeout=6)
                return
            self._open_external_change_prompt(
                tuple(
                    state
                    for state in raced_states
                    if state.change_state is not BufferChangeState.UNCHANGED
                ),
                manual_reload=False,
            )
            return
        if not saved:
            return
        self._pending_save_intent = None
        if intent == "quit":
            self._complete_quit()
        else:
            self.notify(
                f"Saved {', '.join(dirty_panes)}",
                timeout=2,
            )

    def _open_external_change_prompt(
        self,
        states: tuple[TrackedFileState, ...],
        *,
        manual_reload: bool,
    ) -> None:
        self._pending_external_states = states
        self._pending_external_manual_reload = manual_reload
        self.push_screen(
            ExternalFileChangeModal(
                states,
                manual_reload=manual_reload,
            ),
            callback=self._finish_external_change_resolution,
        )

    def action_reload_files(self) -> None:
        """Reload both tracked files, protecting any unsaved buffers first."""

        self._cancel_focused_vim_command()
        try:
            states = self._tracked_file_states()
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=6)
            return
        if any(state.memory_changed for state in states):
            relevant = tuple(
                state
                for state in states
                if state.change_state is not BufferChangeState.UNCHANGED
            )
            self._pending_save_intent = None
            self._open_external_change_prompt(relevant, manual_reload=True)
            return
        self.call_next(self._reload_all_files)

    async def _reload_all_files(self) -> None:
        try:
            payload = await self._run_with_processing(
                "Reloading source and annotations…",
                lambda: self._read_reload_payload(("source", "annotations")),
            )
        except (
            AnnotationDocumentValidationError,
            AnnotationFileError,
            TextFileError,
            DiskFingerprintError,
        ) as error:
            self.notify(str(error), severity="error", timeout=7)
            return
        self._apply_reload_payload(payload)
        self.notify("Reloaded source and annotations from disk", timeout=2.5)

    def _read_reload_payload(self, panes: tuple[str, ...]) -> ReloadPayload:
        source = (
            load_source_file(self.source.path)
            if "source" in panes
            else None
        )
        source_fingerprint = (
            source.disk_fingerprint or fingerprint_path(source.path)
            if source is not None
            else None
        )
        annotation_text: Optional[str] = None
        annotation_fingerprint: Optional[DiskFingerprint] = None
        if "annotations" in panes:
            (
                annotation_text,
                annotation_fingerprint,
            ) = self.annotation_store.read_snapshot()
            validate_annotation_document(annotation_text)
        return ReloadPayload(
            source=source,
            source_fingerprint=source_fingerprint,
            annotation_text=annotation_text,
            annotation_fingerprint=annotation_fingerprint,
        )

    def _apply_reload_payload(self, payload: ReloadPayload) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        focus_id = "annotator" if annotator.has_focus else "viewer"
        source_cursor = viewer.cursor_location

        if payload.source is not None:
            viewer.leave_edit_mode()
            self.source = payload.source
            self._saved_source = payload.source
            assert payload.source_fingerprint is not None
            self._source_disk_fingerprint = payload.source_fingerprint
            viewer.load_text(payload.source.text)
            source_lines = payload.source.text.split("\n")
            row = max(0, min(source_cursor[0], len(source_lines) - 1))
            viewer.move_cursor(
                (row, min(source_cursor[1], len(source_lines[row])))
            )
            self._rebuild_source_line_starts()

        if payload.annotation_text is not None:
            annotator.leave_edit_mode()
            self._replace_annotation_document(payload.annotation_text)
            assert payload.annotation_fingerprint is not None
            self._annotation_disk_fingerprint = payload.annotation_fingerprint
            self._annotation_load_error = None

        self._refresh_source_binding()

        viewer.set_annotation_ranges(self.annotation_ranges)
        self._set_search_match("viewer", None)
        self._set_search_match("annotator", None)
        self._refresh_titlebar()
        (annotator if focus_id == "annotator" else viewer).focus()
        self._refresh_status()
        if self._source_rebind_required:
            self.call_after_refresh(self._open_source_rebind)
        elif self._source_binding_error is not None:
            self.notify(
                "Invalid annotation source binding: "
                + self._source_binding_error,
                severity="error",
                timeout=6,
            )

    async def _finish_external_change_resolution(
        self, choice: Optional[str]
    ) -> None:
        states = self._pending_external_states
        manual_reload = self._pending_external_manual_reload
        save_intent = self._pending_save_intent
        self._pending_external_states = ()
        self._pending_external_manual_reload = False

        if choice is None:
            self._pending_save_intent = None
            self._refresh_status()
            return

        panes_to_reload = (
            ("source", "annotations")
            if manual_reload
            else tuple(state.pane for state in states if state.disk_changed)
        )
        copy_inputs = tuple(
            (state.path, self._buffer_text_for_pane(state.pane))
            for state in states
            if state.memory_changed
            and (manual_reload or state.disk_changed)
        )

        def preserve_and_reload() -> tuple[tuple[Path, ...], ReloadPayload]:
            copies = (
                tuple(
                    write_conflict_copy(path, text)
                    for path, text in copy_inputs
                )
                if choice == "copy"
                else ()
            )
            return copies, self._read_reload_payload(panes_to_reload)

        try:
            copies, payload = await self._run_with_processing(
                (
                    "Preserving conflict copies and reloading…"
                    if choice == "copy"
                    else "Reloading changed files…"
                ),
                preserve_and_reload,
            )
        except (
            AnnotationDocumentValidationError,
            AnnotationFileError,
            TextFileError,
            DiskFingerprintError,
        ) as error:
            self._pending_save_intent = None
            self.notify(str(error), severity="error", timeout=7)
            return

        self._apply_reload_payload(payload)
        copy_message = (
            " Conflict copies: "
            + ", ".join(path.name for path in copies)
            + "."
            if copies
            else ""
        )
        self.notify(
            "Reloaded disk changes." + copy_message,
            timeout=6 if copies else 2.5,
        )
        self._pending_save_intent = None
        if save_intent is not None:
            self._request_save(save_intent)

    def _jump_through_history(self, *, forward: bool) -> None:
        """Move through only the focused pane's independent jumplist."""

        target = self._focused_search_target()
        if target is None:
            return
        target.cancel_vim_command()
        if isinstance(target, VimTextArea) and target.visual_mode:
            target.action_cancel_visual()
        moved = target.jump_forward() if forward else target.jump_back()
        if not moved:
            direction = "newer" if forward else "older"
            self.notify(
                f"No {direction} jump position in this pane",
                timeout=1.5,
            )
        self._refresh_status()

    def action_jump_back(self) -> None:
        self._jump_through_history(forward=False)

    def action_jump_forward(self) -> None:
        self._jump_through_history(forward=True)

    def action_ignore_tab(self) -> None:
        """Keep Shift+Tab from traversing between the two text panes."""

        target = self._focused_search_target()
        if target is not None:
            target.cancel_vim_command()

    def _request_quit(self) -> None:
        self._cancel_focused_vim_command()
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
        self._request_save("quit")

    def _complete_quit(self) -> None:
        """Capture resumable state only after the quit decision is complete."""

        viewer = self.query_one("#viewer", VimTextArea)
        self.exit_source_cursor = viewer.cursor_location
        self.quit_completed = True
        self.exit()

    def _refresh_pdf_copy_after_annotation_save(
        self,
        *,
        source: Optional[LoadedTextFile] = None,
        manual: bool = False,
    ) -> None:
        """Synchronize the source PDF after saving or an explicit menu action."""

        current_source = self.source if source is None else source
        if current_source.source_kind != "pdf":
            if manual:
                self.notify(
                    "PDF embedding is available only for PDF sources",
                    severity="warning",
                    timeout=2.5,
                )
            return
        if not manual and not self.embed_on_annotation_save:
            return

        from .embed import (
            PdfEmbedError,
            default_output_path,
            embed_pdf_annotations,
        )
        from .embed_state import load_current_embed_receipt

        output_path = default_output_path(current_source.path).resolve()
        receipt = load_current_embed_receipt(
            current_source.path,
            self.annotation_store.path,
            output_path,
        )
        if receipt is not None:
            try:
                self._source_disk_fingerprint = fingerprint_path(
                    current_source.path
                )
                self._annotation_disk_fingerprint = fingerprint_path(
                    self.annotation_store.path
                )
            except DiskFingerprintError as error:
                self.notify(str(error), severity="error", timeout=5)
                return
            self.notify(
                f"PDF and sidecar already synchronized: {output_path.name}",
                timeout=2,
            )
            return
        try:
            result = embed_pdf_annotations(
                current_source.path,
                annotations_path=self.annotation_store.path,
                force=True,
                loaded_source=current_source,
            )
        except PdfEmbedError as error:
            message = (
                f"Could not synchronize source PDF: {error}"
                if manual
                else "Annotations were saved, but the source PDF could not "
                f"be synchronized: {error}"
            )
            self.notify(
                message,
                severity="error",
                timeout=6,
            )
            return

        try:
            refreshed_source = load_source_file(current_source.path)
            (
                refreshed_annotations,
                refreshed_annotation_fingerprint,
            ) = self.annotation_store.read_snapshot()
        except (TextFileError, AnnotationFileError) as error:
            self.notify(
                "PDF was synchronized, but the refreshed pair could not be "
                f"loaded: {error}",
                severity="error",
                timeout=6,
            )
            return

        self.source = refreshed_source
        self._saved_source = refreshed_source
        try:
            self._source_disk_fingerprint = (
                refreshed_source.disk_fingerprint
                or fingerprint_path(refreshed_source.path)
            )
        except DiskFingerprintError as error:
            self.notify(str(error), severity="error", timeout=5)
            return
        self._annotation_disk_fingerprint = refreshed_annotation_fingerprint
        if refreshed_annotations != self._saved_annotation_text:
            annotator = self.query_one("#annotator", AnnotationTextArea)
            viewer = self.query_one("#viewer", VimTextArea)
            full_cursor = annotator._full_cursor_location()
            viewport_position = annotator._cursor_viewport_position()
            self.annotation_text = refreshed_annotations
            self._saved_annotation_text = refreshed_annotations
            self.annotation_links = parse_annotation_links(refreshed_annotations)
            self.annotation_ranges = tuple(
                link.source_range for link in self.annotation_links
            )
            annotator.load_annotation_text(refreshed_annotations)
            annotator.set_annotation_links(self.annotation_links)
            display_row = annotator._display_line_for_full(full_cursor[0])
            display_lines = annotator.text.split("\n")
            display_line = display_lines[display_row] if display_lines else ""
            annotator.move_cursor(
                (display_row, min(full_cursor[1], len(display_line)))
            )
            annotator._restore_cursor_viewport_position(viewport_position)
            viewer.set_annotation_ranges(self.annotation_ranges)
            self._set_search_match("annotator", None)
        self._refresh_titlebar()
        self.notify(
            f"Synchronized source PDF: {result.output_path.name}",
            timeout=3,
        )

    def _save_dirty_changes(self) -> bool:
        viewer = self.query_one("#viewer", VimTextArea)
        annotator = self.query_one("#annotator", AnnotationTextArea)

        if viewer.text != self._saved_source.text:
            try:
                if self._source_writer is save_text_file:
                    saved_source = save_text_file(
                        self._saved_source,
                        viewer.text,
                        expected_fingerprint=self._source_disk_fingerprint,
                    )
                else:
                    if (
                        fingerprint_path(self.source.path)
                        != self._source_disk_fingerprint
                    ):
                        raise ConcurrentFileChangeError(
                            "source file changed on disk after it was loaded: "
                            f"{self.source.path}"
                        )
                    saved_source = self._source_writer(
                        self._saved_source, viewer.text
                    )
            except (ConcurrentFileChangeError, DiskFingerprintError):
                raise
            except TextFileError as error:
                self.notify(str(error), severity="error", timeout=5)
                return False
            self._saved_source = saved_source
            self.source = saved_source
            self._source_disk_fingerprint = (
                saved_source.disk_fingerprint
                or fingerprint_path(saved_source.path)
            )
            self._rebuild_source_line_starts()
            self._refresh_titlebar()

        if annotator.canonical_text != self._saved_annotation_text:
            try:
                saved_annotations = self.annotation_store.write(
                    annotator.canonical_text,
                    expected_fingerprint=self._annotation_disk_fingerprint,
                )
            except (ConcurrentFileChangeError, DiskFingerprintError):
                raise
            except AnnotationFileError as error:
                self.notify(str(error), severity="error", timeout=5)
                self._refresh_status()
                return False
            self._saved_annotation_text = saved_annotations
            self.annotation_text = saved_annotations
            self._annotation_disk_fingerprint = fingerprint_bytes(
                saved_annotations.encode("utf-8")
            )
            self._refresh_pdf_copy_after_annotation_save()

        self._refresh_status()
        return True

    def _clear_pane_switch_prefix(self) -> None:
        self._pane_switch_pending = False
        self._pane_switch_timer = None
        self._refresh_status()

    def action_start_pane_switch(self) -> None:
        self._cancel_focused_vim_command()
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

        self._cancel_focused_vim_command()
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
        if not self._require_confirmed_source_binding("edit the source"):
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
        if not self._require_confirmed_source_binding(
            "edit annotations"
        ):
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
        viewer = self.query_one("#viewer", VimTextArea)
        viewer.cancel_vim_command()
        self._refresh_status()
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
        annotator = self.query_one("#annotator", AnnotationTextArea)
        annotator.cancel_vim_command()
        self._refresh_status()
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

    @on(HalfPageBottomScrollTextArea.CommandStateChanged)
    def on_vim_command_state_changed(
        self, _event: HalfPageBottomScrollTextArea.CommandStateChanged
    ) -> None:
        self._refresh_status()

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
        self._start_typed_annotation(
            AnnotationType.NOTE,
            selection=event.selection,
        )

    def _start_typed_annotation(
        self,
        annotation_type: AnnotationType,
        *,
        selection: Optional[Selection] = None,
    ) -> None:
        """Prepare a note, highlight, or cursor-position bookmark."""

        if not self._require_confirmed_source_binding("add annotations"):
            return
        if self._dirty_panes():
            self.notify(
                "Save or discard text edits before adding an annotation",
                severity="warning",
                timeout=3,
            )
            return

        annotation_type = AnnotationType(annotation_type)
        viewer = self.query_one("#viewer", VimTextArea)
        bookmark_position: Optional[int] = None
        if annotation_type is AnnotationType.BOOKMARK:
            source_text = self.source.text
            if not source_text:
                self.notify(
                    "Cannot bookmark an empty source",
                    severity="warning",
                    timeout=2,
                )
                return
            bookmark_position = location_to_offset(
                source_text, viewer.cursor_location
            )
            if bookmark_position < len(source_text):
                start = bookmark_position
                end = start + 1
            else:
                start = len(source_text) - 1
                end = len(source_text)
            draft = AnnotationDraft(start, end, source_text[start:end])
        else:
            current_selection = selection or viewer.selection
            try:
                draft = draft_from_selection(
                    self.source.text,
                    current_selection.start,
                    current_selection.end,
                )
            except ValueError:
                self.notify(
                    f"Select source text before adding a {annotation_type.value}",
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

        if annotation_type is AnnotationType.NOTE:
            self.push_screen(
                NoteModal(draft),
                callback=lambda note: self._finish_annotation(
                    draft,
                    note,
                    pdf_anchor,
                    annotation_type=annotation_type,
                ),
            )
            return

        self._finish_annotation(
            draft,
            "",
            pdf_anchor,
            annotation_type=annotation_type,
            bookmark_position=bookmark_position,
        )

    def _finish_annotation(
        self,
        draft: AnnotationDraft,
        note: Optional[str],
        pdf_anchor: Optional[PdfAnchor] = None,
        *,
        annotation_type: AnnotationType = AnnotationType.NOTE,
        bookmark_position: Optional[int] = None,
    ) -> None:
        viewer = self.query_one("#viewer", VimTextArea)
        if note is None:
            viewer.focus()
            return

        record = create_record(
            draft,
            note,
            annotation_type=annotation_type,
            bookmark_position=bookmark_position,
            source_text=self.source.text,
            pdf_anchor=pdf_anchor,
        )
        updated = self.annotation_store.append_to_text(
            self.query_one(
                "#annotator", AnnotationTextArea
            ).canonical_text,
            (record,),
        )
        self._replace_annotation_document(updated, mark_saved=False)
        annotator = self.query_one("#annotator", AnnotationTextArea)
        annotator.set_annotation_type_filter(self.annotation_type_filter)
        visible_links = self._valid_annotation_links()
        if visible_links and self.annotation_links[-1] in visible_links:
            newest_link = self.annotation_links[-1]
            annotator.show_link(newest_link, move_cursor=True)
            viewer.set_active_annotation_range(newest_link.source_range)
        elif self.annotation_links[-1] not in visible_links:
            annotator.clear_active_link()
            viewer.set_active_annotation_range(None)
        viewer.action_cancel_visual()
        viewer.focus()
        self._request_save("save")
        if "annotations" not in self._dirty_panes():
            self.notify(
                f"Saved {annotation_type.value} for range "
                f"[{draft.start}, {draft.end})"
                + (
                    " · hidden by the current filter"
                    if self.annotation_links[-1] not in visible_links
                    else ""
                ),
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

