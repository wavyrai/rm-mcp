"""
MCP Tools for reMarkable tablet access.

All tools are read-only and idempotent - they only retrieve data from the
reMarkable Cloud and do not modify any documents.
"""

# Import tool modules to trigger registration with the MCP server
from rm_mcp.tools import (  # noqa: F401
    browse,
    image,
    read,
    recent,
    search,
    status,
)
