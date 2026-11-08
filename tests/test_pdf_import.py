import hashlib
from pathlib import Path
from typing import Optional, Sequence, Tuple

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight, Text
from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationStore,
    create_record,
)
from pure_text_annotator.embed import PdfEmbedError, embed_pdf_annotations
from pure_text_annotator.files import load_source_file
from pure_text_annotator.import_cli import build_parser, main
from pure_text_annotator.pdf_import import (
    PdfConflictDecision,
    PdfConflictKind,
    PdfConflictResolution,
    PdfImportConflictError,
    PdfImportError,
    import_pdf_text_markups,
    resolve_pdf_annotation_conflicts,
)
from pure_text_annotator.rematch import parse_annotation_records

from pdf_fixtures import write_text_pdf


MarkupSpec = Tuple[str, str, Optional[str], str]


def _write_text_markups(
    path: Path,
    specs: Sequence[MarkupSpec],
    *,
    add_unsupported_note: bool = False,
) -> None:
    source = load_source_file(path, use_pdf_cache=False)
    assert source.pdf_document is not None
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    for quote, subtype, contents, annotation_name in specs:
        start = source.text.index(quote)
        anchor = source.pdf_document.anchor_for_range(start, start + len(quote))
        assert anchor is not None
        assert len(anchor.fragments) == 1
        fragment = anchor.fragments[0]
        annotation = Highlight(
            rect=fragment.rect_pdf,
            quad_points=ArrayObject(
                FloatObject(coordinate)
                for quad in fragment.quad_points_pdf
                for coordinate in quad
            ),
            highlight_color="ffff00",
            printing=True,
            title_bar="Preview Author",
        )
        annotation[NameObject("/Subtype")] = NameObject(f"/{subtype}")
        annotation[NameObject("/NM")] = TextStringObject(annotation_name)
        annotation[NameObject("/Subj")] = TextStringObject("Imported review")
        annotation[NameObject("/CreationDate")] = TextStringObject(
            "D:20261108093000+08'00'"
        )
        annotation[NameObject("/M")] = TextStringObject(
            "D:20261108100000+08'00'"
        )
        if contents is not None:
            annotation[NameObject("/Contents")] = TextStringObject(contents)
        writer.add_annotation(fragment.page_index, annotation)

    if add_unsupported_note:
        writer.add_annotation(
            0,
            Text(
                rect=(200, 650, 220, 670),
                text="A standalone sticky note",
            ),
        )
    with path.open("wb") as stream:
        writer.write(stream)
    writer.close()


def _page_annotations(path: Path, page_index: int = 0):
    raw = PdfReader(path).pages[page_index].get("/Annots")
    if raw is None:
        return ()
    return tuple(item.get_object() for item in raw.get_object())


def test_imports_supported_pdf_text_markup_with_provenance(tmp_path: Path) -> None:
    source_path = write_text_pdf(
        tmp_path / "review.pdf",
        ("Alpha beta gamma delta",),
    )
    _write_text_markups(
        source_path,
        (
            ("Alpha", "Highlight", "Check the opening.", "preview-highlight"),
            ("beta", "Underline", None, "preview-underline"),
            ("gamma", "Squiggly", "Spelling?", "preview-squiggly"),
            ("delta", "StrikeOut", "Remove this.", "preview-strikeout"),
        ),
        add_unsupported_note=True,
    )

    result = import_pdf_text_markups(source_path, use_pdf_cache=False)

    assert result.imported_count == 4
    assert [item.subtype for item in result.imported] == [
        "Highlight",
        "Underline",
        "Squiggly",
        "StrikeOut",
    ]
    assert all(item.mapping_confidence == 100 for item in result.imported)
    assert result.imported[1].generated_note is True
    assert len(result.skipped) == 1
    assert result.skipped[0].subtype == "Text"
    assert result.skipped[0].reason == "unsupported annotation subtype"

    saved = result.annotations_path.read_text(encoding="utf-8")
    assert saved.count("pdf_import:") == 4
    assert 'subtype: "Underline"' in saved
    assert 'annotation_name: "preview-highlight"' in saved
    assert 'author: "Preview Author"' in saved
    assert 'creation_date: "D:20261108093000+08\'00\'"' in saved
    assert 'created_at: "2026-11-08T09:30:00+08:00"' in saved
    assert "generated_note: true" in saved
    assert saved.count("pdf_state_sha256:") == 4
    assert saved.count("sidecar_state_sha256:") == 4
    assert "_Imported PDF underline without a comment._" in saved
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() in saved
    assert saved.count("pdf_anchor:") == 4
    records = parse_annotation_records(saved)
    assert len(records) == 4
    assert all(not record.errors for record in records)
    assert [record.quote for record in records] == [
        "Alpha",
        "beta",
        "gamma",
        "delta",
    ]


def test_pdf_markup_import_is_idempotent(tmp_path: Path) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    _write_text_markups(
        source_path,
        (("beta", "Highlight", "Review this.", "stable-preview-id"),),
    )

    first = import_pdf_text_markups(source_path, use_pdf_cache=False)
    first_bytes = first.annotations_path.read_bytes()
    second = import_pdf_text_markups(source_path, use_pdf_cache=False)

    assert first.imported_count == 1
    assert second.imported_count == 0
    assert second.already_imported_count == 0
    assert second.skipped == ()
    assert second.annotations_path.read_bytes() == first_bytes


def test_import_skips_markup_that_does_not_match_text(tmp_path: Path) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    reader = PdfReader(source_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.add_annotation(
        0,
        Highlight(
            rect=(400, 100, 450, 120),
            quad_points=ArrayObject(
                FloatObject(value)
                for value in (400, 120, 450, 120, 400, 100, 450, 100)
            ),
            highlight_color="ffff00",
            printing=True,
            title_bar="Preview Author",
        ),
    )
    with source_path.open("wb") as stream:
        writer.write(stream)
    writer.close()

    result = import_pdf_text_markups(source_path, use_pdf_cache=False)

    assert result.imported_count == 0
    assert len(result.skipped) == 1
    assert "does not overlap extracted text" in result.skipped[0].reason
    assert not result.annotations_path.exists()


def test_embedding_does_not_duplicate_imported_source_markup(tmp_path: Path) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    _write_text_markups(
        source_path,
        (("Alpha", "Highlight", "Existing Preview comment", "preview-existing"),),
    )
    original_annotations = _page_annotations(source_path)
    assert len(original_annotations) == 1
    import_result = import_pdf_text_markups(source_path, use_pdf_cache=False)

    embed_result = embed_pdf_annotations(source_path)

    assert import_result.imported_count == 1
    assert embed_result.output_path == source_path
    assert embed_result.annotation_count == 1
    assert embed_result.highlight_count == 1
    embedded_annotations = _page_annotations(embed_result.output_path)
    assert len(embedded_annotations) == 1
    assert str(embedded_annotations[0]["/NM"]).startswith("pdf-import-")
    assert embedded_annotations[0]["/Contents"] == "Existing Preview comment"


def test_import_refreshes_exact_anchor_after_pdf_bytes_change(tmp_path: Path) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    original = load_source_file(source_path, use_pdf_cache=False)
    assert original.pdf_document is not None
    start = original.text.index("beta")
    end = start + len("beta")
    original_anchor = original.pdf_document.anchor_for_range(start, end)
    assert original_anchor is not None
    store = AnnotationStore(source_path)
    before = store.append(
        create_record(
            AnnotationDraft(start, end, "beta"),
            "Keep this note.",
            annotation_id="existing-note",
            source_text=original.text,
            pdf_anchor=original_anchor,
        )
    )
    source_path.write_bytes(source_path.read_bytes() + b"\n% external save\n")
    current_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

    result = import_pdf_text_markups(source_path, use_pdf_cache=False)

    assert result.imported_count == 0
    assert result.refreshed_annotation_ids == ("existing-note",)
    after = store.read_text()
    assert after != before
    assert original_anchor.document_sha256 not in after
    assert current_hash in after
    assert "Keep this note." in after
    embedded = embed_pdf_annotations(source_path)
    assert embedded.annotation_count == 1


def test_import_does_not_refresh_anchor_when_extracted_text_changed(
    tmp_path: Path,
) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    original = load_source_file(source_path, use_pdf_cache=False)
    assert original.pdf_document is not None
    start = original.text.index("beta")
    anchor = original.pdf_document.anchor_for_range(start, start + len("beta"))
    assert anchor is not None
    store = AnnotationStore(source_path)
    before = store.append(
        create_record(
            AnnotationDraft(start, start + len("beta"), "beta"),
            "Do not move silently.",
            annotation_id="stale-note",
            source_text=original.text,
            pdf_anchor=anchor,
        )
    )
    write_text_pdf(source_path, ("Alpha changed beta",))

    result = import_pdf_text_markups(source_path, use_pdf_cache=False)

    assert result.refreshed_anchor_count == 0
    after = store.read_text()
    assert "Do not move silently." in after
    assert anchor.document_sha256 in after
    assert "pdf_sync_document_sha256:" in after
    assert after != before
    with pytest.raises(PdfEmbedError, match="no longer matches"):
        embed_pdf_annotations(source_path)


def test_pdf_import_cli_reports_new_and_existing_markup(
    tmp_path: Path, capsys
) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    _write_text_markups(
        source_path,
        (("beta", "Highlight", None, "cli-preview-id"),),
    )

    assert main([str(source_path), "--no-pdf-cache"]) == 0
    captured = capsys.readouterr()
    assert "Imported 1 PDF text-markup annotation(s)" in captured.out
    assert "page 1 · Highlight" in captured.out
    assert "100% confidence · generated note" in captured.out
    assert captured.err == ""

    assert main([str(source_path), "--no-pdf-cache"]) == 0
    captured = capsys.readouterr()
    assert "PDF and sidecar already match" in captured.out
    assert captured.err == ""

    parser = build_parser()
    arguments = parser.parse_args(["--rebuild-pdf-cache", str(source_path)])
    assert arguments.rebuild_pdf_cache is True


def test_preview_note_edit_reconciles_and_deletion_requires_a_choice(
    tmp_path: Path,
) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    _write_text_markups(
        source_path,
        (("beta", "Highlight", "First note", "preview-stable-id"),),
    )
    imported = import_pdf_text_markups(source_path, use_pdf_cache=False)
    annotation_id = imported.imported[0].annotation_id
    store = AnnotationStore(source_path)

    reader = PdfReader(source_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    annotation = writer.pages[0]["/Annots"][0].get_object()
    annotation[NameObject("/Contents")] = TextStringObject("Edited in Preview")
    with source_path.open("wb") as stream:
        writer.write(stream)
    writer.close()

    updated = import_pdf_text_markups(source_path, use_pdf_cache=False)

    assert updated.updated_annotation_ids == (annotation_id,)
    assert "Edited in Preview" in store.read_text()
    assert "First note" not in store.read_text()

    reader = PdfReader(source_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    del writer.pages[0][NameObject("/Annots")]
    with source_path.open("wb") as stream:
        writer.write(stream)
    writer.close()

    with pytest.raises(PdfImportConflictError) as raised:
        import_pdf_text_markups(source_path, use_pdf_cache=False)
    conflict = raised.value.report.conflicts[0]
    assert conflict.kind is PdfConflictKind.DELETED_FROM_PDF
    resolved = resolve_pdf_annotation_conflicts(
        source_path,
        raised.value.report,
        (
            PdfConflictResolution(
                annotation_id,
                PdfConflictDecision.KEEP_PDF,
            ),
        ),
        use_pdf_cache=False,
        backup_label="before-preview-delete-test",
    )

    assert resolved.accepted_from_pdf == (annotation_id,)
    assert parse_annotation_records(store.read_text()) == ()


def test_sidecar_only_edit_is_not_overwritten_when_pdf_is_unchanged(
    tmp_path: Path,
) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    _write_text_markups(
        source_path,
        (("beta", "Highlight", "PDF baseline", "preview-stable-id"),),
    )
    imported = import_pdf_text_markups(source_path, use_pdf_cache=False)
    sidecar = imported.annotations_path
    edited = sidecar.read_text(encoding="utf-8").replace(
        "PDF baseline", "Markdown-only edit"
    )
    sidecar.write_text(edited, encoding="utf-8")

    result = import_pdf_text_markups(source_path, use_pdf_cache=False)

    assert result.updated_count == 0
    assert sidecar.read_text(encoding="utf-8") == edited


@pytest.mark.parametrize("legacy_sync_metadata", (False, True))
def test_sidecar_edit_and_separate_pdf_addition_merge_without_data_loss(
    tmp_path: Path,
    legacy_sync_metadata: bool,
) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    _write_text_markups(
        source_path,
        (("Alpha", "Highlight", "PDF baseline", "preview-stable-id"),),
    )
    imported = import_pdf_text_markups(source_path, use_pdf_cache=False)
    sidecar = imported.annotations_path
    synchronized = sidecar.read_text(encoding="utf-8")
    if legacy_sync_metadata:
        synchronized = "".join(
            line
            for line in synchronized.splitlines(keepends=True)
            if "pdf_state_sha256:" not in line
            and "sidecar_state_sha256:" not in line
        )
    edited = synchronized.replace(
        "PDF baseline", "PDF baseline\nMarkdown addition"
    )
    sidecar.write_text(edited, encoding="utf-8")

    _write_text_markups(
        source_path,
        (("beta", "Highlight", None, "preview-new-id"),),
    )
    result = import_pdf_text_markups(source_path, use_pdf_cache=False)

    reconciled = sidecar.read_text(encoding="utf-8")
    records = parse_annotation_records(reconciled)
    assert result.imported_count == 1
    assert len(records) == 2
    assert [record.quote for record in records] == ["Alpha", "beta"]
    assert records[0].note == "PDF baseline\nMarkdown addition"
    assert "_Imported PDF highlight without a comment._" in records[1].note


def test_concurrent_pdf_and_sidecar_edits_fail_without_overwriting_either(
    tmp_path: Path,
) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    _write_text_markups(
        source_path,
        (("beta", "Highlight", "Baseline", "preview-stable-id"),),
    )
    imported = import_pdf_text_markups(source_path, use_pdf_cache=False)
    sidecar = imported.annotations_path
    edited_sidecar = sidecar.read_text(encoding="utf-8").replace(
        "Baseline", "Markdown changed too"
    )
    sidecar.write_text(edited_sidecar, encoding="utf-8")

    reader = PdfReader(source_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    annotation = writer.pages[0]["/Annots"][0].get_object()
    annotation[NameObject("/Contents")] = TextStringObject("Preview changed too")
    with source_path.open("wb") as stream:
        writer.write(stream)
    writer.close()
    pdf_after_preview = source_path.read_bytes()

    with pytest.raises(
        PdfImportConflictError, match="same annotation changed"
    ) as raised:
        import_pdf_text_markups(source_path, use_pdf_cache=False)

    conflict = raised.value.report.conflicts[0]
    assert conflict.kind is PdfConflictKind.BOTH_MODIFIED
    assert conflict.annotation_id.startswith("pdf-import-")
    assert conflict.sidecar_note == "Markdown changed too"
    assert conflict.pdf_note == "Preview changed too"
    assert source_path.read_bytes() == pdf_after_preview
    assert sidecar.read_text(encoding="utf-8") == edited_sidecar


@pytest.mark.parametrize(
    "decision, expected_note",
    (
        (PdfConflictDecision.KEEP_MARKDOWN, "Markdown changed too"),
        (PdfConflictDecision.KEEP_PDF, "Preview changed too"),
    ),
)
def test_concurrent_note_conflict_can_choose_either_version(
    tmp_path: Path,
    decision: PdfConflictDecision,
    expected_note: str,
) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    _write_text_markups(
        source_path,
        (("beta", "Highlight", "Baseline", "preview-stable-id"),),
    )
    imported = import_pdf_text_markups(source_path, use_pdf_cache=False)
    annotation_id = imported.imported[0].annotation_id
    sidecar = imported.annotations_path
    sidecar.write_text(
        sidecar.read_text(encoding="utf-8").replace(
            "Baseline", "Markdown changed too"
        ),
        encoding="utf-8",
    )

    reader = PdfReader(source_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    annotation = writer.pages[0]["/Annots"][0].get_object()
    annotation[NameObject("/Contents")] = TextStringObject(
        "Preview changed too"
    )
    with source_path.open("wb") as stream:
        writer.write(stream)
    writer.close()

    with pytest.raises(PdfImportConflictError) as raised:
        import_pdf_text_markups(source_path, use_pdf_cache=False)

    result = resolve_pdf_annotation_conflicts(
        source_path,
        raised.value.report,
        (PdfConflictResolution(annotation_id, decision),),
        use_pdf_cache=False,
        backup_label="before-note-conflict-test",
    )

    records = parse_annotation_records(sidecar.read_text(encoding="utf-8"))
    annotations = _page_annotations(source_path)
    assert len(records) == 1
    assert records[0].note == expected_note
    assert len(annotations) == 1
    assert annotations[0]["/Contents"] == expected_note
    assert result.backup_path is not None


@pytest.mark.parametrize(
    "decision",
    (PdfConflictDecision.KEEP_MARKDOWN, PdfConflictDecision.KEEP_PDF),
)
def test_deleted_pdf_annotation_conflict_can_restore_or_delete(
    tmp_path: Path,
    decision: PdfConflictDecision,
) -> None:
    source_path = write_text_pdf(tmp_path / "review.pdf", ("Alpha beta",))
    _write_text_markups(
        source_path,
        (("beta", "Highlight", "Baseline", "preview-stable-id"),),
    )
    imported = import_pdf_text_markups(source_path, use_pdf_cache=False)
    annotation_id = imported.imported[0].annotation_id
    sidecar = imported.annotations_path
    sidecar.write_text(
        sidecar.read_text(encoding="utf-8").replace(
            "Baseline", "Edited in Markdown"
        ),
        encoding="utf-8",
    )

    reader = PdfReader(source_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    del writer.pages[0][NameObject("/Annots")]
    with source_path.open("wb") as stream:
        writer.write(stream)
    writer.close()
    pdf_without_annotation = source_path.read_bytes()
    sidecar_before_resolution = sidecar.read_bytes()

    with pytest.raises(PdfImportConflictError) as raised:
        import_pdf_text_markups(source_path, use_pdf_cache=False)

    report = raised.value.report
    assert len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict.annotation_id == annotation_id
    assert conflict.kind is PdfConflictKind.DELETED_FROM_PDF
    assert conflict.sidecar_note == "Edited in Markdown"
    assert conflict.pdf_note is None
    assert source_path.read_bytes() == pdf_without_annotation
    assert sidecar.read_bytes() == sidecar_before_resolution

    result = resolve_pdf_annotation_conflicts(
        source_path,
        report,
        (PdfConflictResolution(annotation_id, decision),),
        use_pdf_cache=False,
        backup_label="before-pdf-conflict-test",
    )

    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == sidecar_before_resolution
    records = parse_annotation_records(sidecar.read_text(encoding="utf-8"))
    annotations = _page_annotations(source_path)
    if decision is PdfConflictDecision.KEEP_MARKDOWN:
        assert result.restored_to_pdf == (annotation_id,)
        assert result.accepted_from_pdf == ()
        assert len(records) == 1
        assert records[0].note == "Edited in Markdown"
        assert len(annotations) == 1
        assert annotations[0]["/NM"] == annotation_id
        assert annotations[0]["/Contents"] == "Edited in Markdown"
    else:
        assert result.restored_to_pdf == ()
        assert result.accepted_from_pdf == (annotation_id,)
        assert records == ()
        assert annotations == ()
