"""Loading and validating source files for the viewer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Union


SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".markdown"})
PathInput = Union[str, Path]


class TextFileError(ValueError):
    """Raised when a source file cannot be displayed safely."""


@dataclass(frozen=True)
class LoadedTextFile:
    """A decoded source file and the metadata needed by the interface."""

    path: Path
    text: str
    size_bytes: int
    line_count: int


def _normalise_newlines(text: str) -> str:
    """Use one internal newline representation without modifying the file."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def load_text_file(path_input: PathInput) -> LoadedTextFile:
    """Load a supported UTF-8 text file.

    Markdown is intentionally returned as source text. The function never
    writes to the supplied path.
    """

    path = Path(path_input).expanduser()
    if not path.exists():
        raise TextFileError(f"file does not exist: {path}")
    if not path.is_file():
        raise TextFileError(f"not a regular file: {path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise TextFileError(
            f"unsupported file type {path.suffix or '(none)'}; expected {supported}"
        )

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise TextFileError(f"cannot read {path}: {error}") from error

    if b"\x00" in raw:
        raise TextFileError(f"file appears to be binary: {path}")

    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise TextFileError(f"file is not valid UTF-8: {path}") from error

    text = _normalise_newlines(decoded)
    line_count = 0 if not text else text.count("\n") + 1
    return LoadedTextFile(
        path=path.resolve(),
        text=text,
        size_bytes=len(raw),
        line_count=line_count,
    )

