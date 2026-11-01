from pathlib import Path

import pytest

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationFileError,
    AnnotationRange,
    AnnotationStore,
    create_record,
)
from pure_text_annotator.rematch import (
    RematchChoice,
    RematchStatus,
    apply_rematches,
    make_manual_candidate,
    parse_annotation_records,
    scan_rematches,
    validate_applied_rematches,
)


def make_sidecar(
    tmp_path: Path,
    source_text: str,
    quote: str,
    *,
    note: str = "Keep this note unchanged.",
    annotation_id: str = "test-note",
) -> tuple[AnnotationStore, str]:
    source_path = tmp_path / "source.md"
    source_path.write_text(source_text, encoding="utf-8")
    start = source_text.index(quote)
    store = AnnotationStore(source_path)
    sidecar = store.append(
        create_record(
            AnnotationDraft(start, start + len(quote), quote),
            note,
            annotation_id=annotation_id,
            created_at="2026-11-01T10:00:00Z",
            source_text=source_text,
        )
    )
    return store, sidecar


def test_new_records_capture_backward_compatible_anchors(tmp_path: Path) -> None:
    source = "prefix text | selected words | suffix text"
    _, sidecar = make_sidecar(tmp_path, source, "selected words")

    record = parse_annotation_records(sidecar)[0]

    assert record.errors == ()
    assert record.prefix == "prefix text | "
    assert record.suffix == " | suffix text"
    assert record.original_start == source.index("selected words")
    assert record.quote_sha256 is not None
    assert record.source_sha256 is not None


def test_unique_exact_quote_is_proposed_after_offset_shift(tmp_path: Path) -> None:
    original = "Intro.\nTarget sentence stays intact.\nOutro.\n"
    _, sidecar = make_sidecar(
        tmp_path, original, "Target sentence stays intact."
    )
    changed = "A newly inserted preface.\n" + original

    report = scan_rematches(changed, sidecar)
    item = report.items[0]

    assert item.status is RematchStatus.SHIFTED
    assert item.proposed is not None
    assert item.proposed.text == "Target sentence stays intact."
    assert item.proposed.source_range.start == changed.index("Target sentence")
    assert item.proposed.confidence >= 90


def test_fuzzy_match_recovers_a_slightly_edited_passage(tmp_path: Path) -> None:
    original = (
        "Before context.\n"
        "The archive closes every evening at six.\n"
        "After context.\n"
    )
    _, sidecar = make_sidecar(
        tmp_path, original, "The archive closes every evening at six."
    )
    changed = (
        "Before context.\n"
        "The archive now closes every evening at seven.\n"
        "After context.\n"
    )

    item = scan_rematches(changed, sidecar).items[0]

    assert item.status in {RematchStatus.MODIFIED, RematchStatus.AMBIGUOUS}
    assert item.proposed is not None
    assert item.proposed.text == "The archive now closes every evening at seven."
    assert item.proposed.confidence >= 60


def test_saved_context_disambiguates_duplicate_exact_quotes(
    tmp_path: Path,
) -> None:
    original = (
        "Alpha section begins. The signal is clear. Alpha section ends.\n"
        "Beta section begins. The signal is clear. Beta section ends.\n"
    )
    second_start = original.rindex("The signal is clear.")
    source_path = tmp_path / "source.md"
    source_path.write_text(original, encoding="utf-8")
    store = AnnotationStore(source_path)
    sidecar = store.append(
        create_record(
            AnnotationDraft(
                second_start,
                second_start + len("The signal is clear."),
                "The signal is clear.",
            ),
            "This belongs to Beta.",
            annotation_id="duplicate-note",
            created_at="2026-11-01T10:00:00Z",
            source_text=original,
        )
    )
    changed = "Inserted heading.\n" + original

    item = scan_rematches(changed, sidecar).items[0]

    assert item.status is RematchStatus.SHIFTED
    assert item.proposed is not None
    assert item.proposed.source_range.start == changed.rindex(
        "The signal is clear."
    )
    assert item.proposed.context_similarity > item.candidates[1].context_similarity


def test_deleted_passage_remains_orphaned(tmp_path: Path) -> None:
    original = "Opening.\nA singular passage that will vanish completely.\nClosing.\n"
    _, sidecar = make_sidecar(
        tmp_path,
        original,
        "A singular passage that will vanish completely.",
    )
    changed = "Opening.\nNothing related remains here.\nClosing.\n"

    item = scan_rematches(changed, sidecar).items[0]

    assert item.status is RematchStatus.ORPHANED
    assert item.candidates == ()


def test_apply_updates_only_selected_record_and_preserves_identity(
    tmp_path: Path,
) -> None:
    original = "Intro.\nTarget sentence stays intact.\nOutro.\n"
    _, sidecar = make_sidecar(
        tmp_path,
        original,
        "Target sentence stays intact.",
        note="Identity must survive.",
    )
    changed = "A newly inserted preface.\n" + original
    report = scan_rematches(changed, sidecar)
    candidate = report.items[0].proposed
    assert candidate is not None
    choices = (RematchChoice("test-note", candidate),)

    updated = apply_rematches(
        sidecar,
        changed,
        choices,
        rematched_at="2026-11-01T12:00:00Z",
    )
    validate_applied_rematches(sidecar, updated, changed, choices)
    record = parse_annotation_records(updated)[0]

    assert record.start == changed.index("Target sentence")
    assert record.end == record.start + len("Target sentence stays intact.")
    assert record.note == "Identity must survive."
    assert record.created_at == "2026-11-01T10:00:00Z"
    assert "rematch_history:" in updated
    assert 'method: "exact"' in updated
    assert "previous_start:" in updated


def test_transactional_write_restores_original_after_failed_validation(
    tmp_path: Path,
) -> None:
    original = "Intro.\nTarget sentence.\n"
    store, sidecar = make_sidecar(tmp_path, original, "Target sentence.")

    def reject(_saved: str) -> None:
        raise ValueError("injected validation failure")

    with pytest.raises(AnnotationFileError, match="original sidecar restored"):
        store.transactional_write(
            sidecar + "changed",
            backup_label="before-rematch-test",
            validate=reject,
        )

    assert store.path.read_text(encoding="utf-8") == sidecar
    backup = store.path.with_name(
        f"{store.path.name}.before-rematch-test"
    )
    assert backup.read_text(encoding="utf-8") == sidecar


def test_normalized_match_maps_unicode_and_whitespace_to_original_offsets(
    tmp_path: Path,
) -> None:
    original = "Heading\nCafe\u0301   au lait\nEnding\n"
    _, sidecar = make_sidecar(tmp_path, original, "Cafe\u0301   au lait")
    changed = "New line\nHeading\nCafé au\nlait\nEnding\n"

    item = scan_rematches(changed, sidecar).items[0]

    assert item.status is RematchStatus.MODIFIED
    assert item.proposed is not None
    assert item.proposed.method == "normalized-exact"
    assert item.proposed.text == "Café au\nlait"
    assert changed[item.proposed.source_range.start : item.proposed.source_range.end] == item.proposed.text


def test_duplicate_legacy_quote_without_context_stays_ambiguous(
    tmp_path: Path,
) -> None:
    original = "Repeated phrase."
    _, sidecar = make_sidecar(tmp_path, original, original)
    record = parse_annotation_records(sidecar)[0]
    assert record.prefix == ""
    # Remove optional anchors to simulate a version-1 legacy record.
    sidecar = sidecar.replace(
        sidecar[sidecar.index("anchor:\n") : sidecar.index("```\n", sidecar.index("anchor:\n"))],
        "",
    )
    changed = "X Repeated phrase. Y Repeated phrase."

    item = scan_rematches(changed, sidecar).items[0]

    assert item.status is RematchStatus.AMBIGUOUS
    assert len(item.candidates) == 2


def test_manual_selection_can_reconnect_an_orphaned_annotation(
    tmp_path: Path,
) -> None:
    original = "The old sentence is entirely gone."
    _, sidecar = make_sidecar(tmp_path, original, original)
    changed = "A human chooses this replacement passage."
    report = scan_rematches(changed, sidecar)
    item = report.items[0]
    assert item.status is RematchStatus.ORPHANED
    manual_range = AnnotationRange(0, len(changed))
    candidate = make_manual_candidate(changed, item.annotation, manual_range)
    choice = RematchChoice(item.annotation.annotation_id, candidate)

    updated = apply_rematches(
        sidecar,
        changed,
        (choice,),
        rematched_at="2026-11-01T12:00:00Z",
    )
    validate_applied_rematches(sidecar, updated, changed, (choice,))

    saved = parse_annotation_records(updated)[0]
    assert saved.quote == changed
    assert 'method: "manual-selection"' in updated


def test_duplicate_annotation_ids_are_malformed(tmp_path: Path) -> None:
    original = "alpha beta"
    source_path = tmp_path / "source.md"
    source_path.write_text(original, encoding="utf-8")
    store = AnnotationStore(source_path)
    for quote in ("alpha", "beta"):
        start = original.index(quote)
        store.append(
            create_record(
                AnnotationDraft(start, start + len(quote), quote),
                f"Note for {quote}",
                annotation_id="duplicate-id",
                created_at="2026-11-01T10:00:00Z",
                source_text=original,
            )
        )

    report = scan_rematches(original, store.read_text())

    assert all(item.status is RematchStatus.MALFORMED for item in report.items)
    assert all("duplicate annotation ID" in item.explanation for item in report.items)


def test_repeated_rematches_append_history_and_refresh_anchors(
    tmp_path: Path,
) -> None:
    original = "Intro.\nTarget sentence stays intact.\nOutro.\n"
    _, sidecar = make_sidecar(
        tmp_path, original, "Target sentence stays intact."
    )

    first_source = "First insertion.\n" + original
    first_candidate = scan_rematches(first_source, sidecar).items[0].proposed
    assert first_candidate is not None
    first_choice = RematchChoice("test-note", first_candidate)
    once = apply_rematches(
        sidecar,
        first_source,
        (first_choice,),
        rematched_at="2026-11-01T12:00:00Z",
    )

    second_source = "Second insertion.\n" + first_source
    second_candidate = scan_rematches(second_source, once).items[0].proposed
    assert second_candidate is not None
    second_choice = RematchChoice("test-note", second_candidate)
    twice = apply_rematches(
        once,
        second_source,
        (second_choice,),
        rematched_at="2026-11-01T13:00:00Z",
    )
    validate_applied_rematches(once, twice, second_source, (second_choice,))

    assert twice.count("  - rematched_at:") == 2
    record = parse_annotation_records(twice)[0]
    assert record.start == second_source.index("Target sentence")
    assert record.source_sha256 is not None
