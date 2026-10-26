import asyncio
from pathlib import Path

from textual.widgets import TextArea

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationRange,
    AnnotationStore,
    create_record,
)
from pure_text_annotator.app import (
    AnnotationTextArea,
    NoteModal,
    SearchInput,
    ViewerApp,
    VimTextArea,
)
from pure_text_annotator.files import LoadedTextFile, load_text_file


def make_source(text: str) -> LoadedTextFile:
    return LoadedTextFile(
        path=Path("/tmp/sample.md"),
        text=text,
        size_bytes=len(text.encode("utf-8")),
        line_count=0 if not text else text.count("\n") + 1,
    )


def test_viewer_is_read_only_and_supports_vim_movement() -> None:
    async def scenario() -> None:
        app = ViewerApp(make_source("alpha\nbeta\ngamma"), clipboard_writer=lambda _text: "test")
        async with app.run_test(size=(80, 16)) as pilot:
            viewer = app.query_one(VimTextArea)
            assert viewer.read_only is True
            assert viewer.cursor_location == (0, 0)

            await pilot.press("j", "l", "l")
            assert viewer.cursor_location == (1, 2)

            await pilot.press("0")
            assert viewer.cursor_location == (1, 0)

            await pilot.press("$")
            assert viewer.cursor_location == (1, 4)

            original = viewer.text
            await pilot.press("i", "x", "backspace", "delete")
            assert viewer.text == original

    asyncio.run(scenario())


def test_visual_selection_can_be_yanked() -> None:
    async def scenario() -> None:
        copied = []
        app = ViewerApp(
            make_source("alpha\nbeta"),
            clipboard_writer=lambda text: copied.append(text) or "test",
        )
        async with app.run_test(size=(80, 16)) as pilot:
            viewer = app.query_one(VimTextArea)

            await pilot.press("v", "l", "l", "y")
            await pilot.pause()

            assert copied == ["al"]
            assert app.clipboard == "al"
            assert viewer.visual_mode is False
            assert viewer.selected_text == ""

    asyncio.run(scenario())


def test_visual_selection_copies_unicode_text() -> None:
    async def scenario() -> None:
        copied = []
        app = ViewerApp(
            make_source("中文内容"),
            clipboard_writer=lambda text: copied.append(text) or "test",
        )
        async with app.run_test(size=(80, 16)) as pilot:
            await pilot.press("v", "l", "l", "y")
            await pilot.pause()

            assert copied == ["中文"]

    asyncio.run(scenario())


def test_visual_selection_can_cross_lines() -> None:
    async def scenario() -> None:
        copied = []
        app = ViewerApp(
            make_source("one\ntwo"),
            clipboard_writer=lambda text: copied.append(text) or "test",
        )
        async with app.run_test(size=(80, 16)) as pilot:
            await pilot.press("v", "$", "j", "y")
            await pilot.pause()

            assert copied == ["one\ntwo"]

    asyncio.run(scenario())


def test_page_navigation_and_document_edges() -> None:
    async def scenario() -> None:
        text = "\n".join(f"line {number}" for number in range(100))
        app = ViewerApp(make_source(text), clipboard_writer=lambda _text: "test")
        async with app.run_test(size=(80, 12)) as pilot:
            viewer = app.query_one(VimTextArea)

            await pilot.press("ctrl+f")
            assert viewer.cursor_location[0] > 0

            await pilot.press("ctrl+b")
            assert viewer.cursor_location == (0, 0)

            await pilot.press("g")
            assert viewer.cursor_location == (0, 0)

            await pilot.press("G")
            assert viewer.cursor_location == viewer.document.end

    asyncio.run(scenario())


def test_annotation_flow_saves_sidecar_and_refreshes_pane(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_path = tmp_path / "source.md"
        source_path.write_text("alpha\nbeta", encoding="utf-8")
        original_bytes = source_path.read_bytes()
        source = load_text_file(source_path)
        app = ViewerApp(source, clipboard_writer=lambda _text: "test")

        async with app.run_test(size=(120, 28)) as pilot:
            annotator = app.query_one("#annotator", TextArea)
            assert annotator.read_only is True
            assert annotator.text == app.annotation_store.initial_text()

            await pilot.press("v", "l", "l", "a")
            await pilot.pause()

            assert isinstance(app.screen, NoteModal)
            note_input = app.screen.query_one("#note-input", TextArea)
            note_input.load_text("My first **note**.")
            await pilot.press("ctrl+s")
            await pilot.pause()

            sidecar = tmp_path / "source.md.annotations.md"
            saved = sidecar.read_text(encoding="utf-8")
            assert "  start: 0" in saved
            assert "  end: 2" in saved
            assert "> al" in saved
            assert "My first **note**." in saved
            assert app.query_one("#annotator", TextArea).text == saved
            viewer = app.query_one(VimTextArea)
            assert viewer.selected_text == ""
            assert viewer.annotation_ranges == (AnnotationRange(0, 2),)
            assert viewer.active_annotation_range == AnnotationRange(0, 2)
            assert [(span.start, span.end) for span in viewer.get_line(0).spans] == [
                (0, 2)
            ]
            assert source_path.read_bytes() == original_bytes

    asyncio.run(scenario())


def test_annotation_requires_a_selection(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_path = tmp_path / "source.txt"
        source_path.write_text("alpha", encoding="utf-8")
        app = ViewerApp(load_text_file(source_path), clipboard_writer=lambda _text: "test")

        async with app.run_test(size=(100, 22)) as pilot:
            await pilot.press("a")
            await pilot.pause()

            assert not isinstance(app.screen, NoteModal)
            assert not (tmp_path / "source.txt.annotations.md").exists()

    asyncio.run(scenario())


def test_existing_annotation_file_is_shown_on_launch(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_path = tmp_path / "source.txt"
        source_path.write_text("alpha", encoding="utf-8")
        sidecar = tmp_path / "source.txt.annotations.md"
        sidecar.write_text("# Existing notes\n\nAlready here.\n", encoding="utf-8")
        app = ViewerApp(load_text_file(source_path), clipboard_writer=lambda _text: "test")

        async with app.run_test(size=(100, 22)):
            assert app.query_one("#annotator", TextArea).text == sidecar.read_text(
                encoding="utf-8"
            )

    asyncio.run(scenario())


def test_existing_annotation_ranges_are_highlighted_on_launch(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_path = tmp_path / "source.txt"
        source_path.write_text("alpha\nbeta", encoding="utf-8")
        sidecar = tmp_path / "source.txt.annotations.md"
        sidecar.write_text(
            """<!-- pta:annotation id=\"existing\" -->
## Annotation 1

```yaml
id: "existing"
type: note
range:
  start: 3
  end: 8
created_at: "2026-10-21T10:00:00Z"
```

### Original text

> ha
> be

### Note

Existing note.

<!-- /pta:annotation -->
""",
            encoding="utf-8",
        )
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(100, 22)) as pilot:
            await pilot.pause()
            viewer = app.query_one(VimTextArea)
            assert viewer.annotation_ranges == (AnnotationRange(3, 8),)
            assert viewer.active_annotation_range == AnnotationRange(3, 8)
            assert [(span.start, span.end) for span in viewer.get_line(0).spans] == [
                (3, 5)
            ]
            assert [(span.start, span.end) for span in viewer.get_line(1).spans] == [
                (0, 2)
            ]

    asyncio.run(scenario())


def test_out_of_bounds_annotation_range_is_not_highlighted(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_path = tmp_path / "source.txt"
        source_path.write_text("alpha", encoding="utf-8")
        sidecar = tmp_path / "source.txt.annotations.md"
        sidecar.write_text(
            """<!-- pta:annotation id=\"stale\" -->
```yaml
range:
  start: 0
  end: 99
```
<!-- /pta:annotation -->
""",
            encoding="utf-8",
        )
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(100, 22)):
            viewer = app.query_one(VimTextArea)
            assert viewer.annotation_ranges == ()
            assert viewer.get_line(0).spans == []

    asyncio.run(scenario())


def test_source_and_annotation_navigation_stay_aligned(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_path = tmp_path / "source.txt"
        source_path.write_text("zero\none\ntwo\nthree", encoding="utf-8")
        store = AnnotationStore(source_path)
        store.append(
            create_record(
                AnnotationDraft(0, 4, "zero"),
                "First note",
                annotation_id="first",
                created_at="2026-10-21T10:00:00Z",
            )
        )
        store.append(
            create_record(
                AnnotationDraft(13, 18, "three"),
                "Second note",
                annotation_id="second",
                created_at="2026-10-21T10:01:00Z",
            )
        )
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(120, 28)) as pilot:
            viewer = app.query_one(VimTextArea)
            annotator = app.query_one(AnnotationTextArea)
            await pilot.pause()

            assert annotator.active_link == app.annotation_links[0]
            assert (
                annotator.cursor_location[0]
                == app.annotation_links[0].sidecar_note_line
            )
            first_link = app.annotation_links[0]
            second_link = app.annotation_links[1]
            assert viewer.active_annotation_range == first_link.source_range
            active_source_style = viewer.get_component_rich_style(
                "text-area--active-annotation"
            )
            active_quote_style = annotator.get_component_rich_style(
                "text-area--annotation-quote"
            )
            normal_source_style = viewer.get_component_rich_style(
                "text-area--annotation"
            )
            assert active_source_style.bgcolor == active_quote_style.bgcolor
            first_source_style = viewer.get_line(0).spans[0].style
            second_source_style = viewer.get_line(3).spans[0].style
            assert first_source_style == active_source_style
            assert second_source_style == normal_source_style
            metadata_line = annotator.get_line(first_link.sidecar_start_line)
            assert [
                (span.start, span.end) for span in metadata_line.spans
            ] == [(0, len(metadata_line.plain))]
            annotation_heading_line = first_link.sidecar_start_line + 1
            assert (
                annotator.get_line(annotation_heading_line).plain
                == "## Annotation 1"
            )
            assert annotator.get_line(annotation_heading_line).spans == []
            assert annotator.get_line(0).spans
            assert first_link.sidecar_quote_start_line is not None
            quote_line = annotator.get_line(
                first_link.sidecar_quote_start_line
            )
            assert [
                (span.start, span.end) for span in quote_line.spans
            ] == [(0, len("> zero"))]
            assert annotator.get_line(first_link.sidecar_note_line).spans == []
            assert first_link.sidecar_note_start_line is not None
            note_line = annotator.get_line(first_link.sidecar_note_start_line)
            assert [
                (span.start, span.end)
                for span in note_line.spans
            ] == [(0, len("First note"))]
            assert metadata_line.spans[0].style != quote_line.spans[0].style
            assert quote_line.spans[0].style != note_line.spans[0].style

            second_heading_line = second_link.sidecar_start_line + 1
            inactive_heading = annotator.get_line(second_heading_line)
            assert inactive_heading.plain == "## Annotation 2"
            assert inactive_heading.spans
            inactive_style = inactive_heading.spans[0].style
            assert inactive_style != quote_line.spans[0].style
            assert inactive_style != note_line.spans[0].style

            await pilot.press("tab")
            assert annotator.has_focus
            await pilot.press("shift+tab")
            assert viewer.has_focus

            await pilot.press("G")
            await pilot.pause()

            assert annotator.active_link == app.annotation_links[1]
            assert (
                annotator.cursor_location[0]
                == app.annotation_links[1].sidecar_note_line
            )
            assert viewer.active_annotation_range == second_link.source_range
            assert viewer.get_line(0).spans[0].style == second_source_style
            assert viewer.get_line(3).spans[0].style == first_source_style
            assert annotator.get_line(second_heading_line).spans == []
            assert annotator.get_line(annotation_heading_line).spans

            annotator.focus()
            await pilot.press("g")
            await pilot.pause()

            assert viewer.cursor_location == (0, 0)
            assert annotator.active_link == app.annotation_links[0]
            assert viewer.active_annotation_range == first_link.source_range

            await pilot.press("G")
            await pilot.pause()

            assert viewer.cursor_location == (3, 0)
            assert annotator.active_link == app.annotation_links[1]
            assert viewer.active_annotation_range == second_link.source_range

    asyncio.run(scenario())


def test_source_alignment_uses_nearest_annotation_in_gaps(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source_path = tmp_path / "source.txt"
        source_text = "first\ngap gap gap gap\n\nsecond"
        source_path.write_text(source_text, encoding="utf-8")
        store = AnnotationStore(source_path)
        store.append(
            create_record(
                AnnotationDraft(0, 5, "first"),
                "First note",
                annotation_id="first",
                created_at="2026-10-21T10:00:00Z",
            )
        )
        second_start = source_text.index("second")
        store.append(
            create_record(
                AnnotationDraft(
                    second_start,
                    second_start + len("second"),
                    "second",
                ),
                "Second note",
                annotation_id="second",
                created_at="2026-10-21T10:01:00Z",
            )
        )
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(100, 22)) as pilot:
            viewer = app.query_one(VimTextArea)
            annotator = app.query_one(AnnotationTextArea)

            viewer.move_cursor((3, 0))
            await pilot.pause()
            assert annotator.active_link == app.annotation_links[1]

            await pilot.press("k")
            await pilot.pause()
            assert viewer.cursor_location == (2, 0)
            assert annotator.active_link == app.annotation_links[1]

            await pilot.press("k")
            await pilot.pause()
            assert viewer.cursor_location == (1, 0)
            assert annotator.active_link == app.annotation_links[0]

    asyncio.run(scenario())


def test_search_source_with_vim_repeat_keys() -> None:
    async def scenario() -> None:
        app = ViewerApp(
            make_source("zero quiet\nmiddle\nquiet end"),
            clipboard_writer=lambda _text: "test",
        )

        async with app.run_test(size=(100, 22)) as pilot:
            viewer = app.query_one(VimTextArea)
            search_input = app.query_one(SearchInput)

            await pilot.press("/", "q", "u", "i", "e", "t", "enter")
            await pilot.pause()

            assert viewer.has_focus
            assert search_input.display is False
            assert viewer.cursor_location == (0, 5)
            assert [
                (span.start, span.end) for span in viewer.get_line(0).spans
            ] == [(5, 10)]

            await pilot.press("n")
            await pilot.pause()
            assert viewer.cursor_location == (2, 0)
            assert viewer.get_line(0).spans == []
            assert [
                (span.start, span.end) for span in viewer.get_line(2).spans
            ] == [(0, 5)]

            await pilot.press("n")
            await pilot.pause()
            assert viewer.cursor_location == (0, 5)

            await pilot.press("shift+n")
            await pilot.pause()
            assert viewer.cursor_location == (2, 0)

    asyncio.run(scenario())


def test_search_annotation_pane_and_cancel_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        source_path = tmp_path / "source.txt"
        source_path.write_text("source", encoding="utf-8")
        sidecar = tmp_path / "source.txt.annotations.md"
        sidecar.write_text(
            "# Notes\nneedle one\nfiller\nneedle two\n",
            encoding="utf-8",
        )
        app = ViewerApp(
            load_text_file(source_path), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(100, 22)) as pilot:
            annotator = app.query_one(AnnotationTextArea)
            search_input = app.query_one(SearchInput)

            await pilot.press("tab", "/")
            assert search_input.has_focus
            await pilot.press("n", "e", "e", "d", "l", "e", "enter")
            await pilot.pause()

            assert annotator.has_focus
            assert annotator.cursor_location == (1, 0)
            assert [
                (span.start, span.end) for span in annotator.get_line(1).spans
            ] == [(0, 6)]

            await pilot.press("n")
            await pilot.pause()
            assert annotator.cursor_location == (3, 0)

            await pilot.press("/", "m", "i", "s", "s", "i", "n", "g")
            assert search_input.has_focus
            await pilot.press("escape")
            await pilot.pause()

            assert annotator.has_focus
            assert search_input.display is False
            assert annotator.cursor_location == (3, 0)

            await pilot.press("n")
            await pilot.pause()
            assert annotator.cursor_location == (1, 0)

    asyncio.run(scenario())


def test_ctrl_w_w_switches_panes_without_breaking_word_motion() -> None:
    async def scenario() -> None:
        app = ViewerApp(
            make_source("alpha beta"), clipboard_writer=lambda _text: "test"
        )

        async with app.run_test(size=(100, 22)) as pilot:
            viewer = app.query_one(VimTextArea)
            annotator = app.query_one(AnnotationTextArea)

            await pilot.press("w")
            assert viewer.cursor_location == (0, 5)

            await pilot.press("0", "ctrl+w")
            assert viewer.has_focus
            assert app._pane_switch_pending is True

            await pilot.press("w")
            assert annotator.has_focus
            assert viewer.cursor_location == (0, 0)
            assert app._pane_switch_pending is False

            await pilot.press("ctrl+w", "w")
            assert viewer.has_focus
            assert app._pane_switch_pending is False

    asyncio.run(scenario())

