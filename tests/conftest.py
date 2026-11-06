from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_pdf_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep automatic caches and session state inside temporary directories."""

    monkeypatch.setenv("PTA_PDF_CACHE_DIR", str(tmp_path / ".pta-pdf-cache"))
    monkeypatch.setenv(
        "PTA_SESSION_STATE_DIR", str(tmp_path / ".pta-session-state")
    )
    monkeypatch.setenv(
        "PTA_EMBED_STATE_DIR", str(tmp_path / ".pta-embed-state")
    )
