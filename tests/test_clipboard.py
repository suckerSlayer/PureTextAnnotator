from types import SimpleNamespace

from pure_text_annotator import clipboard


def test_native_clipboard_uses_first_available_command(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(clipboard, "_clipboard_commands", lambda: [["missing"], ["copy"]])
    monkeypatch.setattr(
        clipboard.shutil,
        "which",
        lambda name: "/usr/bin/copy" if name == "copy" else None,
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)

    backend = clipboard.copy_to_system_clipboard("selected text")

    assert backend == "copy"
    assert calls[0][0] == ["/usr/bin/copy"]
    assert calls[0][1]["input"] == b"selected text"
    assert calls[0][1]["check"] is True


def test_native_clipboard_is_optional(monkeypatch) -> None:
    monkeypatch.setattr(clipboard.shutil, "which", lambda _name: None)

    assert clipboard.copy_to_system_clipboard("selected text") is None

