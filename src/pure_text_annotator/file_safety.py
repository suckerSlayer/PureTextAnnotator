"""Exact disk fingerprints and recoverable conflict-copy writes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


class DiskFingerprintError(RuntimeError):
    """Raised when a tracked path cannot be fingerprinted safely."""


class ConcurrentFileChangeError(RuntimeError):
    """Raised instead of replacing a file whose disk bytes changed."""


@dataclass(frozen=True)
class DiskFingerprint:
    """An exact fingerprint that also represents a missing file."""

    exists: bool
    size_bytes: int
    sha256: Optional[str]

    @classmethod
    def missing(cls) -> "DiskFingerprint":
        return cls(exists=False, size_bytes=0, sha256=None)


class BufferChangeState(str, Enum):
    """Relationship between one loaded buffer and its tracked disk file."""

    UNCHANGED = "unchanged"
    MEMORY_ONLY = "memory-only"
    DISK_ONLY = "disk-only"
    BOTH = "memory-and-disk"

    @property
    def label(self) -> str:
        return {
            BufferChangeState.UNCHANGED: "unchanged",
            BufferChangeState.MEMORY_ONLY: "memory changed only",
            BufferChangeState.DISK_ONLY: "changed outside PTA only",
            BufferChangeState.BOTH: "changed both in PTA and on disk",
        }[self]


def classify_buffer_change(
    *, memory_changed: bool, disk_changed: bool
) -> BufferChangeState:
    """Classify the four possible in-memory/on-disk change combinations."""

    if memory_changed and disk_changed:
        return BufferChangeState.BOTH
    if memory_changed:
        return BufferChangeState.MEMORY_ONLY
    if disk_changed:
        return BufferChangeState.DISK_ONLY
    return BufferChangeState.UNCHANGED


def fingerprint_path(path: Path) -> DiskFingerprint:
    """Hash the exact bytes at ``path`` without normalizing their content."""

    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return DiskFingerprint.missing()
    except OSError as error:
        raise DiskFingerprintError(
            f"cannot fingerprint {path}: {error}"
        ) from error
    return fingerprint_bytes(data)


def fingerprint_bytes(data: bytes) -> DiskFingerprint:
    """Fingerprint bytes already obtained by a successful file read."""

    return DiskFingerprint(
        exists=True,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def require_unchanged(
    path: Path,
    expected: DiskFingerprint,
    *,
    label: str,
) -> None:
    """Refuse a write when exact disk bytes differ from the loaded baseline."""

    current = fingerprint_path(path)
    if current != expected:
        raise ConcurrentFileChangeError(
            f"{label} changed on disk after it was loaded: {path}"
        )


def _conflict_copy_candidate(path: Path, timestamp: str) -> Path:
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    name = f"{stem}.pta-conflict-{timestamp}{suffix}"
    return path.with_name(name)


def write_conflict_copy(path: Path, text: str) -> Path:
    """Atomically preserve a UTF-8 buffer beside its original path.

    The destination is always new; neither the tracked file nor an older
    conflict copy is replaced.
    """

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = _conflict_copy_candidate(path, timestamp)
    counter = 2
    while candidate.exists():
        base = _conflict_copy_candidate(path, timestamp)
        candidate = base.with_name(
            f"{base.stem}-{counter}{base.suffix}"
        )
        counter += 1

    candidate.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=candidate.parent,
            prefix=f".{candidate.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        # The candidate was checked above and is deliberately unique. Use a
        # hard-link publication so an unexpected race still cannot overwrite
        # an existing conflict copy.
        try:
            os.link(temporary_path, candidate)
        except FileExistsError:
            temporary_path.unlink(missing_ok=True)
            temporary_path = None
            return write_conflict_copy(path, text)
        temporary_path.unlink()
        temporary_path = None
    except OSError as error:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise DiskFingerprintError(
            f"cannot save conflict copy beside {path}: {error}"
        ) from error
    return candidate
