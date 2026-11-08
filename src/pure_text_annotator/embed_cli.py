"""Command-line interface for synchronizing sidecar notes into a PDF."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .embed import PdfEmbedError, embed_pdf_annotations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pta-embed",
        description=(
            "Synchronize Pure Text Annotator sidecar notes as standard "
            "text-markup annotations in the source PDF atomically."
        ),
    )
    parser.add_argument("source", type=Path, help="text-layer source PDF")
    parser.add_argument(
        "--annotations",
        type=Path,
        help="sidecar path (default: SOURCE.pdf.annotations.md)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional separate export path (default: update SOURCE in place)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing explicit --output file",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = embed_pdf_annotations(
            arguments.source,
            annotations_path=arguments.annotations,
            output_path=arguments.output,
            force=arguments.force,
        )
    except PdfEmbedError as error:
        print(f"pta-embed: {error}", file=sys.stderr)
        return 2

    print(
        f"Synchronized {result.annotation_count} sidecar annotation(s) as "
        f"{result.highlight_count} PDF text-markup annotation(s): "
        f"{result.output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
