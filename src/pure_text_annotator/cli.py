"""Command-line entry point for the text annotator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .app import ViewerApp
from .files import TextFileError, load_source_file


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

    ViewerApp(source, soft_wrap=not args.no_wrap).run()
    return 0

