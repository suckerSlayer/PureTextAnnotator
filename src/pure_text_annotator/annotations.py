"""Human-readable sidecar annotation files and source range utilities."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple


Location = Tuple[int, int]
PdfRectangle = Tuple[float, float, float, float]
PdfQuad = Tuple[float, float, float, float, float, float, float, float]
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
class PdfAnchorFragment:
    """One page-local piece of a selected PDF text range."""

    page_index: int
    page_label: Optional[str]
    page_text_sha256: str
    text_start: int
    text_end: int
    crop_box: PdfRectangle
    rotation: int
    rect_pdf: PdfRectangle
    quad_points_pdf: Tuple[PdfQuad, ...]


@dataclass(frozen=True)
class PdfAnchor:
    """Optional geometry retained for safe PDF highlight export."""

    version: int
    document_sha256: str
    extractor_name: str
    extractor_version: str
    extraction_profile: str
    coordinate_system: str
    fragments: Tuple[PdfAnchorFragment, ...]


@dataclass(frozen=True)
class AnnotationRecord:
    """One persistent note associated with a source range."""

    annotation_id: str
    start: int
    end: int
    quote: str
    note: str
    created_at: str
    source_sha256: Optional[str] = None
    quote_sha256: Optional[str] = None
    prefix: Optional[str] = None
    suffix: Optional[str] = None
    original_start: Optional[int] = None
    original_end: Optional[int] = None
    pdf_anchor: Optional[PdfAnchor] = None


def _pdf_number_list(values: Tuple[float, ...]) -> str:
    """Render compact, deterministic YAML flow sequences."""

    rounded = [round(float(value), 4) for value in values]
    return json.dumps(rounded, ensure_ascii=False)


def render_pdf_anchor(anchor: PdfAnchor) -> str:
    """Render one complete PDF geometry anchor as deterministic YAML."""

    lines = [
        "pdf_anchor:",
        f"  version: {anchor.version}",
        f"  document_sha256: {json.dumps(anchor.document_sha256)}",
        "  extractor:",
        f"    name: {json.dumps(anchor.extractor_name)}",
        f"    version: {json.dumps(anchor.extractor_version)}",
        f"    profile: {json.dumps(anchor.extraction_profile)}",
        f"  coordinate_system: {json.dumps(anchor.coordinate_system)}",
        "  fragments:",
    ]
    for fragment in anchor.fragments:
        lines.extend(
            [
                f"    - page_index: {fragment.page_index}",
                "      page_label: "
                + (
                    "null"
                    if fragment.page_label is None
                    else json.dumps(fragment.page_label, ensure_ascii=False)
                ),
                "      page_text_sha256: "
                + json.dumps(fragment.page_text_sha256),
                "      text_range:",
                f"        start: {fragment.text_start}",
                f"        end: {fragment.text_end}",
                "      crop_box: " + _pdf_number_list(fragment.crop_box),
                f"      rotation: {fragment.rotation}",
                "      rect_pdf: " + _pdf_number_list(fragment.rect_pdf),
                "      quad_points_pdf:",
            ]
        )
        lines.extend(
            "        - " + _pdf_number_list(quad)
            for quad in fragment.quad_points_pdf
        )
    return "\n".join(lines) + "\n"


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
    source_text: Optional[str] = None,
    pdf_anchor: Optional[PdfAnchor] = None,
) -> AnnotationRecord:
    """Turn a selected range and note into a persistent record."""

    if not note.strip():
        raise ValueError("annotation note cannot be empty")
    timestamp = created_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    anchor = {}
    if source_text is not None:
        if not 0 <= draft.start < draft.end <= len(source_text):
            raise ValueError("annotation range is outside the source text")
        if source_text[draft.start : draft.end] != draft.quote:
            raise ValueError("annotation quote does not match the source range")
        anchor = {
            "source_sha256": hashlib.sha256(
                source_text.encode("utf-8")
            ).hexdigest(),
            "quote_sha256": hashlib.sha256(
                draft.quote.encode("utf-8")
            ).hexdigest(),
            "prefix": source_text[max(0, draft.start - 80) : draft.start],
            "suffix": source_text[draft.end : draft.end + 80],
            "original_start": draft.start,
            "original_end": draft.end,
        }
    return AnnotationRecord(
        annotation_id=annotation_id or str(uuid.uuid4()),
        start=draft.start,
        end=draft.end,
        quote=draft.quote,
        note=note.rstrip(),
        created_at=timestamp,
        pdf_anchor=pdf_anchor,
        **anchor,
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
        anchor = ""
        if (
            record.source_sha256 is not None
            and record.quote_sha256 is not None
            and record.prefix is not None
            and record.suffix is not None
            and record.original_start is not None
            and record.original_end is not None
        ):
            anchor = (
                "anchor:\n"
                f"  source_sha256: {json.dumps(record.source_sha256)}\n"
                f"  quote_sha256: {json.dumps(record.quote_sha256)}\n"
                f"  prefix: {json.dumps(record.prefix, ensure_ascii=False)}\n"
                f"  suffix: {json.dumps(record.suffix, ensure_ascii=False)}\n"
                f"  original_start: {record.original_start}\n"
                f"  original_end: {record.original_end}\n"
            )
        pdf_anchor = (
            render_pdf_anchor(record.pdf_anchor)
            if record.pdf_anchor is not None
            else ""
        )
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
            f"{anchor}"
            f"{pdf_anchor}"
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

    def write(self, text: str) -> str:
        """Atomically replace the complete annotation document."""

        self._atomic_write(text)
        return text

    def transactional_write(
        self,
        text: str,
        *,
        backup_label: str,
        validate: Callable[[str], None],
    ) -> Tuple[str, Optional[Path]]:
        """Write, validate, and roll back while retaining an exact backup."""

        original_exists = self.path.exists()
        original = self.path.read_bytes() if original_exists else b""
        backup_path: Optional[Path] = None
        if original_exists:
            candidate = self.path.with_name(
                f"{self.path.name}.{backup_label}"
            )
            counter = 2
            while candidate.exists():
                candidate = self.path.with_name(
                    f"{self.path.name}.{backup_label}-{counter}"
                )
                counter += 1
            try:
                self._atomic_write_bytes(candidate, original)
            except OSError as error:
                raise AnnotationFileError(
                    f"cannot create annotation backup {candidate}: {error}"
                ) from error
            backup_path = candidate

        try:
            self._atomic_write(text)
            saved = self.read_text()
            validate(saved)
        except Exception as error:
            try:
                if original_exists:
                    self._atomic_write_bytes(self.path, original)
                else:
                    self.path.unlink(missing_ok=True)
            except OSError as rollback_error:
                raise AnnotationFileError(
                    "rematch validation failed and rollback also failed: "
                    f"{rollback_error}"
                ) from error
            if isinstance(error, AnnotationFileError):
                raise
            raise AnnotationFileError(
                f"rematch validation failed; original sidecar restored: {error}"
            ) from error
        return saved, backup_path

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        """Atomically replace ``path`` with exact bytes."""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        except OSError:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

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
