"""Command-line entry point for the text annotator."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import __version__
from .annotations import AnnotationFileError, AnnotationStore
from .app import ViewerApp
from .files import LoadedTextFile, TextFileError, load_source_file
from .rematch import parse_annotation_records
from .session_state import load_cursor_position, save_cursor_position


@dataclass(frozen=True)
class EmbeddedSummary:
    """Outcome of the optional PDF refresh after a confirmed quit."""

    annotation_count: int
    highlight_count: int
    output_path: Path
    refreshed: bool


class QuitActionError(RuntimeError):
    """Raised when a requested post-quit action cannot be completed."""


def _embed_pdf_copy_on_quit(
    source: LoadedTextFile, annotations_path: Path
) -> Optional[EmbeddedSummary]:
    """Refresh the default annotated PDF copy when saved notes exist."""

    if source.source_kind != "pdf" or not annotations_path.exists():
        return None
    try:
        sidecar_text = AnnotationStore(
            source.path, annotations_path
        ).read_text()
    except AnnotationFileError as error:
        raise QuitActionError(str(error)) from error
    if not parse_annotation_records(sidecar_text):
        return None

    from .embed import (
        PdfEmbedError,
        default_output_path,
        embed_pdf_annotations,
    )
    from .embed_state import load_current_embed_receipt

    output_path = default_output_path(source.path).resolve()
    receipt = load_current_embed_receipt(
        source.path, annotations_path, output_path
    )
    if receipt is not None:
        return EmbeddedSummary(
            receipt.annotation_count,
            receipt.highlight_count,
            output_path,
            False,
        )

    try:
        result = embed_pdf_annotations(
            source.path,
            annotations_path=annotations_path,
            force=True,
            loaded_source=source,
        )
    except PdfEmbedError as error:
        raise QuitActionError(str(error)) from error
    return EmbeddedSummary(
        result.annotation_count,
        result.highlight_count,
        result.output_path,
        True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pta-view",
        description=(
            "View and annotate UTF-8 text, Markdown, or a text-layer PDF "
            "in a Vim-style TUI."
        ),
    )
    parser.add_argument(
        "file",
        type=Path,
        help="path to a .txt, .md, .markdown, or text-layer .pdf file",
    )
    parser.add_argument(
        "--no-wrap",
        action="store_true",
        help="do not wrap long lines",
    )
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--no-pdf-cache",
        action="store_true",
        help="extract a PDF without reading or writing its local cache",
    )
    cache_group.add_argument(
        "--rebuild-pdf-cache",
        action="store_true",
        help="ignore and replace the cache for this exact PDF",
    )
    parser.add_argument(
        "--no-embed-on-quit",
        action="store_true",
        help="do not refresh the default annotated PDF copy when quitting",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = load_source_file(
            args.file,
            use_pdf_cache=not args.no_pdf_cache,
            rebuild_pdf_cache=args.rebuild_pdf_cache,
        )
    except TextFileError as error:
        print(f"pta-view: {error}", file=sys.stderr)
        return 2

    app = ViewerApp(
        source,
        soft_wrap=not args.no_wrap,
        initial_source_cursor=load_cursor_position(source),
    )
    app.run()
    if not app.quit_completed:
        return 0

    if app.exit_source_cursor is not None:
        save_cursor_position(app.source, app.exit_source_cursor)

    if not args.no_embed_on_quit:
        try:
            embedded = _embed_pdf_copy_on_quit(
                app.source, app.annotation_store.path
            )
        except QuitActionError as error:
            print(
                f"pta-view: could not refresh annotated PDF: {error}",
                file=sys.stderr,
            )
            return 2
        if embedded is not None:
            if embedded.refreshed:
                print(
                    f"Embedded {embedded.annotation_count} annotation(s) as "
                    f"{embedded.highlight_count} highlight(s): "
                    f"{embedded.output_path}"
                )
            else:
                print(f"Annotated PDF already current: {embedded.output_path}")
    return 0

