"""
MCP Tools for reMarkable tablet access.

Reading tools (browse, read, search, recent, image, status) never modify
anything. The organising tools (rename, move, create folder) change the
library's structure but never its contents — no tool deletes anything or
alters a document's pages.
"""

# Import tool modules to trigger registration with the MCP server
from rm_mcp.tools import (  # noqa: F401
    browse,
    image,
    organize,
    read,
    recent,
    search,
    status,
)
