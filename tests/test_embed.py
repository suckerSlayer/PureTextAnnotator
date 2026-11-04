import hashlib
import re
from pathlib import Path
from typing import Tuple

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    TextStringObject,
)

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationStore,
    create_record,
)
from pure_text_annotator.embed import PdfEmbedError, embed_pdf_annotations
from pure_text_annotator.embed_cli import main
from pure_text_annotator.files import LoadedTextFile, load_source_file

from pdf_fixtures import write_text_pdf


def _append_note(
    source: LoadedTextFile,
    quote: str,
    *,
    annotation_id: str,
    note: str,
    occurrence: int = 0,
) -> Path:
    assert source.pdf_document is not None
    starts = tuple(match.start() for match in re.finditer(re.escape(quote), source.text))
    start = starts[occurrence]
    end = start + len(quote)
    anchor = source.pdf_document.anchor_for_range(start, end)
    assert anchor is not None
    store = AnnotationStore(source.path)
    store.append(
        create_record(
            AnnotationDraft(start, end, quote),
            note,
            annotation_id=annotation_id,
            created_at="2026-11-05T09:30:00+08:00",
            source_text=source.text,
            pdf_anchor=anchor,
        )
    )
    return store.path


def _annotations(reader: PdfReader, page_index: int) -> Tuple[DictionaryObject, ...]:
    raw = reader.pages[page_index].get("/Annots")
    if raw is None:
        return ()
    return tuple(item.get_object() for item in raw.get_object())


def _write_existing_highlight(
    source_path: Path, *, annotation_name: str, contents: str = "Existing"
) -> None:
    reader = PdfReader(source_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    annotation = Highlight(
        rect=(72, 715, 110, 730),
        quad_points=ArrayObject(
            FloatObject(value)
            for value in (72, 730, 110, 730, 72, 715, 110, 715)
        ),
        highlight_color="00ff00",
        printing=True,
        title_bar="Existing author",
    )
    annotation[NameObject("/NM")] = TextStringObject(annotation_name)
    annotation[NameObject("/Contents")] = TextStringObject(contents)
    writer.add_annotation(0, annotation)
    with source_path.open("wb") as stream:
        writer.write(stream)
    writer.close()


def test_embed_single_page_note_as_standard_highlight(tmp_path: Path) -> None:
    source_path = write_text_pdf(
        tmp_path / "paper.pdf", ("Alpha stable geometry anchor omega",)
    )
    source = load_source_file(source_path)
    sidecar = _append_note(
        source,
        "stable geometry anchor",
        annotation_id="pdf-note-1",
        note="Check this claim.",
    )
    source_before = source_path.read_bytes()
    sidecar_before = sidecar.read_bytes()

    result = embed_pdf_annotations(source_path)

    assert result.output_path == tmp_path / "paper.annotated.pdf"
    assert result.annotation_count == 1
    assert result.highlight_count == 1
    assert source_path.read_bytes() == source_before
    assert sidecar.read_bytes() == sidecar_before

    reader = PdfReader(result.output_path)
    annotations = _annotations(reader, 0)
    assert len(annotations) == 1
    annotation = annotations[0]
    assert annotation["/Subtype"] == "/Highlight"
    assert annotation["/NM"] == "pdf-note-1"
    assert annotation["/Contents"] == "Check this claim."
    assert annotation["/T"] == "Pure Text Annotator"
    assert annotation["/Subj"] == "Pure Text Annotator note"
    assert annotation["/M"] == "D:20261105013000Z"
    assert list(annotation["/C"]) == pytest.approx([1.0, 1.0, 0.0])
    assert int(annotation["/F"]) & 4
    assert len(annotation["/Rect"]) == 4
    assert len(annotation["/QuadPoints"]) == 8


def test_embed_multi_page_note_creates_one_highlight_per_page(
    tmp_path: Path,
) -> None:
    source_path = write_text_pdf(
        tmp_path / "paper.pdf",
        ("Page one begins CROSS PAGE", "CONTINUES on page two"),
    )
    source = load_source_file(source_path)
    start = source.text.index("CROSS PAGE")
    end = source.text.index("CONTINUES") + len("CONTINUES")
    quote = source.text[start:end]
    assert source.pdf_document is not None
    anchor = source.pdf_document.anchor_for_range(start, end)
    assert anchor is not None
    AnnotationStore(source.path).append(
        create_record(
            AnnotationDraft(start, end, quote),
            "Spans two pages.",
            annotation_id="cross-page",
            created_at="2026-11-05T01:30:00Z",
            source_text=source.text,
            pdf_anchor=anchor,
        )
    )

    result = embed_pdf_annotations(source_path)

    assert result.annotation_count == 1
    assert result.highlight_count == 2
    reader = PdfReader(result.output_path)
    for page_index in (0, 1):
        annotation = _annotations(reader, page_index)[0]
        assert annotation["/Subtype"] == "/Highlight"
        assert annotation["/NM"] == "cross-page"
        assert annotation["/Contents"] == "Spans two pages."


def test_existing_noncolliding_annotations_are_preserved(tmp_path: Path) -> None:
    source_path = write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta gamma",))
    _write_existing_highlight(source_path, annotation_name="existing-note")
    source = load_source_file(source_path)
    _append_note(
        source,
        "beta",
        annotation_id="new-note",
        note="New note",
    )

    result = embed_pdf_annotations(source_path)

    annotations = _annotations(PdfReader(result.output_path), 0)
    assert {str(item["/NM"]) for item in annotations} == {
        "existing-note",
        "new-note",
    }
    existing = next(item for item in annotations if item["/NM"] == "existing-note")
    assert existing["/Contents"] == "Existing"


def test_existing_annotation_name_collision_fails_without_output(
    tmp_path: Path,
) -> None:
    source_path = write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta gamma",))
    _write_existing_highlight(source_path, annotation_name="same-id")
    source = load_source_file(source_path)
    _append_note(source, "beta", annotation_id="same-id", note="New note")

    with pytest.raises(PdfEmbedError, match="name collision"):
        embed_pdf_annotations(source_path)

    assert not (tmp_path / "paper.annotated.pdf").exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda text: text.replace("pdf_anchor:", "legacy_pdf_anchor:", 1), "pdf_anchor"),
        (
            lambda text: re.sub(
                r"(?m)^(  document_sha256: ).+$",
                r'\1"' + "0" * 64 + '"',
                text,
                count=1,
            ),
            "byte hash",
        ),
        (
            lambda text: re.sub(
                r"(?m)^(      rect_pdf: \[)([-0-9.]+)",
                lambda match: match.group(1) + str(float(match.group(2)) + 10.0),
                text,
                count=1,
            ),
            "geometry is stale",
        ),
        (
            lambda text: text.replace(
                'id: "strict-id"', 'id: "strict-id"\nid: "duplicate"', 1
            ),
            "duplicate key",
        ),
    ),
)
def test_invalid_or_stale_sidecars_fail_without_partial_output(
    tmp_path: Path, mutation, message: str
) -> None:
    source_path = write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta gamma",))
    source = load_source_file(source_path)
    sidecar = _append_note(
        source,
        "beta",
        annotation_id="strict-id",
        note="Strict note",
    )
    sidecar.write_text(mutation(sidecar.read_text(encoding="utf-8")), encoding="utf-8")

    with pytest.raises(PdfEmbedError, match=message):
        embed_pdf_annotations(source_path)

    assert not (tmp_path / "paper.annotated.pdf").exists()


def test_duplicate_annotation_ids_fail_all_or_nothing(tmp_path: Path) -> None:
    source_path = write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta gamma",))
    source = load_source_file(source_path)
    _append_note(source, "Alpha", annotation_id="duplicate-id", note="First")
    _append_note(source, "gamma", annotation_id="duplicate-id", note="Second")

    with pytest.raises(PdfEmbedError, match="duplicate annotation ID"):
        embed_pdf_annotations(source_path)

    assert not (tmp_path / "paper.annotated.pdf").exists()


def test_output_collision_requires_force_and_source_can_never_be_output(
    tmp_path: Path,
) -> None:
    source_path = write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta gamma",))
    source = load_source_file(source_path)
    _append_note(source, "beta", annotation_id="replace-me", note="A note")
    output = tmp_path / "custom.pdf"
    output.write_bytes(b"existing output")

    with pytest.raises(PdfEmbedError, match="--force"):
        embed_pdf_annotations(source_path, output_path=output)
    assert output.read_bytes() == b"existing output"

    result = embed_pdf_annotations(source_path, output_path=output, force=True)
    assert result.output_path == output
    assert output.read_bytes().startswith(b"%PDF-")

    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    with pytest.raises(PdfEmbedError, match="overwrite the source"):
        embed_pdf_annotations(source_path, output_path=source_path, force=True)
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash


def test_encrypted_and_signed_pdfs_are_rejected(tmp_path: Path) -> None:
    encrypted = write_text_pdf(tmp_path / "encrypted.pdf", ("Secret text",))
    reader = PdfReader(encrypted)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.encrypt("secret")
    with encrypted.open("wb") as stream:
        writer.write(stream)
    writer.close()
    encrypted.with_name(encrypted.name + ".annotations.md").write_text(
        "# Annotations\n", encoding="utf-8"
    )

    with pytest.raises(PdfEmbedError, match="encrypted"):
        embed_pdf_annotations(encrypted)

    signed = write_text_pdf(tmp_path / "signed.pdf", ("Signed text",))
    reader = PdfReader(signed)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    signature = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Sig"),
            NameObject("/Contents"): TextStringObject("test-signature"),
        }
    )
    writer.root_object[NameObject("/Perms")] = DictionaryObject(
        {NameObject("/DocMDP"): writer._add_object(signature)}
    )
    with signed.open("wb") as stream:
        writer.write(stream)
    writer.close()
    signed.with_name(signed.name + ".annotations.md").write_text(
        "# Annotations\n", encoding="utf-8"
    )

    with pytest.raises(PdfEmbedError, match="signed PDFs"):
        embed_pdf_annotations(signed)


def test_cli_reports_success_and_validation_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_path = write_text_pdf(tmp_path / "paper.pdf", ("Alpha beta gamma",))
    source = load_source_file(source_path)
    _append_note(source, "beta", annotation_id="cli-note", note="CLI note")
    output = tmp_path / "cli-output.pdf"

    assert main([str(source_path), "--output", str(output)]) == 0
    captured = capsys.readouterr()
    assert "Embedded 1 annotation(s) as 1 highlight(s)" in captured.out
    assert str(output) in captured.out
    assert captured.err == ""

    assert main([str(source_path), "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pta-embed: output already exists" in captured.err
