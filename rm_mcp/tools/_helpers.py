"""
Shared helpers, caches, and re-exports for MCP tool modules.

Tool modules access commonly-patched names through this module
(e.g., ``_helpers.get_cached_collection()``) so that a single
``unittest.mock.patch`` target works for all tools.
"""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List

from mcp.types import ToolAnnotations

# --- Re-exports (commonly patched in tests) ---
# Tool modules access these via ``_helpers.X()`` so tests can patch once
# at ``rm_mcp.tools._helpers.X``.
from rm_mcp.api import (  # noqa: F401
    REMARKABLE_TOKEN,
    get_file_type,
)
from rm_mcp.cache import get_cached_collection  # noqa: F401
from rm_mcp.extract import (  # noqa: F401
    cache_page_ocr,
    extract_text_from_document_zip,
    extract_text_from_epub,
    extract_text_from_pdf,
    get_background_color,
    get_cached_ocr_result,
    get_cached_page_ocr,
    get_document_page_count,
    render_page_from_document_zip,
    render_page_from_document_zip_svg,
)
from rm_mcp.ocr.sampling import (  # noqa: F401
    get_ocr_backend,
    ocr_via_sampling,
    should_use_sampling_ocr,
)
from rm_mcp.paths import (  # noqa: F401
    _apply_root_filter,
    _find_document,
    _get_root_path,
    _is_within_root,
    get_item_path,
    get_items_by_id,
    get_items_by_parent,
)
from rm_mcp.responses import make_error, make_response  # noqa: F401

# --- Helper functions ---


def is_compact(compact_output: bool = False) -> bool:
    """Check parameter or REMARKABLE_COMPACT env var."""
    return compact_output or os.environ.get("REMARKABLE_COMPACT", "") in ("1", "true")


MAX_OUTPUT_CHARS = int(os.environ.get("REMARKABLE_MAX_OUTPUT_CHARS", "50000"))


def suggest_for_error(e: Exception) -> str:
    """Generate a context-aware suggestion based on error content."""
    msg = str(e).lower()
    if "not authenticated" in msg or "no device token" in msg:
        return "Run: uvx rm-mcp --setup to authenticate."
    if "re-authenticate" in msg or ("token" in msg and ("expired" in msg or "401" in msg)):
        return "Your token may have expired. Run: uvx rm-mcp --setup to re-authenticate."
    if "network error" in msg or "connection" in msg or "timeout" in msg:
        return "Check your internet connection and try again."
    if "empty response" in msg:
        return (
            "The reMarkable API returned an empty response. "
            "Your token may have expired. Run: uvx rm-mcp --setup"
        )
    return "Check remarkable_status() for diagnostics."


def parse_pages(pages_str: str, total_pages: int) -> List[int]:
    """Parse 'all', '1-3', '2,4,5', '1-3,5' into sorted page list.

    Out-of-range pages are clamped to [1, total_pages].
    """
    if pages_str.strip().lower() == "all":
        return list(range(1, total_pages + 1))

    pages: set = set()
    for part in pages_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                start = max(1, int(bounds[0].strip()))
                end = min(total_pages, int(bounds[1].strip()))
                pages.update(range(start, end + 1))
            except (ValueError, IndexError):
                continue
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    pages.add(p)
            except ValueError:
                continue
    return sorted(pages)


@contextmanager
def _temp_document(data: bytes, suffix: str = ".zip"):
    """Context manager for writing data to a temp file with guaranteed cleanup."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(data)
        yield tmp_path
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


# --- Caches ---

_file_type_cache: Dict[str, str] = {}
_MAX_FILE_TYPE_CACHE = 200

_rendered_image_cache: Dict[str, str] = {}  # key: f"{doc_id}:{page}" -> base64 PNG


def _get_file_type_cached(client, doc) -> str:
    """Get file type with caching to avoid repeated lookups."""
    doc_id = doc.ID
    if doc_id in _file_type_cache:
        return _file_type_cache[doc_id]
    file_type = get_file_type(client, doc)
    if len(_file_type_cache) >= _MAX_FILE_TYPE_CACHE:
        _file_type_cache.clear()
    _file_type_cache[doc_id] = file_type
    return file_type


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


def _is_cloud_archived(item) -> bool:
    """Check if an item is cloud-archived (not available on device).

    Items in the trash (parent == "trash") are not downloadable.
    """
    if hasattr(item, "is_cloud_archived"):
        return item.is_cloud_archived
    parent = item.Parent if hasattr(item, "Parent") else getattr(item, "parent", "")
    return parent == "trash"


# --- Tool annotations ---

# Base annotations for read-only operations
_BASE_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,  # Private cloud account, not open world
}

# Unique annotations for each tool with descriptive titles
READ_ANNOTATIONS = ToolAnnotations(
    title="Read reMarkable Document",
    **_BASE_ANNOTATIONS,
)

BROWSE_ANNOTATIONS = ToolAnnotations(
    title="Browse reMarkable Library",
    **_BASE_ANNOTATIONS,
)

SEARCH_ANNOTATIONS = ToolAnnotations(
    title="Search reMarkable Documents",
    **_BASE_ANNOTATIONS,
)

RECENT_ANNOTATIONS = ToolAnnotations(
    title="Get Recent reMarkable Documents",
    **_BASE_ANNOTATIONS,
)

STATUS_ANNOTATIONS = ToolAnnotations(
    title="Check reMarkable Connection",
    **_BASE_ANNOTATIONS,
)

IMAGE_ANNOTATIONS = ToolAnnotations(
    title="Get reMarkable Page Image",
    **_BASE_ANNOTATIONS,
)

# Default page size for pagination (characters) - used for PDFs/EPUBs
DEFAULT_PAGE_SIZE = int(os.environ.get("REMARKABLE_PAGE_SIZE", "8000"))
