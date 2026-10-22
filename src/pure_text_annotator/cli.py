"""Command-line entry point for the text annotator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .app import ViewerApp
from .files import TextFileError, load_text_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pta-view",
        description="View and annotate a UTF-8 text or Markdown file in a Vim-style TUI.",
    )
    parser.add_argument("file", type=Path, help="path to a .txt, .md, or .markdown file")
    parser.add_argument(
        "--no-wrap",
        action="store_true",
        help="do not wrap long lines",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = load_text_file(args.file)
    except TextFileError as error:
        print(f"pta-view: {error}", file=sys.stderr)
        return 2

    ViewerApp(source, soft_wrap=not args.no_wrap).run()
    return 0

