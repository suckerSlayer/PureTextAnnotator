from pathlib import Path

import pytest

from pure_text_annotator.files import TextFileError, load_text_file


def test_loads_utf8_bom_and_normalises_newlines(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\rgamma")

    loaded = load_text_file(source)

    assert loaded.path == source.resolve()
    assert loaded.text == "alpha\nbeta\ngamma"
    assert loaded.line_count == 3
    assert loaded.size_bytes == len(source.read_bytes())


def test_loads_chinese_utf8_text(tmp_path: Path) -> None:
    source = tmp_path / "中文笔记.txt"
    source.write_text("第一行\n第二行", encoding="utf-8")

    loaded = load_text_file(source)

    assert loaded.text == "第一行\n第二行"
    assert loaded.line_count == 2


def test_rejects_unsupported_extension(tmp_path: Path) -> None:
    source = tmp_path / "notes.rtf"
    source.write_text("not supported", encoding="utf-8")

    with pytest.raises(TextFileError, match="unsupported file type"):
        load_text_file(source)


def test_rejects_non_utf8_input(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"\xff\xfeinvalid")

    with pytest.raises(TextFileError, match="not valid UTF-8"):
        load_text_file(source)


def test_rejects_binary_input(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"alpha\x00beta")

    with pytest.raises(TextFileError, match="appears to be binary"):
        load_text_file(source)

