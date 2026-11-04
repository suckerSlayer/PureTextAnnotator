"""Command-line interface for embedding sidecar notes into a PDF copy."""

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
            "Embed Pure Text Annotator sidecar notes as standard highlights "
            "in a separate PDF copy."
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
        help="output path (default: SOURCE.annotated.pdf)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file; never replaces the source PDF",
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
        f"Embedded {result.annotation_count} annotation(s) as "
        f"{result.highlight_count} highlight(s): {result.output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
