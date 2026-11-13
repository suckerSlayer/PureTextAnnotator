import asyncio
import hashlib
import json
import threading
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
from textual.widgets import Button, LoadingIndicator, Static

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationStore,
    create_record,
)
from pure_text_annotator.app import (
    AnnotationTextArea,
    DemoResetModal,
    PdfConflictConfirmModal,
    PdfConflictReviewModal,
    ProcessingModal,
    RematchConfirmModal,
    RematchReviewModal,
    VimTextArea,
    ViewerApp,
)
from pure_text_annotator.embed import embed_pdf_annotations
from pure_text_annotator.files import load_source_file, load_text_file
from pure_text_annotator.rematch import parse_annotation_records, scan_rematches

from pdf_fixtures import write_text_pdf


def prepare_shifted_case(tmp_path: Path) -> tuple[Path, AnnotationStore, str]:
    original = "Intro.\nTarget sentence stays intact.\nOutro.\n"
    source_path = tmp_path / "source.md"
    source_path.write_text(original, encoding="utf-8")
    start = original.index("Target sentence")
    store = AnnotationStore(source_path)
    before = store.append(
        create_record(
            AnnotationDraft(
                start,
                start + len("Target sentence stays intact."),
                "Target sentence stays intact.",
            ),
            "The note must remain unchanged.",
            annotation_id="shifted-note",
            created_at="2026-11-01T10:00:00Z",
            source_text=original,
        )
    )
    source_path.write_text("Inserted preface.\n" + original, encoding="utf-8")
    return source_path, store, before


def test_annotation_menu_opens_rematch_review(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_path, _store, _before = prepare_shifted_case(tmp_path)
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.press("m", "r")
            await pilot.pause()
            assert isinstance(app.screen, RematchReviewModal)

    asyncio.run(scenario())


def test_rematch_scan_shows_animated_processing_overlay(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        source_path, _store, _before = prepare_shifted_case(tmp_path)
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )
        started = threading.Event()
        release = threading.Event()
        original_scan = scan_rematches

        def slow_scan(source_text: str, sidecar_text: str):
            started.set()
            assert release.wait(timeout=3)
            return original_scan(source_text, sidecar_text)

        monkeypatch.setattr(
            "pure_text_annotator.app.scan_rematches", slow_scan
        )

        async with app.run_test(size=(100, 30)) as pilot:
            press_task = asyncio.create_task(pilot.press("shift+r"))
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.sleep(0.05)
            try:
                assert isinstance(app.screen, ProcessingModal)
                assert app.screen.query_one(LoadingIndicator).is_mounted
                message = app.screen.query_one(
                    "#processing-message", Static
                ).render().plain
                assert "Scanning annotation anchors" in message
            finally:
                release.set()
            await press_task
            await pilot.pause()
            assert isinstance(app.screen, RematchReviewModal)

    asyncio.run(scenario())


def test_rematch_reviews_deleted_pdf_annotation_and_can_restore_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
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
        store.append(
            create_record(
                AnnotationDraft(start, end, "unique target"),
                "Original PDF note.",
                annotation_id="deleted-pdf-note",
                source_text=original.text,
                pdf_anchor=anchor,
            )
        )
        embed_pdf_annotations(source_path)
        store.path.write_text(
            store.path.read_text(encoding="utf-8").replace(
                "Original PDF note.", "Edited Markdown note."
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
        deleted_pdf_bytes = source_path.read_bytes()
        conflicted_sidecar_bytes = store.path.read_bytes()

        app = ViewerApp(
            load_source_file(source_path),
            clipboard_writer=lambda _text: "test",
        )
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, PdfConflictReviewModal)
            await pilot.click("#cancel-pdf-conflict-review")
            await pilot.pause()
            assert not isinstance(app.screen, PdfConflictReviewModal)
            assert source_path.read_bytes() == deleted_pdf_bytes
            assert store.path.read_bytes() == conflicted_sidecar_bytes

            await pilot.press("shift+r")
            await pilot.pause()
            assert isinstance(app.screen, PdfConflictReviewModal)
            detail = app.screen.query_one(
                "#pdf-conflict-review-detail", Static
            ).render().plain
            assert "deleted-from-pdf" in detail
            assert "Edited Markdown note." in detail
            viewer = app.query_one("#viewer", VimTextArea)
            assert viewer.rematch_candidate_range is not None

            await pilot.click("#keep-markdown-conflict")
            await pilot.click("#continue-pdf-conflicts")
            await pilot.pause()
            assert isinstance(app.screen, PdfConflictConfirmModal)
            await pilot.click("#apply-pdf-conflicts")
            await pilot.pause()

            annotations = tuple(
                item.get_object()
                for item in PdfReader(source_path).pages[0][
                    "/Annots"
                ].get_object()
            )
            assert len(annotations) == 1
            assert annotations[0]["/NM"] == "deleted-pdf-note"
            assert annotations[0]["/Contents"] == "Edited Markdown note."
            assert "Edited Markdown note." in store.read_text()
            assert list(tmp_path.glob("*.before-pdf-conflict-*"))

    asyncio.run(scenario())


def test_rematch_reviews_deleted_markdown_annotation_and_can_restore_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source_path = write_text_pdf(
            tmp_path / "markdown-delete.pdf",
            ("Alpha unique target omega",),
        )
        source = load_source_file(source_path)
        assert source.pdf_document is not None
        start = source.text.index("unique target")
        end = start + len("unique target")
        anchor = source.pdf_document.anchor_for_range(start, end)
        assert anchor is not None
        store = AnnotationStore(source_path)
        store.append(
            create_record(
                AnnotationDraft(start, end, "unique target"),
                "Restore this PDF note.",
                annotation_id="deleted-markdown-note",
                source_text=source.text,
                pdf_anchor=anchor,
            )
        )
        embed_pdf_annotations(source_path)
        synchronized = store.read_text()
        record = parse_annotation_records(synchronized)[0]
        externally_deleted = (
            synchronized[: record.block_start]
            + synchronized[record.block_end :]
        )

        app = ViewerApp(
            load_source_file(source_path),
            clipboard_writer=lambda _text: "test",
        )
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            store.path.write_text(externally_deleted, encoding="utf-8")
            await pilot.press("shift+r")
            await pilot.pause()

            assert isinstance(app.screen, PdfConflictReviewModal)
            assert "deleted-markdown-note" not in app.query_one(
                AnnotationTextArea
            ).canonical_text
            detail = app.screen.query_one(
                "#pdf-conflict-review-detail", Static
            ).render().plain
            assert "deleted-from-markdown" in detail
            assert "Restore this PDF note." in detail
            assert (
                str(
                    app.screen.query_one(
                        "#keep-markdown-conflict", Button
                    ).label
                )
                == "Delete PDF"
            )
            assert (
                str(
                    app.screen.query_one(
                        "#keep-pdf-conflict", Button
                    ).label
                )
                == "Restore MD"
            )

            await pilot.click("#keep-pdf-conflict")
            await pilot.click("#continue-pdf-conflicts")
            await pilot.pause()
            assert isinstance(app.screen, PdfConflictConfirmModal)
            confirmation = app.screen.query_one(
                "#pdf-conflict-confirm-message", Static
            ).render().plain
            assert "restore annotation into Markdown" in confirmation
            await pilot.click("#apply-pdf-conflicts")
            await pilot.pause()

            restored = parse_annotation_records(store.read_text())
            assert len(restored) == 1
            assert restored[0].annotation_id == "deleted-markdown-note"
            assert restored[0].note == "Restore this PDF note."
            assert list(tmp_path.glob("*.before-pdf-conflict-*"))

    asyncio.run(scenario())


def test_pdf_rematch_apply_refreshes_geometry_anchor(tmp_path: Path) -> None:
    async def scenario() -> None:
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
        store.append(
            create_record(
                AnnotationDraft(start, end, "unique target"),
                "Move the PDF anchor too.",
                annotation_id="pdf-rematch-app",
                source_text=original.text,
                pdf_anchor=anchor,
            )
        )
        write_text_pdf(
            source_path,
            ("A newly inserted page", "Alpha unique target omega"),
        )
        current = load_source_file(source_path)
        assert current.pdf_document is not None
        app = ViewerApp(current, clipboard_writer=lambda _text: "test")

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.press("shift+r", "ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, RematchConfirmModal)
            await pilot.click("#apply-rematch-confirm")
            await pilot.pause()

            saved = store.path.read_text(encoding="utf-8")
            synchronized_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            assert synchronized_hash in saved
            assert current.pdf_document.document_sha256 not in saved
            assert original.pdf_document.document_sha256 not in saved
            assert "    - page_index: 1" in saved
            output_path = source_path
            assert output_path.is_file()
            annotations = tuple(
                item.get_object()
                for item in PdfReader(output_path).pages[1][
                    "/Annots"
                ].get_object()
            )
            assert annotations[0]["/NM"] == "pdf-rematch-app"

    asyncio.run(scenario())


def test_rematch_cancel_is_read_only_and_apply_creates_backup(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source_path, store, before = prepare_shifted_case(tmp_path)
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(120, 34)) as pilot:
            annotator = app.query_one(AnnotationTextArea)
            viewer = app.query_one(VimTextArea)
            assert annotator.folding_enabled is True
            assert annotator.canonical_text == before
            assert annotator.text != before

            await pilot.press("shift+r")
            await pilot.pause()
            assert isinstance(app.screen, RematchReviewModal)
            assert store.path.read_text(encoding="utf-8") == before

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, RematchConfirmModal)
            confirm = app.screen
            dialog = confirm.query_one("#rematch-confirm-dialog")
            message = app.screen.query_one(
                "#rematch-confirm-message", Static
            ).render().plain
            assert dialog.region.height <= 10
            assert dialog.region.y > 0
            assert "Estimated correctness" in message
            assert "Lowest selected estimate" in message
            assert "Evidence: exact: 1" in message
            assert viewer.rematch_candidate_range == (
                confirm.choices[0].candidate.source_range
            )
            assert annotator.active_link is not None

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, RematchConfirmModal)
            assert viewer.rematch_candidate_range is None
            assert store.path.read_text(encoding="utf-8") == before
            assert list(tmp_path.glob("*.before-rematch-*")) == []

            await pilot.press("shift+r", "ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, RematchConfirmModal)
            await pilot.click("#apply-rematch-confirm")
            await pilot.pause()

            saved = store.path.read_text(encoding="utf-8")
            assert annotator.canonical_text == saved
            assert annotator.text != saved
            record = parse_annotation_records(saved)[0]
            changed = source_path.read_text(encoding="utf-8")
            assert record.start == changed.index("Target sentence")
            assert record.note == "The note must remain unchanged."
            backups = list(tmp_path.glob("*.before-rematch-*"))
            assert len(backups) == 1
            assert backups[0].read_text(encoding="utf-8") == before

    asyncio.run(scenario())


def test_rematch_confirmation_previews_each_selected_change(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        original = (
            "First target remains stable.\n"
            "Middle context.\n"
            "Second target remains stable.\n"
        )
        source_path = tmp_path / "source.md"
        source_path.write_text(original, encoding="utf-8")
        store = AnnotationStore(source_path)
        for ordinal, quote in enumerate(
            (
                "First target remains stable.",
                "Second target remains stable.",
            ),
            start=1,
        ):
            start = original.index(quote)
            store.append(
                create_record(
                    AnnotationDraft(start, start + len(quote), quote),
                    f"Note {ordinal}",
                    annotation_id=f"selected-{ordinal}",
                    created_at=f"2026-11-02T10:0{ordinal}:00Z",
                    source_text=original,
                )
            )
        source_path.write_text("Inserted preface.\n" + original, encoding="utf-8")
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(100, 28)) as pilot:
            viewer = app.query_one(VimTextArea)
            await pilot.press("shift+r", "ctrl+s")
            await pilot.pause()

            assert isinstance(app.screen, RematchConfirmModal)
            confirm = app.screen
            assert len(confirm.choices) == 2
            message_widget = confirm.query_one(
                "#rematch-confirm-message", Static
            )
            first_message = message_widget.render().plain
            assert "#1 · Shifted" in first_message
            assert viewer.rematch_candidate_range == (
                confirm.choices[0].candidate.source_range
            )

            await pilot.press("j")
            await pilot.pause()
            second_message = message_widget.render().plain
            assert "#2 · Shifted" in second_message
            assert first_message != second_message
            assert viewer.rematch_candidate_range == (
                confirm.choices[1].candidate.source_range
            )

            await pilot.press("k")
            await pilot.pause()
            assert "#1 · Shifted" in message_widget.render().plain
            assert viewer.rematch_candidate_range == (
                confirm.choices[0].candidate.source_range
            )

            await pilot.press("escape")
            await pilot.pause()
            assert viewer.rematch_candidate_range is None

    asyncio.run(scenario())


def test_rematch_review_is_compact_and_previews_both_panes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source_path, _store, _before = prepare_shifted_case(tmp_path)
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(80, 24)) as pilot:
            viewer = app.query_one(VimTextArea)
            annotator = app.query_one(AnnotationTextArea)
            original_viewer_cursor = viewer.cursor_location
            original_annotator_cursor = annotator.cursor_location
            original_active_link = annotator.active_link

            await pilot.press("shift+r")
            await pilot.pause()

            assert isinstance(app.screen, RematchReviewModal)
            review = app.screen
            dialog = review.query_one("#rematch-review-dialog")
            detail = app.screen.query_one(
                "#rematch-review-detail", Static
            ).render().plain
            candidate = review._current_candidate(review.items[0])
            assert candidate is not None

            assert dialog.region.height <= 10
            assert dialog.region.y > 0
            assert "#1 · Shifted" in detail
            assert "candidate 1/1" in detail
            assert "evidence:" in detail
            assert viewer.rematch_candidate_range == candidate.source_range
            assert annotator.active_link is not None
            assert viewer.cursor_location == app._source_location_for_offset(
                candidate.source_range.start
            )

            await pilot.press("escape")
            await pilot.pause()
            assert viewer.rematch_candidate_range is None
            assert viewer.cursor_location == original_viewer_cursor
            assert annotator.cursor_location == original_annotator_cursor
            assert annotator.active_link == original_active_link

    asyncio.run(scenario())


def test_rematch_review_exposes_and_selects_alternative_candidates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        original = "Repeated phrase."
        source_path = tmp_path / "source.md"
        source_path.write_text(original, encoding="utf-8")
        store = AnnotationStore(source_path)
        store.append(
            create_record(
                AnnotationDraft(0, len(original), original),
                "Choose the occurrence with the intended context.",
                annotation_id="ambiguous-note",
                created_at="2026-11-02T10:00:00Z",
                source_text=original,
            )
        )
        changed = (
            "Alpha context.\nRepeated phrase.\nMiddle context.\n"
            "Repeated phrase.\nOmega context."
        )
        source_path.write_text(changed, encoding="utf-8")
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.press("shift+r")
            await pilot.pause()

            assert isinstance(app.screen, RematchReviewModal)
            review = app.screen
            item = review.items[0]
            annotation_id = item.annotation.annotation_id
            candidates = review.candidates_by_id[annotation_id]
            assert len(candidates) == 2
            viewer = app.query_one(VimTextArea)
            annotator = app.query_one(AnnotationTextArea)

            detail_widget = review.query_one("#rematch-review-detail", Static)
            first_detail = detail_widget.render().plain
            assert "candidate 1/2" in first_detail
            assert "line 2, column 1" in first_detail
            assert viewer.rematch_candidate_range == candidates[0].source_range
            active_link = annotator.active_link
            assert active_link is not None

            await pilot.press("c")
            await pilot.pause()
            second_detail = detail_widget.render().plain
            assert "candidate 2/2" in second_detail
            assert "line 4, column 1" in second_detail
            assert first_detail != second_detail
            assert viewer.rematch_candidate_range == candidates[1].source_range
            assert annotator.active_link == active_link

            await pilot.press("space")
            choices = review._choices()
            assert len(choices) == 1
            assert choices[0].candidate.source_range == candidates[1].source_range

            await pilot.press("shift+c")
            await pilot.pause()
            assert "candidate 1/2" in detail_widget.render().plain
            assert viewer.rematch_candidate_range == candidates[0].source_range

    asyncio.run(scenario())


def test_disk_change_after_review_invalidates_the_proposal(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_path, store, before = prepare_shifted_case(tmp_path)
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.press("shift+r", "ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, RematchConfirmModal)
            source_path.write_text(
                source_path.read_text(encoding="utf-8") + "external edit\n",
                encoding="utf-8",
            )

            await pilot.click("#apply-rematch-confirm")
            await pilot.pause()

            assert store.path.read_text(encoding="utf-8") == before
            assert list(tmp_path.glob("*.before-rematch-*")) == []

    asyncio.run(scenario())


def test_marked_demo_first_r_requires_reset_then_second_r_scans(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source_path, store, sidecar_default = prepare_shifted_case(tmp_path)
        source_default = source_path.read_text(encoding="utf-8")
        case_id = "test-reset-case"
        source_default = (
            f'<!-- pta:demo-case id="{case_id}" -->\n' + source_default
        )
        source_path.write_text("locally changed demo\n", encoding="utf-8")
        store.path.write_text("locally changed annotations\n", encoding="utf-8")
        manifest = {
            "format": "pure-text-annotator-rematch-demo",
            "version": 1,
            "id": case_id,
            "name": "Test reset demo",
            "description": "A reset test.",
            "source_text": source_default,
            "annotation_text": sidecar_default,
        }
        source_path.with_name(source_path.name + ".pta-demo.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.press("shift+r")
            await pilot.pause()
            assert isinstance(app.screen, DemoResetModal)
            assert source_path.read_text(encoding="utf-8") == "locally changed demo\n"

            await pilot.click("#confirm-demo-reset")
            await pilot.pause()
            assert source_path.read_text(encoding="utf-8") == source_default
            assert store.path.read_text(encoding="utf-8") == sidecar_default

            await pilot.press("shift+r")
            await pilot.pause()
            assert isinstance(app.screen, RematchReviewModal)

    asyncio.run(scenario())
