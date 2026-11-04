"""Small dependency-free PDF fixtures for extraction tests."""

from pathlib import Path
from typing import Iterable


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_text_pdf(path: Path, pages: Iterable[str]) -> Path:
    """Write a minimal Helvetica PDF with one content stream per page."""

    page_texts = tuple(pages)
    catalog_id = 1
    pages_id = 2
    font_id = 3
    page_ids = tuple(4 + index * 2 for index in range(len(page_texts)))
    content_ids = tuple(page_id + 1 for page_id in page_ids)
    objects = {
        catalog_id: b"<< /Type /Catalog /Pages 2 0 R >>",
        pages_id: (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{page_id} 0 R" for page_id in page_ids)
            + f"] /Count {len(page_ids)} >>"
        ).encode("ascii"),
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for page_id, content_id, page_text in zip(
        page_ids, content_ids, page_texts
    ):
        objects[page_id] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        commands = ["BT", "/F1 12 Tf", "72 720 Td"]
        for line_index, line in enumerate(page_text.split("\n")):
            if line_index:
                commands.append("0 -18 Td")
            if line:
                commands.append(f"({_escape_pdf_text(line)}) Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max(objects) + 1)
    for object_id in sorted(objects):
        offsets[object_id] = len(output)
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for object_id in range(1, len(offsets)):
        output.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(output)
    return path
