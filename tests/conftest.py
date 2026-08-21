"""Pytest configuration for local and AWS integration test execution."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add an explicit switch for tests requiring external AWS services."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run tests that require a real AWS IoT Core connection.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip external integration tests unless explicitly requested."""
    if config.getoption("--run-integration"):
        return
    skip_integration = pytest.mark.skip(
        reason="Use --run-integration to run real AWS integration tests"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
