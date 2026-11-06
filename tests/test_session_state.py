import json
import os
from pathlib import Path

import pure_text_annotator.cli as viewer_cli
from pure_text_annotator.files import LoadedTextFile
from pure_text_annotator.session_state import (
    load_cursor_position,
    save_cursor_position,
    session_state_path,
)


def _source(path: Path, text: str = "alpha\nbeta\ngamma") -> LoadedTextFile:
    return LoadedTextFile(
        path=path.resolve(),
        text=text,
        size_bytes=len(text.encode("utf-8")),
        line_count=text.count("\n") + 1,
    )


def test_cursor_state_round_trips_privately(tmp_path: Path) -> None:
    source = _source(tmp_path / "chapter.md")
    state_dir = tmp_path / "state"

    assert save_cursor_position(source, (1, 3), state_dir=state_dir)

    path = session_state_path(source, state_dir)
    assert path.is_file()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert load_cursor_position(source, state_dir=state_dir) == (1, 3)


def test_cursor_state_requires_same_path_and_source_fingerprint(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    original = _source(tmp_path / "chapter.md")
    assert save_cursor_position(original, (2, 2), state_dir=state_dir)

    changed = _source(tmp_path / "chapter.md", "inserted\nalpha\nbeta\ngamma")
    moved = _source(tmp_path / "moved.md")

    assert load_cursor_position(changed, state_dir=state_dir) is None
    assert load_cursor_position(moved, state_dir=state_dir) is None


def test_invalid_or_unwritable_cursor_state_is_ignored(tmp_path: Path) -> None:
    source = _source(tmp_path / "chapter.md")
    state_dir = tmp_path / "state"
    state_path = session_state_path(source, state_dir)
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not json", encoding="utf-8")
    assert load_cursor_position(source, state_dir=state_dir) is None

    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "source_path": str(source.path),
                "source_fingerprint": "wrong",
                "row": 99,
                "column": 0,
            }
        ),
        encoding="utf-8",
    )
    assert load_cursor_position(source, state_dir=state_dir) is None

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked", encoding="utf-8")
    assert not save_cursor_position(source, (0, 1), state_dir=blocked)


def test_pta_view_saves_cursor_and_configures_annotation_save_embedding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _source(tmp_path / "paper.pdf")
    saved = []
    embedding_settings = []

    class FakeViewerApp:
        def __init__(
            self,
            loaded_source,
            *,
            soft_wrap,
            initial_source_cursor,
            embed_on_annotation_save,
        ) -> None:
            assert loaded_source is source
            assert soft_wrap is True
            assert initial_source_cursor == (1, 2)
            embedding_settings.append(embed_on_annotation_save)
            self.source = loaded_source
            self.quit_completed = True
            self.exit_source_cursor = (2, 3)

        def run(self) -> None:
            return None

    monkeypatch.setattr(viewer_cli, "ViewerApp", FakeViewerApp)
    monkeypatch.setattr(
        viewer_cli, "load_source_file", lambda *_args, **_kwargs: source
    )
    monkeypatch.setattr(
        viewer_cli, "load_cursor_position", lambda _source: (1, 2)
    )
    monkeypatch.setattr(
        viewer_cli,
        "save_cursor_position",
        lambda saved_source, cursor: saved.append((saved_source, cursor)) or True,
    )
    assert viewer_cli.main([str(source.path)]) == 0

    assert saved == [(source, (2, 3))]
    assert embedding_settings == [True]

    saved.clear()
    embedding_settings.clear()
    assert viewer_cli.main(
        ["--no-embed-on-save", str(source.path)]
    ) == 0
    assert saved == [(source, (2, 3))]
    assert embedding_settings == [False]
