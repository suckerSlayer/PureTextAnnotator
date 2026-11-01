"""Resettable rematch demonstration cases shipped with the project."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .annotations import AnnotationFileError, AnnotationStore
from .files import LoadedTextFile, TextFileError, load_text_file, save_text_file


DEMO_FORMAT = "pure-text-annotator-rematch-demo"


class DemoCaseError(RuntimeError):
    """Raised when a marked demonstration cannot be loaded or reset safely."""


@dataclass(frozen=True)
class DemoCase:
    """Immutable baseline for one deliberately stale source/sidecar pair."""

    case_id: str
    name: str
    description: str
    manifest_path: Path
    source_text: str
    annotation_text: str


def demo_manifest_path(source_path: Path) -> Path:
    return source_path.with_name(source_path.name + ".pta-demo.json")


def load_demo_case(source_path: Path) -> Optional[DemoCase]:
    """Load a companion manifest, or return ``None`` for ordinary files."""

    manifest_path = demo_manifest_path(source_path)
    if not manifest_path.exists():
        return None
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DemoCaseError(f"cannot read demo manifest {manifest_path}: {error}") from error
    if not isinstance(value, dict) or value.get("format") != DEMO_FORMAT:
        raise DemoCaseError(f"invalid demo manifest format: {manifest_path}")
    required = ("id", "name", "description", "source_text", "annotation_text")
    if any(not isinstance(value.get(key), str) for key in required):
        raise DemoCaseError(f"demo manifest has missing string fields: {manifest_path}")
    marker = f'<!-- pta:demo-case id={json.dumps(value["id"])} -->'
    if marker not in value["source_text"].splitlines()[:5]:
        raise DemoCaseError(
            f"demo baseline is missing its visible case marker: {manifest_path}"
        )
    return DemoCase(
        case_id=value["id"],
        name=value["name"],
        description=value["description"],
        manifest_path=manifest_path,
        source_text=value["source_text"],
        annotation_text=value["annotation_text"],
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def reset_demo_case(
    source: LoadedTextFile,
    store: AnnotationStore,
    demo: DemoCase,
) -> Tuple[LoadedTextFile, str]:
    """Restore both demo files, rolling back if either atomic write fails."""

    source_path = source.path
    sidecar_path = store.path
    try:
        original_source = source_path.read_bytes()
        sidecar_existed = sidecar_path.exists()
        original_sidecar = sidecar_path.read_bytes() if sidecar_existed else b""
    except OSError as error:
        raise DemoCaseError(f"cannot snapshot demo files before reset: {error}") from error

    try:
        saved_source = save_text_file(source, demo.source_text)
        saved_annotations = store.write(demo.annotation_text)
        reloaded_source = load_text_file(source_path)
        reloaded_annotations = store.read_text()
        if reloaded_source.text != demo.source_text:
            raise DemoCaseError("source verification failed after demo reset")
        if reloaded_annotations != demo.annotation_text:
            raise DemoCaseError("annotation verification failed after demo reset")
        return saved_source, saved_annotations
    except (OSError, TextFileError, AnnotationFileError, DemoCaseError) as error:
        try:
            _atomic_write_bytes(source_path, original_source)
            if sidecar_existed:
                _atomic_write_bytes(sidecar_path, original_sidecar)
            else:
                sidecar_path.unlink(missing_ok=True)
        except OSError as rollback_error:
            raise DemoCaseError(
                f"demo reset failed and rollback also failed: {rollback_error}"
            ) from error
        raise DemoCaseError(
            f"demo reset failed; previous files restored: {error}"
        ) from error
