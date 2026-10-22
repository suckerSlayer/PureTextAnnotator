"""Human-readable sidecar annotation files and source range utilities."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


Location = Tuple[int, int]
ANNOTATION_MARKER = "<!-- pta:annotation "


class AnnotationFileError(RuntimeError):
    """Raised when a sidecar annotation file cannot be read or written."""


@dataclass(frozen=True)
class AnnotationDraft:
    """A selected source range waiting for the user's note."""

    start: int
    end: int
    quote: str


@dataclass(frozen=True)
class AnnotationRange:
    """A persisted, zero-based half-open source range."""

    start: int
    end: int


@dataclass(frozen=True)
class AnnotationLink:
    """Connect a source range to its block in the sidecar document."""

    source_range: AnnotationRange
    sidecar_start_line: int
    sidecar_end_line: int
    sidecar_metadata_end_line: int
    sidecar_quote_start_line: Optional[int]
    sidecar_quote_end_line: Optional[int]
    sidecar_note_line: int
    sidecar_note_start_line: Optional[int]
    sidecar_note_end_line: Optional[int]


@dataclass(frozen=True)
class AnnotationRecord:
    """One persistent note associated with a source range."""

    annotation_id: str
    start: int
    end: int
    quote: str
    note: str
    created_at: str


def parse_annotation_links(annotation_text: str) -> Tuple[AnnotationLink, ...]:
    """Extract source-to-sidecar links from annotation metadata blocks.

    Only the first YAML metadata block inside each Pure Text Annotator marker is
    considered. Markdown examples in a user's note are therefore never treated
    as annotation metadata.
    """

    links = []
    in_annotation = False
    in_metadata = False
    metadata_seen = False
    in_range = False
    sidecar_start_line: Optional[int] = None
    sidecar_metadata_end_line: Optional[int] = None
    sidecar_original_text_line: Optional[int] = None
    sidecar_note_line: Optional[int] = None
    range_start: Optional[int] = None
    range_end: Optional[int] = None

    lines = annotation_text.splitlines()
    for line_number, line in enumerate(lines):
        stripped = line.strip()

        if line.startswith(ANNOTATION_MARKER):
            in_annotation = True
            in_metadata = False
            metadata_seen = False
            in_range = False
            sidecar_start_line = line_number
            sidecar_metadata_end_line = None
            sidecar_original_text_line = None
            sidecar_note_line = None
            range_start = None
            range_end = None
            continue

        if not in_annotation:
            continue

        if stripped == "<!-- /pta:annotation -->":
            if (
                range_start is not None
                and range_end is not None
                and 0 <= range_start < range_end
                and sidecar_start_line is not None
            ):
                quote_start_line: Optional[int] = None
                quote_end_line: Optional[int] = None
                if (
                    sidecar_original_text_line is not None
                    and sidecar_note_line is not None
                    and sidecar_original_text_line < sidecar_note_line
                ):
                    content_start = sidecar_original_text_line + 1
                    while (
                        content_start < sidecar_note_line
                        and not lines[content_start].strip()
                    ):
                        content_start += 1
                    content_end = sidecar_note_line - 1
                    while (
                        content_end >= content_start
                        and not lines[content_end].strip()
                    ):
                        content_end -= 1
                    if content_start <= content_end:
                        quote_start_line = content_start
                        quote_end_line = content_end

                note_start_line: Optional[int] = None
                note_end_line: Optional[int] = None
                if sidecar_note_line is not None:
                    content_start = sidecar_note_line + 1
                    while (
                        content_start < line_number
                        and not lines[content_start].strip()
                    ):
                        content_start += 1
                    content_end = line_number - 1
                    while (
                        content_end >= content_start
                        and not lines[content_end].strip()
                    ):
                        content_end -= 1
                    if content_start <= content_end:
                        note_start_line = content_start
                        note_end_line = content_end

                links.append(
                    AnnotationLink(
                        source_range=AnnotationRange(range_start, range_end),
                        sidecar_start_line=sidecar_start_line,
                        sidecar_end_line=line_number,
                        sidecar_metadata_end_line=(
                            sidecar_metadata_end_line
                            if sidecar_metadata_end_line is not None
                            else sidecar_start_line
                        ),
                        sidecar_quote_start_line=quote_start_line,
                        sidecar_quote_end_line=quote_end_line,
                        sidecar_note_line=(
                            sidecar_note_line
                            if sidecar_note_line is not None
                            else sidecar_start_line
                        ),
                        sidecar_note_start_line=note_start_line,
                        sidecar_note_end_line=note_end_line,
                    )
                )
            in_annotation = False
            continue

        if stripped == "```yaml" and not metadata_seen:
            in_metadata = True
            continue

        if stripped == "```" and in_metadata:
            in_metadata = False
            metadata_seen = True
            in_range = False
            sidecar_metadata_end_line = line_number
            continue

        if (
            not in_metadata
            and stripped == "### Original text"
            and sidecar_original_text_line is None
        ):
            sidecar_original_text_line = line_number
            continue

        if (
            not in_metadata
            and stripped == "### Note"
            and sidecar_note_line is None
        ):
            sidecar_note_line = line_number
            continue

        if not in_metadata:
            continue

        if stripped == "range:":
            in_range = True
            range_start = None
            range_end = None
            continue

        if not in_range:
            continue

        if stripped.startswith("start:"):
            value = stripped.partition(":")[2].strip()
            if value.isdecimal():
                range_start = int(value)
            continue

        if stripped.startswith("end:"):
            value = stripped.partition(":")[2].strip()
            if value.isdecimal():
                range_end = int(value)
            continue

        if line and not line[0].isspace():
            in_range = False

    return tuple(links)


def parse_annotation_ranges(annotation_text: str) -> Tuple[AnnotationRange, ...]:
    """Extract valid source ranges from annotation metadata blocks."""

    return tuple(
        link.source_range for link in parse_annotation_links(annotation_text)
    )


def location_to_offset(text: str, location: Location) -> int:
    """Convert a Textual ``(row, column)`` location to a code-point offset."""

    row, column = location
    lines = text.split("\n")
    if row < 0 or row >= len(lines):
        raise ValueError(f"row {row} is outside the document")
    if column < 0 or column > len(lines[row]):
        raise ValueError(f"column {column} is outside row {row}")
    return sum(len(line) + 1 for line in lines[:row]) + column


def draft_from_selection(
    text: str, selection_start: Location, selection_end: Location
) -> AnnotationDraft:
    """Create a normalized half-open range from a possibly reversed selection."""

    first, last = sorted((selection_start, selection_end))
    start = location_to_offset(text, first)
    end = location_to_offset(text, last)
    if start == end:
        raise ValueError("cannot annotate an empty selection")
    return AnnotationDraft(start=start, end=end, quote=text[start:end])


def create_record(
    draft: AnnotationDraft,
    note: str,
    *,
    annotation_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> AnnotationRecord:
    """Turn a selected range and note into a persistent record."""

    if not note.strip():
        raise ValueError("annotation note cannot be empty")
    timestamp = created_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    return AnnotationRecord(
        annotation_id=annotation_id or str(uuid.uuid4()),
        start=draft.start,
        end=draft.end,
        quote=draft.quote,
        note=note.rstrip(),
        created_at=timestamp,
    )


class AnnotationStore:
    """Read and append annotations in a Markdown sidecar file."""

    MARKER = ANNOTATION_MARKER

    def __init__(self, source_path: Path, sidecar_path: Optional[Path] = None) -> None:
        self.source_path = source_path.resolve()
        self.path = (
            sidecar_path.resolve()
            if sidecar_path is not None
            else self.source_path.with_name(self.source_path.name + ".annotations.md")
        )

    def initial_text(self) -> str:
        source_name = json.dumps(self.source_path.name, ensure_ascii=False)
        return (
            "---\n"
            "format: pure-text-annotator\n"
            "version: 1\n"
            f"source: {source_name}\n"
            "range_unit: unicode-code-point\n"
            "range_convention: zero-based-half-open\n"
            "---\n\n"
            "# Annotations\n"
        )

    def read_text(self) -> str:
        """Return the exact sidecar text or the initial unsaved document."""

        if not self.path.exists():
            return self.initial_text()
        if not self.path.is_file():
            raise AnnotationFileError(f"annotation path is not a file: {self.path}")
        try:
            raw = self.path.read_bytes()
        except OSError as error:
            raise AnnotationFileError(
                f"cannot read annotation file {self.path}: {error}"
            ) from error
        if b"\x00" in raw:
            raise AnnotationFileError(
                f"annotation file appears to be binary: {self.path}"
            )
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise AnnotationFileError(
                f"annotation file is not valid UTF-8: {self.path}"
            ) from error

    @staticmethod
    def _quote_block(quote: str) -> str:
        return "\n".join(f"> {line}" if line else ">" for line in quote.split("\n"))

    def _render_record(self, record: AnnotationRecord, ordinal: int) -> str:
        annotation_id = json.dumps(record.annotation_id, ensure_ascii=False)
        created_at = json.dumps(record.created_at, ensure_ascii=False)
        quote = self._quote_block(record.quote)
        return (
            f"{self.MARKER}id={annotation_id} -->\n"
            f"## Annotation {ordinal}\n\n"
            "```yaml\n"
            f"id: {annotation_id}\n"
            "type: note\n"
            "range:\n"
            f"  start: {record.start}\n"
            f"  end: {record.end}\n"
            f"created_at: {created_at}\n"
            "```\n\n"
            "### Original text\n\n"
            f"{quote}\n\n"
            "### Note\n\n"
            f"{record.note}\n\n"
            "<!-- /pta:annotation -->\n"
        )

    @staticmethod
    def _with_blank_line(text: str) -> str:
        if text.endswith("\n\n"):
            return text
        if text.endswith("\n"):
            return text + "\n"
        return text + "\n\n"

    def append(self, record: AnnotationRecord) -> str:
        """Append a record atomically and return the complete saved document."""

        current = self.read_text()
        ordinal = current.count(self.MARKER) + 1
        updated = self._with_blank_line(current) + self._render_record(record, ordinal)
        self._atomic_write(updated)
        return updated

    def _atomic_write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(text)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise AnnotationFileError(
                f"cannot save annotation file {self.path}: {error}"
            ) from error
