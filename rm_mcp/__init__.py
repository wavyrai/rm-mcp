"""
reMarkable MCP Server

An MCP server that provides access to reMarkable tablet data through the reMarkable Cloud API.
"""

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
]
