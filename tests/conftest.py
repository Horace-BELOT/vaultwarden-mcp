import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Run anyio tests on asyncio only (the MCP SDK's own backend)."""
    return "asyncio"
