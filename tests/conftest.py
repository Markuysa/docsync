import os
import shutil
from pathlib import Path

import pytest

from docsync import config, demo


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated environment: data/ in a temp dir, mock Confluence, local git, no LLM."""
    data = tmp_path / "data"
    monkeypatch.setenv("DOCSYNC_DATA_DIR", str(data))
    monkeypatch.setenv("DOCSYNC_MODEL_REPO", str(data / "repos" / "architecture-model"))
    monkeypatch.setenv("DOCSYNC_SERVICE_REPOS", f"payments/orders-service={data / 'repos' / 'orders-service'}")
    monkeypatch.setenv("CONFLUENCE_MODE", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("KROKI_URL", os.environ.get("KROKI_URL", "http://localhost:8000"))
    config.reset_settings()
    info = demo.seed(data)
    yield {"data": data, **info}
    config.reset_settings()
