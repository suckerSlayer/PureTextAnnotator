"""Human-readable sidecar annotation files and source range utilities."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from .file_safety import (
    ConcurrentFileChangeError,
    DiskFingerprint,
    DiskFingerprintError,
    fingerprint_bytes,
    fingerprint_path,
    require_unchanged,
)


Location = Tuple[int, int]
PdfRectangle = Tuple[float, float, float, float]
PdfQuad = Tuple[float, float, float, float, float, float, float, float]
ANNOTATION_MARKER = "<!-- pta:annotation "


class AnnotationType(str, Enum):
    """The three annotation kinds required by the project specification."""

    NOTE = "note"
    HIGHLIGHT = "highlight"
    BOOKMARK = "bookmark"

    @property
    def label(self) -> str:
        """Return a compact user-facing label."""

        return self.value.capitalize()


class AnnotationFileError(RuntimeError):
    """Raised when a sidecar annotation file cannot be read or written."""


@dataclass(frozen=True)
class SourceBinding:
    """Association declared by a managed sidecar front matter block."""

    managed: bool
    valid: bool
    recorded_reference: Optional[str]
    recorded_path: Optional[Path]
    current_path: Path
    matches: bool
    error: Optional[str] = None


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
    annotation_id: str = ""
    annotation_type: AnnotationType = AnnotationType.NOTE
    bookmark_position: Optional[int] = None


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
class PdfImportMetadata:
    """Provenance retained when a PDF text-markup annotation is imported."""

    version: int
    source_document_sha256: str
    fingerprint: str
    subtype: str
    page_index: int
    annotation_name: Optional[str]
    author: Optional[str]
    subject: Optional[str]
    creation_date: Optional[str]
    modified_date: Optional[str]
    mapping_confidence: int
    generated_note: bool
    color: Tuple[float, ...]
    rect_pdf: PdfRectangle
    quad_points_pdf: Tuple[PdfQuad, ...]


@dataclass(frozen=True)
class PdfSyncMetadata:
    """Identity of a sidecar record confirmed to exist in its source PDF."""

    version: int
    origin: str
    annotation_name: Optional[str]
    fingerprint: str
    last_seen_document_sha256: str
    pdf_state_sha256: str
    sidecar_state_sha256: str


@dataclass(frozen=True)
class AnnotationRecord:
    """One persistent note associated with a source range."""

    annotation_id: str
    start: int
    end: int
    quote: str
    note: str
    created_at: str
    annotation_type: AnnotationType = AnnotationType.NOTE
    bookmark_position: Optional[int] = None
    source_sha256: Optional[str] = None
    quote_sha256: Optional[str] = None
    prefix: Optional[str] = None
    suffix: Optional[str] = None
    original_start: Optional[int] = None
    original_end: Optional[int] = None
    pdf_anchor: Optional[PdfAnchor] = None
    pdf_import: Optional[PdfImportMetadata] = None
    pdf_sync: Optional[PdfSyncMetadata] = None


def source_reference_for(source_path: Path, sidecar_path: Path) -> str:
    """Return a portable path relative to the sidecar when possible."""

    source = source_path.expanduser().resolve()
    sidecar_parent = sidecar_path.expanduser().resolve().parent
    try:
        relative = os.path.relpath(source, start=sidecar_parent)
    except ValueError:
        # Different Windows drives cannot be represented relatively.
        return source.as_posix()
    return Path(relative).as_posix()


def _managed_front_matter_lines(
    annotation_text: str,
) -> Tuple[list[str], Optional[int]]:
    """Return physical lines and the managed front matter closing index."""

    lines = annotation_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return lines, None
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:101], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if closing is None:
        return lines, None
    format_values = [
        line.partition(":")[2].strip().strip('"\'')
        for line in lines[1:closing]
        if line.startswith("format:")
    ]
    if format_values != ["pure-text-annotator"]:
        return lines, None
    return lines, closing


def inspect_source_binding(
    annotation_text: str,
    *,
    sidecar_path: Path,
    current_source_path: Path,
) -> SourceBinding:
    """Inspect a managed sidecar's recorded source without changing it."""

    current = current_source_path.expanduser().resolve()
    sidecar = sidecar_path.expanduser().resolve()
    lines, closing = _managed_front_matter_lines(annotation_text)
    if closing is None:
        return SourceBinding(
            managed=False,
            valid=True,
            recorded_reference=None,
            recorded_path=None,
            current_path=current,
            matches=True,
        )
    source_lines = [
        line for line in lines[1:closing] if line.startswith("source:")
    ]
    if len(source_lines) != 1:
        return SourceBinding(
            managed=True,
            valid=False,
            recorded_reference=None,
            recorded_path=None,
            current_path=current,
            matches=False,
            error="managed sidecar must contain exactly one source field",
        )
    raw_reference = source_lines[0].partition(":")[2].strip()
    try:
        reference = json.loads(raw_reference)
    except json.JSONDecodeError:
        reference = None
    if not isinstance(reference, str) or not reference:
        return SourceBinding(
            managed=True,
            valid=False,
            recorded_reference=None,
            recorded_path=None,
            current_path=current,
            matches=False,
            error="managed sidecar source field must be a JSON string",
        )
    reference_path = Path(reference.replace("/", os.sep)).expanduser()
    recorded = (
        reference_path.resolve()
        if reference_path.is_absolute()
        else (sidecar.parent / reference_path).resolve()
    )
    return SourceBinding(
        managed=True,
        valid=True,
        recorded_reference=reference,
        recorded_path=recorded,
        current_path=current,
        matches=recorded == current,
    )


def rewrite_source_binding(
    annotation_text: str,
    *,
    sidecar_path: Path,
    current_source_path: Path,
) -> str:
    """Rewrite only the managed front matter's source reference."""

    binding = inspect_source_binding(
        annotation_text,
        sidecar_path=sidecar_path,
        current_source_path=current_source_path,
    )
    if not binding.managed:
        raise ValueError("annotation file has no managed PTA front matter")
    if not binding.valid:
        raise ValueError(binding.error or "invalid annotation source binding")
    lines, closing = _managed_front_matter_lines(annotation_text)
    assert closing is not None
    source_indexes = [
        index
        for index, line in enumerate(lines[1:closing], start=1)
        if line.startswith("source:")
    ]
    assert len(source_indexes) == 1
    index = source_indexes[0]
    old_line = lines[index]
    content = old_line.rstrip("\r\n")
    newline = old_line[len(content) :] or "\n"
    reference = source_reference_for(current_source_path, sidecar_path)
    lines[index] = (
        "source: " + json.dumps(reference, ensure_ascii=False) + newline
    )
    return "".join(lines)


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


def render_pdf_import(metadata: PdfImportMetadata) -> str:
    """Render deterministic provenance for one imported PDF annotation."""

    optional_strings = (
        ("annotation_name", metadata.annotation_name),
        ("author", metadata.author),
        ("subject", metadata.subject),
        ("creation_date", metadata.creation_date),
        ("modified_date", metadata.modified_date),
    )
    lines = [
        "pdf_import:",
        f"  version: {metadata.version}",
        "  source_document_sha256: "
        + json.dumps(metadata.source_document_sha256),
        f"  fingerprint: {json.dumps(metadata.fingerprint)}",
        f"  subtype: {json.dumps(metadata.subtype, ensure_ascii=False)}",
        f"  page_index: {metadata.page_index}",
    ]
    lines.extend(
        f"  {name}: "
        + (
            "null"
            if value is None
            else json.dumps(value, ensure_ascii=False)
        )
        for name, value in optional_strings
    )
    lines.extend(
        [
            f"  mapping_confidence: {metadata.mapping_confidence}",
            "  generated_note: "
            + ("true" if metadata.generated_note else "false"),
            "  color: " + _pdf_number_list(metadata.color),
            "  rect_pdf: " + _pdf_number_list(metadata.rect_pdf),
            "  quad_points_pdf:",
        ]
    )
    lines.extend(
        "    - " + _pdf_number_list(quad)
        for quad in metadata.quad_points_pdf
    )
    return "\n".join(lines) + "\n"


def render_pdf_sync(metadata: PdfSyncMetadata) -> str:
    """Render the durable PDF/sidecar identity used for reconciliation."""

    return (
        "pdf_sync:\n"
        f"  version: {metadata.version}\n"
        f"  origin: {json.dumps(metadata.origin)}\n"
        "  annotation_name: "
        + (
            "null"
            if metadata.annotation_name is None
            else json.dumps(metadata.annotation_name, ensure_ascii=False)
        )
        + "\n"
        f"  fingerprint: {json.dumps(metadata.fingerprint)}\n"
        "  last_seen_document_sha256: "
        f"{json.dumps(metadata.last_seen_document_sha256)}\n"
        f"  pdf_state_sha256: {json.dumps(metadata.pdf_state_sha256)}\n"
        "  sidecar_state_sha256: "
        f"{json.dumps(metadata.sidecar_state_sha256)}\n"
    )


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
    annotation_id = ""
    annotation_type: Optional[AnnotationType] = AnnotationType.NOTE
    bookmark_position: Optional[int] = None

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
            annotation_id = ""
            annotation_type = AnnotationType.NOTE
            bookmark_position = None
            marker_id = line.partition("id=")[2].rpartition(" -->")[0]
            if marker_id:
                try:
                    parsed_id = json.loads(marker_id)
                    if isinstance(parsed_id, str):
                        annotation_id = parsed_id
                except json.JSONDecodeError:
                    pass
            continue

        if not in_annotation:
            continue

        if stripped == "<!-- /pta:annotation -->":
            if (
                range_start is not None
                and range_end is not None
                and 0 <= range_start < range_end
                and annotation_type is not None
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
                        annotation_id=annotation_id,
                        annotation_type=annotation_type,
                        bookmark_position=(
                            bookmark_position
                            if annotation_type is AnnotationType.BOOKMARK
                            and bookmark_position is not None
                            and range_start <= bookmark_position <= range_end
                            else range_start
                            if annotation_type is AnnotationType.BOOKMARK
                            else None
                        ),
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

        if not line.startswith((" ", "\t")) and stripped.startswith("id:"):
            raw_value = stripped.partition(":")[2].strip()
            try:
                parsed_id = json.loads(raw_value)
                if isinstance(parsed_id, str):
                    annotation_id = parsed_id
            except json.JSONDecodeError:
                pass
            continue

        if not line.startswith((" ", "\t")) and stripped.startswith("type:"):
            raw_value = stripped.partition(":")[2].strip().strip('"\'')
            try:
                annotation_type = AnnotationType(raw_value)
            except ValueError:
                annotation_type = None
            continue

        if not line.startswith((" ", "\t")) and stripped.startswith("position:"):
            value = stripped.partition(":")[2].strip()
            bookmark_position = int(value) if value.isdecimal() else None
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


def delete_annotation_block(
    annotation_text: str, link: AnnotationLink
) -> str:
    """Remove one parsed annotation block and renumber remaining headings.

    The link comes from :func:`parse_annotation_links`, so its line boundaries
    identify structural marker lines rather than Markdown that merely resembles
    a heading inside a user's note.
    """

    lines = annotation_text.splitlines(keepends=True)
    if not (
        0 <= link.sidecar_start_line <= link.sidecar_end_line < len(lines)
    ):
        raise ValueError("annotation block is outside the sidecar document")
    if not lines[link.sidecar_start_line].startswith(ANNOTATION_MARKER):
        raise ValueError("annotation block start marker no longer matches")
    if lines[link.sidecar_end_line].strip() != "<!-- /pta:annotation -->":
        raise ValueError("annotation block end marker no longer matches")

    block_start = sum(len(line) for line in lines[: link.sidecar_start_line])
    block_end = sum(len(line) for line in lines[: link.sidecar_end_line + 1])
    prefix = annotation_text[:block_start].rstrip("\r\n")
    suffix = annotation_text[block_end:].lstrip("\r\n")
    if prefix and suffix:
        updated = prefix + "\n\n" + suffix
    elif prefix:
        updated = prefix + "\n"
    else:
        updated = suffix

    return renumber_annotation_headings(updated)


def annotation_note_text(
    annotation_text: str, link: AnnotationLink
) -> str:
    """Return one parsed annotation's body without surrounding blank lines."""

    if (
        link.sidecar_note_start_line is None
        or link.sidecar_note_end_line is None
    ):
        return ""
    lines = annotation_text.splitlines()
    if not (
        0
        <= link.sidecar_note_start_line
        <= link.sidecar_note_end_line
        < len(lines)
    ):
        raise ValueError("annotation note is outside the sidecar document")
    return "\n".join(
        lines[
            link.sidecar_note_start_line : link.sidecar_note_end_line + 1
        ]
    )


def update_annotation_block(
    annotation_text: str,
    link: AnnotationLink,
    *,
    annotation_type: AnnotationType,
    note: str,
) -> str:
    """Replace one record's type and body while preserving its stable identity."""

    annotation_type = AnnotationType(annotation_type)
    note = note.rstrip()
    if annotation_type is AnnotationType.NOTE and not note.strip():
        raise ValueError("annotation note cannot be empty")

    lines = annotation_text.splitlines(keepends=True)
    if not (
        0 <= link.sidecar_start_line <= link.sidecar_end_line < len(lines)
    ):
        raise ValueError("annotation block is outside the sidecar document")
    block_start = sum(len(line) for line in lines[: link.sidecar_start_line])
    block_end = sum(len(line) for line in lines[: link.sidecar_end_line + 1])
    block = annotation_text[block_start:block_end]
    if not block.startswith(ANNOTATION_MARKER):
        raise ValueError("annotation block start marker no longer matches")

    fence_open = re.search(r"(?m)^```yaml[ \t]*(?:\r?\n|$)", block)
    if fence_open is None:
        raise ValueError("annotation metadata block is missing")
    fence_close_relative = re.search(
        r"(?m)^```[ \t]*(?:\r?\n|$)", block[fence_open.end() :]
    )
    if fence_close_relative is None:
        raise ValueError("annotation metadata terminator is missing")
    metadata_start = fence_open.end()
    metadata_end = metadata_start + fence_close_relative.start()
    metadata = block[metadata_start:metadata_end]
    metadata, type_count = re.subn(
        r"(?m)^type:[ \t]*[^\r\n]+[ \t]*$",
        f"type: {annotation_type.value}",
        metadata,
    )
    if type_count != 1:
        raise ValueError("annotation type metadata is ambiguous")
    metadata = re.sub(
        r"(?m)^position:[ \t]*\d+[ \t]*(?:\r?\n|$)",
        "",
        metadata,
    )
    if annotation_type is AnnotationType.BOOKMARK:
        position = (
            link.bookmark_position
            if link.bookmark_position is not None
            else link.source_range.start
        )
        type_line = re.search(r"(?m)^type:[^\r\n]*(?:\r?\n|$)", metadata)
        assert type_line is not None
        metadata = (
            metadata[: type_line.end()]
            + f"position: {position}\n"
            + metadata[type_line.end() :]
        )
    block = block[:metadata_start] + metadata + block[metadata_end:]

    note_heading = re.search(
        r"(?m)^### Note[ \t]*(?:\r?\n|$)", block
    )
    close_marker = re.search(r"(?m)^<!-- /pta:annotation -->", block)
    if note_heading is None or close_marker is None:
        raise ValueError("annotation note section is missing")
    updated_block = (
        block[: note_heading.end()].rstrip("\r\n")
        + "\n\n"
        + (note + "\n\n" if note else "\n")
        + block[close_marker.start() :]
    )
    return annotation_text[:block_start] + updated_block + annotation_text[block_end:]


def renumber_annotation_headings(annotation_text: str) -> str:
    """Renumber structural ``## Annotation`` headings in document order."""

    renumbered = annotation_text.splitlines(keepends=True)
    ordinal = 0
    waiting_for_heading = False
    for index, line in enumerate(renumbered):
        if line.startswith(ANNOTATION_MARKER):
            ordinal += 1
            waiting_for_heading = True
            continue
        if line.strip() == "<!-- /pta:annotation -->":
            waiting_for_heading = False
            continue
        if waiting_for_heading and line.startswith("## Annotation "):
            content = line.rstrip("\r\n")
            newline = line[len(content) :]
            renumbered[index] = f"## Annotation {ordinal}{newline}"
            waiting_for_heading = False
        elif waiting_for_heading and line.strip() == "```yaml":
            waiting_for_heading = False
    return "".join(renumbered)


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
    annotation_type: AnnotationType = AnnotationType.NOTE,
    bookmark_position: Optional[int] = None,
    annotation_id: Optional[str] = None,
    created_at: Optional[str] = None,
    source_text: Optional[str] = None,
    pdf_anchor: Optional[PdfAnchor] = None,
    pdf_import: Optional[PdfImportMetadata] = None,
    pdf_sync: Optional[PdfSyncMetadata] = None,
) -> AnnotationRecord:
    """Turn a source anchor into a persistent typed annotation record."""

    annotation_type = AnnotationType(annotation_type)
    if annotation_type is AnnotationType.NOTE and not note.strip():
        raise ValueError("annotation note cannot be empty")
    if annotation_type is AnnotationType.BOOKMARK:
        if bookmark_position is None:
            bookmark_position = draft.start
        if not draft.start <= bookmark_position <= draft.end:
            raise ValueError("bookmark position is outside its source anchor")
    elif bookmark_position is not None:
        raise ValueError("only bookmarks can store a bookmark position")
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
        annotation_type=annotation_type,
        bookmark_position=bookmark_position,
        pdf_anchor=pdf_anchor,
        pdf_import=pdf_import,
        pdf_sync=pdf_sync,
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
        source_reference = json.dumps(
            source_reference_for(self.source_path, self.path),
            ensure_ascii=False,
        )
        return (
            "---\n"
            "format: pure-text-annotator\n"
            "version: 1\n"
            f"source: {source_reference}\n"
            "range_unit: unicode-code-point\n"
            "range_convention: zero-based-half-open\n"
            "---\n\n"
            "# Annotations\n"
        )

    def read_text(self) -> str:
        """Return the exact sidecar text or the initial unsaved document."""

        text, _fingerprint = self.read_snapshot()
        return text

    def read_snapshot(self) -> Tuple[str, DiskFingerprint]:
        """Read text and exact fingerprint from the same byte snapshot."""

        if not self.path.exists():
            return self.initial_text(), DiskFingerprint.missing()
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
            return raw.decode("utf-8-sig"), fingerprint_bytes(raw)
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
        pdf_import = (
            render_pdf_import(record.pdf_import)
            if record.pdf_import is not None
            else ""
        )
        pdf_sync = (
            render_pdf_sync(record.pdf_sync)
            if record.pdf_sync is not None
            else ""
        )
        position = (
            f"position: {record.bookmark_position}\n"
            if record.annotation_type is AnnotationType.BOOKMARK
            and record.bookmark_position is not None
            else ""
        )
        return (
            f"{self.MARKER}id={annotation_id} -->\n"
            f"## Annotation {ordinal}\n\n"
            "```yaml\n"
            f"id: {annotation_id}\n"
            f"type: {record.annotation_type.value}\n"
            f"{position}"
            "range:\n"
            f"  start: {record.start}\n"
            f"  end: {record.end}\n"
            f"created_at: {created_at}\n"
            f"{anchor}"
            f"{pdf_anchor}"
            f"{pdf_import}"
            f"{pdf_sync}"
            "```\n\n"
            "### Original text\n\n"
            f"{quote}\n\n"
            "### Note\n\n"
            f"{record.note}\n\n"
            "<!-- /pta:annotation -->\n"
        )

    def render_record(self, record: AnnotationRecord, ordinal: int) -> str:
        """Render one canonical record block for transactional reconciliation."""

        return self._render_record(record, ordinal)

    @staticmethod
    def _with_blank_line(text: str) -> str:
        if text.endswith("\n\n"):
            return text
        if text.endswith("\n"):
            return text + "\n"
        return text + "\n\n"

    def append(
        self,
        record: AnnotationRecord,
        *,
        expected_fingerprint: Optional[DiskFingerprint] = None,
    ) -> str:
        """Append a record atomically and return the complete saved document."""

        return self.append_many(
            (record,), expected_fingerprint=expected_fingerprint
        )

    def append_many(
        self,
        records: Sequence[AnnotationRecord],
        *,
        expected_text: Optional[str] = None,
        base_text: Optional[str] = None,
        expected_fingerprint: Optional[DiskFingerprint] = None,
    ) -> str:
        """Append several records with one all-or-nothing sidecar replacement."""

        current = self.read_text()
        if expected_text is not None and current != expected_text:
            raise AnnotationFileError(
                "annotation sidecar changed while records were being prepared"
            )
        base = current if base_text is None else base_text
        if not records and base == current:
            return current
        updated = self.append_to_text(base, records)
        self._atomic_write(
            updated, expected_fingerprint=expected_fingerprint
        )
        return updated

    def append_to_text(
        self,
        base_text: str,
        records: Sequence[AnnotationRecord],
    ) -> str:
        """Render appended records without touching the disk."""

        if not records:
            return base_text
        first_ordinal = base_text.count(self.MARKER) + 1
        rendered = "\n\n".join(
            self._render_record(record, first_ordinal + offset).rstrip("\n")
            for offset, record in enumerate(records)
        )
        return self._with_blank_line(base_text) + rendered + "\n"

    def write(
        self,
        text: str,
        *,
        expected_fingerprint: Optional[DiskFingerprint] = None,
    ) -> str:
        """Atomically replace the complete annotation document."""

        self._atomic_write(
            text, expected_fingerprint=expected_fingerprint
        )
        return text

    def transactional_write(
        self,
        text: str,
        *,
        backup_label: str,
        validate: Callable[[str], None],
        expected_fingerprint: Optional[DiskFingerprint] = None,
    ) -> Tuple[str, Optional[Path]]:
        """Write, validate, and roll back while retaining an exact backup."""

        if expected_fingerprint is not None:
            require_unchanged(
                self.path,
                expected_fingerprint,
                label="annotation file",
            )
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

        written_fingerprint: Optional[DiskFingerprint] = None
        try:
            self._atomic_write(
                text, expected_fingerprint=expected_fingerprint
            )
            written_fingerprint = fingerprint_path(self.path)
            saved = self.read_text()
            validate(saved)
        except (ConcurrentFileChangeError, DiskFingerprintError):
            raise
        except Exception as error:
            if written_fingerprint is not None:
                try:
                    require_unchanged(
                        self.path,
                        written_fingerprint,
                        label="annotation file",
                    )
                except (
                    ConcurrentFileChangeError,
                    DiskFingerprintError,
                ) as concurrent_error:
                    raise AnnotationFileError(
                        "rematch validation failed, but the annotation file "
                        "changed again on disk; the newer disk bytes were not "
                        "overwritten"
                    ) from concurrent_error
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

    def _atomic_write(
        self,
        text: str,
        *,
        expected_fingerprint: Optional[DiskFingerprint] = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Optional[Path] = None
        try:
            if expected_fingerprint is not None:
                require_unchanged(
                    self.path,
                    expected_fingerprint,
                    label="annotation file",
                )
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
            if expected_fingerprint is not None:
                require_unchanged(
                    self.path,
                    expected_fingerprint,
                    label="annotation file",
                )
            os.replace(temporary_path, self.path)
            temporary_path = None
        except (ConcurrentFileChangeError, DiskFingerprintError):
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise AnnotationFileError(
                f"cannot save annotation file {self.path}: {error}"
            ) from error
