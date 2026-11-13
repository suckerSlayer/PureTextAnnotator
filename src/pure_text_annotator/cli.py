"""Command-line entry point for the text annotator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .annotations import AnnotationStore
from .app import ViewerApp
from .files import TextFileError, load_source_file
from .session_state import load_cursor_position, save_cursor_position


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
    parser.add_argument(
        "--annotations",
        type=Path,
        help=(
            "use an existing annotation sidecar; if it records another source "
            "path, PTA asks before rebinding it"
        ),
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
        "--no-embed-on-save",
        action="store_true",
        help=(
            "do not synchronize annotations into the source PDF after saves"
        ),
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

    annotation_store = AnnotationStore(
        source.path,
        sidecar_path=args.annotations,
    )
    app = ViewerApp(
        source,
        soft_wrap=not args.no_wrap,
        initial_source_cursor=load_cursor_position(source),
        embed_on_annotation_save=not args.no_embed_on_save,
        annotation_store=annotation_store,
    )
    app.run()
    if not app.quit_completed:
        return 0

    if app.exit_source_cursor is not None:
        save_cursor_position(app.source, app.exit_source_cursor)

    return 0

