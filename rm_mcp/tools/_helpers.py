"""
Shared helpers, caches, and re-exports for MCP tool modules.

Tool modules access commonly-patched names through this module
(e.g., ``_helpers.get_cached_collection()``) so that a single
``unittest.mock.patch`` target works for all tools.
"""

import functools
import logging
import os
import tempfile
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Callable, List

import anyio
from mcp.types import ToolAnnotations

# --- Re-exports (commonly patched in tests) ---
# Tool modules access these via ``_helpers.X()`` so tests can patch once
# at ``rm_mcp.tools._helpers.X``.
from rm_mcp.api import (  # noqa: F401
    REMARKABLE_TOKEN,
    get_file_type,
)
from rm_mcp.cache import get_cached_collection  # noqa: F401
from rm_mcp.config import env_int  # noqa: F401
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
    _resolve_root_path,
    get_item_path,
    get_items_by_id,
    get_items_by_parent,
)
from rm_mcp.responses import make_error, make_response  # noqa: F401

logger = logging.getLogger(__name__)

# --- Helper functions ---


def is_compact(compact_output: bool = False) -> bool:
    """Check parameter or REMARKABLE_COMPACT env var."""
    return compact_output or os.environ.get("REMARKABLE_COMPACT", "").lower() in (
        "1",
        "true",
        "yes",
    )


async def run_blocking(func: Callable[..., Any], *args, **kwargs) -> Any:
    """Run a blocking (network/CPU-bound) callable off the event loop.

    FastMCP awaits async tools directly on the event loop, so any synchronous
    cloud download or extraction inside a tool would stall every other request
    on the connection until it finished.
    """
    return await anyio.to_thread.run_sync(functools.partial(func, *args, **kwargs))


def get_max_output_chars() -> int:
    """Max characters returned by a single tool call (read live from the env)."""
    return env_int("REMARKABLE_MAX_OUTPUT_CHARS", 50000, minimum=1000)


def get_page_size() -> int:
    """Character page size used to paginate PDF/EPUB text (read live from the env)."""
    return env_int("REMARKABLE_PAGE_SIZE", 8000, minimum=500)


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


@asynccontextmanager
async def open_document(client, doc, suffix: str = ".zip"):
    """Download a document off the event loop and yield a temp file path.

    Downloads are cached per document version for a short window, so reading
    several pages of the same notebook costs one download rather than one per
    page.
    """
    data = await run_blocking(download_document, client, doc)
    with _temp_document(data, suffix=suffix) as tmp_path:
        yield tmp_path


async def extract_document(client, doc, include_ocr: bool = False, include_source: bool = True):
    """Download and extract a document's text, entirely off the event loop."""

    def _work():
        data = download_document(client, doc)
        with _temp_document(data) as tmp_path:
            return extract_text_from_document_zip(
                tmp_path,
                include_ocr=include_ocr,
                doc_id=doc.ID,
                include_source=include_source,
            )

    return await run_blocking(_work)


# --- Caches ---

_file_type_cache: "OrderedDict[str, str]" = OrderedDict()
_MAX_FILE_TYPE_CACHE = 200

# Rendered pages, keyed by (doc hash, page, format, background) so that a
# different background or format never serves another variant's render.
# Bounded by total bytes, because a single page render can be several MB.
_rendered_image_cache: "OrderedDict[str, str]" = OrderedDict()
_MAX_RENDERED_IMAGE_BYTES = 64 * 1024 * 1024


def render_cache_key(doc, page: int, output_format: str, background: str) -> str:
    """Build a cache key that captures everything the render depends on."""
    version = getattr(doc, "hash", "") or ""
    return f"{doc.ID}:{version}:{page}:{output_format}:{background}"


def get_rendered_image(key: str):
    """Return a cached render, marking it as most recently used."""
    value = _rendered_image_cache.get(key)
    if value is not None:
        _rendered_image_cache.move_to_end(key)
    return value


def put_rendered_image(key: str, value: str) -> None:
    """Cache a render, evicting least-recently-used entries past the byte budget."""
    _rendered_image_cache[key] = value
    _rendered_image_cache.move_to_end(key)
    total = sum(len(v) for v in _rendered_image_cache.values())
    while total > _MAX_RENDERED_IMAGE_BYTES and len(_rendered_image_cache) > 1:
        _, evicted = _rendered_image_cache.popitem(last=False)
        total -= len(evicted)


# Downloaded document archives, keyed by document version. Rendering five pages
# of one notebook used to mean five downloads of the same archive.
_download_cache: "OrderedDict[str, bytes]" = OrderedDict()
_MAX_DOWNLOAD_CACHE_BYTES = 128 * 1024 * 1024
_download_lock = threading.Lock()


def download_document(client, doc) -> bytes:
    """Download a document's archive, reusing a recent download when possible."""
    key = f"{doc.ID}:{getattr(doc, 'hash', '') or ''}"
    with _download_lock:
        cached = _download_cache.get(key)
        if cached is not None:
            _download_cache.move_to_end(key)
            return cached

    data = client.download(doc)

    with _download_lock:
        _download_cache[key] = data
        _download_cache.move_to_end(key)
        total = sum(len(v) for v in _download_cache.values())
        while total > _MAX_DOWNLOAD_CACHE_BYTES and len(_download_cache) > 1:
            _, evicted = _download_cache.popitem(last=False)
            total -= len(evicted)
    return data


def clear_download_cache() -> None:
    """Drop every cached document archive."""
    with _download_lock:
        _download_cache.clear()


def record_page_count(doc_id: str, page_count: int) -> None:
    """Remember a document's page count so later calls need not recount it.

    Resource-template completion reads this instead of downloading the
    document on every keystroke.
    """
    if not page_count:
        return
    try:
        from rm_mcp.index import get_instance

        index = get_instance()
        if index is not None:
            index.upsert_document(doc_id=doc_id, page_count=page_count)
    except Exception:
        logger.debug("Could not record page count for %s", doc_id, exc_info=True)


def _get_file_type_cached(client, doc) -> str:
    """Get file type with caching to avoid repeated lookups."""
    doc_id = doc.ID
    if doc_id in _file_type_cache:
        _file_type_cache.move_to_end(doc_id)
        return _file_type_cache[doc_id]
    file_type = get_file_type(client, doc)
    _file_type_cache[doc_id] = file_type
    while len(_file_type_cache) > _MAX_FILE_TYPE_CACHE:
        _file_type_cache.popitem(last=False)
    return file_type


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
