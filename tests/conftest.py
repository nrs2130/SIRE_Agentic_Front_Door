"""Shared pytest fixtures for the Nightingale test suite.

Provides an isolated environment so tests never touch a real ``.env`` or live
Azure services — everything runs against mocks per the project's definition of done.
"""

from __future__ import annotations

import os
import sys

import pytest

# Make the repo root importable (config.py, search_client.py, src/ live there).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the minimum required env vars so ``AppConfig.from_env`` succeeds."""
    monkeypatch.setenv("AZURE_VOICELIVE_ENDPOINT", "https://example.services.ai.azure.com/")
    monkeypatch.setenv("AZURE_VOICELIVE_USE_TOKEN", "true")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://example.search.windows.net")
    monkeypatch.setenv("AZURE_SEARCH_API_KEY", "test-key")
