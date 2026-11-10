"""
MCP tools for reMarkable tablet access.

The read tools (find, list, read, view, annotations, status) never change
anything. remarkable_save_transcript writes only to the local index. The
organising tools (rename, move, create folder) change the library's structure
but never its contents; no tool deletes anything.
"""

# ruff: noqa: F401, I001
# Importing a module registers its tools; this order is the order of tools/list.
from rm_mcp.tools import find, listing, read, view, annotations, transcript, status
from rm_mcp.tools import organize
from rm_mcp.tools import deprecated
