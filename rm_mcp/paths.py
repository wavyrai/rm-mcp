"""
Path utilities for reMarkable MCP.

Root path filtering, item path building, document lookup, and fuzzy matching.
"""

import os
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

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


def get_items_by_parent(collection) -> Dict[str, List]:
    """Build a lookup dict of items grouped by parent ID."""
    items_by_parent: Dict[str, List] = {}
    for item in collection:
        parent = item.Parent if hasattr(item, "Parent") else ""
        if parent not in items_by_parent:
            items_by_parent[parent] = []
        items_by_parent[parent].append(item)
    return items_by_parent


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


# --- Document lookup helper ---


def _find_document(document: str, collection, items_by_id: Dict[str, Any], root: str):
    """Find a document by name or path in the collection.

    Args:
        document: Document name or path to find (already resolved to actual path)
        collection: List of all items
        items_by_id: ID-to-item mapping
        root: Root path filter

    Returns:
        Tuple of (target_doc, doc_path) if found, or (None, error_json_str) if not found.
    """
    from rm_mcp.responses import make_error

    documents = [item for item in collection if not item.is_folder]
    target_doc = None
    document_lower = document.lower().strip("/")

    for doc in documents:
        # Skip trashed documents
        if getattr(doc, "Parent", "") == "trash":
            continue
        doc_path = get_item_path(doc, items_by_id)
        # Filter by root path
        if not _is_within_root(doc_path, root):
            continue
        # Match by name (case-insensitive)
        if doc.VissibleName.lower() == document_lower:
            target_doc = doc
            break
        # Also try matching by full path (case-insensitive)
        if doc_path.lower().strip("/") == document_lower:
            target_doc = doc
            break

    if not target_doc:
        # Find similar documents for suggestion (only within root)
        filtered_docs = [
            doc
            for doc in documents
            if getattr(doc, "Parent", "") != "trash"
            and _is_within_root(get_item_path(doc, items_by_id), root)
        ]
        # Use the original user-provided document name for suggestions
        similar = find_similar_documents(document, filtered_docs)
        search_term = document.split()[0] if document else "notes"
        error = make_error(
            error_type="document_not_found",
            message=f"Document not found: '{document}'",
            suggestion=(
                f"Try remarkable_browse(query='{search_term}') to search, "
                "or remarkable_browse('/') to list all files."
            ),
            did_you_mean=similar if similar else None,
        )
        return None, error

    doc_path = get_item_path(target_doc, items_by_id)
    return target_doc, doc_path


# --- Fuzzy matching ---


def find_similar_documents(query: str, documents: List, limit: int = 5) -> List[str]:
    """Find documents with similar names for 'did you mean' suggestions."""
    query_lower = query.lower()
    scored = []
    for doc in documents:
        name = doc.VissibleName
        # Use sequence matcher for fuzzy matching
        ratio = SequenceMatcher(None, query_lower, name.lower()).ratio()
        # Boost partial matches
        if query_lower in name.lower():
            ratio += 0.3
        scored.append((name, ratio))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [name for name, score in scored[:limit] if score > 0.3]
