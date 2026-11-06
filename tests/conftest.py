from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_pdf_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep automatic PDF caches inside each test's temporary directory."""

    monkeypatch.setenv("PTA_PDF_CACHE_DIR", str(tmp_path / ".pta-pdf-cache"))
