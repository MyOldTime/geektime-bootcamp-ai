"""Keep unit tests independent of developer credentials and configuration."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in os.environ:
        if key == "DATABASES" or key.startswith(
            (
                "DATABASE_",
                "OPENAI_",
                "SECURITY_",
                "VALIDATION_",
                "CACHE_",
                "RESILIENCE_",
                "OBSERVABILITY_",
            )
        ):
            monkeypatch.delenv(key, raising=False)
