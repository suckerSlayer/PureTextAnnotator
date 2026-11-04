import asyncio
import hashlib
from pathlib import Path

import pytest

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationRange,
    AnnotationStore,
    create_record,
    parse_annotation_ranges,
)
from pure_text_annotator.app import (
    AnnotationTextArea,
    EditWarningModal,
    NoteModal,
    ViewerApp,
    VimTextArea,
)
from pure_text_annotator.files import TextFileError, load_source_file, save_text_file

from pdf_fixtures import write_text_pdf


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

    asyncio.run(scenario())
