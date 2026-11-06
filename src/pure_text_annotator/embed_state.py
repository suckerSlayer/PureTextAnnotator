"""Private receipts used to skip unchanged PDF embedding safely."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from platformdirs import user_state_path


PathInput = Union[str, Path]
EMBED_STATE_VERSION = 1
EMBED_STATE_DIRECTORY_ENV = "PTA_EMBED_STATE_DIR"
_MAX_STATE_BYTES = 64 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PdfEmbedReceipt:
    """Counts from a previously verified embed whose files still match."""

    annotation_count: int
    highlight_count: int


def embed_state_directory(state_dir: Optional[PathInput] = None) -> Path:
    """Return the private directory containing successful embed receipts."""

    if state_dir is not None:
        return Path(state_dir).expanduser()
    configured = os.environ.get(EMBED_STATE_DIRECTORY_ENV)
    if configured:
        return Path(configured).expanduser()
    return user_state_path(
        "pure-text-annotator", appauthor=False
    ) / "pdf-embeds"


def _resolved(path: PathInput) -> Path:
    return Path(path).expanduser().resolve()


def embed_state_path(
    source_path: PathInput,
    annotations_path: PathInput,
    output_path: PathInput,
    *,
    state_dir: Optional[PathInput] = None,
) -> Path:
    """Return a non-identifying receipt path for one embed destination."""

    identity = "\0".join(
        str(_resolved(path))
        for path in (source_path, annotations_path, output_path)
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return embed_state_directory(state_dir) / f"{digest}.json"


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def file_sha256(path_input: PathInput) -> Optional[str]:
    """Hash one stable regular file, returning None on races or I/O failure."""

    path = Path(path_input)
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.stat()
    except OSError:
        return None
    if not (
        _stat_identity(before)
        == _stat_identity(after)
        == _stat_identity(current)
    ):
        return None
    return digest.hexdigest()


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def load_current_embed_receipt(
    source_path: PathInput,
    annotations_path: PathInput,
    output_path: PathInput,
    *,
    state_dir: Optional[PathInput] = None,
) -> Optional[PdfEmbedReceipt]:
    """Return a receipt only when all three current files match exactly."""

    source = _resolved(source_path)
    annotations = _resolved(annotations_path)
    output = _resolved(output_path)
    path = embed_state_path(
        source, annotations, output, state_dir=state_dir
    )
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    annotation_count = data.get("annotation_count")
    highlight_count = data.get("highlight_count")
    if (
        data.get("version") != EMBED_STATE_VERSION
        or data.get("source_path") != str(source)
        or data.get("annotations_path") != str(annotations)
        or data.get("output_path") != str(output)
        or isinstance(annotation_count, bool)
        or not isinstance(annotation_count, int)
        or annotation_count < 0
        or isinstance(highlight_count, bool)
        or not isinstance(highlight_count, int)
        or highlight_count < 0
    ):
        return None

    expected_hashes = (
        ("source_sha256", source),
        ("annotations_sha256", annotations),
        ("output_sha256", output),
    )
    for field, current_path in expected_hashes:
        expected = data.get(field)
        if not _valid_digest(expected):
            return None
        if file_sha256(current_path) != expected:
            return None
    return PdfEmbedReceipt(annotation_count, highlight_count)


def save_embed_receipt(
    source_path: PathInput,
    annotations_path: PathInput,
    output_path: PathInput,
    *,
    source_sha256: str,
    annotations_sha256: str,
    output_sha256: str,
    annotation_count: int,
    highlight_count: int,
    state_dir: Optional[PathInput] = None,
) -> bool:
    """Atomically save a successful embed receipt; failure is non-fatal."""

    if (
        not all(
            _valid_digest(value)
            for value in (
                source_sha256,
                annotations_sha256,
                output_sha256,
            )
        )
        or isinstance(annotation_count, bool)
        or not isinstance(annotation_count, int)
        or annotation_count < 0
        or isinstance(highlight_count, bool)
        or not isinstance(highlight_count, int)
        or highlight_count < 0
    ):
        return False
    source = _resolved(source_path)
    annotations = _resolved(annotations_path)
    output = _resolved(output_path)
    data = {
        "version": EMBED_STATE_VERSION,
        "source_path": str(source),
        "annotations_path": str(annotations),
        "output_path": str(output),
        "source_sha256": source_sha256,
        "annotations_sha256": annotations_sha256,
        "output_sha256": output_sha256,
        "annotation_count": annotation_count,
        "highlight_count": highlight_count,
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
    path = embed_state_path(
        source, annotations, output, state_dir=state_dir
    )
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
