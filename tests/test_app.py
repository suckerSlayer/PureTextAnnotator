import asyncio
from pathlib import Path

from pure_text_annotator.app import ViewerApp, VimTextArea
from pure_text_annotator.files import LoadedTextFile


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

