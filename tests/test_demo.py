from pathlib import Path

from pure_text_annotator.demo import load_demo_case
from pure_text_annotator.rematch import (
    RematchChoice,
    apply_rematches,
    parse_annotation_records,
    scan_rematches,
    validate_applied_rematches,
)


def test_shipped_rematch_demo_is_marked_and_matches_its_reset_baseline() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "rematch_cases"
        / "cases.md"
    )

    demo = load_demo_case(source_path)

    assert demo is not None
    assert demo.case_id == "mixed-rematch-suite"
    assert source_path.read_text(encoding="utf-8") == demo.source_text
    sidecar = source_path.with_name(source_path.name + ".annotations.md")
    assert sidecar.read_text(encoding="utf-8") == demo.annotation_text


def test_shipped_demo_proposals_form_a_valid_multi_annotation_update() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "rematch_cases"
        / "cases.md"
    )
    source = source_path.read_text(encoding="utf-8")
    sidecar = source_path.with_name(
        source_path.name + ".annotations.md"
    ).read_text(encoding="utf-8")
    report = scan_rematches(source, sidecar)
    choices = tuple(
        RematchChoice(item.annotation.annotation_id, item.proposed)
        for item in report.items
        if item.proposed is not None
    )

    updated = apply_rematches(
        sidecar,
        source,
        choices,
        rematched_at="2026-11-01T12:00:00Z",
    )
    validate_applied_rematches(sidecar, updated, source, choices)

    records = parse_annotation_records(updated)
    rematched = {choice.annotation_id for choice in choices}
    for record in records:
        if record.annotation_id in rematched:
            assert record.start is not None and record.end is not None
            assert record.quote == source[record.start : record.end]
    orphan = next(
        record for record in records if record.annotation_id == "case-orphaned"
    )
    assert "cobalt paragraph" in (orphan.quote or "")
