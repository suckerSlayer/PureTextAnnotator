import asyncio
import json
from pathlib import Path

from textual.widgets import Static

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationStore,
    create_record,
)
from pure_text_annotator.app import (
    AnnotationTextArea,
    DemoResetModal,
    RematchConfirmModal,
    RematchReviewModal,
    ViewerApp,
)
from pure_text_annotator.files import load_text_file
from pure_text_annotator.rematch import parse_annotation_records


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
            message = app.screen.query_one(
                "#rematch-confirm-message", Static
            ).render().plain
            assert "Estimated correctness" in message
            assert "Lowest selected estimate" in message
            assert "Evidence: exact: 1" in message

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, RematchConfirmModal)
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
