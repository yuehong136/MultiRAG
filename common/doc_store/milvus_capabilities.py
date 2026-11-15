"""Native query capabilities shared by chunk and memory Milvus adapters."""

import re
from importlib.metadata import version

from pymilvus.orm import utility


def supports_verified_milvus_3(using: str) -> bool:
    """Check the supported stable SDK/server family; propagate failed probes.

    The verified official 3.0.6 image reports its build identity instead of a
    patch semver. Unknown builds and prereleases are deliberately not enabled.
    """
    if re.fullmatch(r"3\.\d+\.\d+", version("pymilvus")) is None:
        return False
    server_version = utility.get_server_version(using=using, timeout=5)
    return re.fullmatch(r"v?3\.\d+\.\d+", server_version) is not None or server_version == "3.0-20261110-63100f8f5b4"


def supports_native_query_order_by(using: str) -> bool:
    """Require the supported pair for the scalar query ORDER BY capability."""
    return supports_verified_milvus_3(using)
