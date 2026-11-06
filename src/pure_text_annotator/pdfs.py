"""Deterministic text-layer PDF extraction and geometry anchoring."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple

import pdfplumber
from pdfplumber.utils.text import LIGATURES
from platformdirs import user_cache_path

from .annotations import PdfAnchor, PdfAnchorFragment, PdfQuad, PdfRectangle
from .files import LoadedTextFile, PathInput, TextFileError


EXTRACTION_PROFILE = "pta-canonical-v1"
COORDINATE_SYSTEM = "pdf-user-space-bottom-left"
PAGE_HEADING = "--- PDF page {page} of {total} ---"
LINE_TOLERANCE = 3.0
CACHE_SCHEMA_VERSION = 1
CACHE_MAGIC = b"PTA-PDF-CACHE\x00"
CACHE_DIRECTORY_ENV = "PTA_PDF_CACHE_DIR"
_CACHE_LENGTH = struct.Struct("<Q")
_CACHE_WORD = struct.Struct("<iiqqdddd")
_CACHE_MAX_HEADER_BYTES = 64 * 1024
_CACHE_MAX_PAGES_BYTES = 64 * 1024 * 1024
_CACHE_MAX_TEXT_BYTES = 1024 * 1024 * 1024
_CACHE_MAX_WORDS = 20_000_000
_CACHE_WRITE_BUFFER_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PdfWordBox:
    """A canonical word span and its page-local PDF rectangle."""

    page_index: int
    line_index: int
    start: int
    end: int
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class PdfPage:
    """Page metadata needed to interpret a geometry anchor later."""

    page_index: int
    page_label: str
    text_start: int
    text_end: int
    page_text_sha256: str
    crop_box: PdfRectangle
    rotation: int


@dataclass(frozen=True)
class PdfDocument:
    """The canonical extracted-text projection of one immutable PDF."""

    document_sha256: str
    extractor_name: str
    extractor_version: str
    extraction_profile: str
    pages: Tuple[PdfPage, ...]
    words: Tuple[PdfWordBox, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def anchor_for_range(self, start: int, end: int) -> Optional[PdfAnchor]:
        """Map a canonical text range to page rectangles and highlight quads."""

        if start >= end:
            return None
        page_words: Dict[int, List[Tuple[PdfWordBox, PdfRectangle]]] = {}
        page_ranges: Dict[int, List[Tuple[int, int]]] = {}
        for word in self.words:
            selected_start = max(start, word.start)
            selected_end = min(end, word.end)
            if selected_start >= selected_end:
                continue
            width = word.x1 - word.x0
            character_count = max(1, word.end - word.start)
            left_fraction = (selected_start - word.start) / character_count
            right_fraction = (selected_end - word.start) / character_count
            rect = (
                word.x0 + width * left_fraction,
                word.y0,
                word.x0 + width * right_fraction,
                word.y1,
            )
            page_words.setdefault(word.page_index, []).append((word, rect))
            page_ranges.setdefault(word.page_index, []).append(
                (selected_start, selected_end)
            )

        fragments = []
        page_lookup = {page.page_index: page for page in self.pages}
        for page_index in sorted(page_words):
            page = page_lookup[page_index]
            quads = _rectangles_to_quads(page_words[page_index])
            if not quads:
                continue
            x_values = [coordinate for quad in quads for coordinate in quad[0::2]]
            y_values = [coordinate for quad in quads for coordinate in quad[1::2]]
            text_ranges = page_ranges[page_index]
            fragments.append(
                PdfAnchorFragment(
                    page_index=page_index,
                    page_label=page.page_label,
                    page_text_sha256=page.page_text_sha256,
                    text_start=min(item[0] for item in text_ranges),
                    text_end=max(item[1] for item in text_ranges),
                    crop_box=page.crop_box,
                    rotation=page.rotation,
                    rect_pdf=(
                        min(x_values),
                        min(y_values),
                        max(x_values),
                        max(y_values),
                    ),
                    quad_points_pdf=quads,
                )
            )

        if not fragments:
            return None
        return PdfAnchor(
            version=1,
            document_sha256=self.document_sha256,
            extractor_name=self.extractor_name,
            extractor_version=self.extractor_version,
            extraction_profile=self.extraction_profile,
            coordinate_system=COORDINATE_SYSTEM,
            fragments=tuple(fragments),
        )


class _PdfCacheError(ValueError):
    """Raised internally when cached extraction data is invalid."""


def pdf_cache_directory(cache_dir: Optional[PathInput] = None) -> Path:
    """Return the directory used for content-addressed PDF extraction caches."""

    if cache_dir is not None:
        return Path(cache_dir).expanduser()
    configured = os.environ.get(CACHE_DIRECTORY_ENV)
    if configured:
        return Path(configured).expanduser()
    return user_cache_path(
        "pure-text-annotator", appauthor=False
    ) / "pdf-extractions"


def _cache_identity() -> str:
    identity = (
        f"{CACHE_SCHEMA_VERSION}\0pdfplumber\0{pdfplumber.__version__}"
        f"\0{EXTRACTION_PROFILE}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def pdf_cache_path(
    document_sha256: str, cache_dir: Optional[PathInput] = None
) -> Path:
    """Return the cache path for one exact PDF and extraction implementation."""

    return pdf_cache_directory(cache_dir) / (
        f"{document_sha256}.{_cache_identity()}.pta-pdf-cache.gz"
    )


def _read_exact(stream: BinaryIO, size: int, field: str) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise _PdfCacheError(f"truncated {field}")
    return value


def _write_blob(stream: BinaryIO, value: bytes) -> None:
    stream.write(_CACHE_LENGTH.pack(len(value)))
    stream.write(value)


def _read_blob(
    stream: BinaryIO, *, field: str, maximum_bytes: int
) -> bytes:
    raw_length = _read_exact(stream, _CACHE_LENGTH.size, f"{field} length")
    length = _CACHE_LENGTH.unpack(raw_length)[0]
    if length > maximum_bytes:
        raise _PdfCacheError(f"{field} is too large")
    return _read_exact(stream, length, field)


def _cache_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _PdfCacheError(f"{field} must be an integer")
    return value


def _cache_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _PdfCacheError(f"{field} must be a non-empty string")
    return value


def _cache_sha256(value: object, field: str) -> str:
    digest = _cache_string(value, field)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise _PdfCacheError(f"{field} must be a SHA-256 digest")
    return digest


def _cache_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _PdfCacheError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise _PdfCacheError(f"{field} must be finite")
    return result


def _decode_cache_pages(value: object, text: str) -> Tuple[PdfPage, ...]:
    if not isinstance(value, list) or not value:
        raise _PdfCacheError("pages must be a non-empty list")
    pages = []
    previous_end = 0
    for ordinal, raw_page in enumerate(value):
        if not isinstance(raw_page, list) or len(raw_page) != 7:
            raise _PdfCacheError(f"page {ordinal} has an invalid record")
        page_index = _cache_integer(raw_page[0], f"page {ordinal} index")
        if page_index != ordinal:
            raise _PdfCacheError("page indexes must be contiguous")
        page_label = _cache_string(raw_page[1], f"page {ordinal} label")
        text_start = _cache_integer(raw_page[2], f"page {ordinal} text start")
        text_end = _cache_integer(raw_page[3], f"page {ordinal} text end")
        if not previous_end <= text_start <= text_end <= len(text):
            raise _PdfCacheError(f"page {ordinal} text range is invalid")
        page_text_sha256 = _cache_sha256(
            raw_page[4], f"page {ordinal} text hash"
        )
        if hashlib.sha256(
            text[text_start:text_end].encode("utf-8")
        ).hexdigest() != page_text_sha256:
            raise _PdfCacheError(f"page {ordinal} text hash does not match")
        raw_crop_box = raw_page[5]
        if not isinstance(raw_crop_box, list) or len(raw_crop_box) != 4:
            raise _PdfCacheError(f"page {ordinal} crop box is invalid")
        crop_box = tuple(
            _cache_float(coordinate, f"page {ordinal} crop box")
            for coordinate in raw_crop_box
        )
        if crop_box[0] >= crop_box[2] or crop_box[1] >= crop_box[3]:
            raise _PdfCacheError(f"page {ordinal} crop box is invalid")
        rotation = _cache_integer(raw_page[6], f"page {ordinal} rotation")
        pages.append(
            PdfPage(
                page_index=page_index,
                page_label=page_label,
                text_start=text_start,
                text_end=text_end,
                page_text_sha256=page_text_sha256,
                crop_box=crop_box,  # type: ignore[arg-type]
                rotation=rotation,
            )
        )
        previous_end = text_end
    return tuple(pages)


def _decode_cache_words(
    stream: BinaryIO, *, pages: Tuple[PdfPage, ...], text_length: int
) -> Tuple[PdfWordBox, ...]:
    raw_count = _read_exact(stream, _CACHE_LENGTH.size, "word count")
    word_count = _CACHE_LENGTH.unpack(raw_count)[0]
    if word_count == 0 or word_count > _CACHE_MAX_WORDS:
        raise _PdfCacheError("word count is invalid")

    words = []
    remaining = word_count
    last_key = (-1, -1, -1)
    while remaining:
        batch_count = min(remaining, 8192)
        batch = _read_exact(
            stream,
            batch_count * _CACHE_WORD.size,
            "word geometry",
        )
        for raw_word in struct.iter_unpack(_CACHE_WORD.format, batch):
            page_index, line_index, start, end, x0, y0, x1, y1 = raw_word
            if not 0 <= page_index < len(pages):
                raise _PdfCacheError("word page index is invalid")
            if line_index < 0:
                raise _PdfCacheError("word line index is invalid")
            page = pages[page_index]
            if not page.text_start <= start < end <= page.text_end:
                raise _PdfCacheError("word text range is invalid")
            if not end <= text_length:
                raise _PdfCacheError("word text range exceeds the document")
            coordinates = (x0, y0, x1, y1)
            if any(not math.isfinite(value) for value in coordinates):
                raise _PdfCacheError("word geometry is not finite")
            key = (page_index, line_index, start)
            if key < last_key:
                raise _PdfCacheError("word geometry is not ordered")
            last_key = key
            words.append(
                PdfWordBox(
                    page_index=page_index,
                    line_index=line_index,
                    start=start,
                    end=end,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                )
            )
        remaining -= batch_count
    return tuple(words)


def _load_pdf_cache(
    path: Path,
    *,
    expected_document_sha256: str,
    expected_size_bytes: int,
) -> Optional[Tuple[str, PdfDocument]]:
    try:
        with gzip.open(path, "rb") as stream:
            if _read_exact(stream, len(CACHE_MAGIC), "cache signature") != CACHE_MAGIC:
                raise _PdfCacheError("cache signature does not match")
            header_bytes = _read_blob(
                stream,
                field="cache header",
                maximum_bytes=_CACHE_MAX_HEADER_BYTES,
            )
            header = json.loads(header_bytes.decode("utf-8"))
            if not isinstance(header, dict):
                raise _PdfCacheError("cache header is not a mapping")
            if _cache_integer(
                header.get("schema_version"), "cache schema"
            ) != CACHE_SCHEMA_VERSION:
                raise _PdfCacheError("cache schema does not match")
            if _cache_sha256(
                header.get("document_sha256"), "document hash"
            ) != expected_document_sha256:
                raise _PdfCacheError("document hash does not match")
            if _cache_integer(
                header.get("source_size_bytes"), "source size"
            ) != expected_size_bytes:
                raise _PdfCacheError("source size does not match")
            if _cache_string(
                header.get("extractor_name"), "extractor name"
            ) != "pdfplumber":
                raise _PdfCacheError("extractor name does not match")
            if _cache_string(
                header.get("extractor_version"), "extractor version"
            ) != pdfplumber.__version__:
                raise _PdfCacheError("extractor version does not match")
            if _cache_string(
                header.get("extraction_profile"), "extraction profile"
            ) != EXTRACTION_PROFILE:
                raise _PdfCacheError("extraction profile does not match")

            text_bytes = _read_blob(
                stream,
                field="canonical text",
                maximum_bytes=_CACHE_MAX_TEXT_BYTES,
            )
            text = text_bytes.decode("utf-8")
            pages_bytes = _read_blob(
                stream,
                field="page metadata",
                maximum_bytes=_CACHE_MAX_PAGES_BYTES,
            )
            pages = _decode_cache_pages(
                json.loads(pages_bytes.decode("utf-8")), text
            )
            words = _decode_cache_words(
                stream, pages=pages, text_length=len(text)
            )
            if stream.read(1):
                raise _PdfCacheError("cache has trailing data")
    except (OSError, EOFError, UnicodeError, ValueError, TypeError, struct.error):
        return None

    return (
        text,
        PdfDocument(
            document_sha256=expected_document_sha256,
            extractor_name="pdfplumber",
            extractor_version=pdfplumber.__version__,
            extraction_profile=EXTRACTION_PROFILE,
            pages=pages,
            words=words,
        ),
    )


def _store_pdf_cache(
    path: Path,
    *,
    text: str,
    document: PdfDocument,
    source_size_bytes: int,
) -> None:
    """Best-effort atomic cache write; failures never block the viewer."""

    if len(document.words) > _CACHE_MAX_WORDS:
        return
    text_bytes = text.encode("utf-8")
    if len(text_bytes) > _CACHE_MAX_TEXT_BYTES:
        return
    header = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "document_sha256": document.document_sha256,
        "source_size_bytes": source_size_bytes,
        "extractor_name": document.extractor_name,
        "extractor_version": document.extractor_version,
        "extraction_profile": document.extraction_profile,
    }
    pages = [
        [
            page.page_index,
            page.page_label,
            page.text_start,
            page.text_end,
            page.page_text_sha256,
            list(page.crop_box),
            page.rotation,
        ]
        for page in document.pages
    ]
    header_bytes = json.dumps(
        header, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    pages_bytes = json.dumps(
        pages, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if (
        len(header_bytes) > _CACHE_MAX_HEADER_BYTES
        or len(pages_bytes) > _CACHE_MAX_PAGES_BYTES
    ):
        return

    temporary_path: Optional[Path] = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), 0o600)
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=6,
                fileobj=temporary,
                mtime=0,
            ) as stream:
                stream.write(CACHE_MAGIC)
                _write_blob(stream, header_bytes)
                _write_blob(stream, text_bytes)
                _write_blob(stream, pages_bytes)
                stream.write(_CACHE_LENGTH.pack(len(document.words)))
                buffer = bytearray()
                for word in document.words:
                    buffer.extend(
                        _CACHE_WORD.pack(
                            word.page_index,
                            word.line_index,
                            word.start,
                            word.end,
                            word.x0,
                            word.y0,
                            word.x1,
                            word.y1,
                        )
                    )
                    if len(buffer) >= _CACHE_WRITE_BUFFER_BYTES:
                        stream.write(buffer)
                        buffer.clear()
                if buffer:
                    stream.write(buffer)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, OverflowError, struct.error, ValueError):
        pass
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _float_tuple(values: Sequence[object]) -> PdfRectangle:
    if len(values) != 4:
        raise ValueError("PDF page box must contain four coordinates")
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def _page_crop_box(page: object) -> PdfRectangle:
    crop_box = getattr(page, "cropbox", None)
    if crop_box is None:
        crop_box = getattr(page, "mediabox", None)
    if crop_box is None:
        crop_box = (0.0, 0.0, getattr(page, "width"), getattr(page, "height"))
    return _float_tuple(crop_box)


def _word_pdf_box(word: dict, page_height: float) -> PdfRectangle:
    """Return a word rectangle when character geometry is unavailable."""

    return (
        float(word["x0"]),
        page_height - float(word["bottom"]),
        float(word["x1"]),
        page_height - float(word["top"]),
    )


def _group_words(raw_words: Iterable[dict]) -> Tuple[Tuple[dict, ...], ...]:
    words = []
    for raw_word in raw_words:
        text = str(raw_word.get("text", "")).replace("\r", " ").replace("\n", " ")
        if not text:
            continue
        word = dict(raw_word)
        word["text"] = text
        words.append(word)
    words.sort(key=lambda word: (float(word["top"]), float(word["x0"])))

    lines: List[List[dict]] = []
    line_tops: List[float] = []
    for word in words:
        top = float(word["top"])
        if not lines or abs(top - line_tops[-1]) > LINE_TOLERANCE:
            lines.append([word])
            line_tops.append(top)
        else:
            lines[-1].append(word)
    for line in lines:
        line.sort(key=lambda word: float(word["x0"]))
    return tuple(tuple(line) for line in lines)


def _rectangles_to_quads(
    selected_words: Iterable[Tuple[PdfWordBox, PdfRectangle]],
) -> Tuple[PdfQuad, ...]:
    """Merge nearby boxes on one line and emit PDF text-markup quad order."""

    ordered = sorted(
        selected_words,
        key=lambda item: (item[0].line_index, item[1][0], item[1][1]),
    )
    merged: List[Tuple[int, PdfRectangle]] = []
    for word, rect in ordered:
        if merged and merged[-1][0] == word.line_index:
            previous_line, previous = merged[-1]
            height = max(previous[3] - previous[1], rect[3] - rect[1])
            if rect[0] - previous[2] <= max(3.0, height * 0.75):
                merged[-1] = (
                    previous_line,
                    (
                        min(previous[0], rect[0]),
                        min(previous[1], rect[1]),
                        max(previous[2], rect[2]),
                        max(previous[3], rect[3]),
                    ),
                )
                continue
        merged.append((word.line_index, rect))

    # PDF text markup uses upper-left, upper-right, lower-left, lower-right.
    return tuple(
        (x0, y1, x1, y1, x0, y0, x1, y0)
        for _line, (x0, y0, x1, y1) in merged
    )


def load_pdf_file(
    path_input: PathInput,
    *,
    use_cache: bool = True,
    rebuild_cache: bool = False,
    cache_dir: Optional[PathInput] = None,
) -> LoadedTextFile:
    """Load a stable PDF projection, reusing exact cached extraction data."""

    path = Path(path_input).expanduser()
    if not path.exists():
        raise TextFileError(f"file does not exist: {path}")
    if not path.is_file():
        raise TextFileError(f"not a regular file: {path}")
    if path.suffix.lower() != ".pdf":
        raise TextFileError(f"unsupported PDF file type: {path.suffix or '(none)'}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise TextFileError(f"cannot read {path}: {error}") from error
    document_sha256 = hashlib.sha256(raw).hexdigest()
    resolved_path = path.resolve()
    cache_path: Optional[Path] = None
    if use_cache:
        try:
            cache_path = pdf_cache_path(document_sha256, cache_dir)
            if not rebuild_cache:
                cached = _load_pdf_cache(
                    cache_path,
                    expected_document_sha256=document_sha256,
                    expected_size_bytes=len(raw),
                )
                if cached is not None:
                    cached_text, cached_document = cached
                    return LoadedTextFile(
                        path=resolved_path,
                        text=cached_text,
                        size_bytes=len(raw),
                        line_count=cached_text.count("\n") + 1,
                        source_kind="pdf",
                        editable=False,
                        pdf_document=cached_document,
                    )
        except (OSError, RuntimeError, ValueError):
            cache_path = None

    text_parts: List[str] = []
    pages: List[PdfPage] = []
    words: List[PdfWordBox] = []
    cursor = 0

    def append(value: str) -> None:
        nonlocal cursor
        text_parts.append(value)
        cursor += len(value)

    try:
        with pdfplumber.open(io.BytesIO(raw), unicode_norm="NFC") as pdf:
            total_pages = len(pdf.pages)
            if total_pages == 0:
                raise TextFileError(f"PDF contains no pages: {path}")
            for page_index, page in enumerate(pdf.pages):
                if page_index:
                    append("\n\n")
                append(
                    PAGE_HEADING.format(
                        page=page_index + 1,
                        total=total_pages,
                    )
                    + "\n"
                )
                body_start = cursor
                body_parts_start = len(text_parts)
                raw_words = page.extract_words(
                    x_tolerance=3,
                    y_tolerance=3,
                    use_text_flow=False,
                    split_at_punctuation=False,
                    expand_ligatures=True,
                    return_chars=True,
                )
                lines = _group_words(raw_words)
                for line_index, line in enumerate(lines):
                    if line_index:
                        append("\n")
                    for word_index, word in enumerate(line):
                        if word_index:
                            append(" ")
                        characters = word.get("chars") or ()
                        expanded_characters = tuple(
                            (
                                LIGATURES.get(
                                    str(character.get("text", "")),
                                    str(character.get("text", "")),
                                ),
                                character,
                            )
                            for character in characters
                        )
                        expanded_text = "".join(
                            character_text
                            for character_text, _character in expanded_characters
                        )
                        if expanded_characters and expanded_text == word["text"]:
                            for character_text, character in expanded_characters:
                                character_start = cursor
                                append(character_text)
                                character_end = cursor
                                if character_start == character_end:
                                    continue
                                words.append(
                                    PdfWordBox(
                                        page_index=page_index,
                                        line_index=line_index,
                                        start=character_start,
                                        end=character_end,
                                        x0=float(character["x0"]),
                                        y0=float(character["y0"]),
                                        x1=float(character["x1"]),
                                        y1=float(character["y1"]),
                                    )
                                )
                        else:
                            word_start = cursor
                            append(word["text"])
                            word_end = cursor
                            x0, y0, x1, y1 = _word_pdf_box(
                                word, float(page.height)
                            )
                            words.append(
                                PdfWordBox(
                                    page_index=page_index,
                                    line_index=line_index,
                                    start=word_start,
                                    end=word_end,
                                    x0=x0,
                                    y0=y0,
                                    x1=x1,
                                    y1=y1,
                                )
                            )
                body_end = cursor
                body_text = "".join(text_parts[body_parts_start:])
                pages.append(
                    PdfPage(
                        page_index=page_index,
                        page_label=str(page_index + 1),
                        text_start=body_start,
                        text_end=body_end,
                        page_text_sha256=hashlib.sha256(
                            body_text.encode("utf-8")
                        ).hexdigest(),
                        crop_box=_page_crop_box(page),
                        rotation=int(getattr(page, "rotation", 0) or 0),
                    )
                )
    except TextFileError:
        raise
    except Exception as error:
        raise TextFileError(f"cannot extract PDF text from {path}: {error}") from error

    if not words:
        raise TextFileError(
            "PDF has no extractable text layer; scanned PDFs require OCR, "
            "which is not supported yet"
        )

    text = "".join(text_parts)
    document = PdfDocument(
        document_sha256=document_sha256,
        extractor_name="pdfplumber",
        extractor_version=pdfplumber.__version__,
        extraction_profile=EXTRACTION_PROFILE,
        pages=tuple(pages),
        words=tuple(words),
    )
    loaded = LoadedTextFile(
        path=resolved_path,
        text=text,
        size_bytes=len(raw),
        line_count=text.count("\n") + 1,
        source_kind="pdf",
        editable=False,
        pdf_document=document,
    )
    if cache_path is not None:
        _store_pdf_cache(
            cache_path,
            text=text,
            document=document,
            source_size_bytes=len(raw),
        )
    return loaded
