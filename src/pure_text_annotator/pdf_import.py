"""Import standard PDF text-markup annotations into the Markdown sidecar."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml
from pypdf import PdfReader
from pypdf.generic import DictionaryObject

from .annotations import (
    AnnotationDraft,
    AnnotationFileError,
    AnnotationRecord,
    AnnotationStore,
    PdfAnchor,
    PdfImportMetadata,
    PdfQuad,
    PdfRectangle,
    PdfSyncMetadata,
    create_record,
    renumber_annotation_headings,
    render_pdf_anchor,
)
from .files import LoadedTextFile, TextFileError, load_source_file
from .pdfs import PdfDocument, PdfWordBox
from .rematch import ParsedAnnotation, parse_annotation_records, text_sha256


SUPPORTED_TEXT_MARKUP_SUBTYPES = frozenset(
    {"Highlight", "Underline", "Squiggly", "StrikeOut"}
)
PTA_ANNOTATION_AUTHOR = "Pure Text Annotator"
PTA_ANNOTATION_SUBJECT = "Pure Text Annotator note"
IMPORT_ID_PREFIX = "pdf-import-"
MIN_BOX_OVERLAP = 0.55
MIN_MAPPING_CONFIDENCE = 75
MAX_QUAD_COUNT = 10_000
_PDF_DATE_RE = re.compile(
    r"^D:(?P<year>\d{4})"
    r"(?P<month>\d{2})?"
    r"(?P<day>\d{2})?"
    r"(?P<hour>\d{2})?"
    r"(?P<minute>\d{2})?"
    r"(?P<second>\d{2})?"
    r"(?P<zone>Z|z|[+-]\d{2}'?\d{2}'?)?$"
)


class PdfImportError(RuntimeError):
    """Raised when a PDF annotation import cannot be performed safely."""


class PdfConflictKind(str, Enum):
    """Why one PDF annotation needs an explicit reconciliation decision."""

    BOTH_MODIFIED = "both-modified"
    DELETED_FROM_PDF = "deleted-from-pdf"


class PdfConflictDecision(str, Enum):
    """Which representation wins an explicitly reviewed PDF conflict."""

    KEEP_MARKDOWN = "keep-markdown"
    KEEP_PDF = "keep-pdf"


@dataclass(frozen=True)
class PdfAnnotationConflict:
    """One record-level disagreement between a PDF and its sidecar."""

    annotation_id: str
    ordinal: int
    kind: PdfConflictKind
    sidecar_start: Optional[int]
    sidecar_end: Optional[int]
    sidecar_quote: Optional[str]
    sidecar_note: Optional[str]
    pdf_start: Optional[int]
    pdf_end: Optional[int]
    pdf_quote: Optional[str]
    pdf_note: Optional[str]
    pdf_page_index: Optional[int]
    mapping_confidence: Optional[int]


@dataclass(frozen=True)
class PdfConflictReport:
    """Fingerprint-protected set of PDF/sidecar conflicts for review."""

    source_sha256: str
    sidecar_sha256: str
    conflicts: Tuple[PdfAnnotationConflict, ...]


class PdfImportConflictError(PdfImportError):
    """Raised when reconciliation needs explicit per-annotation choices."""

    def __init__(self, report: PdfConflictReport) -> None:
        self.report = report
        quoted = ", ".join(
            repr(conflict.annotation_id) for conflict in report.conflicts
        )
        if all(
            conflict.kind is PdfConflictKind.BOTH_MODIFIED
            for conflict in report.conflicts
        ):
            message = (
                "the same annotation changed in both the PDF and sidecar: "
                f"{quoted}; neither file was modified"
            )
        else:
            message = (
                "PDF/sidecar annotation conflicts require review: "
                f"{quoted}; neither file was modified"
            )
        super().__init__(message)


@dataclass(frozen=True)
class PdfConflictResolution:
    """One user-reviewed choice for a PDF annotation conflict."""

    annotation_id: str
    decision: PdfConflictDecision


@dataclass(frozen=True)
class PdfConflictResolutionResult:
    """Summary of a confirmed PDF/sidecar conflict-resolution batch."""

    source_path: Path
    annotations_path: Path
    backup_path: Optional[Path]
    restored_to_pdf: Tuple[str, ...]
    accepted_from_pdf: Tuple[str, ...]


@dataclass(frozen=True)
class PdfImportSkip:
    """One PDF annotation intentionally left out of the sidecar."""

    page_index: int
    annotation_index: int
    subtype: str
    reason: str


@dataclass(frozen=True)
class PdfImportItem:
    """Summary of one sidecar record created from PDF markup."""

    annotation_id: str
    fingerprint: str
    subtype: str
    page_index: int
    start: int
    end: int
    quote: str
    mapping_confidence: int
    generated_note: bool


@dataclass(frozen=True)
class PdfImportResult:
    """Summary of one atomic PDF-to-sidecar reconciliation."""

    source_path: Path
    annotations_path: Path
    imported: Tuple[PdfImportItem, ...]
    skipped: Tuple[PdfImportSkip, ...]
    already_imported_count: int
    refreshed_annotation_ids: Tuple[str, ...]
    updated_annotation_ids: Tuple[str, ...] = ()
    removed_annotation_ids: Tuple[str, ...] = ()
    document_revision_updated: bool = False

    @property
    def imported_count(self) -> int:
        return len(self.imported)

    @property
    def refreshed_anchor_count(self) -> int:
        return len(self.refreshed_annotation_ids)

    @property
    def updated_count(self) -> int:
        return len(self.updated_annotation_ids)

    @property
    def removed_count(self) -> int:
        return len(self.removed_annotation_ids)


@dataclass(frozen=True)
class _GeometryMatch:
    start: int
    end: int
    quote: str
    confidence: int
    pdf_anchor: PdfAnchor


@dataclass(frozen=True)
class _ScannedMarkup:
    """One supported PDF markup mapped onto the canonical text projection."""

    page_index: int
    annotation_index: int
    subtype: str
    match: _GeometryMatch
    rect: PdfRectangle
    quads: Tuple[PdfQuad, ...]
    annotation_name: Optional[str]
    author: Optional[str]
    subject: Optional[str]
    creation_date: Optional[str]
    modified_date: Optional[str]
    contents: str
    generated_note: bool
    color: Tuple[float, ...]
    fingerprint: str
    origin: str


def _resolve(value: object) -> object:
    get_object = getattr(value, "get_object", None)
    return get_object() if callable(get_object) else value


def _optional_text(value: object) -> Optional[str]:
    if value is None:
        return None
    resolved = _resolve(value)
    text = str(resolved).replace("\r\n", "\n").replace("\r", "\n")
    return text if text else None


def _numbers(value: object, field: str) -> Tuple[float, ...]:
    resolved = _resolve(value)
    try:
        values = tuple(float(item) for item in resolved)  # type: ignore[union-attr]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an array of numbers") from error
    if any(not math.isfinite(item) for item in values):
        raise ValueError(f"{field} contains a non-finite number")
    return values


def _quad_points(annotation: DictionaryObject) -> Tuple[PdfQuad, ...]:
    values = _numbers(annotation.get("/QuadPoints"), "/QuadPoints")
    if not values or len(values) % 8:
        raise ValueError("/QuadPoints must contain a non-empty multiple of 8 numbers")
    if len(values) // 8 > MAX_QUAD_COUNT:
        raise ValueError("/QuadPoints contains too many quadrilaterals")
    quads = []
    for offset in range(0, len(values), 8):
        quad = values[offset : offset + 8]
        x_values = quad[0::2]
        y_values = quad[1::2]
        if min(x_values) >= max(x_values) or min(y_values) >= max(y_values):
            raise ValueError("/QuadPoints contains a degenerate quadrilateral")
        quads.append(quad)
    return tuple(quads)  # type: ignore[return-value]


def _quad_rectangle(quad: PdfQuad) -> PdfRectangle:
    x_values = quad[0::2]
    y_values = quad[1::2]
    return min(x_values), min(y_values), max(x_values), max(y_values)


def _annotation_rectangle(
    annotation: DictionaryObject, quads: Sequence[PdfQuad]
) -> PdfRectangle:
    try:
        values = _numbers(annotation.get("/Rect"), "/Rect")
        if len(values) != 4:
            raise ValueError("/Rect must contain four numbers")
        x0, y0, x1, y1 = values
        if x0 >= x1 or y0 >= y1:
            raise ValueError("/Rect is degenerate")
        return x0, y0, x1, y1
    except ValueError:
        rectangles = tuple(_quad_rectangle(quad) for quad in quads)
        return (
            min(rectangle[0] for rectangle in rectangles),
            min(rectangle[1] for rectangle in rectangles),
            max(rectangle[2] for rectangle in rectangles),
            max(rectangle[3] for rectangle in rectangles),
        )


def _annotation_color(annotation: DictionaryObject) -> Tuple[float, ...]:
    if annotation.get("/C") is None:
        return ()
    try:
        values = _numbers(annotation.get("/C"), "/C")
    except ValueError:
        return ()
    return values if len(values) in {1, 3, 4} else ()


def _intersection_area(first: PdfRectangle, second: PdfRectangle) -> float:
    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def _box_overlap(box: PdfWordBox, rectangle: PdfRectangle) -> float:
    word_rectangle = (box.x0, box.y0, box.x1, box.y1)
    area = max(0.0, box.x1 - box.x0) * max(0.0, box.y1 - box.y0)
    if area <= 0:
        return 0.0
    return min(1.0, _intersection_area(word_rectangle, rectangle) / area)


def _match_geometry(
    source_text: str,
    document: PdfDocument,
    *,
    page_index: int,
    quads: Sequence[PdfQuad],
) -> _GeometryMatch:
    rectangles = tuple(_quad_rectangle(quad) for quad in quads)
    selected: List[Tuple[PdfWordBox, float]] = []
    quad_hits = [False] * len(rectangles)
    for box in document.words:
        if box.page_index != page_index:
            continue
        overlaps = tuple(_box_overlap(box, rectangle) for rectangle in rectangles)
        best_overlap = max(overlaps, default=0.0)
        if best_overlap < MIN_BOX_OVERLAP:
            continue
        selected.append((box, best_overlap))
        for index, overlap in enumerate(overlaps):
            if overlap >= MIN_BOX_OVERLAP:
                quad_hits[index] = True

    if not selected:
        raise ValueError("markup geometry does not overlap extracted text")
    if not all(quad_hits):
        raise ValueError("one or more markup quadrilaterals do not match text")

    ordered = sorted(selected, key=lambda item: (item[0].start, item[0].end))
    deduplicated: List[Tuple[PdfWordBox, float]] = []
    seen_spans = set()
    for box, overlap in ordered:
        span = (box.start, box.end)
        if span in seen_spans:
            continue
        seen_spans.add(span)
        deduplicated.append((box, overlap))

    start = deduplicated[0][0].start
    end = deduplicated[0][0].end
    for box, _overlap in deduplicated[1:]:
        if box.start > end and not source_text[end : box.start].isspace():
            raise ValueError("markup maps to discontiguous source text")
        end = max(end, box.end)

    if not 0 <= start < end <= len(source_text):
        raise ValueError("matched text range is outside the PDF projection")
    quote = source_text[start:end]
    if not quote.strip():
        raise ValueError("markup maps only to whitespace")

    weighted_overlap = sum(
        overlap * max(1, box.end - box.start)
        for box, overlap in deduplicated
    ) / sum(max(1, box.end - box.start) for box, _overlap in deduplicated)
    boundary_overlap = min(deduplicated[0][1], deduplicated[-1][1])
    confidence = round(100 * (0.7 * weighted_overlap + 0.3 * boundary_overlap))
    confidence = max(0, min(100, confidence))
    if confidence < MIN_MAPPING_CONFIDENCE:
        raise ValueError(
            f"geometry match confidence {confidence}% is below "
            f"the {MIN_MAPPING_CONFIDENCE}% threshold"
        )

    anchor = document.anchor_for_range(start, end)
    if anchor is None:
        raise ValueError("matched source range has no canonical PDF anchor")
    return _GeometryMatch(
        start=start,
        end=end,
        quote=quote,
        confidence=confidence,
        pdf_anchor=anchor,
    )


def _pdf_date_to_iso(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    match = _PDF_DATE_RE.fullmatch(value.strip())
    if match is None:
        return None
    groups = match.groupdict()
    zone_text = groups["zone"]
    zone = timezone.utc
    if zone_text and zone_text not in {"Z", "z"}:
        sign = 1 if zone_text[0] == "+" else -1
        digits = zone_text[1:].replace("'", "")
        zone = timezone(
            sign
            * timedelta(
                hours=int(digits[:2]),
                minutes=int(digits[2:]),
            )
        )
    try:
        parsed = datetime(
            int(groups["year"]),
            int(groups["month"] or 1),
            int(groups["day"] or 1),
            int(groups["hour"] or 0),
            int(groups["minute"] or 0),
            int(groups["second"] or 0),
            tzinfo=zone,
        )
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(
    *,
    page_index: int,
    subtype: str,
    annotation_name: Optional[str],
    author: Optional[str],
    quads: Sequence[PdfQuad],
) -> str:
    payload = {
        "version": 1,
        "page_index": page_index,
        "subtype": subtype,
    }
    if annotation_name:
        payload["annotation_name"] = annotation_name
    else:
        payload["author"] = author
        payload["quad_points_pdf"] = [
            [round(float(coordinate), 4) for coordinate in quad]
            for quad in quads
        ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _page_annotations(page: object) -> Tuple[DictionaryObject, ...]:
    annotations = page.get("/Annots")  # type: ignore[attr-defined]
    if annotations is None:
        return ()
    resolved = _resolve(annotations)
    try:
        return tuple(
            annotation
            for raw in resolved  # type: ignore[union-attr]
            for annotation in (_resolve(raw),)
            if isinstance(annotation, DictionaryObject)
        )
    except (TypeError, ValueError) as error:
        raise PdfImportError(f"cannot inspect page annotations: {error}") from error


def _scan_markup(
    *,
    annotation: DictionaryObject,
    source: LoadedTextFile,
    page_index: int,
    annotation_index: int,
    subtype: str,
) -> _ScannedMarkup:
    document = source.pdf_document
    if document is None:
        raise ValueError("source does not provide PDF geometry")
    quads = _quad_points(annotation)
    rect = _annotation_rectangle(annotation, quads)
    match = _match_geometry(
        source.text,
        document,
        page_index=page_index,
        quads=quads,
    )
    annotation_name = _optional_text(annotation.get("/NM"))
    author = _optional_text(annotation.get("/T"))
    subject = _optional_text(annotation.get("/Subj"))
    creation_date = _optional_text(annotation.get("/CreationDate"))
    modified_date = _optional_text(annotation.get("/M"))
    fingerprint = _fingerprint(
        page_index=page_index,
        subtype=subtype,
        annotation_name=annotation_name,
        author=author,
        quads=quads,
    )
    contents = (_optional_text(annotation.get("/Contents")) or "").rstrip()
    return _ScannedMarkup(
        page_index=page_index,
        annotation_index=annotation_index,
        subtype=subtype,
        match=match,
        rect=rect,
        quads=tuple(quads),
        annotation_name=annotation_name,
        author=author,
        subject=subject,
        creation_date=creation_date,
        modified_date=modified_date,
        contents=contents,
        generated_note=not bool(contents.strip()),
        color=_annotation_color(annotation),
        fingerprint=fingerprint,
        origin=(
            "pta"
            if author == PTA_ANNOTATION_AUTHOR
            and subject == PTA_ANNOTATION_SUBJECT
            else "external"
        ),
    )


def _record_metadata(
    sidecar_text: str, block_start: int, block_end: int
) -> Mapping[str, object]:
    block = sidecar_text[block_start:block_end]
    bounds = _metadata_bounds(block)
    if bounds is None:
        return {}
    start, end = bounds
    try:
        value = yaml.safe_load(block[start:end])
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _mapping_value(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _string_value(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class _ExistingPdfRecord:
    annotation_id: str
    metadata: Mapping[str, object]
    confirmed: bool
    annotation_name: Optional[str]
    fingerprint: Optional[str]
    pdf_state_sha256: Optional[str]
    sidecar_state_sha256: Optional[str]


def _existing_pdf_records(
    sidecar_text: str,
) -> Tuple[Tuple[ParsedAnnotation, _ExistingPdfRecord], ...]:
    output = []
    for record in parse_annotation_records(sidecar_text):
        metadata = _record_metadata(
            sidecar_text, record.block_start, record.block_end
        )
        sync = _mapping_value(metadata.get("pdf_sync"))
        imported = _mapping_value(metadata.get("pdf_import"))
        confirmed = bool(sync) or bool(imported)
        annotation_name = _string_value(sync.get("annotation_name"))
        if annotation_name is None:
            annotation_name = _string_value(imported.get("annotation_name"))
        fingerprint = _string_value(sync.get("fingerprint"))
        if fingerprint is None:
            fingerprint = _string_value(imported.get("fingerprint"))
        output.append(
            (
                record,
                _ExistingPdfRecord(
                    annotation_id=record.annotation_id,
                    metadata=metadata,
                    confirmed=confirmed,
                    annotation_name=annotation_name,
                    fingerprint=fingerprint,
                    pdf_state_sha256=_string_value(
                        sync.get("pdf_state_sha256")
                    ),
                    sidecar_state_sha256=_string_value(
                        sync.get("sidecar_state_sha256")
                    ),
                ),
            )
        )
    return tuple(output)


def _combined_fingerprint(parts: Sequence[_ScannedMarkup]) -> str:
    if len(parts) == 1:
        return parts[0].fingerprint
    encoded = json.dumps(
        [part.fingerprint for part in parts],
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _state_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pdf_state_fingerprint(parts: Sequence[_ScannedMarkup]) -> str:
    return _state_sha256(
        {
            "version": 1,
            "parts": [
                {
                    "page_index": part.page_index,
                    "subtype": part.subtype,
                    "annotation_name": part.annotation_name,
                    "author": part.author,
                    "subject": part.subject,
                    "creation_date": part.creation_date,
                    "modified_date": part.modified_date,
                    "contents": part.contents,
                    "color": [round(value, 4) for value in part.color],
                    "rect_pdf": [round(value, 4) for value in part.rect],
                    "quad_points_pdf": [
                        [round(value, 4) for value in quad]
                        for quad in part.quads
                    ],
                }
                for part in parts
            ],
        }
    )


def _canonical_state_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _canonical_state_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_state_value(item) for item in value]
    if isinstance(value, float):
        return round(value, 4)
    return value


def _sidecar_state_fingerprint(
    *,
    annotation_id: str,
    start: Optional[int],
    end: Optional[int],
    quote: Optional[str],
    note: Optional[str],
    created_at: Optional[str],
    pdf_import: object,
) -> str:
    return _state_sha256(
        {
            "version": 1,
            "annotation_id": annotation_id,
            "start": start,
            "end": end,
            "quote": quote,
            "note": note,
            "created_at": created_at,
            "pdf_import": _canonical_state_value(pdf_import),
        }
    )


def _parsed_sidecar_state_fingerprint(
    record: ParsedAnnotation, metadata: Mapping[str, object]
) -> str:
    return _sidecar_state_fingerprint(
        annotation_id=record.annotation_id,
        start=record.start,
        end=record.end,
        quote=record.quote,
        note=record.note,
        created_at=record.created_at,
        pdf_import=metadata.get("pdf_import"),
    )


def _normal_numbers(value: object) -> object:
    if not isinstance(value, (list, tuple)):
        return value
    return [
        _normal_numbers(item)
        if isinstance(item, (list, tuple))
        else round(float(item), 4)
        if isinstance(item, (int, float)) and not isinstance(item, bool)
        else item
        for item in value
    ]


def _legacy_pdf_state_unchanged(
    parts: Sequence[_ScannedMarkup],
    existing: ParsedAnnotation,
    identity: _ExistingPdfRecord,
) -> bool:
    if not parts or _combined_fingerprint(parts) != identity.fingerprint:
        return False
    first = parts[0]
    imported = _mapping_value(identity.metadata.get("pdf_import"))
    if imported:
        comparisons = (
            (imported.get("subtype"), first.subtype),
            (imported.get("page_index"), first.page_index),
            (imported.get("annotation_name"), first.annotation_name),
            (imported.get("author"), first.author),
            (imported.get("subject"), first.subject),
            (imported.get("creation_date"), first.creation_date),
            (imported.get("modified_date"), first.modified_date),
            (_normal_numbers(imported.get("color")), _normal_numbers(first.color)),
            (_normal_numbers(imported.get("rect_pdf")), _normal_numbers(first.rect)),
            (
                _normal_numbers(imported.get("quad_points_pdf")),
                _normal_numbers(first.quads),
            ),
        )
        return all(left == right for left, right in comparisons)
    return (
        first.match.start == existing.start
        and first.match.end == existing.end
        and first.match.quote == existing.quote
        and _pdf_date_to_iso(first.modified_date) == existing.created_at
    )


def _pdf_group_matches_sidecar(
    parts: Sequence[_ScannedMarkup],
    existing: ParsedAnnotation,
    source_text: str,
) -> bool:
    if not parts or existing.start is None or existing.end is None:
        return False
    start = min(part.match.start for part in parts)
    end = max(part.match.end for part in parts)
    first = parts[0]
    note = first.contents if not first.generated_note else (
        f"_Imported PDF {first.subtype.lower()} without a comment._"
    )
    return (
        start == existing.start
        and end == existing.end
        and source_text[start:end] == existing.quote
        and note == existing.note
    )


def _pdf_note(parts: Sequence[_ScannedMarkup]) -> Optional[str]:
    if not parts:
        return None
    first = parts[0]
    if first.generated_note:
        return f"_Imported PDF {first.subtype.lower()} without a comment._"
    return first.contents


def _annotation_conflict(
    existing: ParsedAnnotation,
    *,
    kind: PdfConflictKind,
    parts: Sequence[_ScannedMarkup] = (),
) -> PdfAnnotationConflict:
    pdf_start = min((part.match.start for part in parts), default=None)
    pdf_end = max((part.match.end for part in parts), default=None)
    return PdfAnnotationConflict(
        annotation_id=existing.annotation_id,
        ordinal=existing.ordinal,
        kind=kind,
        sidecar_start=existing.start,
        sidecar_end=existing.end,
        sidecar_quote=existing.quote,
        sidecar_note=existing.note,
        pdf_start=pdf_start,
        pdf_end=pdf_end,
        pdf_quote=(
            parts[0].match.quote
            if len(parts) == 1
            else None
            if not parts
            else "\n".join(part.match.quote for part in parts)
        ),
        pdf_note=_pdf_note(parts),
        pdf_page_index=parts[0].page_index if parts else None,
        mapping_confidence=(
            min(part.match.confidence for part in parts) if parts else None
        ),
    )


def _record_for_group(
    *,
    annotation_id: str,
    parts: Sequence[_ScannedMarkup],
    source: LoadedTextFile,
    existing: Optional[ParsedAnnotation],
    prefer_existing: bool = False,
) -> Tuple[AnnotationRecord, PdfImportItem]:
    if not parts:
        raise ValueError("PDF annotation group is empty")
    document = source.pdf_document
    if document is None:
        raise ValueError("source does not provide PDF geometry")
    first = parts[0]
    pdf_start = min(part.match.start for part in parts)
    pdf_end = max(part.match.end for part in parts)
    pdf_quote = source.text[pdf_start:pdf_end]
    pdf_note = first.contents if not first.generated_note else (
        f"_Imported PDF {first.subtype.lower()} without a comment._"
    )
    if prefer_existing:
        if (
            existing is None
            or existing.errors
            or existing.start is None
            or existing.end is None
            or existing.quote is None
            or existing.note is None
            or not 0 <= existing.start < existing.end <= len(source.text)
            or source.text[existing.start : existing.end] != existing.quote
            or not existing.note.strip()
        ):
            raise ValueError(
                "sidecar changes cannot be preserved because the record is stale"
            )
        start = existing.start
        end = existing.end
        quote = existing.quote
        note = existing.note
    else:
        start = pdf_start
        end = pdf_end
        quote = pdf_quote
        note = pdf_note
    pdf_anchor = document.anchor_for_range(start, end)
    if pdf_anchor is None:
        raise ValueError("matched source range has no canonical PDF anchor")
    fingerprint = _combined_fingerprint(parts)
    existing_created_at = existing.created_at if existing is not None else None
    created_at = (
        existing_created_at
        or _pdf_date_to_iso(first.creation_date)
        or _pdf_date_to_iso(first.modified_date)
    )
    import_metadata = None
    if first.origin == "external":
        import_metadata = PdfImportMetadata(
            version=1,
            source_document_sha256=document.document_sha256,
            fingerprint=fingerprint,
            subtype=first.subtype,
            page_index=first.page_index,
            annotation_name=first.annotation_name,
            author=first.author,
            subject=first.subject,
            creation_date=first.creation_date,
            modified_date=first.modified_date,
            mapping_confidence=min(part.match.confidence for part in parts),
            generated_note=first.generated_note,
            color=first.color,
            rect_pdf=first.rect,
            quad_points_pdf=first.quads,
        )
    sidecar_state_sha256 = _sidecar_state_fingerprint(
        annotation_id=annotation_id,
        start=start,
        end=end,
        quote=quote,
        note=note,
        created_at=created_at,
        pdf_import=(asdict(import_metadata) if import_metadata is not None else None),
    )
    sync_metadata = PdfSyncMetadata(
        version=1,
        origin=first.origin,
        annotation_name=first.annotation_name,
        fingerprint=fingerprint,
        last_seen_document_sha256=document.document_sha256,
        pdf_state_sha256=_pdf_state_fingerprint(parts),
        sidecar_state_sha256=sidecar_state_sha256,
    )
    record = create_record(
        AnnotationDraft(start, end, quote),
        note,
        annotation_id=annotation_id,
        created_at=created_at,
        source_text=source.text,
        pdf_anchor=pdf_anchor,
        pdf_import=import_metadata,
        pdf_sync=sync_metadata,
    )
    item = PdfImportItem(
        annotation_id=annotation_id,
        fingerprint=fingerprint,
        subtype=first.subtype,
        page_index=first.page_index,
        start=pdf_start,
        end=pdf_end,
        quote=pdf_quote,
        mapping_confidence=min(part.match.confidence for part in parts),
        generated_note=first.generated_note,
    )
    return record, item


def _apply_block_replacements(
    sidecar_text: str,
    replacements: Sequence[Tuple[int, int, str]],
) -> str:
    updated = sidecar_text
    for start, end, replacement in sorted(replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    updated = re.sub(
        r"(?m)(?<=<!-- /pta:annotation -->)\n{3,}(?=<!-- pta:annotation )",
        "\n\n",
        updated,
    )
    return renumber_annotation_headings(updated)


_SYNC_STATE_LINE_RE = re.compile(
    r'(?m)^pdf_sync_(?:document|sidecar)_sha256:[ \t]*'
    r'(?:"[0-9a-f]{64}"|[^\n]*)\n?'
)


def _without_sidecar_sync_fields(sidecar_text: str) -> str:
    if not sidecar_text.startswith("---\n"):
        return sidecar_text
    closing = sidecar_text.find("\n---", 4)
    if closing < 0:
        return sidecar_text
    cleaned_front_matter = _SYNC_STATE_LINE_RE.sub(
        "", sidecar_text[:closing]
    ).rstrip("\r\n")
    return (
        cleaned_front_matter
        + sidecar_text[closing:]
    )


def _sidecar_content_sha256(sidecar_text: str) -> str:
    """Hash user-visible sidecar state without self-referential sync fields."""

    normalized = _without_sidecar_sync_fields(sidecar_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _sidecar_sync_state(sidecar_text: str) -> Tuple[Optional[str], Optional[str]]:
    if not sidecar_text.startswith("---\n"):
        return None, None
    closing = sidecar_text.find("\n---", 4)
    if closing < 0:
        return None, None
    front_matter = sidecar_text[4:closing]

    def field(name: str) -> Optional[str]:
        match = re.search(
            rf'(?m)^{re.escape(name)}:[ \t]*"([0-9a-f]{{64}})"[ \t]*$',
            front_matter,
        )
        return match.group(1) if match is not None else None

    return (
        field("pdf_sync_document_sha256"),
        field("pdf_sync_sidecar_sha256"),
    )


def _set_sidecar_sync_state(
    sidecar_text: str, document_sha256: str
) -> Tuple[str, bool]:
    """Record the exact PDF and sidecar revisions used as a sync baseline."""

    original = sidecar_text
    without_state = _without_sidecar_sync_fields(sidecar_text)
    if not without_state.startswith("---\n"):
        return sidecar_text, False
    closing = without_state.find("\n---", 4)
    if closing < 0:
        return sidecar_text, False
    sidecar_sha256 = _sidecar_content_sha256(without_state)
    fields = (
        "\npdf_sync_document_sha256: "
        + json.dumps(document_sha256)
        + "\npdf_sync_sidecar_sha256: "
        + json.dumps(sidecar_sha256)
    )
    updated = without_state[:closing] + fields + without_state[closing:]
    return updated, updated != original


def _metadata_bounds(block: str) -> Optional[Tuple[int, int]]:
    opening = re.search(r"(?m)^```yaml[ \t]*(?:\n|$)", block)
    if opening is None:
        return None
    closing = re.search(r"(?m)^```[ \t]*(?:\n|$)", block[opening.end() :])
    if closing is None:
        return None
    return opening.end(), opening.end() + closing.start()


def _top_level_block_bounds(
    metadata: str, name: str
) -> Optional[Tuple[int, int]]:
    matches = tuple(
        re.finditer(
            rf"(?m)^{re.escape(name)}:[ \t]*(?:\n|$)",
            metadata,
        )
    )
    if len(matches) != 1:
        return None
    match = matches[0]
    tail = metadata[match.end() :]
    following = re.search(r"(?m)^(?=\S)", tail)
    end = match.end() + (
        following.start() if following is not None else len(tail)
    )
    return match.start(), end


def _refresh_exact_pdf_anchors(
    sidecar_text: str,
    source_text: str,
    document: PdfDocument,
) -> Tuple[str, Tuple[str, ...]]:
    """Refresh stale PDF containers only when every text anchor is exact."""

    current_source_sha = text_sha256(source_text)
    replacements = []
    refreshed_ids = []
    for record in parse_annotation_records(sidecar_text):
        if (
            record.errors
            or record.start is None
            or record.end is None
            or record.quote is None
            or record.source_sha256 != current_source_sha
            or record.quote_sha256 != text_sha256(record.quote)
            or not 0 <= record.start < record.end <= len(source_text)
            or source_text[record.start : record.end] != record.quote
        ):
            continue
        expected_anchor = document.anchor_for_range(record.start, record.end)
        if expected_anchor is None:
            continue
        block = sidecar_text[record.block_start : record.block_end]
        bounds = _metadata_bounds(block)
        if bounds is None:
            continue
        metadata_start, metadata_end = bounds
        metadata = block[metadata_start:metadata_end]
        anchor_bounds = _top_level_block_bounds(metadata, "pdf_anchor")
        if anchor_bounds is None:
            continue
        anchor_start, anchor_end = anchor_bounds
        expected_yaml = render_pdf_anchor(expected_anchor)
        if metadata[anchor_start:anchor_end] == expected_yaml:
            continue
        refreshed_metadata = (
            metadata[:anchor_start] + expected_yaml + metadata[anchor_end:]
        )
        refreshed_block = (
            block[:metadata_start]
            + refreshed_metadata
            + block[metadata_end:]
        )
        replacements.append(
            (record.block_start, record.block_end, refreshed_block)
        )
        refreshed_ids.append(record.annotation_id)

    updated = sidecar_text
    for start, end, replacement in sorted(replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    return updated, tuple(refreshed_ids)


def import_pdf_text_markups(
    source_path: Path,
    *,
    annotations_path: Optional[Path] = None,
    loaded_source: Optional[LoadedTextFile] = None,
    use_pdf_cache: bool = True,
    rebuild_pdf_cache: bool = False,
    allow_sidecar_changes: bool = False,
    conflict_resolutions: Optional[
        Mapping[str, PdfConflictDecision]
    ] = None,
) -> PdfImportResult:
    """Reconcile confidently matched PDF text markup into a sidecar atomically."""

    normalized_resolutions: Dict[str, PdfConflictDecision] = {}
    for annotation_id, raw_decision in (conflict_resolutions or {}).items():
        try:
            decision = PdfConflictDecision(raw_decision)
        except ValueError as error:
            raise PdfImportError(
                f"invalid conflict decision for {annotation_id!r}: "
                f"{raw_decision!r}"
            ) from error
        normalized_resolutions[str(annotation_id)] = decision

    source_path = Path(source_path).expanduser().resolve()
    if source_path.suffix.lower() != ".pdf":
        raise PdfImportError(f"source must be a PDF: {source_path}")
    if not source_path.exists() or not source_path.is_file():
        raise PdfImportError(
            f"source PDF does not exist or is not a file: {source_path}"
        )
    try:
        source_bytes = source_path.read_bytes()
    except OSError as error:
        raise PdfImportError(f"cannot read source PDF: {error}") from error
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    try:
        reader = PdfReader(io.BytesIO(source_bytes))
        if reader.is_encrypted:
            raise PdfImportError("encrypted PDFs are not supported for import")
    except PdfImportError:
        raise
    except Exception as error:
        raise PdfImportError(f"cannot inspect source PDF: {error}") from error

    if loaded_source is None:
        try:
            loaded_source = load_source_file(
                source_path,
                use_pdf_cache=use_pdf_cache,
                rebuild_pdf_cache=rebuild_pdf_cache,
            )
        except TextFileError as error:
            raise PdfImportError(str(error)) from error
    elif (
        loaded_source.path.resolve() != source_path
        or loaded_source.source_kind != "pdf"
        or loaded_source.pdf_document is None
        or loaded_source.pdf_document.document_sha256 != source_sha256
    ):
        raise PdfImportError(
            "loaded PDF source no longer matches the source file on disk"
        )
    document = loaded_source.pdf_document
    if document is None:
        raise PdfImportError("source does not provide PDF geometry")
    if len(reader.pages) != document.page_count:
        raise PdfImportError("PDF page count differs from the extracted geometry")

    store = AnnotationStore(source_path, sidecar_path=annotations_path)
    if store.path == source_path:
        raise PdfImportError(
            "annotation sidecar and source PDF must be different files"
        )
    try:
        current_sidecar = store.read_text()
    except AnnotationFileError as error:
        raise PdfImportError(str(error)) from error
    previous_document_sha256, previous_sidecar_sha256 = _sidecar_sync_state(
        current_sidecar
    )
    if previous_document_sha256 == document.document_sha256:
        baseline_text = current_sidecar
        baseline_updated = False
        if previous_sidecar_sha256 is None:
            baseline_text, baseline_updated = _set_sidecar_sync_state(
                current_sidecar,
                document.document_sha256,
            )
            try:
                store.append_many(
                    (),
                    expected_text=current_sidecar,
                    base_text=baseline_text,
                )
            except AnnotationFileError as error:
                raise PdfImportError(str(error)) from error
        return PdfImportResult(
            source_path=source_path,
            annotations_path=store.path,
            imported=(),
            skipped=(),
            already_imported_count=0,
            refreshed_annotation_ids=(),
            document_revision_updated=baseline_updated,
        )
    sidecar_changed_since_sync = (
        previous_sidecar_sha256 is not None
        and _sidecar_content_sha256(current_sidecar) != previous_sidecar_sha256
    )
    refreshed_sidecar, refreshed_annotation_ids = _refresh_exact_pdf_anchors(
        current_sidecar,
        loaded_source.text,
        document,
    )
    existing_pairs = _existing_pdf_records(refreshed_sidecar)
    existing_by_id = {
        identity.annotation_id: (record, identity)
        for record, identity in existing_pairs
    }

    def unique_identity_map(field: str) -> Dict[str, str]:
        candidates: Dict[str, List[str]] = {}
        for _record, identity in existing_pairs:
            value = getattr(identity, field)
            if value is not None:
                candidates.setdefault(value, []).append(identity.annotation_id)
        return {
            value: annotation_ids[0]
            for value, annotation_ids in candidates.items()
            if len(annotation_ids) == 1
        }

    id_by_name = unique_identity_map("annotation_name")
    id_by_fingerprint = unique_identity_map("fingerprint")

    groups: Dict[str, List[_ScannedMarkup]] = {}
    skipped: List[PdfImportSkip] = []
    complete_supported_scan = True
    for page_index, page in enumerate(reader.pages):
        try:
            annotations = _page_annotations(page)
        except PdfImportError as error:
            raise PdfImportError(f"page {page_index + 1}: {error}") from error
        for annotation_index, annotation in enumerate(annotations):
            raw_subtype = _optional_text(annotation.get("/Subtype")) or "Unknown"
            subtype = raw_subtype.removeprefix("/")
            if subtype not in SUPPORTED_TEXT_MARKUP_SUBTYPES:
                skipped.append(
                    PdfImportSkip(
                        page_index,
                        annotation_index,
                        subtype,
                        "unsupported annotation subtype",
                    )
                )
                continue
            try:
                scanned = _scan_markup(
                    annotation=annotation,
                    source=loaded_source,
                    page_index=page_index,
                    annotation_index=annotation_index,
                    subtype=subtype,
                )
            except (TypeError, ValueError) as error:
                complete_supported_scan = False
                skipped.append(
                    PdfImportSkip(
                        page_index,
                        annotation_index,
                        subtype,
                        str(error),
                    )
                )
                continue

            annotation_id = None
            if scanned.annotation_name:
                if scanned.annotation_name in existing_by_id:
                    annotation_id = scanned.annotation_name
                else:
                    annotation_id = id_by_name.get(scanned.annotation_name)
            if annotation_id is None:
                annotation_id = id_by_fingerprint.get(scanned.fingerprint)
            if (
                annotation_id is None
                and scanned.origin == "pta"
                and scanned.annotation_name
            ):
                annotation_id = scanned.annotation_name
            if annotation_id is None:
                annotation_id = IMPORT_ID_PREFIX + scanned.fingerprint[:32]

            if annotation_id in groups:
                previous = groups[annotation_id][0]
                can_group = (
                    scanned.origin == previous.origin == "pta"
                    and scanned.annotation_name is not None
                    and scanned.annotation_name == previous.annotation_name
                    and scanned.page_index != previous.page_index
                )
                if not can_group:
                    annotation_id = IMPORT_ID_PREFIX + scanned.fingerprint[:32]
                    suffix = 2
                    base_id = annotation_id
                    while annotation_id in groups:
                        annotation_id = f"{base_id}-{suffix}"
                        suffix += 1
            groups.setdefault(annotation_id, []).append(scanned)

    try:
        if source_path.read_bytes() != source_bytes:
            raise PdfImportError("source PDF changed while annotations were scanned")
    except OSError as error:
        raise PdfImportError(f"cannot recheck source PDF: {error}") from error

    replacements: List[Tuple[int, int, str]] = []
    new_records: List[AnnotationRecord] = []
    imported: List[PdfImportItem] = []
    updated_ids: List[str] = []
    removed_ids: List[str] = []
    conflicts: List[PdfAnnotationConflict] = []
    encountered_conflict_ids: set[str] = set()
    matched_ids = set(groups)
    already_imported_count = 0

    for annotation_id, parts in groups.items():
        existing_pair = existing_by_id.get(annotation_id)
        existing_record = existing_pair[0] if existing_pair is not None else None
        identity = existing_pair[1] if existing_pair is not None else None
        prefer_existing = False
        if existing_record is not None and identity is not None:
            sidecar_record_changed = (
                _parsed_sidecar_state_fingerprint(
                    existing_record, identity.metadata
                )
                != identity.sidecar_state_sha256
                if identity.sidecar_state_sha256 is not None
                else sidecar_changed_since_sync
            )
            pdf_record_changed = (
                _pdf_state_fingerprint(parts) != identity.pdf_state_sha256
                if identity.pdf_state_sha256 is not None
                else not _legacy_pdf_state_unchanged(
                    parts, existing_record, identity
                )
            )
            if allow_sidecar_changes:
                prefer_existing = True
            elif sidecar_record_changed and pdf_record_changed:
                if _pdf_group_matches_sidecar(
                    parts, existing_record, loaded_source.text
                ):
                    prefer_existing = True
                elif (
                    normalized_resolutions.get(annotation_id)
                    is PdfConflictDecision.KEEP_MARKDOWN
                ):
                    encountered_conflict_ids.add(annotation_id)
                    prefer_existing = True
                elif (
                    normalized_resolutions.get(annotation_id)
                    is PdfConflictDecision.KEEP_PDF
                ):
                    encountered_conflict_ids.add(annotation_id)
                else:
                    encountered_conflict_ids.add(annotation_id)
                    conflicts.append(
                        _annotation_conflict(
                            existing_record,
                            kind=PdfConflictKind.BOTH_MODIFIED,
                            parts=parts,
                        )
                    )
                    continue
            elif sidecar_record_changed:
                prefer_existing = True
        try:
            record, item = _record_for_group(
                annotation_id=annotation_id,
                parts=parts,
                source=loaded_source,
                existing=existing_record,
                prefer_existing=prefer_existing,
            )
        except ValueError as error:
            complete_supported_scan = False
            first = parts[0]
            skipped.append(
                PdfImportSkip(
                    first.page_index,
                    first.annotation_index,
                    first.subtype,
                    str(error),
                )
            )
            continue
        if existing_record is None:
            new_records.append(record)
            imported.append(item)
            continue
        already_imported_count += 1
        rendered = store.render_record(record, existing_record.ordinal)
        current_block = refreshed_sidecar[
            existing_record.block_start : existing_record.block_end
        ]
        if rendered != current_block:
            replacements.append(
                (
                    existing_record.block_start,
                    existing_record.block_end,
                    rendered,
                )
            )
            updated_ids.append(annotation_id)

    if complete_supported_scan:
        for existing_record, identity in existing_pairs:
            if identity.confirmed and identity.annotation_id not in matched_ids:
                annotation_id = identity.annotation_id
                if allow_sidecar_changes:
                    raise PdfImportError(
                        "the synchronized PDF is missing sidecar annotation "
                        f"{annotation_id!r}"
                    )
                encountered_conflict_ids.add(annotation_id)
                decision = normalized_resolutions.get(annotation_id)
                if decision is PdfConflictDecision.KEEP_MARKDOWN:
                    continue
                if decision is not PdfConflictDecision.KEEP_PDF:
                    conflicts.append(
                        _annotation_conflict(
                            existing_record,
                            kind=PdfConflictKind.DELETED_FROM_PDF,
                        )
                    )
                    continue
                replacements.append(
                    (existing_record.block_start, existing_record.block_end, "")
                )
                removed_ids.append(identity.annotation_id)

    unexpected_resolutions = (
        set(normalized_resolutions) - encountered_conflict_ids
    )
    if unexpected_resolutions:
        quoted = ", ".join(repr(value) for value in sorted(unexpected_resolutions))
        raise PdfImportError(
            f"conflict set changed before apply: {quoted} no longer need a decision"
        )
    if conflicts:
        raise PdfImportConflictError(
            PdfConflictReport(
                source_sha256=document.document_sha256,
                sidecar_sha256=text_sha256(current_sidecar),
                conflicts=tuple(conflicts),
            )
        )

    reconciled_sidecar = _apply_block_replacements(
        refreshed_sidecar,
        replacements,
    )
    if new_records:
        first_ordinal = reconciled_sidecar.count(store.MARKER) + 1
        rendered_records = "\n\n".join(
            store.render_record(record, first_ordinal + offset).rstrip("\n")
            for offset, record in enumerate(new_records)
        )
        reconciled_sidecar = (
            reconciled_sidecar.rstrip("\r\n")
            + "\n\n"
            + rendered_records
            + "\n"
        )
    document_revision_updated = False
    if complete_supported_scan:
        reconciled_sidecar, document_revision_updated = _set_sidecar_sync_state(
            reconciled_sidecar,
            document.document_sha256,
        )
    try:
        store.append_many(
            (),
            expected_text=current_sidecar,
            base_text=reconciled_sidecar,
        )
    except AnnotationFileError as error:
        raise PdfImportError(str(error)) from error
    return PdfImportResult(
        source_path=source_path,
        annotations_path=store.path,
        imported=tuple(imported),
        skipped=tuple(skipped),
        already_imported_count=already_imported_count,
        refreshed_annotation_ids=refreshed_annotation_ids,
        updated_annotation_ids=tuple(updated_ids),
        removed_annotation_ids=tuple(removed_ids),
        document_revision_updated=document_revision_updated,
    )


def _atomic_replace_bytes(path: Path, data: bytes, mode: int) -> None:
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
            os.fchmod(temporary.fileno(), mode & 0o666)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _write_conflict_backup(
    sidecar_path: Path,
    sidecar_bytes: bytes,
    *,
    label: str,
    mode: int,
) -> Path:
    backup_path = sidecar_path.with_name(f"{sidecar_path.name}.{label}")
    suffix = 2
    while backup_path.exists():
        backup_path = sidecar_path.with_name(
            f"{sidecar_path.name}.{label}-{suffix}"
        )
        suffix += 1
    _atomic_replace_bytes(backup_path, sidecar_bytes, mode)
    return backup_path


def resolve_pdf_annotation_conflicts(
    source_path: Path,
    report: PdfConflictReport,
    resolutions: Iterable[PdfConflictResolution],
    *,
    annotations_path: Optional[Path] = None,
    use_pdf_cache: bool = True,
    backup_label: Optional[str] = None,
) -> PdfConflictResolutionResult:
    """Apply a fully reviewed conflict batch, backing up and rolling back safely."""

    source = Path(source_path).expanduser().resolve()
    store = AnnotationStore(source, sidecar_path=annotations_path)
    resolution_list = tuple(resolutions)
    decisions: Dict[str, PdfConflictDecision] = {}
    for resolution in resolution_list:
        if resolution.annotation_id in decisions:
            raise PdfImportError(
                f"duplicate conflict decision: {resolution.annotation_id!r}"
            )
        decisions[resolution.annotation_id] = PdfConflictDecision(
            resolution.decision
        )
    expected_ids = {conflict.annotation_id for conflict in report.conflicts}
    if set(decisions) != expected_ids:
        missing = sorted(expected_ids - set(decisions))
        unexpected = sorted(set(decisions) - expected_ids)
        details = []
        if missing:
            details.append("missing " + ", ".join(repr(value) for value in missing))
        if unexpected:
            details.append(
                "unexpected " + ", ".join(repr(value) for value in unexpected)
            )
        raise PdfImportError(
            "every PDF conflict requires exactly one decision: "
            + "; ".join(details)
        )

    try:
        source_bytes = source.read_bytes()
        sidecar_bytes = store.path.read_bytes()
        sidecar_text = sidecar_bytes.decode("utf-8-sig")
        sidecar_mode = store.path.stat().st_mode
    except (OSError, UnicodeDecodeError) as error:
        raise PdfImportError(f"cannot read the conflict pair: {error}") from error
    if hashlib.sha256(source_bytes).hexdigest() != report.source_sha256:
        raise PdfImportError(
            "the PDF changed after conflict review; press R to scan again"
        )
    if text_sha256(sidecar_text) != report.sidecar_sha256:
        raise PdfImportError(
            "the annotation sidecar changed after conflict review; "
            "press R to scan again"
        )

    label = backup_label or (
        "before-pdf-conflict-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    try:
        backup_path = _write_conflict_backup(
            store.path,
            sidecar_bytes,
            label=label,
            mode=sidecar_mode,
        )
    except OSError as error:
        raise PdfImportError(
            f"cannot create annotation backup before conflict resolution: {error}"
        ) from error

    prepared_sidecar_bytes: Optional[bytes] = None
    try:
        import_pdf_text_markups(
            source,
            annotations_path=store.path,
            use_pdf_cache=use_pdf_cache,
            conflict_resolutions=decisions,
        )
        prepared_sidecar_bytes = store.path.read_bytes()
        if PdfConflictDecision.KEEP_MARKDOWN in decisions.values():
            from .embed import PdfEmbedError, embed_pdf_annotations

            try:
                embed_pdf_annotations(
                    source,
                    annotations_path=store.path,
                )
            except PdfEmbedError as error:
                raise PdfImportError(str(error)) from error
    except (PdfImportError, OSError) as error:
        try:
            current_sidecar_bytes = store.path.read_bytes()
            if (
                prepared_sidecar_bytes is not None
                and current_sidecar_bytes == prepared_sidecar_bytes
            ):
                _atomic_replace_bytes(store.path, sidecar_bytes, sidecar_mode)
            elif current_sidecar_bytes != sidecar_bytes:
                raise PdfImportError(
                    "the sidecar changed concurrently during conflict "
                    "resolution; that newer file was not overwritten. "
                    f"Original backup: {backup_path}"
                )
        except PdfImportError:
            raise
        except OSError as rollback_error:
            raise PdfImportError(
                "conflict resolution failed and sidecar rollback was "
                f"incomplete ({rollback_error}): {error}"
            ) from error
        raise PdfImportError(
            f"conflict resolution failed; original pair restored: {error}"
        ) from error

    restored = tuple(
        resolution.annotation_id
        for resolution in resolution_list
        if decisions[resolution.annotation_id]
        == PdfConflictDecision.KEEP_MARKDOWN
    )
    accepted = tuple(
        resolution.annotation_id
        for resolution in resolution_list
        if decisions[resolution.annotation_id] == PdfConflictDecision.KEEP_PDF
    )
    return PdfConflictResolutionResult(
        source_path=source,
        annotations_path=store.path,
        backup_path=backup_path,
        restored_to_pdf=restored,
        accepted_from_pdf=accepted,
    )


def skip_reason_counts(skips: Iterable[PdfImportSkip]) -> Tuple[Tuple[str, int], ...]:
    """Return deterministic aggregate counts for CLI reporting."""

    counts = {}
    for skip in skips:
        counts[skip.reason] = counts.get(skip.reason, 0) + 1
    return tuple(sorted(counts.items()))
