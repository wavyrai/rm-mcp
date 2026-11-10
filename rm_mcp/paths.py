"""
Path utilities for reMarkable MCP.

Root path filtering, item path building, document lookup, and fuzzy matching.
"""

import os
from typing import Any, Dict, Optional

# --- Root path utilities ---


def _get_root_path() -> str:
    """Get the configured root path filter, or '/' for full access.

    Handles: empty string, '/', '/Work', '/Work/', 'Work' -> normalized path
    """
    root = os.environ.get("REMARKABLE_ROOT_PATH", "").strip()
    # Empty or "/" means full access
    if not root or root == "/":
        return "/"
    # Normalize: ensure starts with / and no trailing slash
    if not root.startswith("/"):
        root = "/" + root
    if root.endswith("/"):
        root = root.rstrip("/")
    return root


def _is_within_root(path: str, root: str) -> bool:
    """Check if a path is within the configured root (case-insensitive)."""
    if root == "/":
        return True
    # Path must equal root or be a child of root (case-insensitive)
    path_lower = path.lower()
    root_lower = root.lower()
    return path_lower == root_lower or path_lower.startswith(root_lower + "/")


def _apply_root_filter(path: str, root: Optional[str] = None) -> str:
    """Apply root filter to a path for display/API purposes.

    If root is '/Work', then '/Work/Project' becomes '/Project' in output.
    Case-insensitive matching, preserves original case in output.

    Args:
        path: The full path to filter
        root: The root path. If None, reads from _get_root_path().
    """
    if root is None:
        root = _get_root_path()
    if root == "/":
        return path
    path_lower = path.lower()
    root_lower = root.lower()
    if path_lower == root_lower:
        return "/"
    if path_lower.startswith(root_lower + "/"):
        return path[len(root) :]
    return path


def _resolve_root_path(path: str) -> str:
    """Resolve a user-provided path to the actual path on device.

    If root is '/Work', then '/Project' becomes '/Work/Project'.
    """
    root = _get_root_path()
    if root == "/":
        return path
    if path == "/":
        return root
    # Prepend root to the path
    return root + path


# --- Collection utility functions ---


def get_items_by_id(collection) -> Dict[str, Any]:
    """Build a lookup dict of items by ID."""
    return {item.ID: item for item in collection}


def get_item_path(item, items_by_id: Dict[str, Any]) -> str:
    """Get the full path of an item."""
    path_parts = [item.VissibleName]
    parent_id = item.Parent if hasattr(item, "Parent") else ""
    visited = set()
    if hasattr(item, "ID"):
        visited.add(item.ID)
    while parent_id and parent_id in items_by_id and parent_id not in visited:
        visited.add(parent_id)
        parent = items_by_id[parent_id]
        path_parts.insert(0, parent.VissibleName)
        parent_id = parent.Parent if hasattr(parent, "Parent") else ""
    return "/" + "/".join(path_parts)
