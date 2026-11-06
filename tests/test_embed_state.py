import hashlib
import os
from pathlib import Path

from pure_text_annotator.embed_state import (
    PdfEmbedReceipt,
    embed_state_path,
    file_sha256,
    load_current_embed_receipt,
    save_embed_receipt,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_embed_receipt_requires_all_three_exact_files(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    annotations = tmp_path / "paper.pdf.annotations.md"
    output = tmp_path / "paper.annotated.pdf"
    source_bytes = b"source PDF"
    annotations_bytes = b"annotation sidecar"
    output_bytes = b"annotated PDF"
    source.write_bytes(source_bytes)
    annotations.write_bytes(annotations_bytes)
    output.write_bytes(output_bytes)

    assert save_embed_receipt(
        source,
        annotations,
        output,
        source_sha256=_digest(source_bytes),
        annotations_sha256=_digest(annotations_bytes),
        output_sha256=_digest(output_bytes),
        annotation_count=2,
        highlight_count=3,
    )
    assert load_current_embed_receipt(
        source, annotations, output
    ) == PdfEmbedReceipt(2, 3)

    annotations.write_bytes(annotations_bytes + b" changed")
    assert load_current_embed_receipt(source, annotations, output) is None
    annotations.write_bytes(annotations_bytes)
    assert load_current_embed_receipt(
        source, annotations, output
    ) == PdfEmbedReceipt(2, 3)

    output.write_bytes(output_bytes + b" changed")
    assert load_current_embed_receipt(source, annotations, output) is None


def test_embed_receipt_is_private_and_corruption_is_ignored(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    annotations = tmp_path / "paper.pdf.annotations.md"
    output = tmp_path / "paper.annotated.pdf"
    for path, value in (
        (source, b"source"),
        (annotations, b"notes"),
        (output, b"output"),
    ):
        path.write_bytes(value)
    assert save_embed_receipt(
        source,
        annotations,
        output,
        source_sha256=file_sha256(source) or "",
        annotations_sha256=file_sha256(annotations) or "",
        output_sha256=file_sha256(output) or "",
        annotation_count=1,
        highlight_count=1,
    )
    receipt_path = embed_state_path(source, annotations, output)
    if os.name != "nt":
        assert receipt_path.stat().st_mode & 0o777 == 0o600

    receipt_path.write_text("not json", encoding="utf-8")
    assert load_current_embed_receipt(source, annotations, output) is None
