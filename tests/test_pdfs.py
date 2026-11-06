import asyncio
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pypdf import PdfReader

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationRange,
    AnnotationStore,
    create_record,
    parse_annotation_ranges,
)
from pure_text_annotator import pdfs
from pure_text_annotator.app import (
    AnnotationTextArea,
    EditWarningModal,
    NoteModal,
    ViewerApp,
    VimTextArea,
)
from pure_text_annotator.cli import build_parser
from pure_text_annotator.embed import embed_pdf_annotations
from pure_text_annotator.files import TextFileError, load_source_file, save_text_file
from pure_text_annotator.pdfs import load_pdf_file, pdf_cache_path
from pure_text_annotator.rematch import (
    RematchChoice,
    apply_rematches,
    scan_rematches,
    validate_applied_rematches,
)

from pdf_fixtures import write_text_pdf


def test_pdf_cache_reuses_exact_text_and_geometry(tmp_path: Path) -> None:
    pdf_path = write_text_pdf(
        tmp_path / "cached.pdf",
        ("Alpha beta gamma", "Delta epsilon"),
    )
    cache_dir = tmp_path / "cache"

    first = load_pdf_file(pdf_path, cache_dir=cache_dir)
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    cache_path = pdf_cache_path(digest, cache_dir)
    assert cache_path.is_file()
    if os.name != "nt":
        assert cache_path.stat().st_mode & 0o777 == 0o600

    with patch.object(
        pdfs.pdfplumber,
        "open",
        side_effect=AssertionError("cache hit invoked pdfplumber"),
    ):
        second = load_pdf_file(pdf_path, cache_dir=cache_dir)

    assert second.text == first.text
    assert second.pdf_document == first.pdf_document
    assert second.pdf_document is not None
    start = second.text.index("beta")
    assert second.pdf_document.anchor_for_range(
        start, start + len("beta")
    ) == first.pdf_document.anchor_for_range(start, start + len("beta"))


def test_pdf_cache_misses_when_pdf_bytes_change(tmp_path: Path) -> None:
    pdf_path = write_text_pdf(tmp_path / "changed.pdf", ("Original words",))
    cache_dir = tmp_path / "cache"
    first = load_pdf_file(pdf_path, cache_dir=cache_dir)
    original_stat = pdf_path.stat()

    write_text_pdf(pdf_path, ("Modified words",))
    assert pdf_path.stat().st_size == original_stat.st_size
    os.utime(
        pdf_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    with patch.object(
        pdfs.pdfplumber, "open", wraps=pdfs.pdfplumber.open
    ) as extractor:
        second = load_pdf_file(pdf_path, cache_dir=cache_dir)

    assert extractor.call_count == 1
    assert first.pdf_document is not None
    assert second.pdf_document is not None
    assert first.pdf_document.document_sha256 != second.pdf_document.document_sha256
    assert "Modified words" in second.text
    assert len(tuple(cache_dir.glob("*.pta-pdf-cache.gz"))) == 2


def test_corrupt_pdf_cache_is_rebuilt(tmp_path: Path) -> None:
    pdf_path = write_text_pdf(tmp_path / "corrupt.pdf", ("Alpha beta",))
    cache_dir = tmp_path / "cache"
    first = load_pdf_file(pdf_path, cache_dir=cache_dir)
    assert first.pdf_document is not None
    cache_path = pdf_cache_path(first.pdf_document.document_sha256, cache_dir)
    cache_path.write_bytes(b"not a valid cache")

    with patch.object(
        pdfs.pdfplumber, "open", wraps=pdfs.pdfplumber.open
    ) as extractor:
        rebuilt = load_pdf_file(pdf_path, cache_dir=cache_dir)
    assert extractor.call_count == 1
    assert rebuilt.text == first.text

    with patch.object(
        pdfs.pdfplumber,
        "open",
        side_effect=AssertionError("rebuilt cache was not used"),
    ):
        cached = load_pdf_file(pdf_path, cache_dir=cache_dir)
    assert cached.pdf_document == first.pdf_document


def test_rebuild_and_disable_pdf_cache_controls(tmp_path: Path) -> None:
    pdf_path = write_text_pdf(tmp_path / "controls.pdf", ("Alpha beta",))
    cache_dir = tmp_path / "cache"
    first = load_pdf_file(pdf_path, cache_dir=cache_dir)

    with patch.object(
        pdfs.pdfplumber, "open", wraps=pdfs.pdfplumber.open
    ) as extractor:
        rebuilt = load_pdf_file(
            pdf_path, cache_dir=cache_dir, rebuild_cache=True
        )
    assert extractor.call_count == 1
    assert rebuilt.pdf_document == first.pdf_document

    other_cache = tmp_path / "disabled-cache"
    load_pdf_file(pdf_path, cache_dir=other_cache, use_cache=False)
    assert not other_cache.exists()


def test_pdf_cache_profile_change_and_write_failure_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = write_text_pdf(tmp_path / "profile.pdf", ("Alpha beta",))
    cache_dir = tmp_path / "cache"
    first = load_pdf_file(pdf_path, cache_dir=cache_dir)
    assert first.pdf_document is not None

    monkeypatch.setattr(pdfs, "EXTRACTION_PROFILE", "pta-canonical-test-v2")
    with patch.object(
        pdfs.pdfplumber, "open", wraps=pdfs.pdfplumber.open
    ) as extractor:
        changed_profile = load_pdf_file(pdf_path, cache_dir=cache_dir)
    assert extractor.call_count == 1
    assert changed_profile.pdf_document is not None
    assert changed_profile.pdf_document.extraction_profile == "pta-canonical-test-v2"

    blocked_cache = tmp_path / "not-a-directory"
    blocked_cache.write_text("cache writes are unavailable", encoding="utf-8")
    uncached = load_pdf_file(pdf_path, cache_dir=blocked_cache)
    assert uncached.text == first.text


def test_pdf_cache_cli_controls_are_mutually_exclusive() -> None:
    parser = build_parser()
    disabled = parser.parse_args(["--no-pdf-cache", "paper.pdf"])
    rebuilt = parser.parse_args(["--rebuild-pdf-cache", "paper.pdf"])

    assert disabled.no_pdf_cache is True
    assert disabled.rebuild_pdf_cache is False
    assert rebuilt.no_pdf_cache is False
    assert rebuilt.rebuild_pdf_cache is True
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--no-pdf-cache", "--rebuild-pdf-cache", "paper.pdf"]
        )


def test_load_pdf_builds_canonical_pages_and_geometry(tmp_path: Path) -> None:
    pdf_path = write_text_pdf(
        tmp_path / "paper.pdf",
        ("Alpha beta gamma\nFirst page line two.", "Delta epsilon"),
    )

    source = load_source_file(pdf_path)

    assert source.source_kind == "pdf"
    assert source.editable is False
    assert source.pdf_document is not None
    assert source.pdf_document.page_count == 2
    assert "--- PDF page 1 of 2 ---\nAlpha beta gamma" in source.text
    assert "--- PDF page 2 of 2 ---\nDelta epsilon" in source.text
    assert source.pdf_document.document_sha256 == hashlib.sha256(
        pdf_path.read_bytes()
    ).hexdigest()

    start = source.text.index("beta")
    anchor = source.pdf_document.anchor_for_range(start, start + 4)

    assert anchor is not None
    assert anchor.extractor_name == "pdfplumber"
    assert anchor.coordinate_system == "pdf-user-space-bottom-left"
    assert len(anchor.fragments) == 1
    fragment = anchor.fragments[0]
    assert fragment.page_index == 0
    assert fragment.page_label == "1"
    assert (fragment.text_start, fragment.text_end) == (start, start + 4)
    assert fragment.crop_box == pytest.approx((0, 0, 612, 792))
    assert fragment.rotation == 0
    assert len(fragment.quad_points_pdf) == 1
    assert fragment.rect_pdf[0] < fragment.rect_pdf[2]
    assert fragment.rect_pdf[1] < fragment.rect_pdf[3]


def test_pdf_anchor_handles_partial_words_and_multiple_pages(tmp_path: Path) -> None:
    source = load_source_file(
        write_text_pdf(
            tmp_path / "paper.pdf",
            ("Alpha beta gamma", "Delta epsilon"),
        )
    )
    assert source.pdf_document is not None
    beta_start = source.text.index("beta")

    whole = source.pdf_document.anchor_for_range(beta_start, beta_start + 4)
    partial = source.pdf_document.anchor_for_range(beta_start + 1, beta_start + 4)
    assert whole is not None and partial is not None
    whole_width = whole.fragments[0].rect_pdf[2] - whole.fragments[0].rect_pdf[0]
    partial_width = (
        partial.fragments[0].rect_pdf[2] - partial.fragments[0].rect_pdf[0]
    )
    assert 0 < partial_width < whole_width

    epsilon_end = source.text.index("epsilon") + len("epsilon")
    across_pages = source.pdf_document.anchor_for_range(beta_start, epsilon_end)
    assert across_pages is not None
    assert [fragment.page_index for fragment in across_pages.fragments] == [0, 1]


def test_pdf_anchor_is_written_beside_text_anchor(tmp_path: Path) -> None:
    source = load_source_file(
        write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta gamma",))
    )
    assert source.pdf_document is not None
    start = source.text.index("beta")
    draft = AnnotationDraft(start, start + 4, "beta")
    pdf_anchor = source.pdf_document.anchor_for_range(draft.start, draft.end)
    assert pdf_anchor is not None
    store = AnnotationStore(source.path)

    saved = store.append(
        create_record(
            draft,
            "A PDF note.",
            annotation_id="pdf-note-1",
            created_at="2026-11-04T10:00:00Z",
            source_text=source.text,
            pdf_anchor=pdf_anchor,
        )
    )

    assert "anchor:" in saved
    assert "pdf_anchor:" in saved
    assert "  document_sha256:" in saved
    assert '    name: "pdfplumber"' in saved
    assert "    - page_index: 0" in saved
    assert "      text_range:" in saved
    assert "      rect_pdf: [" in saved
    assert "      quad_points_pdf:" in saved
    assert parse_annotation_ranges(saved) == (AnnotationRange(start, start + 4),)


def test_pdf_rematch_refreshes_geometry_for_embedding(tmp_path: Path) -> None:
    source_path = write_text_pdf(
        tmp_path / "paper.pdf", ("Alpha unique target omega",)
    )
    original = load_source_file(source_path)
    assert original.pdf_document is not None
    start = original.text.index("unique target")
    end = start + len("unique target")
    original_anchor = original.pdf_document.anchor_for_range(start, end)
    assert original_anchor is not None
    store = AnnotationStore(source_path)
    before = store.append(
        create_record(
            AnnotationDraft(start, end, "unique target"),
            "Move this PDF highlight with its text.",
            annotation_id="pdf-rematch",
            created_at="2026-11-06T10:00:00Z",
            source_text=original.text,
            pdf_anchor=original_anchor,
        )
    )

    write_text_pdf(
        source_path,
        ("A newly inserted first page", "Alpha unique target omega"),
    )
    current = load_source_file(source_path)
    assert current.pdf_document is not None
    assert (
        current.pdf_document.document_sha256
        != original.pdf_document.document_sha256
    )
    candidate = scan_rematches(current.text, before).items[0].proposed
    assert candidate is not None
    choice = RematchChoice("pdf-rematch", candidate)

    updated = apply_rematches(
        before,
        current.text,
        (choice,),
        rematched_at="2026-11-06T11:00:00Z",
        pdf_document=current.pdf_document,
    )
    validate_applied_rematches(
        before,
        updated,
        current.text,
        (choice,),
        pdf_document=current.pdf_document,
    )

    assert original.pdf_document.document_sha256 not in updated
    assert current.pdf_document.document_sha256 in updated
    assert "    - page_index: 1" in updated
    store.path.write_text(updated, encoding="utf-8")
    result = embed_pdf_annotations(source_path, loaded_source=current)
    reader = PdfReader(result.output_path)
    assert reader.pages[0].get("/Annots") is None
    annotations = tuple(
        item.get_object()
        for item in reader.pages[1]["/Annots"].get_object()
    )
    assert len(annotations) == 1
    assert annotations[0]["/NM"] == "pdf-rematch"
    assert annotations[0]["/Contents"] == "Move this PDF highlight with its text."


def test_rematch_without_pdf_geometry_removes_stale_pdf_anchor(
    tmp_path: Path,
) -> None:
    source_path = write_text_pdf(
        tmp_path / "paper.pdf", ("Alpha unique target omega",)
    )
    original = load_source_file(source_path)
    assert original.pdf_document is not None
    start = original.text.index("unique target")
    end = start + len("unique target")
    anchor = original.pdf_document.anchor_for_range(start, end)
    assert anchor is not None
    store = AnnotationStore(source_path)
    before = store.append(
        create_record(
            AnnotationDraft(start, end, "unique target"),
            "A note.",
            annotation_id="safe-rematch",
            source_text=original.text,
            pdf_anchor=anchor,
        )
    )
    changed = "Inserted text before the PDF projection.\n" + original.text
    candidate = scan_rematches(changed, before).items[0].proposed
    assert candidate is not None
    choice = RematchChoice("safe-rematch", candidate)

    updated = apply_rematches(
        before,
        changed,
        (choice,),
        rematched_at="2026-11-06T11:00:00Z",
    )
    validate_applied_rematches(before, updated, changed, (choice,))

    assert "pdf_anchor:" not in updated


def test_blank_pdf_reports_missing_text_layer(tmp_path: Path) -> None:
    pdf_path = write_text_pdf(tmp_path / "scan.pdf", ("",))

    with pytest.raises(TextFileError, match="no extractable text layer"):
        load_source_file(pdf_path)


def test_pdf_extracted_source_cannot_be_saved(tmp_path: Path) -> None:
    source = load_source_file(
        write_text_pdf(tmp_path / "paper.pdf", ("Read only",))
    )

    with pytest.raises(TextFileError, match="read-only view"):
        save_text_file(source, source.text + "changed")


def test_pdf_app_blocks_source_edit_and_saves_geometry_note(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = load_source_file(
            write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta",))
        )
        output_path = tmp_path / "paper.annotated.pdf"
        app = ViewerApp(source, clipboard_writer=lambda _text: "test")

        async with app.run_test(size=(120, 28)) as pilot:
            viewer = app.query_one(VimTextArea)
            annotator = app.query_one(AnnotationTextArea)
            assert viewer.border_title == "Extracted PDF text"

            await pilot.press("i")
            await pilot.pause()
            assert viewer.edit_mode is False
            assert viewer.read_only is True
            assert not isinstance(app.screen, EditWarningModal)

            viewer.move_cursor((1, 0))
            await pilot.press("v", "l", "l", "l", "l", "l", "a")
            await pilot.pause()
            assert isinstance(app.screen, NoteModal)
            app.screen.query_one("#note-input").load_text("First PDF note.")
            await pilot.press("ctrl+s")
            await pilot.pause()

            saved = (tmp_path / "paper.pdf.annotations.md").read_text(
                encoding="utf-8"
            )
            assert "> Alpha" in saved
            assert "pdf_anchor:" in saved
            assert "quad_points_pdf:" in saved
            assert annotator.canonical_text == saved
            assert output_path.is_file()
            annotations = tuple(
                item.get_object()
                for item in PdfReader(output_path).pages[0][
                    "/Annots"
                ].get_object()
            )
            assert len(annotations) == 1
            assert annotations[0]["/Contents"] == "First PDF note."

    asyncio.run(scenario())


def test_pdf_annotation_ctrl_s_refreshes_copy(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = load_source_file(
            write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta",))
        )
        assert source.pdf_document is not None
        start = source.text.index("Alpha")
        end = start + len("Alpha")
        anchor = source.pdf_document.anchor_for_range(start, end)
        assert anchor is not None
        AnnotationStore(source.path).append(
            create_record(
                AnnotationDraft(start, end, "Alpha"),
                "Original note",
                annotation_id="save-refresh",
                source_text=source.text,
                pdf_anchor=anchor,
            )
        )
        output_path = tmp_path / "paper.annotated.pdf"
        app = ViewerApp(source, clipboard_writer=lambda _text: "test")

        async with app.run_test(size=(110, 28)) as pilot:
            annotator = app.query_one(AnnotationTextArea)
            await pilot.press("tab", "i")
            note_end = annotator.text.index("Original note") + len(
                "Original note"
            )
            annotator.move_cursor(
                annotator.document.get_location_from_index(note_end)
            )
            await pilot.press(
                "space",
                "u",
                "p",
                "d",
                "a",
                "t",
                "e",
                "d",
                "escape",
                "ctrl+s",
            )
            await pilot.pause()

            assert app.quit_completed is False
            assert output_path.is_file()
            annotations = tuple(
                item.get_object()
                for item in PdfReader(output_path).pages[0][
                    "/Annots"
                ].get_object()
            )
            assert annotations[0]["/Contents"] == "Original note updated"

    asyncio.run(scenario())


def test_pdf_quit_without_annotation_save_does_not_embed(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = load_source_file(
            write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta",))
        )
        assert source.pdf_document is not None
        start = source.text.index("Alpha")
        end = start + len("Alpha")
        anchor = source.pdf_document.anchor_for_range(start, end)
        assert anchor is not None
        AnnotationStore(source.path).append(
            create_record(
                AnnotationDraft(start, end, "Alpha"),
                "Existing note",
                annotation_id="quit-does-not-embed",
                source_text=source.text,
                pdf_anchor=anchor,
            )
        )
        output_path = tmp_path / "paper.annotated.pdf"
        app = ViewerApp(source, clipboard_writer=lambda _text: "test")

        async with app.run_test(size=(110, 28)) as pilot:
            assert not output_path.exists()
            await pilot.press("q")
            await pilot.pause()

        assert app.quit_completed is True
        assert not output_path.exists()

    asyncio.run(scenario())
