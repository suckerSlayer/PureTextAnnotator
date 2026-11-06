"""Loading and validating source files for the viewer."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union


if TYPE_CHECKING:
    from .pdfs import PdfDocument


SUPPORTED_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown"})
SUPPORTED_SOURCE_SUFFIXES = SUPPORTED_TEXT_SUFFIXES | {".pdf"}
# Retain the original public name for callers that mean text files specifically.
SUPPORTED_SUFFIXES = SUPPORTED_TEXT_SUFFIXES
PathInput = Union[str, Path]


class TextFileError(ValueError):
    """Raised when a source file cannot be displayed safely."""


@dataclass(frozen=True)
class LoadedTextFile:
    """A source represented as canonical text for the interface."""

    path: Path
    text: str
    size_bytes: int
    line_count: int
    source_kind: str = "text"
    editable: bool = True
    pdf_document: Optional["PdfDocument"] = None


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
    if path.suffix.lower() not in SUPPORTED_TEXT_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_TEXT_SUFFIXES))
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


def load_source_file(
    path_input: PathInput,
    *,
    use_pdf_cache: bool = True,
    rebuild_pdf_cache: bool = False,
    pdf_cache_dir: Optional[PathInput] = None,
) -> LoadedTextFile:
    """Load a supported text/Markdown file or a text-layer PDF."""

    path = Path(path_input).expanduser()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        # Keep the optional extraction dependency out of the plain-text path.
        from .pdfs import load_pdf_file

        return load_pdf_file(
            path,
            use_cache=use_pdf_cache,
            rebuild_cache=rebuild_pdf_cache,
            cache_dir=pdf_cache_dir,
        )
    if suffix in SUPPORTED_TEXT_SUFFIXES:
        return load_text_file(path)
    supported = ", ".join(sorted(SUPPORTED_SOURCE_SUFFIXES))
    raise TextFileError(
        f"unsupported file type {path.suffix or '(none)'}; expected {supported}"
    )


def save_text_file(source: LoadedTextFile, text: str) -> LoadedTextFile:
    """Atomically save edited text while preserving basic file formatting."""

    if not source.editable or source.source_kind != "text":
        raise TextFileError(
            "PDF source text is an extracted read-only view and cannot be saved"
        )

    path = source.path
    temporary_path = None
    try:
        original = path.read_bytes()
        mode = path.stat().st_mode & 0o7777
        has_bom = original.startswith(b"\xef\xbb\xbf")
        content = original[3:] if has_bom else original
        if b"\r\n" in content:
            newline = "\r\n"
        elif b"\r" in content:
            newline = "\r"
        else:
            newline = "\n"

        encoded = text.replace("\n", newline).encode("utf-8")
        if has_bom:
            encoded = b"\xef\xbb\xbf" + encoded

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), mode)
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise TextFileError(f"cannot save {path}: {error}") from error

    line_count = 0 if not text else text.count("\n") + 1
    return LoadedTextFile(
        path=path,
        text=text,
        size_bytes=len(encoded),
        line_count=line_count,
    )

