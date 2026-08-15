"""
reMarkable MCP Server

An MCP server that provides access to reMarkable tablet data through the reMarkable Cloud API.
"""

# Capability checking utilities
from rm_mcp.capabilities import (
    client_supports_elicitation,
    client_supports_experimental,
    client_supports_roots,
    client_supports_sampling,
    get_client_capabilities,
    get_client_info,
    get_protocol_version,
)

try:  # Single source of truth is pyproject.toml, patched at release time.
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("rm-mcp")
except Exception:  # Running from a source checkout without an install
    __version__ = "dev"


def get_mcp():
    """Get the MCP server instance. Only imports when called."""
    from rm_mcp.server import mcp

    return mcp


__all__ = [
    "get_mcp",
    "__version__",
    # Capability checking
    "get_client_capabilities",
    "client_supports_sampling",
    "client_supports_elicitation",
    "client_supports_roots",
    "client_supports_experimental",
    "get_client_info",
    "get_protocol_version",
]
