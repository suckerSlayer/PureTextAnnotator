#!/usr/bin/env python3
"""Generate and run the formal thesis acceptance measurements.

The rematch corpus is deterministic and carries construction-time ground truth.
The performance fixture is generated in a temporary directory so a 1 MB source
and its 1000-record sidecar do not have to be committed to the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from textual import events


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from pure_text_annotator import __version__
from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationRecord,
    AnnotationStore,
    AnnotationType,
    create_record,
)
from pure_text_annotator.app import ViewerApp, VimTextArea
from pure_text_annotator.files import load_source_file
from pure_text_annotator.rematch import RematchStatus, scan_rematches


SEED = 20261114
CASE_COUNTS = {
    "unchanged": 20,
    "prefix_insertion": 20,
    "deletion_before_target": 15,
    "local_modification": 20,
    "duplicate_with_context": 15,
    "ambiguous_duplicate": 10,
    "quotation_deleted": 10,
    "file_migration": 10,
}
PERFORMANCE_SOURCE_BYTES = 1_000_000
PERFORMANCE_ANNOTATIONS = 1_000
PERFORMANCE_LINE_BYTES = 100
FORMAL_LOAD_RUNS = 10
FORMAL_OPERATION_COUNT = 200
FRAMEWORK_BASELINE_RUNS = 20
FULL_REMATCH_RUNS = 3


@dataclass(frozen=True)
class AcceptanceCase:
    """One source mutation with independently constructed expected behavior."""

    case_id: str
    category: str
    annotation_type: str
    original_text: str
    current_text: str
    quote: str
    original_start: int
    expected_state: str
    expected_start: Optional[int]
    expected_end: Optional[int]
    unique_unchanged_quote: bool
    legacy_without_context: bool = False


def _sentence_parts(rng: random.Random, index: int) -> Tuple[str, str, str]:
    subjects = (
        "archive",
        "compiler",
        "library",
        "notebook",
        "terminal",
        "dataset",
        "reader",
        "protocol",
    )
    verbs = (
        "preserves",
        "indexes",
        "reviews",
        "records",
        "validates",
        "restores",
        "compares",
        "tracks",
    )
    objects = (
        "the selected paragraph",
        "the local revision",
        "the stable reference",
        "the external note",
        "the unique passage",
        "the saved context",
    )
    subject = rng.choice(subjects)
    verb = rng.choice(verbs)
    object_ = rng.choice(objects)
    quote = (
        f"Marker {index:03d}: the {subject} {verb} {object_} "
        f"for acceptance case {index:03d}."
    )
    before = (
        f"Opening context {index:03d} uses {rng.choice(subjects)} and "
        f"{rng.choice(objects)}."
    )
    after = (
        f"Closing context {index:03d} {rng.choice(verbs)} a separate "
        f"{rng.choice(objects)}."
    )
    return before, quote, after


def generate_rematch_cases() -> Tuple[AcceptanceCase, ...]:
    """Return 120 deterministic cases that were not used to tune thresholds."""

    rng = random.Random(SEED)
    cases: List[AcceptanceCase] = []
    global_index = 0

    def add_range_case(
        category: str,
        original: str,
        current: str,
        quote: str,
        *,
        original_start: Optional[int] = None,
        expected_text: Optional[str] = None,
        unique_unchanged: bool,
    ) -> None:
        nonlocal global_index
        start = (
            original.index(quote)
            if original_start is None
            else original_start
        )
        target = quote if expected_text is None else expected_text
        expected_start = current.index(target)
        annotation_type = tuple(AnnotationType)[global_index % 3].value
        cases.append(
            AcceptanceCase(
                case_id=f"RM-{global_index + 1:03d}",
                category=category,
                annotation_type=annotation_type,
                original_text=original,
                current_text=current,
                quote=quote,
                original_start=start,
                expected_state="range",
                expected_start=expected_start,
                expected_end=expected_start + len(target),
                unique_unchanged_quote=unique_unchanged,
            )
        )
        global_index += 1

    for _ in range(CASE_COUNTS["unchanged"]):
        before, quote, after = _sentence_parts(rng, global_index)
        original = f"{before}\n{quote}\n{after}\n"
        add_range_case(
            "unchanged",
            original,
            original,
            quote,
            unique_unchanged=True,
        )

    for _ in range(CASE_COUNTS["prefix_insertion"]):
        before, quote, after = _sentence_parts(rng, global_index)
        original = f"{before}\n{quote}\n{after}\n"
        current = (
            f"Inserted preface {global_index:03d} is unrelated.\n" + original
        )
        add_range_case(
            "prefix_insertion",
            original,
            current,
            quote,
            unique_unchanged=True,
        )

    for _ in range(CASE_COUNTS["deletion_before_target"]):
        before, quote, after = _sentence_parts(rng, global_index)
        original = f"Disposable lead {global_index:03d}.\n{before}\n{quote}\n{after}\n"
        current = f"{quote}\n{after}\n"
        add_range_case(
            "deletion_before_target",
            original,
            current,
            quote,
            unique_unchanged=True,
        )

    for _ in range(CASE_COUNTS["local_modification"]):
        before, quote, after = _sentence_parts(rng, global_index)
        replacement = quote.replace("acceptance case", "revised acceptance case")
        original = f"{before}\n{quote}\n{after}\n"
        current = f"{before}\n{replacement}\n{after}\n"
        add_range_case(
            "local_modification",
            original,
            current,
            quote,
            expected_text=replacement,
            unique_unchanged=False,
        )

    for _ in range(CASE_COUNTS["duplicate_with_context"]):
        quote = f"Shared checkpoint {global_index:03d} remains available."
        original = (
            f"Alpha context {global_index:03d} begins. {quote} Alpha closes.\n"
            f"Beta context {global_index:03d} begins. {quote} Beta closes.\n"
        )
        current = f"Inserted heading {global_index:03d}.\n" + original
        add_range_case(
            "duplicate_with_context",
            original,
            current,
            quote,
            original_start=original.rindex(quote),
            expected_text=quote,
            unique_unchanged=False,
        )
        # The helper uses current.index; the ground truth is the Beta occurrence.
        previous = cases[-1]
        expected_start = current.rindex(quote)
        cases[-1] = AcceptanceCase(
            **{
                **asdict(previous),
                "expected_start": expected_start,
                "expected_end": expected_start + len(quote),
            }
        )

    for _ in range(CASE_COUNTS["ambiguous_duplicate"]):
        quote = f"Repeated legacy checkpoint {global_index:03d}."
        current = f"Left {quote} Middle {quote} Right."
        annotation_type = tuple(AnnotationType)[global_index % 3].value
        cases.append(
            AcceptanceCase(
                case_id=f"RM-{global_index + 1:03d}",
                category="ambiguous_duplicate",
                annotation_type=annotation_type,
                original_text=quote,
                current_text=current,
                quote=quote,
                original_start=0,
                expected_state="ambiguous",
                expected_start=None,
                expected_end=None,
                unique_unchanged_quote=False,
                legacy_without_context=True,
            )
        )
        global_index += 1

    for _ in range(CASE_COUNTS["quotation_deleted"]):
        before, quote, after = _sentence_parts(rng, global_index)
        original = f"{before}\n{quote}\n{after}\n"
        current = (
            f"A replacement document {global_index:03d} discusses weather, "
            "music, and unrelated geometry only.\n"
        )
        annotation_type = tuple(AnnotationType)[global_index % 3].value
        cases.append(
            AcceptanceCase(
                case_id=f"RM-{global_index + 1:03d}",
                category="quotation_deleted",
                annotation_type=annotation_type,
                original_text=original,
                current_text=current,
                quote=quote,
                original_start=original.index(quote),
                expected_state="orphaned",
                expected_start=None,
                expected_end=None,
                unique_unchanged_quote=False,
            )
        )
        global_index += 1

    for _ in range(CASE_COUNTS["file_migration"]):
        before, quote, after = _sentence_parts(rng, global_index)
        original = f"{before}\n{quote}\n{after}\n"
        add_range_case(
            "file_migration",
            original,
            original,
            quote,
            unique_unchanged=True,
        )

    assert len(cases) == sum(CASE_COUNTS.values()) == 120
    return tuple(cases)


def _sidecar_for_case(case: AcceptanceCase) -> str:
    source_path = Path("/acceptance-corpus") / f"{case.case_id}.md"
    store = AnnotationStore(source_path)
    annotation_type = AnnotationType(case.annotation_type)
    note = (
        f"Acceptance note for {case.case_id}."
        if annotation_type is AnnotationType.NOTE
        else ""
    )
    record = create_record(
        AnnotationDraft(
            case.original_start,
            case.original_start + len(case.quote),
            case.quote,
        ),
        note,
        annotation_type=annotation_type,
        bookmark_position=(
            case.original_start
            if annotation_type is AnnotationType.BOOKMARK
            else None
        ),
        annotation_id=case.case_id,
        created_at="2026-11-14T00:00:00Z",
        source_text=case.original_text,
    )
    sidecar = store.append_to_text(store.initial_text(), (record,))
    if case.legacy_without_context:
        anchor_start = sidecar.index("anchor:\n")
        fence_start = sidecar.index("```\n", anchor_start)
        sidecar = sidecar[:anchor_start] + sidecar[fence_start:]
    return sidecar


def _range_matches(
    actual: Optional[Tuple[int, int]], case: AcceptanceCase
) -> bool:
    return actual == (case.expected_start, case.expected_end)


def _offset_only_outcome(case: AcceptanceCase) -> Tuple[str, Optional[Tuple[int, int]]]:
    end = case.original_start + len(case.quote)
    predicted = (
        (case.original_start, end)
        if end <= len(case.current_text)
        else None
    )
    if case.expected_state == "range":
        if predicted is None:
            return "unexpected_unresolved", None
        return (
            ("automatic_correct" if _range_matches(predicted, case) else "automatic_wrong"),
            predicted,
        )
    if predicted is None:
        return (
            "invalid_recognized"
            if case.expected_state == "orphaned"
            else "pending_confirmation",
            None,
        )
    return "automatic_wrong", predicted


def _all_occurrences(text: str, needle: str) -> Tuple[int, ...]:
    starts: List[int] = []
    cursor = 0
    while True:
        found = text.find(needle, cursor)
        if found < 0:
            break
        starts.append(found)
        cursor = found + 1
    return tuple(starts)


def _quote_only_outcome(case: AcceptanceCase) -> Tuple[str, Optional[Tuple[int, int]]]:
    occurrences = _all_occurrences(case.current_text, case.quote)
    if len(occurrences) > 1:
        return "pending_confirmation", None
    if not occurrences:
        return (
            "invalid_recognized"
            if case.expected_state == "orphaned"
            else "unexpected_unresolved",
            None,
        )
    predicted = (occurrences[0], occurrences[0] + len(case.quote))
    if case.expected_state != "range":
        return "automatic_wrong", predicted
    return (
        ("automatic_correct" if _range_matches(predicted, case) else "automatic_wrong"),
        predicted,
    )


def _combined_outcome(
    case: AcceptanceCase,
    sidecar: str,
) -> Tuple[str, Optional[Tuple[int, int]], str, Optional[int], Optional[str]]:
    item = scan_rematches(case.current_text, sidecar).items[0]
    predicted: Optional[Tuple[int, int]] = None
    confidence: Optional[int] = None
    method: Optional[str] = None
    if item.status is RematchStatus.HEALTHY:
        predicted = (
            case.original_start,
            case.original_start + len(case.quote),
        )
    elif item.proposed is not None:
        predicted = (
            item.proposed.source_range.start,
            item.proposed.source_range.end,
        )
        confidence = item.proposed.confidence
        method = item.proposed.method

    decisive = item.status in {
        RematchStatus.HEALTHY,
        RematchStatus.SHIFTED,
        RematchStatus.MODIFIED,
    }
    if case.expected_state == "range":
        if decisive and predicted is not None:
            outcome = (
                "automatic_correct"
                if _range_matches(predicted, case)
                else "automatic_wrong"
            )
        elif item.status is RematchStatus.AMBIGUOUS:
            outcome = "pending_confirmation"
        else:
            outcome = "unexpected_unresolved"
    elif case.expected_state == "ambiguous":
        outcome = (
            "pending_confirmation"
            if item.status is RematchStatus.AMBIGUOUS
            else "automatic_wrong"
            if decisive
            else "unexpected_unresolved"
        )
    else:
        outcome = (
            "invalid_recognized"
            if item.status is RematchStatus.ORPHANED
            else "pending_confirmation"
            if item.status is RematchStatus.AMBIGUOUS
            else "automatic_wrong"
        )
    return outcome, predicted, item.status.value, confidence, method


def evaluate_rematch_cases(
    cases: Sequence[AcceptanceCase],
) -> Dict[str, object]:
    """Compare offset-only, quote-only, and PTA combined anchors."""

    strategy_counts = {
        "offset_only": Counter(),
        "quote_only": Counter(),
        "combined_anchor": Counter(),
    }
    category_counts = Counter(case.category for case in cases)
    type_counts = Counter(case.annotation_type for case in cases)
    details = []
    unique_results = Counter()

    for case in cases:
        sidecar = _sidecar_for_case(case)
        offset_outcome, offset_range = _offset_only_outcome(case)
        quote_outcome, quote_range = _quote_only_outcome(case)
        (
            combined_outcome,
            combined_range,
            status,
            confidence,
            method,
        ) = _combined_outcome(case, sidecar)
        strategy_counts["offset_only"][offset_outcome] += 1
        strategy_counts["quote_only"][quote_outcome] += 1
        strategy_counts["combined_anchor"][combined_outcome] += 1
        if case.unique_unchanged_quote:
            unique_results[combined_outcome] += 1
        details.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "annotation_type": case.annotation_type,
                "expected_state": case.expected_state,
                "expected_range": (
                    [case.expected_start, case.expected_end]
                    if case.expected_start is not None
                    else None
                ),
                "offset_only": {
                    "outcome": offset_outcome,
                    "range": list(offset_range) if offset_range else None,
                },
                "quote_only": {
                    "outcome": quote_outcome,
                    "range": list(quote_range) if quote_range else None,
                },
                "combined_anchor": {
                    "outcome": combined_outcome,
                    "range": list(combined_range) if combined_range else None,
                    "status": status,
                    "confidence": confidence,
                    "method": method,
                },
            }
        )

    unique_total = sum(unique_results.values())
    unique_correct = unique_results["automatic_correct"]
    return {
        "seed": SEED,
        "case_count": len(cases),
        "category_counts": dict(sorted(category_counts.items())),
        "annotation_type_counts": dict(sorted(type_counts.items())),
        "strategy_outcomes": {
            name: dict(sorted(counts.items()))
            for name, counts in strategy_counts.items()
        },
        "unique_unchanged_quote": {
            "case_count": unique_total,
            "automatic_correct": unique_correct,
            "automatic_wrong": unique_results["automatic_wrong"],
            "pending_confirmation": unique_results["pending_confirmation"],
            "unexpected_unresolved": unique_results["unexpected_unresolved"],
            "correct_rate_percent": round(100 * unique_correct / unique_total, 2),
            "target_percent": 95.0,
            "passes": unique_correct / unique_total >= 0.95,
        },
        "combined_wrong_case_ids": [
            item["case_id"]
            for item in details
            if item["combined_anchor"]["outcome"] == "automatic_wrong"
        ],
        "details": details,
    }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_performance_fixture(directory: Path) -> Dict[str, object]:
    source_path = directory / "performance.md"
    lines: List[str] = []
    anchors: List[Tuple[int, str]] = []
    filler = "lorem ipsum dolor sit amet consectetur adipiscing elit "
    line_count = PERFORMANCE_SOURCE_BYTES // PERFORMANCE_LINE_BYTES
    annotation_interval = line_count // PERFORMANCE_ANNOTATIONS
    for line_index in range(line_count):
        annotation_index = line_index // annotation_interval
        has_anchor = line_index % annotation_interval == 0
        anchor = (
            f"acceptance anchor {annotation_index:04d}"
            if has_anchor
            else ""
        )
        prefix = f"line {line_index:05d} " + (anchor + " " if anchor else "")
        body = (prefix + filler * 3)[: PERFORMANCE_LINE_BYTES - 1]
        body = body.ljust(PERFORMANCE_LINE_BYTES - 1, "x")
        line = body + "\n"
        assert len(line.encode("utf-8")) == PERFORMANCE_LINE_BYTES
        if has_anchor:
            anchors.append(
                (
                    line_index * PERFORMANCE_LINE_BYTES + body.index(anchor),
                    anchor,
                )
            )
        lines.append(line)
    source_text = "".join(lines)
    source_bytes = source_text.encode("utf-8")
    assert len(source_bytes) == PERFORMANCE_SOURCE_BYTES
    source_path.write_bytes(source_bytes)

    store = AnnotationStore(source_path)
    source_hash = _sha256_bytes(source_bytes)
    records: List[AnnotationRecord] = []
    type_counts = Counter()
    annotation_types = tuple(AnnotationType)
    for index, (start, quote) in enumerate(anchors):
        annotation_type = annotation_types[index % len(annotation_types)]
        type_counts[annotation_type.value] += 1
        end = start + len(quote)
        records.append(
            AnnotationRecord(
                annotation_id=f"PERF-{index:04d}",
                annotation_type=annotation_type,
                start=start,
                end=end,
                quote=quote,
                note=(
                    f"Performance acceptance note {index:04d}."
                    if annotation_type is AnnotationType.NOTE
                    else ""
                ),
                created_at="2026-11-14T00:00:00Z",
                bookmark_position=(
                    start if annotation_type is AnnotationType.BOOKMARK else None
                ),
                source_sha256=source_hash,
                quote_sha256=_sha256_bytes(quote.encode("utf-8")),
                prefix=source_text[max(0, start - 80) : start],
                suffix=source_text[end : end + 80],
                original_start=start,
                original_end=end,
            )
        )
    sidecar_text = store.append_to_text(store.initial_text(), records)
    store.write(sidecar_text)
    sidecar_bytes = store.path.read_bytes()
    return {
        "source_path": source_path,
        "sidecar_path": store.path,
        "source_text": source_text,
        "sidecar_text": sidecar_text,
        "manifest": {
            "source_size_bytes": len(source_bytes),
            "source_sha256": source_hash,
            "source_lines": len(lines),
            "line_size_bytes_including_newline": PERFORMANCE_LINE_BYTES,
            "annotation_count": len(records),
            "annotation_type_counts": dict(sorted(type_counts.items())),
            "sidecar_size_bytes": len(sidecar_bytes),
            "sidecar_sha256": _sha256_bytes(sidecar_bytes),
            "generator_seed": SEED,
        },
    }


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _timing_summary(values: Sequence[float], threshold_ms: float) -> Dict[str, object]:
    passing = sum(value <= threshold_ms for value in values)
    return {
        "count": len(values),
        "minimum_ms": round(min(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "maximum_ms": round(max(values), 3),
        "threshold_ms": threshold_ms,
        "within_threshold": passing,
        "within_threshold_percent": round(100 * passing / len(values), 2),
        "passes_95_percent_rule": passing / len(values) >= 0.95,
    }


def _operation_plan() -> List[Tuple[str, str]]:
    """Return the fixed 200-operation workload required by the proposal."""

    plans: List[Tuple[str, str]] = []
    plans.extend(("scroll", "ctrl+e") for _ in range(35))
    plans.extend(("scroll", "ctrl+y") for _ in range(35))
    for _ in range(20):
        plans.extend(
            (
                ("selection", "v"),
                ("selection", "l"),
                ("selection", "v"),
            )
        )
    plans.extend(("annotation_switch", "ctrl+n") for _ in range(35))
    plans.extend(("annotation_switch", "ctrl+p") for _ in range(35))
    assert len(plans) == FORMAL_OPERATION_COUNT
    return plans


def _summarize_operation_timings(
    operation_timings: Dict[str, List[float]],
) -> Dict[str, object]:
    combined = [
        value
        for values in operation_timings.values()
        for value in values
    ]
    return {
        "operation_runs_ms": {
            category: [round(value, 3) for value in values]
            for category, values in operation_timings.items()
        },
        "operation_summaries": {
            category: _timing_summary(values, 100.0)
            for category, values in operation_timings.items()
            if values
        },
        "combined_operation_summary": _timing_summary(combined, 100.0),
    }


def _new_performance_app(
    source_path: Path, sidecar_path: Path
) -> ViewerApp:
    source = load_source_file(source_path)
    return ViewerApp(
        source,
        annotation_store=AnnotationStore(
            source.path,
            sidecar_path=sidecar_path,
        ),
        clipboard_writer=lambda _text: "acceptance",
        embed_on_annotation_save=False,
    )


async def _measure_response_operations(
    source_path: Path,
    sidecar_path: Path,
    plans: Sequence[Tuple[str, str]],
) -> Dict[str, object]:
    """Measure key dispatch through application screen-message settlement.

    Textual's ``Pilot.press`` deliberately waits for every animation to finish.
    That is useful as a conservative supplemental number, but it is not the
    first-response boundary used by the proposal's 100 ms requirement.  Here
    the real Key event is dispatched through the Textual driver and the timer
    ends after all screen widgets have handled messages already queued by the
    operation.  Deliberately unfinished cosmetic scrolling animation is not
    included.
    """

    operation_timings: Dict[str, List[float]] = {
        "scroll": [],
        "selection": [],
        "annotation_switch": [],
    }
    app = _new_performance_app(source_path, sidecar_path)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.query_one(VimTextArea).focus()
        for category, key in plans:
            event = events.Key(key, key if len(key) == 1 else None)
            event.set_sender(app)
            started = time.perf_counter_ns()
            assert app._driver is not None
            app._driver.send_message(event)
            await pilot._wait_for_screen()
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            operation_timings[category].append(elapsed_ms)

    return {
        **_summarize_operation_timings(operation_timings),
        "measurement_boundary": (
            "before Textual Key event dispatch through completion of screen "
            "messages queued by that operation; cosmetic animation completion "
            "is excluded"
        ),
        "acceptance_metric": True,
    }


async def _measure_full_settle_operations(
    source_path: Path,
    sidecar_path: Path,
    plans: Sequence[Tuple[str, str]],
) -> Dict[str, object]:
    """Measure the stricter Pilot boundary including all animation completion."""

    operation_timings: Dict[str, List[float]] = {
        "scroll": [],
        "selection": [],
        "annotation_switch": [],
    }
    app = _new_performance_app(source_path, sidecar_path)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        app.query_one(VimTextArea).focus()
        for category, key in plans:
            started = time.perf_counter_ns()
            await pilot.press(key)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            operation_timings[category].append(elapsed_ms)

    baseline_timings: List[float] = []
    baseline_app = _new_performance_app(source_path, sidecar_path)
    async with baseline_app.run_test(size=(140, 42)) as pilot:
        await pilot.pause()
        baseline_app.query_one(VimTextArea).focus()
        for _ in range(FRAMEWORK_BASELINE_RUNS):
            started = time.perf_counter_ns()
            await pilot.press("f12")
            baseline_timings.append(
                (time.perf_counter_ns() - started) / 1_000_000
            )

    return {
        **_summarize_operation_timings(operation_timings),
        "no_op_framework_baseline": _timing_summary(
            baseline_timings, 100.0
        ),
        "measurement_boundary": (
            "before Textual Pilot key injection through application idle, all "
            "animation completion, and a whole-screen widget message barrier"
        ),
        "acceptance_metric": False,
        "note": (
            "Conservative supplemental diagnostic; Pilot.press waits for all "
            "animations and therefore is not the first-response boundary."
        ),
    }


async def _measure_ui_performance(
    fixture: Dict[str, object],
    *,
    load_runs: int,
    operation_count: int,
) -> Dict[str, object]:
    source_path = fixture["source_path"]
    sidecar_path = fixture["sidecar_path"]
    assert isinstance(source_path, Path)
    assert isinstance(sidecar_path, Path)
    load_timings: List[float] = []

    for _ in range(load_runs):
        gc.collect()
        started = time.perf_counter_ns()
        app = _new_performance_app(source_path, sidecar_path)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            load_timings.append(elapsed_ms)

    plans = _operation_plan()[:operation_count]
    response_operations = await _measure_response_operations(
        source_path, sidecar_path, plans
    )
    full_settle_operations = await _measure_full_settle_operations(
        source_path, sidecar_path, plans
    )
    return {
        "load_runs_ms": [round(value, 3) for value in load_timings],
        "load_summary": _timing_summary(load_timings, 2_000.0),
        "response_operations": response_operations,
        "full_settle_operations": full_settle_operations,
        "terminal_size": {"columns": 140, "rows": 42},
        "load_measurement_boundary": (
            "load: before source read through mounted Textual screen and idle "
            "queue"
        ),
    }


def _measure_full_rematch(
    fixture: Dict[str, object], runs: int
) -> Dict[str, object]:
    source_text = fixture["source_text"]
    sidecar_text = fixture["sidecar_text"]
    assert isinstance(source_text, str)
    assert isinstance(sidecar_text, str)
    changed = "Inserted acceptance preface shifts every annotation.\n" + source_text
    timings = []
    statuses: Optional[Counter] = None
    for _ in range(runs):
        gc.collect()
        started = time.perf_counter_ns()
        report = scan_rematches(changed, sidecar_text)
        timings.append((time.perf_counter_ns() - started) / 1_000_000)
        observed = Counter(item.status.value for item in report.items)
        statuses = observed if statuses is None else statuses
        assert observed == statuses
    return {
        "runs_ms": [round(value, 3) for value in timings],
        "summary": _timing_summary(timings, 2_000.0),
        "status_counts": dict(sorted((statuses or Counter()).items())),
        "mutation": "one preface insertion shifts all 1000 valid ranges",
        "note": "reported separately; the 2 s load threshold is not applied",
    }


def _command_value(command: Sequence[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _code_fingerprint() -> str:
    digest = hashlib.sha256()
    paths = sorted((REPOSITORY_ROOT / "src").rglob("*.py"))
    paths.extend(
        [
            REPOSITORY_ROOT / "pyproject.toml",
            Path(__file__).resolve(),
        ]
    )
    for path in paths:
        digest.update(str(path.relative_to(REPOSITORY_ROOT)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def collect_environment() -> Dict[str, object]:
    dependencies = {}
    for package in ("textual", "pdfplumber", "pypdf", "PyYAML", "platformdirs", "pytest"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = None
    git_status = _command_value(("git", "status", "--porcelain"))
    hardware = _command_value(
        ("system_profiler", "SPHardwareDataType", "-detailLevel", "mini")
    )
    return {
        "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pta_version": __version__,
        "code_sha256": _code_fingerprint(),
        "git_commit": _command_value(("git", "rev-parse", "HEAD")),
        "git_worktree_dirty": bool(git_status),
        "operating_system": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "hardware_overview": hardware,
        "python": sys.version.replace("\n", " "),
        "dependencies": dependencies,
    }


def run_regression_suite() -> Dict[str, object]:
    started = time.perf_counter_ns()
    result = subprocess.run(
        (sys.executable, "-m", "pytest"),
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    output = (result.stdout + "\n" + result.stderr).strip()
    return {
        "command": f"{sys.executable} -m pytest",
        "exit_code": result.returncode,
        "elapsed_ms": round(elapsed_ms, 3),
        "passed": result.returncode == 0,
        "output_tail": "\n".join(output.splitlines()[-8:]),
    }


def _json_ready_case(case: AcceptanceCase) -> Dict[str, object]:
    return asdict(case)


def _build_acceptance_summary(results: Dict[str, object]) -> Dict[str, object]:
    rematch = results["rematch_acceptance"]
    fixture = results["performance_fixture"]
    assert isinstance(rematch, dict)
    assert isinstance(fixture, dict)
    unique = rematch["unique_unchanged_quote"]
    assert isinstance(unique, dict)

    checks: Dict[str, Optional[bool]] = {
        "at_least_100_rematch_cases": rematch["case_count"] >= 100,
        "unique_unchanged_quote_at_least_95_percent": bool(
            unique["passes"]
        ),
        "source_not_over_1_mib": fixture["source_size_bytes"] <= 1_048_576,
        "one_thousand_annotations": fixture["annotation_count"] == 1_000,
        "ten_loads_each_not_over_2_seconds": None,
        "two_hundred_operations_at_least_95_percent_within_100_ms": None,
        "regression_suite": None,
    }
    ui = results.get("ui_performance")
    if isinstance(ui, dict):
        load_summary = ui["load_summary"]
        response = ui["response_operations"]
        assert isinstance(load_summary, dict)
        assert isinstance(response, dict)
        operation_summary = response["combined_operation_summary"]
        assert isinstance(operation_summary, dict)
        checks["ten_loads_each_not_over_2_seconds"] = (
            load_summary["count"] >= 10
            and load_summary["maximum_ms"] <= 2_000.0
        )
        checks[
            "two_hundred_operations_at_least_95_percent_within_100_ms"
        ] = (
            operation_summary["count"] >= 200
            and bool(operation_summary["passes_95_percent_rule"])
        )
    regression = results.get("regression_suite")
    if isinstance(regression, dict):
        checks["regression_suite"] = bool(regression["passed"])

    assessed = [value for value in checks.values() if value is not None]
    all_required_measured = all(value is not None for value in checks.values())
    return {
        "checks": checks,
        "all_required_checks_measured": all_required_measured,
        "overall_pass": all(assessed) if all_required_measured else None,
        "supplemental_full_settle_is_acceptance_metric": False,
    }


async def run_acceptance(
    *,
    output_path: Path,
    cases_path: Path,
    skip_regression: bool,
    skip_ui: bool,
) -> Dict[str, object]:
    cases = generate_rematch_cases()
    case_payload = {
        "schema_version": 1,
        "description": (
            "Deterministic, construction-ground-truth rematch acceptance corpus; "
            "not used to tune production thresholds"
        ),
        "seed": SEED,
        "category_counts": CASE_COUNTS,
        "cases": [_json_ready_case(case) for case in cases],
    }
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    cases_path.write_text(
        json.dumps(case_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    results: Dict[str, object] = {
        "schema_version": 1,
        "environment": collect_environment(),
        "rematch_acceptance": evaluate_rematch_cases(cases),
    }
    with tempfile.TemporaryDirectory(prefix="pta-acceptance-") as temporary:
        fixture = _write_performance_fixture(Path(temporary))
        results["performance_fixture"] = fixture["manifest"]
        results["full_rematch"] = _measure_full_rematch(
            fixture, FULL_REMATCH_RUNS
        )
        if not skip_ui:
            results["ui_performance"] = await _measure_ui_performance(
                fixture,
                load_runs=FORMAL_LOAD_RUNS,
                operation_count=FORMAL_OPERATION_COUNT,
            )
    if not skip_regression:
        results["regression_suite"] = run_regression_suite()
    results["acceptance_summary"] = _build_acceptance_summary(results)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Pure Text Annotator formal thesis acceptance measurements."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "acceptance/results/acceptance_results.json",
    )
    parser.add_argument(
        "--cases-output",
        type=Path,
        default=REPOSITORY_ROOT / "acceptance/data/rematch_cases.json",
    )
    parser.add_argument("--skip-regression", action="store_true")
    parser.add_argument("--skip-ui", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    results = asyncio.run(
        run_acceptance(
            output_path=args.output.resolve(),
            cases_path=args.cases_output.resolve(),
            skip_regression=args.skip_regression,
            skip_ui=args.skip_ui,
        )
    )
    rematch = results["rematch_acceptance"]
    assert isinstance(rematch, dict)
    unique = rematch["unique_unchanged_quote"]
    print(
        f"rematch cases: {rematch['case_count']} · unique unchanged correct: "
        f"{unique['correct_rate_percent']}%"
    )
    if "ui_performance" in results:
        ui = results["ui_performance"]
        response = ui["response_operations"]
        print(
            f"load p95: {ui['load_summary']['p95_ms']} ms · "
            f"response operations within 100 ms: "
            f"{response['combined_operation_summary']['within_threshold_percent']}%"
        )
    if "regression_suite" in results:
        regression = results["regression_suite"]
        print(
            f"regression suite: {'PASS' if regression['passed'] else 'FAIL'}"
        )
    summary = results["acceptance_summary"]
    overall = summary["overall_pass"]
    if overall is not None:
        print(f"formal acceptance: {'PASS' if overall else 'FAIL'}")
    return 1 if overall is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
