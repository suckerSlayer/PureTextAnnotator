"""Command-line interface for importing standard PDF text markup."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .pdf_import import (
    PdfImportError,
    import_pdf_text_markups,
    skip_reason_counts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pta-import",
        description=(
            "Reconcile standard PDF highlights, underlines, squiggles, and "
            "strikeouts into a Pure Text Annotator sidecar."
        ),
    )
    parser.add_argument("source", type=Path, help="annotated text-layer PDF")
    parser.add_argument(
        "--annotations",
        type=Path,
        help="sidecar path (default: SOURCE.pdf.annotations.md)",
    )
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--no-pdf-cache",
        action="store_true",
        help="extract PDF text without reading or writing its local cache",
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = import_pdf_text_markups(
            arguments.source,
            annotations_path=arguments.annotations,
            use_pdf_cache=not arguments.no_pdf_cache,
            rebuild_pdf_cache=arguments.rebuild_pdf_cache,
        )
    except PdfImportError as error:
        print(f"pta-import: {error}", file=sys.stderr)
        return 2

    if result.imported_count:
        print(
            f"Imported {result.imported_count} PDF text-markup annotation(s): "
            f"{result.annotations_path}"
        )
        for item in result.imported:
            generated = " · no PDF comment" if item.generated_note else ""
            print(
                f"  page {item.page_index + 1} · {item.subtype} · "
                f"range [{item.start}, {item.end}) · "
                f"{item.mapping_confidence}% confidence{generated}"
            )
    if result.updated_count:
        print(
            f"Updated {result.updated_count} sidecar annotation(s) from PDF edits."
        )

    if result.removed_count:
        print(
            f"Removed {result.removed_count} sidecar annotation(s) deleted from "
            "the PDF."
        )

    if result.refreshed_anchor_count:
        print(
            f"Refreshed {result.refreshed_anchor_count} existing PDF "
            "anchor(s) for the current PDF bytes."
        )

    if result.document_revision_updated:
        print("Recorded the reconciled source-PDF revision in the sidecar.")

    if not (
        result.imported_count
        or result.updated_count
        or result.removed_count
        or result.refreshed_anchor_count
        or result.document_revision_updated
    ):
        print(f"PDF and sidecar already match: {result.annotations_path}")

    if result.skipped:
        print(f"Skipped {len(result.skipped)} PDF annotation(s):")
        for reason, count in skip_reason_counts(result.skipped):
            print(f"  {count} · {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
