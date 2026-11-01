"""Deterministic, review-first recovery for stale annotation ranges."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .annotations import AnnotationRange


class RematchStatus(str, Enum):
    """The relationship between one annotation and the current source."""

    HEALTHY = "Healthy"
    SHIFTED = "Shifted"
    MODIFIED = "Modified"
    AMBIGUOUS = "Ambiguous"
    ORPHANED = "Orphaned"
    MALFORMED = "Malformed"


@dataclass(frozen=True)
class ParsedAnnotation:
    """A complete annotation plus source spans needed for safe rewriting."""

    ordinal: int
    annotation_id: str
    start: Optional[int]
    end: Optional[int]
    quote: Optional[str]
    note: Optional[str]
    created_at: Optional[str]
    source_sha256: Optional[str]
    quote_sha256: Optional[str]
    prefix: Optional[str]
    suffix: Optional[str]
    original_start: Optional[int]
    original_end: Optional[int]
    block_start: int
    block_end: int
    errors: Tuple[str, ...]

    @property
    def source_range(self) -> Optional[AnnotationRange]:
        if self.start is None or self.end is None:
            return None
        return AnnotationRange(self.start, self.end)


@dataclass(frozen=True)
class RematchCandidate:
    """One explainable possible current source span."""

    source_range: AnnotationRange
    text: str
    confidence: int
    method: str
    evidence: Tuple[str, ...]
    quote_similarity: float
    context_similarity: float


@dataclass(frozen=True)
class RematchItem:
    """Read-only result for one parsed annotation."""

    annotation: ParsedAnnotation
    status: RematchStatus
    candidates: Tuple[RematchCandidate, ...]
    explanation: str

    @property
    def proposed(self) -> Optional[RematchCandidate]:
        return self.candidates[0] if self.candidates else None


@dataclass(frozen=True)
class RematchReport:
    """A fingerprinted scan that can be invalidated before applying."""

    source_sha256: str
    sidecar_sha256: str
    items: Tuple[RematchItem, ...]

    @property
    def stale_items(self) -> Tuple[RematchItem, ...]:
        return tuple(
            item for item in self.items if item.status is not RematchStatus.HEALTHY
        )

    def count(self, status: RematchStatus) -> int:
        return sum(item.status is status for item in self.items)


@dataclass(frozen=True)
class RematchChoice:
    """A candidate explicitly selected in the review screen."""

    annotation_id: str
    candidate: RematchCandidate


_MARKER_RE = re.compile(
    r'^<!-- pta:annotation (?P<attributes>.*?) -->[^\n]*(?:\n|$)',
    re.MULTILINE,
)
_END_MARKER_RE = re.compile(
    r'^<!-- /pta:annotation -->[ \t]*(?:\n|$)', re.MULTILINE
)
_JSON_STRING = r'("(?:\\.|[^"\\])*")'


def text_sha256(text: str) -> str:
    """Return the stable UTF-8 fingerprint used to invalidate stale scans."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_field(text: str, name: str) -> Optional[str]:
    match = re.search(
        rf"(?m)^[ \t]*{re.escape(name)}:[ \t]*{_JSON_STRING}[ \t]*$",
        text,
    )
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, str) else None


def _integer_field(text: str, name: str) -> Optional[int]:
    match = re.search(
        rf"(?m)^[ \t]*{re.escape(name)}:[ \t]*(\d+)[ \t]*$", text
    )
    return int(match.group(1)) if match is not None else None


def _trim_blank_bounds(text: str, start: int, end: int) -> Tuple[int, int]:
    """Trim blank physical lines while retaining exact content offsets."""

    while start < end:
        line_end = text.find("\n", start, end)
        line_end = end if line_end < 0 else line_end + 1
        if text[start:line_end].strip():
            break
        start = line_end
    while end > start:
        line_start = text.rfind("\n", start, max(start, end - 1)) + 1
        if text[line_start:end].strip():
            break
        end = line_start
    return start, end


def _decode_quote(segment: str) -> Optional[str]:
    lines = segment.splitlines()
    decoded = []
    for line in lines:
        if not line.startswith(">"):
            return None
        value = line[1:]
        if value.startswith(" "):
            value = value[1:]
        decoded.append(value)
    return "\n".join(decoded)


def _section_bounds(block: str) -> Optional[Tuple[int, int, int, int]]:
    original = re.search(r"(?m)^### Original text[ \t]*(?:\n|$)", block)
    note = re.search(r"(?m)^### Note[ \t]*(?:\n|$)", block)
    close = re.search(r"(?m)^<!-- /pta:annotation -->", block)
    if original is None or note is None or close is None or note.start() <= original.end():
        return None
    quote_start, quote_end = _trim_blank_bounds(
        block, original.end(), note.start()
    )
    note_start, note_end = _trim_blank_bounds(block, note.end(), close.start())
    return quote_start, quote_end, note_start, note_end


def _parse_block(
    block: str, *, ordinal: int, block_start: int, block_end: int
) -> ParsedAnnotation:
    errors: List[str] = []
    marker = _MARKER_RE.search(block)
    annotation_id = ""
    if marker is not None:
        marker_id = re.search(rf"\bid={_JSON_STRING}", marker.group("attributes"))
        if marker_id is not None:
            try:
                annotation_id = json.loads(marker_id.group(1))
            except json.JSONDecodeError:
                pass

    fence_open = re.search(r"(?m)^```yaml[ \t]*(?:\n|$)", block)
    fence_close = (
        re.search(r"(?m)^```[ \t]*(?:\n|$)", block[fence_open.end() :])
        if fence_open is not None
        else None
    )
    metadata = ""
    if fence_open is None or fence_close is None:
        errors.append("missing YAML metadata block")
    else:
        metadata_start = fence_open.end()
        metadata_end = metadata_start + fence_close.start()
        metadata = block[metadata_start:metadata_end]

    metadata_id = _json_field(metadata, "id")
    if not annotation_id:
        annotation_id = metadata_id or f"malformed-{ordinal}"
    elif metadata_id is not None and metadata_id != annotation_id:
        errors.append("marker ID and metadata ID differ")

    range_match = re.search(r"(?m)^range:[ \t]*(?:\n|$)", metadata)
    start: Optional[int] = None
    end: Optional[int] = None
    if range_match is None:
        errors.append("missing range metadata")
    else:
        tail = metadata[range_match.end() :]
        next_top_level = re.search(r"(?m)^(?=\S)", tail)
        range_body = tail[: next_top_level.start()] if next_top_level else tail
        start = _integer_field(range_body, "start")
        end = _integer_field(range_body, "end")
        if start is None or end is None or start >= end:
            errors.append("invalid source range")

    sections = _section_bounds(block)
    quote: Optional[str] = None
    note: Optional[str] = None
    if sections is None:
        errors.append("missing Original text or Note section")
    else:
        quote_start, quote_end, note_start, note_end = sections
        quote = _decode_quote(block[quote_start:quote_end].rstrip("\r\n"))
        note = block[note_start:note_end].rstrip("\r\n")
        if quote is None:
            errors.append("Original text is not a Markdown blockquote")
        elif not quote:
            errors.append("Original text is empty")

    anchor_match = re.search(r"(?m)^anchor:[ \t]*(?:\n|$)", metadata)
    anchor = ""
    if anchor_match is not None:
        tail = metadata[anchor_match.end() :]
        next_top_level = re.search(r"(?m)^(?=\S)", tail)
        anchor = tail[: next_top_level.start()] if next_top_level else tail

    return ParsedAnnotation(
        ordinal=ordinal,
        annotation_id=annotation_id,
        start=start,
        end=end,
        quote=quote,
        note=note,
        created_at=_json_field(metadata, "created_at"),
        source_sha256=_json_field(anchor, "source_sha256"),
        quote_sha256=_json_field(anchor, "quote_sha256"),
        prefix=_json_field(anchor, "prefix"),
        suffix=_json_field(anchor, "suffix"),
        original_start=_integer_field(anchor, "original_start"),
        original_end=_integer_field(anchor, "original_end"),
        block_start=block_start,
        block_end=block_end,
        errors=tuple(errors),
    )


def parse_annotation_records(sidecar_text: str) -> Tuple[ParsedAnnotation, ...]:
    """Parse complete records without modifying or normalizing the sidecar."""

    records = []
    cursor = 0
    ordinal = 1
    while True:
        marker = _MARKER_RE.search(sidecar_text, cursor)
        if marker is None:
            break
        end_marker = _END_MARKER_RE.search(sidecar_text, marker.end())
        if end_marker is None:
            block_end = len(sidecar_text)
        else:
            block_end = end_marker.end()
        block = sidecar_text[marker.start() : block_end]
        records.append(
            _parse_block(
                block,
                ordinal=ordinal,
                block_start=marker.start(),
                block_end=block_end,
            )
        )
        ordinal += 1
        if end_marker is None:
            break
        cursor = block_end
    counts: Dict[str, int] = {}
    for record in records:
        counts[record.annotation_id] = counts.get(record.annotation_id, 0) + 1
    return tuple(
        replace(
            record,
            errors=record.errors + ("duplicate annotation ID",),
        )
        if counts[record.annotation_id] > 1
        else record
        for record in records
    )


def _all_occurrences(text: str, needle: str) -> Tuple[int, ...]:
    if not needle:
        return ()
    occurrences = []
    cursor = 0
    while True:
        found = text.find(needle, cursor)
        if found < 0:
            return tuple(occurrences)
        occurrences.append(found)
        cursor = found + 1


def _normalized_with_map(text: str) -> Tuple[str, Tuple[Tuple[int, int], ...]]:
    """NFC-normalize and collapse whitespace, retaining original offsets."""

    output: List[str] = []
    offsets: List[Tuple[int, int]] = []
    index = 0
    pending_space: Optional[Tuple[int, int]] = None
    while index < len(text):
        if text[index].isspace():
            start = index
            while index < len(text) and text[index].isspace():
                index += 1
            pending_space = (start, index)
            continue

        if pending_space is not None and output:
            output.append(" ")
            offsets.append(pending_space)
        pending_space = None

        start = index
        index += 1
        while index < len(text) and unicodedata.combining(text[index]):
            index += 1
        normalized = unicodedata.normalize("NFC", text[start:index])
        for character in normalized:
            output.append(character)
            offsets.append((start, index))
    return "".join(output), tuple(offsets)


def _normalized_occurrences(source: str, quote: str) -> Tuple[AnnotationRange, ...]:
    normalized_source, offsets = _normalized_with_map(source)
    normalized_quote, _ = _normalized_with_map(quote)
    if not normalized_quote or not offsets:
        return ()
    ranges = []
    for start in _all_occurrences(normalized_source, normalized_quote):
        end_index = start + len(normalized_quote) - 1
        ranges.append(AnnotationRange(offsets[start][0], offsets[end_index][1]))
    return tuple(dict.fromkeys(ranges))


def _edge_similarity(expected: str, actual: str, *, suffix: bool) -> float:
    if not expected:
        return 1.0
    length = min(len(expected), len(actual), 80)
    if length == 0:
        return 0.0
    expected_edge = expected[-length:] if suffix else expected[:length]
    actual_edge = actual[-length:] if suffix else actual[:length]
    return difflib.SequenceMatcher(
        None, expected_edge, actual_edge, autojunk=False
    ).ratio()


def _context_similarity(
    source: str, record: ParsedAnnotation, source_range: AnnotationRange
) -> Tuple[float, Tuple[str, ...]]:
    scores = []
    evidence = []
    if record.prefix is not None:
        score = _edge_similarity(
            record.prefix,
            source[max(0, source_range.start - 80) : source_range.start],
            suffix=True,
        )
        scores.append(score)
        if score >= 0.95:
            evidence.append("prefix context agrees")
    if record.suffix is not None:
        score = _edge_similarity(
            record.suffix,
            source[source_range.end : source_range.end + 80],
            suffix=False,
        )
        scores.append(score)
        if score >= 0.95:
            evidence.append("suffix context agrees")
    if not scores:
        return 0.7, ("legacy record has no saved context",)
    return sum(scores) / len(scores), tuple(evidence)


def _candidate_confidence(
    source: str,
    record: ParsedAnnotation,
    source_range: AnnotationRange,
    *,
    quote_similarity: float,
    uniqueness: float,
    method: str,
) -> RematchCandidate:
    context, context_evidence = _context_similarity(source, record, source_range)
    assert record.start is not None
    distance_scale = max(100, len(source) // 3)
    proximity = max(
        0.0, 1.0 - abs(source_range.start - record.start) / distance_scale
    )
    neighbor_consistency = 0.75
    raw = 100 * (
        0.45 * quote_similarity
        + 0.20 * context
        + 0.15 * uniqueness
        + 0.10 * proximity
        + 0.10 * neighbor_consistency
    )
    quote = record.quote or ""
    penalties = []
    if len(quote.strip()) < 8:
        raw -= 15
        penalties.append("short quotation")
    if method == "normalized-exact":
        raw -= 5
        penalties.append("whitespace or Unicode normalization required")
    if method == "fuzzy" and quote_similarity < 0.75:
        raw -= 10
        penalties.append("substantial quotation change")
    if record.quote_sha256 and record.quote_sha256 != text_sha256(quote):
        raw -= 5
        penalties.append("stored quotation differs from its saved hash")
    if method == "exact" and uniqueness >= 0.99 and len(quote.strip()) >= 8:
        raw = max(raw, 90)
    confidence = int(max(0, min(100, 5 * round(raw / 5))))
    evidence = [
        "exact quotation" if quote_similarity == 1 else f"{quote_similarity:.0%} quotation similarity",
        *context_evidence,
    ]
    if uniqueness >= 0.99:
        evidence.append("unique candidate")
    elif uniqueness <= 0.25:
        evidence.append("competing candidates are close")
    evidence.extend(penalties)
    return RematchCandidate(
        source_range=source_range,
        text=source[source_range.start : source_range.end],
        confidence=confidence,
        method=method,
        evidence=tuple(evidence),
        quote_similarity=quote_similarity,
        context_similarity=context,
    )


def _score_ranges(
    source: str,
    record: ParsedAnnotation,
    ranges_and_similarity: Sequence[Tuple[AnnotationRange, float]],
    *,
    method: str,
) -> Tuple[RematchCandidate, ...]:
    if not ranges_and_similarity:
        return ()
    ranked = sorted(ranges_and_similarity, key=lambda value: value[1], reverse=True)
    context_scores = [
        _context_similarity(source, record, source_range)[0]
        for source_range, _ in ranked
    ]
    distinguishing_scores = [
        0.7 * similarity + 0.3 * context
        for (_, similarity), context in zip(ranked, context_scores)
    ]
    candidates = []
    for index, (source_range, similarity) in enumerate(ranked):
        competitors = [
            other_score
            for other_index, other_score in enumerate(distinguishing_scores)
            if other_index != index
        ]
        if not competitors:
            uniqueness = 1.0
        else:
            gap = max(0.0, distinguishing_scores[index] - max(competitors))
            uniqueness = min(1.0, gap / 0.10)
        candidates.append(
            _candidate_confidence(
                source,
                record,
                source_range,
                quote_similarity=similarity,
                uniqueness=uniqueness,
                method=method,
            )
        )
    candidates.sort(
        key=lambda candidate: (
            candidate.confidence,
            candidate.quote_similarity,
            candidate.context_similarity,
            -candidate.source_range.start,
        ),
        reverse=True,
    )
    return tuple(candidates[:5])


def _fuzzy_ranges(
    source: str, record: ParsedAnnotation
) -> Tuple[Tuple[AnnotationRange, float], ...]:
    quote = record.quote or ""
    if not quote or not source:
        return ()
    quote_length = len(quote)
    quote_tokens = re.findall(r"\w+|[^\w\s]", quote)
    expected_starts = {max(0, min(record.start or 0, len(source) - 1))}
    direct_scored: Dict[AnnotationRange, float] = {}

    for line_match in re.finditer(r"(?m)^.+$", source):
        line = line_match.group(0)
        if not max(1, quote_length // 2) <= len(line) <= max(
            20, quote_length * 2
        ):
            continue
        character_ratio = difflib.SequenceMatcher(
            None, quote, line, autojunk=False
        ).ratio()
        line_tokens = re.findall(r"\w+|[^\w\s]", line)
        token_ratio = difflib.SequenceMatcher(
            None, quote_tokens, line_tokens, autojunk=False
        ).ratio()
        similarity = 0.7 * character_ratio + 0.3 * token_ratio
        if similarity >= 0.55:
            line_range = AnnotationRange(line_match.start(), line_match.end())
            direct_scored[line_range] = similarity
            expected_starts.add(line_match.start())

    chunk_length = min(16, max(4, quote_length // 4))
    for chunk_start in {0, max(0, (quote_length - chunk_length) // 2), max(0, quote_length - chunk_length)}:
        chunk = quote[chunk_start : chunk_start + chunk_length]
        if not chunk.strip():
            continue
        for found in _all_occurrences(source, chunk):
            expected_starts.add(max(0, found - chunk_start))

    longest = difflib.SequenceMatcher(
        None, quote, source, autojunk=False
    ).find_longest_match(0, len(quote), 0, len(source))
    if longest.size >= max(3, min(12, quote_length // 5)):
        expected_starts.add(max(0, longest.b - longest.a))

    paragraph_starts = []
    for match in re.finditer(r"(?ms)(?:^|\n\s*\n)(.*?)(?=\n\s*\n|$)", source):
        paragraph = match.group(1)
        if paragraph and max(1, quote_length // 3) <= len(paragraph) <= max(
            20, quote_length * 3
        ):
            ratio = difflib.SequenceMatcher(
                None, quote, paragraph, autojunk=False
            ).ratio()
            if ratio >= 0.45:
                paragraph_starts.append((ratio, match.start(1)))
    expected_starts.update(
        start
        for _, start in sorted(paragraph_starts, reverse=True)[:8]
    )

    expected_starts = set(
        sorted(
            expected_starts,
            key=lambda start: (
                difflib.SequenceMatcher(
                    None,
                    quote,
                    source[start : start + quote_length],
                    autojunk=False,
                ).quick_ratio(),
                -abs(start - (record.start or 0)),
            ),
            reverse=True,
        )[:24]
    )

    radius = min(20, max(5, quote_length // 5))
    step = max(1, radius // 8)
    scored: Dict[AnnotationRange, float] = dict(direct_scored)
    for expected in expected_starts:
        starts = range(max(0, expected - radius), min(len(source), expected + radius) + 1, step)
        length_deltas = range(-radius, radius + 1, step)
        for start in starts:
            for delta in length_deltas:
                length = quote_length + delta
                end = min(len(source), start + length)
                if length <= 0 or start >= end:
                    continue
                candidate_text = source[start:end]
                character_matcher = difflib.SequenceMatcher(
                    None, quote, candidate_text, autojunk=False
                )
                if character_matcher.quick_ratio() < 0.45:
                    continue
                character_ratio = character_matcher.ratio()
                candidate_tokens = re.findall(r"\w+|[^\w\s]", candidate_text)
                token_ratio = difflib.SequenceMatcher(
                    None, quote_tokens, candidate_tokens, autojunk=False
                ).ratio()
                similarity = 0.7 * character_ratio + 0.3 * token_ratio
                source_range = AnnotationRange(start, end)
                if similarity > scored.get(source_range, 0):
                    scored[source_range] = similarity

    ranked = sorted(scored.items(), key=lambda item: item[1], reverse=True)
    distinct: List[Tuple[AnnotationRange, float]] = []
    for source_range, similarity in ranked:
        if similarity < 0.55:
            break
        if any(
            abs(source_range.start - previous.start) <= 2
            and abs(source_range.end - previous.end) <= 2
            for previous, _ in distinct
        ):
            continue
        distinct.append((source_range, similarity))
        if len(distinct) >= 8:
            break
    return tuple(distinct)


def _scan_record(source: str, record: ParsedAnnotation) -> RematchItem:
    if record.errors or record.start is None or record.end is None or record.quote is None:
        return RematchItem(
            annotation=record,
            status=RematchStatus.MALFORMED,
            candidates=(),
            explanation="; ".join(record.errors) or "record could not be parsed",
        )

    if (
        0 <= record.start < record.end <= len(source)
        and source[record.start : record.end] == record.quote
    ):
        return RematchItem(
            annotation=record,
            status=RematchStatus.HEALTHY,
            candidates=(),
            explanation="recorded range still exactly matches its quotation",
        )

    exact_ranges = tuple(
        AnnotationRange(start, start + len(record.quote))
        for start in _all_occurrences(source, record.quote)
    )
    if exact_ranges:
        exact = _score_ranges(
            source,
            record,
            tuple((source_range, 1.0) for source_range in exact_ranges),
            method="exact",
        )
        decisive = len(exact) == 1 or (
            exact[0].confidence >= 80
            and exact[0].confidence - exact[1].confidence >= 10
            and exact[0].context_similarity >= 0.75
        )
        return RematchItem(
            annotation=record,
            status=RematchStatus.SHIFTED if decisive else RematchStatus.AMBIGUOUS,
            candidates=exact,
            explanation=(
                "exact quotation found at a new range"
                if decisive
                else "quotation occurs in multiple plausible locations"
            ),
        )

    normalized_ranges = _normalized_occurrences(source, record.quote)
    if normalized_ranges:
        normalized = _score_ranges(
            source,
            record,
            tuple((source_range, 0.98) for source_range in normalized_ranges),
            method="normalized-exact",
        )
        decisive = normalized[0].confidence >= 75 and (
            len(normalized) == 1
            or normalized[0].confidence - normalized[1].confidence >= 15
        )
        return RematchItem(
            annotation=record,
            status=RematchStatus.MODIFIED if decisive else RematchStatus.AMBIGUOUS,
            candidates=normalized,
            explanation=(
                "quotation matches after conservative normalization"
                if decisive
                else "normalized quotation has competing locations"
            ),
        )

    fuzzy_ranges = _fuzzy_ranges(source, record)
    fuzzy = _score_ranges(source, record, fuzzy_ranges, method="fuzzy")
    offered = tuple(candidate for candidate in fuzzy if candidate.confidence >= 60)
    if not offered:
        return RematchItem(
            annotation=record,
            status=RematchStatus.ORPHANED,
            candidates=(),
            explanation="no candidate reached the minimum safe confidence",
        )
    decisive = offered[0].confidence >= 75 and (
        len(offered) == 1
        or offered[0].confidence - offered[1].confidence >= 10
    )
    return RematchItem(
        annotation=record,
        status=RematchStatus.MODIFIED if decisive else RematchStatus.AMBIGUOUS,
        candidates=offered,
        explanation=(
            "a changed passage has a plausible deterministic match"
            if decisive
            else "changed passage has similarly plausible candidates"
        ),
    )


def _apply_document_consistency(
    items: Sequence[RematchItem], source_length: int
) -> Tuple[RematchItem, ...]:
    """Use neighboring order and shared shifts only as a small tie-breaker."""

    ordered_indices = sorted(
        (
            index
            for index, item in enumerate(items)
            if item.annotation.start is not None
        ),
        key=lambda index: (
            items[index].annotation.start or 0,
            items[index].annotation.ordinal,
        ),
    )
    order_position = {
        item_index: position
        for position, item_index in enumerate(ordered_indices)
    }

    def provisional_position(item: RematchItem) -> Optional[int]:
        if item.status is RematchStatus.HEALTHY:
            return item.annotation.start
        return item.proposed.source_range.start if item.proposed is not None else None

    updated_items = []
    for item_index, item in enumerate(items):
        if not item.candidates or item_index not in order_position:
            updated_items.append(item)
            continue
        position = order_position[item_index]
        neighbor_indices = []
        if position > 0:
            neighbor_indices.append(ordered_indices[position - 1])
        if position + 1 < len(ordered_indices):
            neighbor_indices.append(ordered_indices[position + 1])

        candidates = []
        for candidate in item.candidates:
            neighbor_scores = []
            for neighbor_index in neighbor_indices:
                neighbor = items[neighbor_index]
                neighbor_position = provisional_position(neighbor)
                if neighbor_position is None or neighbor.annotation.start is None:
                    continue
                old_delta = (item.annotation.start or 0) - neighbor.annotation.start
                new_delta = candidate.source_range.start - neighbor_position
                item_shift = candidate.source_range.start - (item.annotation.start or 0)
                neighbor_shift = neighbor_position - neighbor.annotation.start
                shift_scale = max(40, source_length // 10)
                shift_agreement = max(
                    0.0,
                    1.0 - abs(item_shift - neighbor_shift) / shift_scale,
                )
                if old_delta == 0:
                    neighbor_scores.append(shift_agreement)
                else:
                    order_agrees = (old_delta < 0 and new_delta < 0) or (
                        old_delta > 0 and new_delta > 0
                    )
                    neighbor_scores.append(
                        0.7 * float(order_agrees) + 0.3 * shift_agreement
                    )
            if not neighbor_scores:
                candidates.append(candidate)
                continue
            consistency = sum(neighbor_scores) / len(neighbor_scores)
            adjusted = candidate.confidence + 10 * (consistency - 0.75)
            confidence = int(max(0, min(100, 5 * round(adjusted / 5))))
            evidence = candidate.evidence
            if consistency >= 0.80:
                evidence = (*evidence, "neighbor order/shift agrees")
            elif consistency <= 0.35:
                evidence = (*evidence, "neighbor order/shift conflicts")
            candidates.append(
                replace(
                    candidate,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
        candidates.sort(
            key=lambda candidate: (
                candidate.confidence,
                candidate.quote_similarity,
                candidate.context_similarity,
                -candidate.source_range.start,
            ),
            reverse=True,
        )
        status = item.status
        if (
            status is RematchStatus.AMBIGUOUS
            and len(candidates) >= 2
            and candidates[0].confidence - candidates[1].confidence >= 10
            and candidates[0].confidence >= 75
        ):
            status = (
                RematchStatus.SHIFTED
                if candidates[0].method == "exact"
                else RematchStatus.MODIFIED
            )
        updated_items.append(
            replace(item, status=status, candidates=tuple(candidates))
        )
    return tuple(updated_items)


def scan_rematches(source_text: str, sidecar_text: str) -> RematchReport:
    """Read, detect, search, and score without mutating either document."""

    records = parse_annotation_records(sidecar_text)
    items = tuple(_scan_record(source_text, record) for record in records)
    return RematchReport(
        source_sha256=text_sha256(source_text),
        sidecar_sha256=text_sha256(sidecar_text),
        items=_apply_document_consistency(items, len(source_text)),
    )


def make_manual_candidate(
    source_text: str,
    annotation: ParsedAnnotation,
    source_range: AnnotationRange,
) -> RematchCandidate:
    """Score a source span the user explicitly selected before opening review."""

    if annotation.errors or annotation.quote is None or annotation.start is None:
        raise ValueError("a malformed annotation cannot accept a manual range")
    if not 0 <= source_range.start < source_range.end <= len(source_text):
        raise ValueError("manual source selection is outside the document")
    candidate_text = source_text[source_range.start : source_range.end]
    character_ratio = difflib.SequenceMatcher(
        None, annotation.quote, candidate_text, autojunk=False
    ).ratio()
    quote_tokens = re.findall(r"\w+|[^\w\s]", annotation.quote)
    candidate_tokens = re.findall(r"\w+|[^\w\s]", candidate_text)
    token_ratio = difflib.SequenceMatcher(
        None, quote_tokens, candidate_tokens, autojunk=False
    ).ratio()
    candidate = _candidate_confidence(
        source_text,
        annotation,
        source_range,
        quote_similarity=0.7 * character_ratio + 0.3 * token_ratio,
        uniqueness=0.5,
        method="manual-selection",
    )
    return RematchCandidate(
        source_range=candidate.source_range,
        text=candidate.text,
        confidence=candidate.confidence,
        method=candidate.method,
        evidence=("explicit source selection", *candidate.evidence),
        quote_similarity=candidate.quote_similarity,
        context_similarity=candidate.context_similarity,
    )


def _quote_block(quote: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in quote.split("\n"))


def _anchor_yaml(source: str, source_range: AnnotationRange) -> str:
    quote = source[source_range.start : source_range.end]
    prefix = source[max(0, source_range.start - 80) : source_range.start]
    suffix = source[source_range.end : source_range.end + 80]
    return (
        "anchor:\n"
        f"  source_sha256: {json.dumps(text_sha256(source))}\n"
        f"  quote_sha256: {json.dumps(text_sha256(quote))}\n"
        f"  prefix: {json.dumps(prefix, ensure_ascii=False)}\n"
        f"  suffix: {json.dumps(suffix, ensure_ascii=False)}\n"
        f"  original_start: {source_range.start}\n"
        f"  original_end: {source_range.end}\n"
    )


def _history_yaml(
    record: ParsedAnnotation, candidate: RematchCandidate, rematched_at: str
) -> str:
    assert record.start is not None and record.end is not None
    return (
        f"  - rematched_at: {json.dumps(rematched_at)}\n"
        f"    previous_start: {record.start}\n"
        f"    previous_end: {record.end}\n"
        f"    method: {json.dumps(candidate.method)}\n"
        f"    estimated_correctness: {candidate.confidence}\n"
    )


def _replace_range(metadata: str, source_range: AnnotationRange) -> str:
    range_match = re.search(r"(?m)^range:[ \t]*(?:\n|$)", metadata)
    if range_match is None:
        raise ValueError("range metadata disappeared during rematch")
    tail = metadata[range_match.end() :]
    next_top_level = re.search(r"(?m)^(?=\S)", tail)
    body_end = range_match.end() + (
        next_top_level.start() if next_top_level is not None else len(tail)
    )
    body = metadata[range_match.end() : body_end]
    start_count = 0
    end_count = 0

    def replace_start(match: re.Match[str]) -> str:
        nonlocal start_count
        start_count += 1
        return f"{match.group(1)}{source_range.start}{match.group(2)}"

    def replace_end(match: re.Match[str]) -> str:
        nonlocal end_count
        end_count += 1
        return f"{match.group(1)}{source_range.end}{match.group(2)}"

    body = re.sub(
        r"(?m)^([ \t]+start:[ \t]*)\d+([ \t]*)$", replace_start, body
    )
    body = re.sub(
        r"(?m)^([ \t]+end:[ \t]*)\d+([ \t]*)$", replace_end, body
    )
    if start_count != 1 or end_count != 1:
        raise ValueError("range metadata is ambiguous")
    return metadata[: range_match.end()] + body + metadata[body_end:]


def _metadata_with_anchors_and_history(
    metadata: str,
    source: str,
    record: ParsedAnnotation,
    candidate: RematchCandidate,
    rematched_at: str,
) -> str:
    metadata = _replace_range(metadata, candidate.source_range)
    anchor_match = re.search(r"(?m)^anchor:[ \t]*(?:\n|$)", metadata)
    if anchor_match is not None:
        tail = metadata[anchor_match.end() :]
        next_top_level = re.search(r"(?m)^(?=\S)", tail)
        anchor_end = anchor_match.end() + (
            next_top_level.start() if next_top_level is not None else len(tail)
        )
        metadata = metadata[: anchor_match.start()] + metadata[anchor_end:]

    anchor_yaml = _anchor_yaml(source, candidate.source_range)
    history_match = re.search(
        r"(?m)^rematch_history:[ \t]*(?:\n|$)", metadata
    )
    history_item = _history_yaml(record, candidate, rematched_at)
    if history_match is None:
        if metadata and not metadata.endswith("\n"):
            metadata += "\n"
        return metadata + anchor_yaml + "rematch_history:\n" + history_item

    tail = metadata[history_match.end() :]
    next_top_level = re.search(r"(?m)^(?=\S)", tail)
    history_end = history_match.end() + (
        next_top_level.start() if next_top_level is not None else len(tail)
    )
    return (
        metadata[: history_match.start()]
        + anchor_yaml
        + metadata[history_match.start() : history_end]
        + history_item
        + metadata[history_end:]
    )


def _rewrite_block(
    block: str,
    source: str,
    record: ParsedAnnotation,
    candidate: RematchCandidate,
    rematched_at: str,
) -> str:
    fence_open = re.search(r"(?m)^```yaml[ \t]*(?:\n|$)", block)
    if fence_open is None:
        raise ValueError("missing YAML metadata block")
    fence_close_relative = re.search(
        r"(?m)^```[ \t]*(?:\n|$)", block[fence_open.end() :]
    )
    if fence_close_relative is None:
        raise ValueError("missing YAML metadata terminator")
    metadata_start = fence_open.end()
    metadata_end = metadata_start + fence_close_relative.start()
    metadata = _metadata_with_anchors_and_history(
        block[metadata_start:metadata_end],
        source,
        record,
        candidate,
        rematched_at,
    )
    block = block[:metadata_start] + metadata + block[metadata_end:]

    original_heading = re.search(
        r"(?m)^### Original text[ \t]*(?:\n|$)", block
    )
    note_heading = re.search(r"(?m)^### Note[ \t]*(?:\n|$)", block)
    if (
        original_heading is None
        or note_heading is None
        or note_heading.start() <= original_heading.end()
    ):
        raise ValueError("annotation sections disappeared during rematch")
    quote = source[candidate.source_range.start : candidate.source_range.end]
    return (
        block[: original_heading.end()]
        + "\n"
        + _quote_block(quote)
        + "\n\n"
        + block[note_heading.start() :]
    )


def apply_rematches(
    sidecar_text: str,
    source_text: str,
    choices: Iterable[RematchChoice],
    *,
    rematched_at: str,
) -> str:
    """Build a rewritten sidecar in memory for explicitly chosen matches."""

    records = parse_annotation_records(sidecar_text)
    by_id = {record.annotation_id: record for record in records}
    replacements = []
    seen = set()
    for choice in choices:
        if choice.annotation_id in seen:
            raise ValueError(f"duplicate rematch choice: {choice.annotation_id}")
        seen.add(choice.annotation_id)
        record = by_id.get(choice.annotation_id)
        if record is None or record.errors:
            raise ValueError(f"cannot rewrite annotation {choice.annotation_id}")
        source_range = choice.candidate.source_range
        if not 0 <= source_range.start < source_range.end <= len(source_text):
            raise ValueError(f"candidate outside source: {choice.annotation_id}")
        block = sidecar_text[record.block_start : record.block_end]
        rewritten = _rewrite_block(
            block, source_text, record, choice.candidate, rematched_at
        )
        replacements.append((record.block_start, record.block_end, rewritten))

    updated = sidecar_text
    for start, end, replacement in sorted(replacements, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    return updated


def validate_applied_rematches(
    before: str,
    after: str,
    source_text: str,
    choices: Iterable[RematchChoice],
) -> None:
    """Verify identity fields and every chosen range after a disk write."""

    old_records = parse_annotation_records(before)
    new_records = parse_annotation_records(after)
    if len(old_records) != len(new_records):
        raise ValueError("annotation count changed")
    old_by_id = {record.annotation_id: record for record in old_records}
    new_by_id = {record.annotation_id: record for record in new_records}
    if old_by_id.keys() != new_by_id.keys():
        raise ValueError("annotation IDs changed")
    choices = tuple(choices)
    selected_ids = {choice.annotation_id for choice in choices}
    for annotation_id, old in old_by_id.items():
        new = new_by_id[annotation_id]
        if old.note != new.note:
            raise ValueError(f"note changed for {annotation_id}")
        if old.created_at != new.created_at:
            raise ValueError(f"creation time changed for {annotation_id}")
        if new.errors:
            raise ValueError(f"rewritten annotation is malformed: {annotation_id}")
        if annotation_id not in selected_ids:
            old_block = before[old.block_start : old.block_end]
            new_block = after[new.block_start : new.block_end]
            if old_block != new_block:
                raise ValueError(f"unselected annotation changed: {annotation_id}")

    for choice in choices:
        record = new_by_id[choice.annotation_id]
        source_range = choice.candidate.source_range
        expected = source_text[source_range.start : source_range.end]
        if record.start != source_range.start or record.end != source_range.end:
            raise ValueError(f"range verification failed for {choice.annotation_id}")
        if record.quote != expected:
            raise ValueError(f"quotation verification failed for {choice.annotation_id}")


def confidence_summary(choices: Sequence[RematchChoice]) -> Tuple[int, int]:
    """Return rounded average and lowest selected correctness estimates."""

    if not choices:
        return 0, 0
    values = [choice.candidate.confidence for choice in choices]
    average = int(5 * round((sum(values) / len(values)) / 5))
    return average, min(values)
