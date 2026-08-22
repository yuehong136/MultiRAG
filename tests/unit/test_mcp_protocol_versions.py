import pytest
from mcp_types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    MODERN_PROTOCOL_VERSIONS,
)

from mcp.client import Client
from scripts.check_mcp_compat import (
    EXPECTED_HANDSHAKE_PROTOCOL_VERSIONS,
    EXPECTED_MODERN_PROTOCOL_VERSIONS,
)


def test_compatibility_matrix_reviews_every_sdk_protocol_revision() -> None:
    assert HANDSHAKE_PROTOCOL_VERSIONS == EXPECTED_HANDSHAKE_PROTOCOL_VERSIONS
    assert MODERN_PROTOCOL_VERSIONS == EXPECTED_MODERN_PROTOCOL_VERSIONS


@pytest.mark.parametrize("version", EXPECTED_HANDSHAKE_PROTOCOL_VERSIONS)
def test_sdk_requires_legacy_mode_instead_of_pinning_handshake_revision(
    version: str,
) -> None:
    with pytest.raises(ValueError, match="use mode='legacy'"):
        Client("http://127.0.0.1:1/mcp", mode=version)


@pytest.mark.parametrize("version", EXPECTED_MODERN_PROTOCOL_VERSIONS)
def test_sdk_accepts_exact_modern_revision_pin(version: str) -> None:
    assert Client("http://127.0.0.1:1/mcp", mode=version).mode == version


def test_sdk_rejects_unknown_protocol_revision_pin() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        Client("http://127.0.0.1:1/mcp", mode="2099-01-01")
