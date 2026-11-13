from pathlib import Path

from pure_text_annotator.file_safety import (
    BufferChangeState,
    classify_buffer_change,
    fingerprint_path,
    write_conflict_copy,
)


def test_classifies_all_memory_and_disk_change_states() -> None:
    assert classify_buffer_change(
        memory_changed=False, disk_changed=False
    ) is BufferChangeState.UNCHANGED
    assert classify_buffer_change(
        memory_changed=True, disk_changed=False
    ) is BufferChangeState.MEMORY_ONLY
    assert classify_buffer_change(
        memory_changed=False, disk_changed=True
    ) is BufferChangeState.DISK_ONLY
    assert classify_buffer_change(
        memory_changed=True, disk_changed=True
    ) is BufferChangeState.BOTH


def test_fingerprint_represents_missing_and_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / "tracked.md"
    missing = fingerprint_path(path)
    assert missing.exists is False

    path.write_bytes(b"alpha\r\n")
    first = fingerprint_path(path)
    path.write_bytes(b"alpha\n")
    second = fingerprint_path(path)

    assert first.exists is True
    assert first.size_bytes == 7
    assert first != second


def test_conflict_copy_is_new_and_never_replaces_original(tmp_path: Path) -> None:
    original = tmp_path / "chapter.md"
    original.write_text("external", encoding="utf-8")

    first = write_conflict_copy(original, "in-memory one")
    second = write_conflict_copy(original, "in-memory two")

    assert original.read_text(encoding="utf-8") == "external"
    assert first != second
    assert first.read_text(encoding="utf-8") == "in-memory one"
    assert second.read_text(encoding="utf-8") == "in-memory two"
