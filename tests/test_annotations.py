from pathlib import Path

import pytest

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationLink,
    AnnotationRange,
    AnnotationStore,
    create_record,
    draft_from_selection,
    location_to_offset,
    parse_annotation_links,
    parse_annotation_ranges,
)


def test_location_to_offset_uses_unicode_code_points() -> None:
    text = "甲乙\nabc"

    assert location_to_offset(text, (0, 1)) == 1
    assert location_to_offset(text, (1, 2)) == 5


def test_draft_normalises_reversed_multiline_selection() -> None:
    draft = draft_from_selection("甲乙\nabc", (1, 2), (0, 1))

    assert draft.start == 1
    assert draft.end == 5
    assert draft.quote == "乙\nab"


def test_draft_rejects_empty_selection() -> None:
    with pytest.raises(ValueError, match="empty selection"):
        draft_from_selection("alpha", (0, 2), (0, 2))


def test_store_appends_human_readable_markdown_atomically(tmp_path: Path) -> None:
    source = tmp_path / "chapter.md"
    source.write_text("alpha beta", encoding="utf-8")
    store = AnnotationStore(source)
    record = create_record(
        AnnotationDraft(start=0, end=5, quote="alpha"),
        "Important opening word.",
        annotation_id="annotation-1",
        created_at="2026-10-21T10:00:00Z",
    )

    saved = store.append(record)

    assert store.path == tmp_path / "chapter.md.annotations.md"
    assert store.path.read_text(encoding="utf-8") == saved
    assert 'source: "chapter.md"' in saved
    assert "range_unit: unicode-code-point" in saved
    assert 'id: "annotation-1"' in saved
    assert "  start: 0" in saved
    assert "  end: 5" in saved
    assert "> alpha" in saved
    assert "Important opening word." in saved


def test_store_preserves_existing_text_when_appending(tmp_path: Path) -> None:
    source = tmp_path / "chapter.txt"
    source.write_text("one two", encoding="utf-8")
    store = AnnotationStore(source)
    store.path.write_text("User heading\n\nUser explanation.\n", encoding="utf-8")
    record = create_record(
        AnnotationDraft(start=4, end=7, quote="two"),
        "Second note",
        annotation_id="annotation-2",
        created_at="2026-10-21T10:01:00Z",
    )

    saved = store.append(record)

    assert saved.startswith("User heading\n\nUser explanation.\n")
    assert "## Annotation 1" in saved
    assert "> two" in saved


def test_parse_annotation_ranges_reads_persisted_metadata(tmp_path: Path) -> None:
    source = tmp_path / "chapter.txt"
    source.write_text("one two three", encoding="utf-8")
    store = AnnotationStore(source)
    first = create_record(
        AnnotationDraft(start=0, end=3, quote="one"),
        "First note",
        annotation_id="annotation-1",
        created_at="2026-10-21T10:00:00Z",
    )
    second = create_record(
        AnnotationDraft(start=8, end=13, quote="three"),
        "A note with a fake range:\n```yaml\nrange:\n  start: 99\n  end: 100\n```",
        annotation_id="annotation-2",
        created_at="2026-10-21T10:01:00Z",
    )

    saved = store.append(first)
    saved = store.append(second)

    assert parse_annotation_ranges(saved) == (
        AnnotationRange(0, 3),
        AnnotationRange(8, 13),
    )
    lines = saved.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if line.startswith("<!-- pta:annotation ")
    ]
    ends = [
        index
        for index, line in enumerate(lines)
        if line == "<!-- /pta:annotation -->"
    ]
    note_lines = [
        index for index, line in enumerate(lines) if line == "### Note"
    ]

    def entry_line(start_line: int, end_line: int, value: str) -> int:
        return next(
            index
            for index in range(start_line, end_line + 1)
            if lines[index] == value
        )

    metadata_ends = []
    original_text_lines = []
    for start_line, end_line in zip(starts, ends):
        metadata_start = entry_line(start_line, end_line, "```yaml")
        metadata_ends.append(
            entry_line(metadata_start + 1, end_line, "```")
        )
        original_text_lines.append(
            entry_line(start_line, end_line, "### Original text")
        )

    def content_bounds(start_line: int, end_line: int) -> tuple[int, int]:
        start = start_line + 1
        while start < end_line and not lines[start].strip():
            start += 1
        end = end_line - 1
        while end >= start and not lines[end].strip():
            end -= 1
        return start, end

    first_quote_start, first_quote_end = content_bounds(
        original_text_lines[0], note_lines[0]
    )
    second_quote_start, second_quote_end = content_bounds(
        original_text_lines[1], note_lines[1]
    )
    first_note_start, first_note_end = content_bounds(
        note_lines[0], ends[0]
    )
    second_note_start, second_note_end = content_bounds(
        note_lines[1], ends[1]
    )
    assert parse_annotation_links(saved) == (
        AnnotationLink(
            source_range=AnnotationRange(0, 3),
            sidecar_start_line=starts[0],
            sidecar_end_line=ends[0],
            sidecar_metadata_end_line=metadata_ends[0],
            sidecar_quote_start_line=first_quote_start,
            sidecar_quote_end_line=first_quote_end,
            sidecar_note_line=note_lines[0],
            sidecar_note_start_line=first_note_start,
            sidecar_note_end_line=first_note_end,
        ),
        AnnotationLink(
            source_range=AnnotationRange(8, 13),
            sidecar_start_line=starts[1],
            sidecar_end_line=ends[1],
            sidecar_metadata_end_line=metadata_ends[1],
            sidecar_quote_start_line=second_quote_start,
            sidecar_quote_end_line=second_quote_end,
            sidecar_note_line=note_lines[1],
            sidecar_note_start_line=second_note_start,
            sidecar_note_end_line=second_note_end,
        ),
    )


def test_parse_annotation_ranges_ignores_invalid_or_incomplete_ranges() -> None:
    annotation_text = """<!-- pta:annotation id=\"bad\" -->
```yaml
range:
  start: 5
  end: 2
```
<!-- /pta:annotation -->
<!-- pta:annotation id=\"incomplete\" -->
```yaml
range:
  start: 1
```
<!-- /pta:annotation -->
"""

    assert parse_annotation_ranges(annotation_text) == ()
