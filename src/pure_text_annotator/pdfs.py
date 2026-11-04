"""Deterministic text-layer PDF extraction and geometry anchoring."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pdfplumber
from pdfplumber.utils.text import LIGATURES

from .annotations import PdfAnchor, PdfAnchorFragment, PdfQuad, PdfRectangle
from .files import LoadedTextFile, PathInput, TextFileError


EXTRACTION_PROFILE = "pta-canonical-v1"
COORDINATE_SYSTEM = "pdf-user-space-bottom-left"
PAGE_HEADING = "--- PDF page {page} of {total} ---"
LINE_TOLERANCE = 3.0


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


def load_pdf_file(path_input: PathInput) -> LoadedTextFile:
    """Extract a stable text projection and word geometry from a PDF."""

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

    text_parts: List[str] = []
    pages: List[PdfPage] = []
    words: List[PdfWordBox] = []
    cursor = 0

    def append(value: str) -> None:
        nonlocal cursor
        text_parts.append(value)
        cursor += len(value)

    try:
        with pdfplumber.open(path, unicode_norm="NFC") as pdf:
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
        document_sha256=hashlib.sha256(raw).hexdigest(),
        extractor_name="pdfplumber",
        extractor_version=pdfplumber.__version__,
        extraction_profile=EXTRACTION_PROFILE,
        pages=tuple(pages),
        words=tuple(words),
    )
    return LoadedTextFile(
        path=path.resolve(),
        text=text,
        size_bytes=len(raw),
        line_count=text.count("\n") + 1,
        source_kind="pdf",
        editable=False,
        pdf_document=document,
    )
