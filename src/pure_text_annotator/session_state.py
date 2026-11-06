"""Private, fingerprinted state used to resume a source cursor safely."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Union

from platformdirs import user_state_path

from .files import LoadedTextFile


CursorLocation = Tuple[int, int]
PathInput = Union[str, Path]
SESSION_STATE_VERSION = 1
SESSION_STATE_DIRECTORY_ENV = "PTA_SESSION_STATE_DIR"
_MAX_STATE_BYTES = 64 * 1024


def session_state_directory(state_dir: Optional[PathInput] = None) -> Path:
    """Return the private directory for resumable viewer state."""

    if state_dir is not None:
        return Path(state_dir).expanduser()
    configured = os.environ.get(SESSION_STATE_DIRECTORY_ENV)
    if configured:
        return Path(configured).expanduser()
    return user_state_path(
        "pure-text-annotator", appauthor=False
    ) / "viewer-sessions"


def _source_path(source: LoadedTextFile) -> str:
    return str(source.path.resolve())


def _source_fingerprint(source: LoadedTextFile) -> str:
    if source.pdf_document is not None:
        document = source.pdf_document
        identity = (
            f"pdf\0{document.document_sha256}\0{document.extractor_name}"
            f"\0{document.extractor_version}\0{document.extraction_profile}"
        )
    else:
        canonical_sha256 = hashlib.sha256(
            source.text.encode("utf-8")
        ).hexdigest()
        identity = f"text\0{canonical_sha256}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def session_state_path(
    source: LoadedTextFile, state_dir: Optional[PathInput] = None
) -> Path:
    """Return a non-identifying filename for one resolved source path."""

    path_digest = hashlib.sha256(_source_path(source).encode("utf-8")).hexdigest()
    return session_state_directory(state_dir) / f"{path_digest}.json"


def _valid_cursor(source: LoadedTextFile, cursor: CursorLocation) -> bool:
    row, column = cursor
    if row < 0 or column < 0:
        return False
    lines = source.text.split("\n")
    return row < len(lines) and column <= len(lines[row])


def load_cursor_position(
    source: LoadedTextFile, *, state_dir: Optional[PathInput] = None
) -> Optional[CursorLocation]:
    """Load a cursor only when path and exact canonical source still match."""

    path = session_state_path(source, state_dir)
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_STATE_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    row = data.get("row")
    column = data.get("column")
    if (
        isinstance(version, bool)
        or version != SESSION_STATE_VERSION
        or data.get("source_path") != _source_path(source)
        or data.get("source_fingerprint") != _source_fingerprint(source)
        or isinstance(row, bool)
        or not isinstance(row, int)
        or isinstance(column, bool)
        or not isinstance(column, int)
    ):
        return None
    cursor = (row, column)
    return cursor if _valid_cursor(source, cursor) else None


def save_cursor_position(
    source: LoadedTextFile,
    cursor: CursorLocation,
    *,
    state_dir: Optional[PathInput] = None,
) -> bool:
    """Atomically save one valid cursor; failure never blocks quitting."""

    if not _valid_cursor(source, cursor):
        return False
    data = {
        "version": SESSION_STATE_VERSION,
        "source_path": _source_path(source),
        "source_fingerprint": _source_fingerprint(source),
        "row": cursor[0],
        "column": cursor[1],
    }
    encoded = (
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    path = session_state_path(source, state_dir)
    temporary_path: Optional[Path] = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        return True
    except OSError:
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
