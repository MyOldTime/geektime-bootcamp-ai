"""Pytest configuration and shared fixtures.

This module provides shared fixtures and configuration for all tests.
"""

import os

import pytest

from pg_mcp.config.settings import reset_settings


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Reset global settings before each test."""
    reset_settings()


@pytest.fixture(autouse=True)
def disable_metrics_for_tests(monkeypatch):
    """Disable metrics for tests to avoid port conflicts."""
    monkeypatch.setenv("OBSERVABILITY_METRICS_ENABLED", "false")
    yield


def pytest_collection_modifyitems(config, items):
    """Keep legacy tests requiring real API credentials explicitly opt-in."""
    if os.getenv("PG_MCP_RUN_LIVE_TESTS") == "1":
        return
    for item in items:
        if item.path.name in {"test_full_flow.py", "test_mcp.py"}:
            item.add_marker(pytest.mark.integration)
            item.add_marker(
                pytest.mark.skip(reason="Set PG_MCP_RUN_LIVE_TESTS=1 for live API tests")
            )
