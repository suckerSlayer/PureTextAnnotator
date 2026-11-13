"""Safely embed sidecar notes as standard PDF highlight annotations."""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    NullObject,
    RectangleObject,
    TextStringObject,
)

from .annotations import AnnotationType, PdfAnchor, PdfAnchorFragment
from .embed_state import file_sha256, save_embed_receipt
from .files import LoadedTextFile, TextFileError, load_source_file
from .pdfs import rebind_loaded_pdf
from .rematch import ParsedAnnotation, parse_annotation_records, text_sha256


COORDINATE_SYSTEM = "pdf-user-space-bottom-left"
DEFAULT_HIGHLIGHT_COLOR = (1.0, 1.0, 0.0)
ANNOTATION_AUTHOR = "Pure Text Annotator"
ANNOTATION_SUBJECT_PREFIX = "Pure Text Annotator"
PTA_TYPE_KEY = "/PTAType"
SUPPORTED_TEXT_MARKUP_SUBTYPES = frozenset(
    {"Highlight", "Underline", "Squiggly", "StrikeOut"}
)
GEOMETRY_TOLERANCE = 0.01
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_YAML_OPEN_RE = re.compile(r"(?m)^```yaml[ \t]*(?:\n|$)")
_YAML_CLOSE_RE = re.compile(r"(?m)^```[ \t]*(?:\n|$)")


class PdfEmbedError(RuntimeError):
    """Raised when exporting would be unsafe or produce an ambiguous PDF."""


def _annotation_subject(annotation_type: AnnotationType) -> str:
    """Return the redundant, human-readable PDF type marker."""

    return f"{ANNOTATION_SUBJECT_PREFIX} {annotation_type.value}"


@dataclass(frozen=True)
class PdfEmbedRecord:
    """One fully validated sidecar record ready for PDF export."""

    annotation_id: str
    annotation_type: AnnotationType
    start: int
    end: int
    quote: str
    note: str
    created_at: str
    pdf_date: str
    pdf_anchor: PdfAnchor
    subtype: str
    color: Tuple[float, ...]
    author: Optional[str]
    subject: Optional[str]


@dataclass(frozen=True)
class PdfEmbedResult:
    """Summary of a successful embedding operation."""

    source_path: Path
    annotations_path: Path
    output_path: Path
    annotation_count: int
    highlight_count: int


@dataclass(frozen=True)
class _PdfPageState:
    """Page properties that embedding highlights must never alter."""

    content_stream_sha256: Tuple[str, ...]
    media_box: Tuple[float, ...]
    crop_box: Tuple[float, ...]
    trim_box: Tuple[float, ...]
    bleed_box: Tuple[float, ...]
    art_box: Tuple[float, ...]
    rotation: int
    user_unit: float
    resource_interface: Tuple[Tuple[str, Tuple[str, ...]], ...]


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> Dict[Any, Any]:
    mapping: Dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def default_annotations_path(source_path: Path) -> Path:
    """Return the viewer's normal sidecar path for a source PDF."""

    source = Path(source_path).expanduser()
    return source.with_name(source.name + ".annotations.md")


def default_output_path(source_path: Path) -> Path:
    """Return the canonical in-place PDF synchronization target."""

    return Path(source_path).expanduser()


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise PdfEmbedError(f"{field} must be a YAML mapping with string keys")
    return value


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise PdfEmbedError(f"{field} must be {qualifier}")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PdfEmbedError(f"{field} must be an integer")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _string(value, field)
    if _SHA256_RE.fullmatch(digest) is None:
        raise PdfEmbedError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def _float_sequence(
    value: object, field: str, *, length: int
) -> Tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise PdfEmbedError(f"{field} must contain exactly {length} numbers")
    output = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise PdfEmbedError(f"{field} must contain only numbers")
        converted = float(item)
        if not math.isfinite(converted):
            raise PdfEmbedError(f"{field} must contain only finite numbers")
        output.append(converted)
    return tuple(output)


def _optional_string(value: object, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PdfEmbedError(f"{field} must be a string or null")
    return value or None


def _annotation_color(value: object, field: str) -> Tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) not in {0, 1, 3, 4}:
        raise PdfEmbedError(f"{field} must contain 0, 1, 3, or 4 numbers")
    output = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise PdfEmbedError(f"{field} must contain only numbers")
        converted = float(item)
        if not math.isfinite(converted) or not 0.0 <= converted <= 1.0:
            raise PdfEmbedError(f"{field} values must be between 0 and 1")
        output.append(converted)
    return tuple(output)


def _sidecar_pdf_sync_hash(sidecar_text: str) -> Optional[str]:
    if not sidecar_text.startswith("---\n"):
        return None
    closing = sidecar_text.find("\n---", 4)
    if closing < 0:
        return None
    front_matter = sidecar_text[4:closing]
    match = re.search(
        r'(?m)^pdf_sync_document_sha256:[ \t]*"([0-9a-f]{64})"[ \t]*$',
        front_matter,
    )
    return match.group(1) if match is not None else None


def _validate_rectangle(rectangle: Sequence[float], field: str) -> None:
    if rectangle[0] >= rectangle[2] or rectangle[1] >= rectangle[3]:
        raise PdfEmbedError(f"{field} must have increasing x and y coordinates")


def _pdf_date(value: object, field: str) -> Tuple[str, str]:
    created_at = _string(value, field)
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise PdfEmbedError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise PdfEmbedError(f"{field} must include a time-zone offset")
    utc = parsed.astimezone(timezone.utc)
    return created_at, utc.strftime("D:%Y%m%d%H%M%SZ")


def _metadata_for_record(sidecar_text: str, record: ParsedAnnotation) -> Mapping[str, Any]:
    block = sidecar_text[record.block_start : record.block_end]
    opening = _YAML_OPEN_RE.search(block)
    if opening is None:
        raise PdfEmbedError("missing YAML metadata block")
    closing = _YAML_CLOSE_RE.search(block, opening.end())
    if closing is None:
        raise PdfEmbedError("unterminated YAML metadata block")
    metadata_text = block[opening.end() : closing.start()]
    try:
        metadata = yaml.load(metadata_text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as error:
        problem = getattr(error, "problem", None) or str(error).splitlines()[0]
        raise PdfEmbedError(f"invalid YAML metadata: {problem}") from error
    return _mapping(metadata, "metadata")


def _parse_pdf_anchor(
    value: object,
    *,
    record_start: int,
    record_end: int,
    page_count: int,
) -> PdfAnchor:
    data = _mapping(value, "pdf_anchor")
    version = _integer(data.get("version"), "pdf_anchor.version")
    if version != 1:
        raise PdfEmbedError("pdf_anchor.version must be 1")
    document_sha256 = _sha256(
        data.get("document_sha256"), "pdf_anchor.document_sha256"
    )
    extractor = _mapping(data.get("extractor"), "pdf_anchor.extractor")
    extractor_name = _string(extractor.get("name"), "pdf_anchor.extractor.name")
    extractor_version = _string(
        extractor.get("version"), "pdf_anchor.extractor.version"
    )
    extraction_profile = _string(
        extractor.get("profile"), "pdf_anchor.extractor.profile"
    )
    coordinate_system = _string(
        data.get("coordinate_system"), "pdf_anchor.coordinate_system"
    )
    if coordinate_system != COORDINATE_SYSTEM:
        raise PdfEmbedError(
            f"pdf_anchor.coordinate_system must be {COORDINATE_SYSTEM!r}"
        )

    raw_fragments = data.get("fragments")
    if not isinstance(raw_fragments, list) or not raw_fragments:
        raise PdfEmbedError("pdf_anchor.fragments must be a non-empty list")

    fragments: List[PdfAnchorFragment] = []
    page_indexes = set()
    for ordinal, raw_fragment in enumerate(raw_fragments, start=1):
        prefix = f"pdf_anchor.fragments[{ordinal}]"
        fragment = _mapping(raw_fragment, prefix)
        page_index = _integer(fragment.get("page_index"), f"{prefix}.page_index")
        if not 0 <= page_index < page_count:
            raise PdfEmbedError(
                f"{prefix}.page_index {page_index} is outside the PDF"
            )
        if page_index in page_indexes:
            raise PdfEmbedError(
                f"pdf_anchor has more than one fragment for page {page_index}"
            )
        page_indexes.add(page_index)

        page_label_value = fragment.get("page_label")
        if page_label_value is not None and not isinstance(page_label_value, str):
            raise PdfEmbedError(f"{prefix}.page_label must be a string or null")
        page_text_sha256 = _sha256(
            fragment.get("page_text_sha256"), f"{prefix}.page_text_sha256"
        )
        text_range = _mapping(fragment.get("text_range"), f"{prefix}.text_range")
        text_start = _integer(text_range.get("start"), f"{prefix}.text_range.start")
        text_end = _integer(text_range.get("end"), f"{prefix}.text_range.end")
        if not record_start <= text_start < text_end <= record_end:
            raise PdfEmbedError(
                f"{prefix}.text_range must be inside the annotation range"
            )

        crop_box = _float_sequence(
            fragment.get("crop_box"), f"{prefix}.crop_box", length=4
        )
        _validate_rectangle(crop_box, f"{prefix}.crop_box")
        rotation = _integer(fragment.get("rotation"), f"{prefix}.rotation")
        if rotation not in (0, 90, 180, 270):
            raise PdfEmbedError(f"{prefix}.rotation must be 0, 90, 180, or 270")
        rect_pdf = _float_sequence(
            fragment.get("rect_pdf"), f"{prefix}.rect_pdf", length=4
        )
        _validate_rectangle(rect_pdf, f"{prefix}.rect_pdf")

        raw_quads = fragment.get("quad_points_pdf")
        if not isinstance(raw_quads, list) or not raw_quads:
            raise PdfEmbedError(f"{prefix}.quad_points_pdf must be a non-empty list")
        quads = tuple(
            _float_sequence(raw_quad, f"{prefix}.quad_points_pdf[{index}]", length=8)
            for index, raw_quad in enumerate(raw_quads, start=1)
        )
        fragments.append(
            PdfAnchorFragment(
                page_index=page_index,
                page_label=page_label_value,
                page_text_sha256=page_text_sha256,
                text_start=text_start,
                text_end=text_end,
                crop_box=crop_box,  # type: ignore[arg-type]
                rotation=rotation,
                rect_pdf=rect_pdf,  # type: ignore[arg-type]
                quad_points_pdf=quads,  # type: ignore[arg-type]
            )
        )

    if tuple(fragment.page_index for fragment in fragments) != tuple(
        sorted(page_indexes)
    ):
        raise PdfEmbedError("pdf_anchor.fragments must be ordered by page_index")

    return PdfAnchor(
        version=version,
        document_sha256=document_sha256,
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        extraction_profile=extraction_profile,
        coordinate_system=coordinate_system,
        fragments=tuple(fragments),
    )


def _close_numbers(left: Sequence[float], right: Sequence[float]) -> bool:
    return len(left) == len(right) and all(
        math.isclose(a, b, rel_tol=0.0, abs_tol=GEOMETRY_TOLERANCE)
        for a, b in zip(left, right)
    )


def _anchors_match(stored: PdfAnchor, expected: PdfAnchor) -> bool:
    if (
        stored.version != expected.version
        or stored.document_sha256 != expected.document_sha256
        or stored.extractor_name != expected.extractor_name
        or stored.extractor_version != expected.extractor_version
        or stored.extraction_profile != expected.extraction_profile
        or stored.coordinate_system != expected.coordinate_system
        or len(stored.fragments) != len(expected.fragments)
    ):
        return False
    for left, right in zip(stored.fragments, expected.fragments):
        if (
            left.page_index != right.page_index
            or left.page_label != right.page_label
            or left.page_text_sha256 != right.page_text_sha256
            or left.text_start != right.text_start
            or left.text_end != right.text_end
            or left.rotation != right.rotation
            or not _close_numbers(left.crop_box, right.crop_box)
            or not _close_numbers(left.rect_pdf, right.rect_pdf)
            or len(left.quad_points_pdf) != len(right.quad_points_pdf)
            or any(
                not _close_numbers(left_quad, right_quad)
                for left_quad, right_quad in zip(
                    left.quad_points_pdf, right.quad_points_pdf
                )
            )
        ):
            return False
    return True


def _record_error(record: ParsedAnnotation, message: str) -> str:
    return f"annotation {record.annotation_id!r}: {message}"


def parse_pdf_embed_records(
    sidecar_text: str,
    *,
    source_text: str,
    pdf_document: object,
) -> Tuple[PdfEmbedRecord, ...]:
    """Parse and verify every record against one extracted PDF projection."""

    document_sha = getattr(pdf_document, "document_sha256", None)
    page_count = getattr(pdf_document, "page_count", None)
    anchor_for_range = getattr(pdf_document, "anchor_for_range", None)
    if (
        not isinstance(document_sha, str)
        or not isinstance(page_count, int)
        or not callable(anchor_for_range)
    ):
        raise PdfEmbedError("source does not provide PDF geometry")

    parsed_records = parse_annotation_records(sidecar_text)

    canonical_sha = text_sha256(source_text)
    output: List[PdfEmbedRecord] = []
    errors: List[str] = []
    for record in parsed_records:
        try:
            if record.errors:
                raise PdfEmbedError("; ".join(record.errors))
            if record.start is None or record.end is None:
                raise PdfEmbedError("missing source range")
            if record.quote is None:
                raise PdfEmbedError("missing quoted original text")
            if record.note is None:
                raise PdfEmbedError("note section is missing")
            if (
                record.annotation_type is AnnotationType.NOTE
                and not record.note.strip()
            ):
                raise PdfEmbedError("note content is empty")
            if not 0 <= record.start < record.end <= len(source_text):
                raise PdfEmbedError("source range is outside the extracted PDF text")
            if source_text[record.start : record.end] != record.quote:
                raise PdfEmbedError(
                    "quoted original text no longer matches its source range; "
                    "review it with R in pta-view"
                )

            metadata = _metadata_for_record(sidecar_text, record)
            metadata_id = _string(metadata.get("id"), "id")
            if metadata_id != record.annotation_id:
                raise PdfEmbedError("marker ID and metadata ID differ")
            if metadata.get("type", "note") != record.annotation_type.value:
                raise PdfEmbedError("parsed and YAML annotation types differ")
            range_data = _mapping(metadata.get("range"), "range")
            if (
                _integer(range_data.get("start"), "range.start") != record.start
                or _integer(range_data.get("end"), "range.end") != record.end
            ):
                raise PdfEmbedError("parsed and YAML source ranges differ")

            created_at, pdf_date = _pdf_date(metadata.get("created_at"), "created_at")
            anchor_data = _mapping(metadata.get("anchor"), "anchor")
            source_sha = _sha256(anchor_data.get("source_sha256"), "anchor.source_sha256")
            if source_sha != canonical_sha:
                raise PdfEmbedError(
                    "canonical source hash is stale; review it with R in pta-view"
                )
            quote_sha = _sha256(anchor_data.get("quote_sha256"), "anchor.quote_sha256")
            if quote_sha != text_sha256(record.quote):
                raise PdfEmbedError("quoted original text hash is stale")

            pdf_anchor = _parse_pdf_anchor(
                metadata.get("pdf_anchor"),
                record_start=record.start,
                record_end=record.end,
                page_count=page_count,
            )
            if pdf_anchor.document_sha256 != document_sha:
                raise PdfEmbedError(
                    "PDF byte hash does not match the source document"
                )
            expected_anchor = anchor_for_range(record.start, record.end)
            if expected_anchor is None:
                raise PdfEmbedError("source range has no highlightable PDF text")
            if not _anchors_match(pdf_anchor, expected_anchor):
                raise PdfEmbedError(
                    "PDF anchor geometry is stale or does not match this PDF"
                )

            subtype = "Highlight"
            color = DEFAULT_HIGHLIGHT_COLOR
            author: Optional[str] = ANNOTATION_AUTHOR
            subject: Optional[str] = _annotation_subject(
                record.annotation_type
            )
            pdf_import_value = metadata.get("pdf_import")
            if pdf_import_value is not None:
                pdf_import = _mapping(pdf_import_value, "pdf_import")
                if _integer(pdf_import.get("version"), "pdf_import.version") != 1:
                    raise PdfEmbedError("pdf_import.version must be 1")
                _sha256(
                    pdf_import.get("source_document_sha256"),
                    "pdf_import.source_document_sha256",
                )
                _sha256(
                    pdf_import.get("fingerprint"),
                    "pdf_import.fingerprint",
                )
                subtype = _string(pdf_import.get("subtype"), "pdf_import.subtype")
                if subtype not in SUPPORTED_TEXT_MARKUP_SUBTYPES:
                    raise PdfEmbedError(
                        f"pdf_import.subtype {subtype!r} is not supported"
                    )
                color = _annotation_color(
                    pdf_import.get("color"), "pdf_import.color"
                )
                author = _optional_string(
                    pdf_import.get("author"), "pdf_import.author"
                )
                subject = _optional_string(
                    pdf_import.get("subject"), "pdf_import.subject"
                )

            output.append(
                PdfEmbedRecord(
                    annotation_id=record.annotation_id,
                    annotation_type=record.annotation_type,
                    start=record.start,
                    end=record.end,
                    quote=record.quote,
                    note=record.note,
                    created_at=created_at,
                    pdf_date=pdf_date,
                    pdf_anchor=pdf_anchor,
                    subtype=subtype,
                    color=color,
                    author=author,
                    subject=subject,
                )
            )
        except PdfEmbedError as error:
            errors.append(_record_error(record, str(error)))

    if errors:
        raise PdfEmbedError("sidecar validation failed:\n- " + "\n- ".join(errors))
    return tuple(output)


def _resolve(value: object) -> object:
    get_object = getattr(value, "get_object", None)
    return get_object() if callable(get_object) else value


def _page_annotations(page: object) -> Tuple[DictionaryObject, ...]:
    try:
        annotations = page.get("/Annots")  # type: ignore[attr-defined]
        if annotations is None:
            return ()
        resolved = _resolve(annotations)
        return tuple(
            annotation
            for raw in resolved  # type: ignore[union-attr]
            for annotation in (_resolve(raw),)
            if isinstance(annotation, DictionaryObject)
        )
    except Exception as error:
        raise PdfEmbedError(f"cannot inspect existing PDF annotations: {error}") from error


def _clone_document(reader: PdfReader, writer: PdfWriter) -> None:
    """Clone a PDF while tolerating Preview's non-standard catalog ``/Info``."""

    root = reader.root_object
    info_key = NameObject("/Info")
    catalog_info = root.raw_get(info_key) if info_key in root else None
    if catalog_info is not None:
        # macOS Preview append-mode saves can place an indirect-to-indirect
        # metadata reference under the Catalog. /Info belongs in the trailer,
        # and pypdf cannot clone that non-standard Catalog value. Temporarily
        # omit only this invalid key; page content and annotations are retained
        # and verified after writing.
        del root[info_key]
    try:
        writer.clone_document_from_reader(reader)
    finally:
        if catalog_info is not None:
            root[info_key] = catalog_info


def _has_digital_signature(reader: PdfReader) -> bool:
    root = reader.root_object
    permissions = _resolve(root.get("/Perms"))
    if isinstance(permissions, DictionaryObject) and any(
        key in permissions for key in ("/DocMDP", "/UR3")
    ):
        return True

    form = _resolve(root.get("/AcroForm"))
    if not isinstance(form, DictionaryObject):
        return False
    fields = _resolve(form.get("/Fields"))
    if not isinstance(fields, (list, ArrayObject)):
        return False

    seen = set()

    def signed(field_reference: object, inherited_type: Optional[str] = None) -> bool:
        field = _resolve(field_reference)
        if not isinstance(field, DictionaryObject):
            return False
        reference = getattr(field_reference, "idnum", None)
        identity = ("ref", reference) if reference is not None else ("obj", id(field))
        if identity in seen:
            return False
        seen.add(identity)
        field_type_value = field.get("/FT", inherited_type)
        field_type = str(field_type_value) if field_type_value is not None else None
        value = field.get("/V")
        if field_type == "/Sig" and value is not None:
            resolved_value = _resolve(value)
            if not isinstance(resolved_value, NullObject):
                return True
        children = _resolve(field.get("/Kids"))
        if isinstance(children, (list, ArrayObject)):
            return any(signed(child, field_type) for child in children)
        return False

    return any(signed(field) for field in fields)


def _supported_annotation_count(reader: PdfReader) -> int:
    return sum(
        str(annotation.get("/Subtype", "")).removeprefix("/")
        in SUPPORTED_TEXT_MARKUP_SUBTYPES
        for page in reader.pages
        for annotation in _page_annotations(page)
    )


def _reference_key(value: object) -> Optional[Tuple[int, int]]:
    idnum = getattr(value, "idnum", None)
    generation = getattr(value, "generation", None)
    if isinstance(idnum, int) and isinstance(generation, int):
        return idnum, generation
    return None


def _remove_supported_text_markups(
    writer: PdfWriter, records: Sequence[PdfEmbedRecord]
) -> Tuple[int, ...]:
    """Remove mirrored markups while preserving unrelated PDF annotations."""

    targets_by_page: Dict[int, set[str]] = {}
    for record in records:
        for fragment in record.pdf_anchor.fragments:
            targets_by_page.setdefault(fragment.page_index, set()).add(
                record.annotation_id
            )

    preserved_counts = []
    collisions = []
    for page_index, page in enumerate(writer.pages):
        raw_annotations = page.get("/Annots")
        if raw_annotations is None:
            preserved_counts.append(0)
            continue
        resolved = _resolve(raw_annotations)
        raw_items = tuple(resolved)  # type: ignore[arg-type]
        removed_references = {
            key
            for raw in raw_items
            for key in (_reference_key(raw),)
            if key is not None
            and str(_resolve(raw).get("/Subtype", "")).removeprefix("/")
            in SUPPORTED_TEXT_MARKUP_SUBTYPES
        }
        kept = ArrayObject()
        for raw in raw_items:
            annotation = _resolve(raw)
            if not isinstance(annotation, DictionaryObject):
                kept.append(raw)
                continue
            subtype = str(annotation.get("/Subtype", "")).removeprefix("/")
            if subtype in SUPPORTED_TEXT_MARKUP_SUBTYPES:
                continue
            if subtype == "Popup":
                parent_key = _reference_key(annotation.raw_get("/Parent")) if "/Parent" in annotation else None
                if parent_key in removed_references:
                    continue
            annotation_name = annotation.get("/NM")
            if (
                annotation_name is not None
                and str(annotation_name) in targets_by_page.get(page_index, set())
            ):
                collisions.append(
                    f"{annotation_name!s} on page {page_index + 1}"
                )
            kept.append(raw)
        if kept:
            page[NameObject("/Annots")] = kept
        elif "/Annots" in page:
            del page[NameObject("/Annots")]
        preserved_counts.append(len(kept))
    if collisions:
        raise PdfEmbedError(
            "an unrelated PDF annotation uses a sidecar annotation ID: "
            + ", ".join(collisions)
        )
    return tuple(preserved_counts)


def _text_markup(
    record: PdfEmbedRecord, fragment: PdfAnchorFragment
) -> DictionaryObject:
    quad_points = ArrayObject(
        FloatObject(coordinate)
        for quad in fragment.quad_points_pdf
        for coordinate in quad
    )
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject(f"/{record.subtype}"),
            NameObject("/Rect"): RectangleObject(fragment.rect_pdf),
            NameObject("/QuadPoints"): quad_points,
            NameObject("/F"): NumberObject(4),
        }
    )
    annotation[NameObject("/Contents")] = TextStringObject(record.note)
    annotation[NameObject("/NM")] = TextStringObject(record.annotation_id)
    annotation[NameObject("/M")] = TextStringObject(record.pdf_date)
    annotation[NameObject("/CreationDate")] = TextStringObject(record.pdf_date)
    annotation[NameObject(PTA_TYPE_KEY)] = TextStringObject(
        record.annotation_type.value
    )
    if record.color:
        annotation[NameObject("/C")] = ArrayObject(
            FloatObject(value) for value in record.color
        )
    if record.author is not None:
        annotation[NameObject("/T")] = TextStringObject(record.author)
    if record.subject is not None:
        annotation[NameObject("/Subj")] = TextStringObject(record.subject)
    return annotation


def _numbers(value: object, field: str) -> Tuple[float, ...]:
    resolved = _resolve(value)
    try:
        return tuple(float(item) for item in resolved)  # type: ignore[union-attr]
    except (TypeError, ValueError) as error:
        raise PdfEmbedError(f"written PDF has invalid {field}") from error


def _page_content_stream_hashes(page: object) -> Tuple[str, ...]:
    contents = page.get("/Contents")  # type: ignore[attr-defined]
    if contents is None:
        return ()
    resolved = _resolve(contents)
    streams = resolved if isinstance(resolved, (list, ArrayObject)) else (resolved,)
    hashes = []
    for raw_stream in streams:
        stream = _resolve(raw_stream)
        get_data = getattr(stream, "get_data", None)
        if not callable(get_data):
            raise PdfEmbedError("PDF page contains an invalid content stream")
        data = get_data()
        if not isinstance(data, bytes):
            raise PdfEmbedError("PDF page content stream did not decode to bytes")
        hashes.append(hashlib.sha256(data).hexdigest())
    return tuple(hashes)


def _page_resource_interface(
    page: object,
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    resources = _resolve(page.get("/Resources"))  # type: ignore[attr-defined]
    if resources is None:
        return ()
    if not isinstance(resources, DictionaryObject):
        raise PdfEmbedError("PDF page contains an invalid resource dictionary")
    interface = []
    for raw_category in sorted(resources, key=str):
        category = str(raw_category)
        value = _resolve(resources[raw_category])
        if isinstance(value, DictionaryObject):
            members = ("dictionary",) + tuple(sorted(map(str, value)))
        elif isinstance(value, (list, ArrayObject)):
            members = ("array",) + tuple(str(_resolve(item)) for item in value)
        else:
            members = ("value", str(value))
        interface.append((category, members))
    return tuple(interface)


def _page_box(page: object, name: str) -> Tuple[float, ...]:
    return tuple(float(value) for value in getattr(page, name))


def _page_state(page: object) -> _PdfPageState:
    """Fingerprint render instructions without re-extracting PDF text."""

    try:
        return _PdfPageState(
            content_stream_sha256=_page_content_stream_hashes(page),
            media_box=_page_box(page, "mediabox"),
            crop_box=_page_box(page, "cropbox"),
            trim_box=_page_box(page, "trimbox"),
            bleed_box=_page_box(page, "bleedbox"),
            art_box=_page_box(page, "artbox"),
            rotation=int(page.rotation),  # type: ignore[attr-defined]
            user_unit=float(page.get("/UserUnit", 1)),  # type: ignore[attr-defined]
            resource_interface=_page_resource_interface(page),
        )
    except PdfEmbedError:
        raise
    except Exception as error:
        raise PdfEmbedError(f"cannot fingerprint PDF page content: {error}") from error


def _verify_output(
    path: Path,
    *,
    source_pages: Sequence[_PdfPageState],
    existing_annotation_counts: Sequence[int],
    records: Sequence[PdfEmbedRecord],
) -> None:
    try:
        reader = PdfReader(path)
    except Exception as error:
        raise PdfEmbedError(f"cannot reopen generated PDF: {error}") from error
    if reader.is_encrypted:
        raise PdfEmbedError("generated PDF is unexpectedly encrypted")
    if len(reader.pages) != len(source_pages):
        raise PdfEmbedError("generated PDF page count differs from the source")

    for page_index, (page, source_page) in enumerate(
        zip(reader.pages, source_pages)
    ):
        if _page_state(page) != source_page:
            raise PdfEmbedError(
                "generated PDF page content differs from the source on page "
                f"{page_index + 1}"
            )

    additions: Dict[int, int] = {}
    for record in records:
        for fragment in record.pdf_anchor.fragments:
            additions[fragment.page_index] = additions.get(fragment.page_index, 0) + 1
            page_annotations = _page_annotations(reader.pages[fragment.page_index])
            matches = [
                annotation
                for annotation in page_annotations
                if str(annotation.get("/NM", "")) == record.annotation_id
            ]
            if len(matches) != 1:
                raise PdfEmbedError(
                    "generated PDF is missing a unique highlight for "
                    f"{record.annotation_id!r} on page {fragment.page_index + 1}"
                )
            annotation = matches[0]
            if str(annotation.get("/Subtype")) != f"/{record.subtype}":
                raise PdfEmbedError(
                    "generated PDF annotation subtype does not match the sidecar"
                )
            if str(annotation.get("/Contents", "")) != record.note:
                raise PdfEmbedError("generated PDF annotation note does not match")
            if (
                str(annotation.get("/T")) if annotation.get("/T") is not None else None
            ) != record.author:
                raise PdfEmbedError("generated PDF annotation author does not match")
            if (
                str(annotation.get("/Subj"))
                if annotation.get("/Subj") is not None
                else None
            ) != record.subject:
                raise PdfEmbedError("generated PDF annotation subject does not match")
            if str(annotation.get(PTA_TYPE_KEY, "")) != record.annotation_type.value:
                raise PdfEmbedError(
                    "generated PDF annotation type marker does not match"
                )
            if str(annotation.get("/M", "")) != record.pdf_date:
                raise PdfEmbedError("generated PDF annotation timestamp does not match")
            if not _close_numbers(
                _numbers(annotation.get("/Rect"), "/Rect"), fragment.rect_pdf
            ):
                raise PdfEmbedError("generated PDF highlight rectangle does not match")
            expected_quads = tuple(
                coordinate
                for quad in fragment.quad_points_pdf
                for coordinate in quad
            )
            if not _close_numbers(
                _numbers(annotation.get("/QuadPoints"), "/QuadPoints"),
                expected_quads,
            ):
                raise PdfEmbedError("generated PDF highlight geometry does not match")
            if record.color and not _close_numbers(
                _numbers(annotation.get("/C"), "/C"),
                record.color,
            ):
                raise PdfEmbedError("generated PDF annotation color does not match")

    for page_index, page in enumerate(reader.pages):
        actual = len(_page_annotations(page))
        expected = existing_annotation_counts[page_index] + additions.get(page_index, 0)
        if actual != expected:
            raise PdfEmbedError(
                f"generated PDF annotation count differs on page {page_index + 1}"
            )


def embed_pdf_annotations(
    source_path: Path,
    *,
    annotations_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    force: bool = False,
    loaded_source: Optional[LoadedTextFile] = None,
) -> PdfEmbedResult:
    """Reconcile every valid sidecar record into an atomically written PDF."""

    source = Path(source_path).expanduser().resolve()
    annotations = (
        Path(annotations_path).expanduser().resolve()
        if annotations_path is not None
        else default_annotations_path(source).resolve()
    )
    output = (
        Path(output_path).expanduser().resolve()
        if output_path is not None
        else default_output_path(source).resolve()
    )
    in_place = output == source

    if source.suffix.lower() != ".pdf":
        raise PdfEmbedError(f"source must be a PDF: {source}")
    if output.suffix.lower() != ".pdf":
        raise PdfEmbedError(f"output must use the .pdf extension: {output}")
    if output == annotations:
        raise PdfEmbedError("output PDF and annotation sidecar must be different files")
    if not source.exists() or not source.is_file():
        raise PdfEmbedError(f"source PDF does not exist or is not a file: {source}")
    if not annotations.exists() or not annotations.is_file():
        raise PdfEmbedError(f"annotation sidecar does not exist: {annotations}")
    if not output.parent.exists() or not output.parent.is_dir():
        raise PdfEmbedError(f"output directory does not exist: {output.parent}")
    if output.exists() and not in_place and not force:
        raise PdfEmbedError(
            f"output already exists: {output}; pass --force to replace it"
        )

    try:
        source_bytes = source.read_bytes()
        sidecar_bytes = annotations.read_bytes()
    except OSError as error:
        raise PdfEmbedError(f"cannot read input files: {error}") from error
    try:
        sidecar_text = sidecar_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PdfEmbedError(
            f"annotation sidecar is not valid UTF-8: {annotations}"
        ) from error
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    annotations_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()

    try:
        reader = PdfReader(io.BytesIO(source_bytes))
        if reader.is_encrypted:
            raise PdfEmbedError("encrypted PDFs are not supported for embedding")
        if _has_digital_signature(reader):
            raise PdfEmbedError(
                "signed PDFs are not modified because embedding would invalidate "
                "signatures"
            )
    except PdfEmbedError:
        raise
    except Exception as error:
        raise PdfEmbedError(f"cannot inspect source PDF: {error}") from error

    synced_revision = _sidecar_pdf_sync_hash(sidecar_text)
    supported_annotation_count = _supported_annotation_count(reader)
    if synced_revision is None and supported_annotation_count:
        raise PdfEmbedError(
            "the source PDF contains text-markup annotations not yet reconciled "
            "with the sidecar; run pta-import first"
        )
    if synced_revision is not None and synced_revision != source_sha256:
        raise PdfEmbedError(
            "the source PDF changed after the last sidecar reconciliation; "
            "run pta-import and review those PDF edits before embedding"
        )

    if loaded_source is None:
        try:
            loaded_source = load_source_file(source)
        except TextFileError as error:
            raise PdfEmbedError(str(error)) from error
    elif (
        loaded_source.path.resolve() != source
        or loaded_source.source_kind != "pdf"
        or loaded_source.pdf_document is None
        or loaded_source.pdf_document.document_sha256 != source_sha256
    ):
        raise PdfEmbedError(
            "loaded PDF source no longer matches the source file on disk"
        )
    pdf_document = loaded_source.pdf_document
    if pdf_document is None:
        raise PdfEmbedError("source does not provide PDF geometry")
    records = parse_pdf_embed_records(
        sidecar_text,
        source_text=loaded_source.text,
        pdf_document=pdf_document,
    )
    source_pages = tuple(_page_state(page) for page in reader.pages)
    highlight_count = sum(len(record.pdf_anchor.fragments) for record in records)

    writer = PdfWriter()
    temporary_path: Optional[Path] = None
    try:
        _clone_document(reader, writer)
        existing_counts = _remove_supported_text_markups(writer, records)
        for record in records:
            for fragment in record.pdf_anchor.fragments:
                writer.add_annotation(
                    page_number=fragment.page_index,
                    annotation=_text_markup(record, fragment),
                )

        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".pdf",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), source.stat().st_mode & 0o666)
            writer.write(temporary)
            temporary.flush()
            os.fsync(temporary.fileno())

        _verify_output(
            temporary_path,
            source_pages=source_pages,
            existing_annotation_counts=existing_counts,
            records=records,
        )
        if source.read_bytes() != source_bytes:
            raise PdfEmbedError("source PDF changed while the output was being prepared")
        if annotations.read_bytes() != sidecar_bytes:
            raise PdfEmbedError(
                "annotation sidecar changed while the output was being prepared"
            )
        if output.exists() and not in_place and not force:
            raise PdfEmbedError(
                f"output appeared during export: {output}; pass --force to replace it"
            )
        output_sha256 = file_sha256(temporary_path)
        os.replace(temporary_path, output)
        temporary_path = None
        if in_place:
            try:
                from .pdf_import import PdfImportError, import_pdf_text_markups

                rebound_source = rebind_loaded_pdf(loaded_source)
                import_pdf_text_markups(
                    source,
                    annotations_path=annotations,
                    loaded_source=rebound_source,
                    allow_sidecar_changes=True,
                )
            except (PdfImportError, TextFileError, OSError) as error:
                restore_path: Optional[Path] = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=source.parent,
                        prefix=f".{source.name}.",
                        suffix=".rollback",
                        delete=False,
                    ) as restore:
                        restore_path = Path(restore.name)
                        os.fchmod(restore.fileno(), source.stat().st_mode & 0o666)
                        restore.write(source_bytes)
                        restore.flush()
                        os.fsync(restore.fileno())
                    os.replace(restore_path, source)
                    restore_path = None
                except OSError as rollback_error:
                    raise PdfEmbedError(
                        "source PDF was written, sidecar reconciliation failed, "
                        f"and PDF rollback also failed: {rollback_error}"
                    ) from error
                finally:
                    if restore_path is not None:
                        try:
                            restore_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                raise PdfEmbedError(
                    "sidecar reconciliation failed; the original source PDF "
                    f"was restored: {error}"
                ) from error

            final_source_sha256 = file_sha256(source)
            final_annotations_sha256 = file_sha256(annotations)
            if final_source_sha256 is not None:
                output_sha256 = final_source_sha256
            if final_annotations_sha256 is not None:
                annotations_sha256 = final_annotations_sha256
            source_sha256 = final_source_sha256 or source_sha256

        if output_sha256 is not None:
            save_embed_receipt(
                source,
                annotations,
                output,
                source_sha256=(
                    output_sha256 if in_place else source_sha256
                ),
                annotations_sha256=annotations_sha256,
                output_sha256=output_sha256,
                annotation_count=len(records),
                highlight_count=highlight_count,
            )
    except PdfEmbedError:
        raise
    except Exception as error:
        raise PdfEmbedError(f"cannot write synchronized PDF: {error}") from error
    finally:
        writer.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    return PdfEmbedResult(
        source_path=source,
        annotations_path=annotations,
        output_path=output,
        annotation_count=len(records),
        highlight_count=highlight_count,
    )
