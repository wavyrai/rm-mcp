"""
Centralized caching for reMarkable MCP.

Consolidates all cache instances and their access functions from
api.py, extract.py, and tools.py into one module.
"""

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from rm_mcp.config import env_int

logger = logging.getLogger(__name__)


# =============================================================================
# Collection cache (from api.py)
# =============================================================================

_cached_collection = None
_cached_root_hash: Optional[str] = None
_cache_timestamp: float = 0.0

# The collection cache is read from tool calls on the event loop and written
# from the background loader's worker thread.
_collection_lock = threading.Lock()

_CACHE_TTL_SECONDS = env_int("REMARKABLE_CACHE_TTL", 60, minimum=0)


def get_cached_collection() -> Tuple[Any, List]:
    """
    Get the client and document collection, using a cache to avoid redundant fetches.

    Cache strategy:
    - If cache is < 60s old, return it immediately (0 requests)
    - Else fetch root hash (1 request), compare to cached hash
    - If unchanged, refresh timestamp and return cache (1 request total)
    - If changed, do a full re-fetch

    Returns:
        Tuple of (client, collection)
    """
    global _cached_collection, _cached_root_hash, _cache_timestamp

    from rm_mcp.api import get_rmapi

    client = get_rmapi()
    if client is None:
        raise RuntimeError("Not authenticated. Run: uvx rm-mcp --setup")
    now = time.time()

    # If we have a valid cache within TTL, return immediately
    with _collection_lock:
        if _cached_collection is not None and (now - _cache_timestamp) < _CACHE_TTL_SECONDS:
            logger.debug("Collection cache hit (within TTL)")
            return client, _cached_collection

    # Check if client supports root hash (for change detection)
    if not hasattr(client, "get_root_hash"):
        collection = client.get_meta_items()
        with _collection_lock:
            _cached_collection = collection
            _cache_timestamp = time.time()
        return client, collection

    # Cloud mode: check root hash to see if anything changed
    try:
        current_hash = client.get_root_hash()
    except Exception:
        # If root hash fetch fails, do a full re-fetch
        collection = client.get_meta_items()
        with _collection_lock:
            _cached_collection = collection
            _cache_timestamp = time.time()
        return client, collection

    # A sync that could not fetch every document is retried even when the root
    # has not moved; the client only refetches what it is missing.
    sync_complete = getattr(client, "sync_complete", True) is not False
    with _collection_lock:
        if _cached_collection is not None and current_hash == _cached_root_hash and sync_complete:
            # Nothing changed, refresh timestamp
            logger.debug("Collection cache hit (root hash unchanged)")
            _cache_timestamp = time.time()
            return client, _cached_collection

    # Root hash changed or no cache. The cloud client syncs incrementally, so
    # this fetches only the documents whose hash changed.
    logger.debug("Collection cache miss — syncing collection")
    collection = client.get_meta_items(root_hash=current_hash)
    with _collection_lock:
        _cached_collection = collection
        _cached_root_hash = current_hash
        _cache_timestamp = time.time()
    return client, collection


def set_cached_collection(client, collection, root_hash: Optional[str] = None) -> None:
    """
    Populate the collection cache from an external source (e.g., background loader).

    This allows the resource loader to share its fetched data with tools,
    so the first tool call after startup doesn't need to re-fetch.

    Args:
        client: The reMarkable API client
        collection: List of document/folder items
        root_hash: Optional root hash to avoid an extra network call.
                   If not provided, will attempt to fetch from client.
    """
    global _cached_collection, _cached_root_hash, _cache_timestamp

    # Also set the client singleton in api.py
    import rm_mcp.api as api_mod

    api_mod._client_singleton = client

    resolved_hash = root_hash
    # Use provided root hash or try to get one for future comparisons
    if resolved_hash is None and hasattr(client, "get_root_hash"):
        try:
            resolved_hash = client.get_root_hash()
        except Exception:
            logger.debug("Could not fetch root hash while seeding cache", exc_info=True)

    with _collection_lock:
        _cached_collection = collection
        if resolved_hash is not None:
            _cached_root_hash = resolved_hash
        _cache_timestamp = time.time()


def invalidate_collection_cache() -> None:
    """Force the next get_cached_collection() call to re-fetch."""
    global _cached_collection, _cached_root_hash, _cache_timestamp
    with _collection_lock:
        _cached_collection = None
        _cached_root_hash = None
        _cache_timestamp = 0.0


# =============================================================================
# Derived-content caches
# =============================================================================
#
# Everything derived from a document is keyed by the content hash of what it was
# derived from, never by document ID alone: an edited document or page then
# simply misses the cache instead of being answered with its old text.
#
# - Extraction results: (doc_id, source_hash), where source_hash addresses the
#   exact archive contents (see extract.notebook.archive_content_hash).
# - Page OCR: (page_hash, backend), where page_hash is the SHA-256 of the
#   page's .rm blob, so editing one page invalidates only that page.
#
# These are written from worker threads (tools run blocking work off the event
# loop) and read from the loop, so every access holds _derived_lock.

EXTRACTION_CACHE_TTL_SECONDS = 300

_MAX_EXTRACTION_CACHE_SIZE = 50
_MAX_PAGE_OCR_CACHE_SIZE = 200

_derived_lock = threading.RLock()

# Key: (doc_id, source_hash)
# Value: {"result": dict, "include_ocr": bool, "include_source": bool, "timestamp": float}
_extraction_cache: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()

# Key: (page_hash, backend)
# Value: {"text": str, "timestamp": float}
_page_ocr_cache: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()


def _is_cache_valid(cached: Dict[str, Any]) -> bool:
    """Check if a cached entry is still valid based on TTL."""
    if "timestamp" not in cached:
        return False  # Unknown age = stale
    return (time.time() - cached["timestamp"]) < EXTRACTION_CACHE_TTL_SECONDS


def clear_extraction_cache(doc_id: Optional[str] = None) -> None:
    """
    Clear the in-memory derived caches.

    Args:
        doc_id: If provided, only clear extraction results for this document.
                If None, clear everything (extraction and page OCR).
    """
    with _derived_lock:
        if doc_id:
            for key in [k for k in _extraction_cache if k[0] == doc_id]:
                _extraction_cache.pop(key, None)
        else:
            _extraction_cache.clear()
            _page_ocr_cache.clear()


def get_cached_page_ocr(
    doc_id: str,
    page: int,
    backend: str,
    page_hash: Optional[str] = None,
) -> Optional[str]:
    """
    Get cached OCR text for exactly these page contents.

    Checks L1 (in-memory) first, then L2 (the index's content-addressed
    page_ocr table). Without a page hash there is no way to know the cached
    text matches the page, so nothing is returned.

    Args:
        doc_id: Document ID (for logging)
        page: Page number (1-indexed, for logging)
        backend: OCR backend used ("sampling")
        page_hash: SHA-256 of the page's .rm blob
    """
    if not page_hash:
        return None
    cache_key = (page_hash, backend)
    with _derived_lock:
        cached = _page_ocr_cache.get(cache_key)
        if cached is not None:
            if _is_cache_valid(cached):
                _page_ocr_cache.move_to_end(cache_key)
                return cached["text"]
            _page_ocr_cache.pop(cache_key, None)

    try:
        from rm_mcp.index import get_instance

        index = get_instance()
        if index is not None:
            text = index.get_page_ocr_by_hash(page_hash, backend)
            if text is not None:
                with _derived_lock:
                    _page_ocr_cache[cache_key] = {"text": text, "timestamp": time.time()}
                    _trim(_page_ocr_cache, _MAX_PAGE_OCR_CACHE_SIZE)
                logger.debug("L2 cache hit for page OCR: %s p%d", doc_id, page)
                return text
    except Exception:
        logger.warning("L2 read failed for page OCR", exc_info=True)

    return None


def cache_page_ocr(
    doc_id: str,
    page: int,
    backend: str,
    text: str,
    page_hash: Optional[str] = None,
) -> None:
    """
    Cache OCR text for a page.

    Writes L1 and the content-addressed L2 table (keyed by page hash), and
    stores the text as the document's page for full-text search.

    Args:
        doc_id: Document ID
        page: Page number (1-indexed)
        backend: OCR backend used ("sampling")
        text: OCR text result
        page_hash: SHA-256 of the page's .rm blob. Without it the text is
            only made searchable, never served from cache.
    """
    if page_hash:
        cache_key = (page_hash, backend)
        with _derived_lock:
            _page_ocr_cache[cache_key] = {"text": text, "timestamp": time.time()}
            _page_ocr_cache.move_to_end(cache_key)
            _trim(_page_ocr_cache, _MAX_PAGE_OCR_CACHE_SIZE)

    try:
        from rm_mcp.index import get_instance

        index = get_instance()
        if index is not None:
            if page_hash:
                index.put_page_ocr_by_hash(page_hash, backend, text)
            index.upsert_page(doc_id, page, text, "ocr", backend)
    except Exception:
        logger.warning("Could not persist page OCR to the index", exc_info=True)


def _trim(cache: "OrderedDict", max_size: int) -> None:
    while len(cache) > max_size:
        cache.popitem(last=False)


def get_cached_ocr_result(
    doc_id: str,
    source_hash: Optional[str],
    include_ocr: bool = True,
    ocr_backend: Optional[str] = None,
    include_source: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Get a cached extraction result for exactly this version of a document.

    Args:
        doc_id: Document ID to look up
        source_hash: Content hash of the archive the result must come from.
            None never matches.
        include_ocr: Whether OCR content is required
        ocr_backend: If specified, only return a result produced by this backend.
        include_source: Whether the PDF/EPUB source text is required

    Returns:
        Cached result dict or None if not cached/expired/insufficient
    """
    if not source_hash:
        return None
    key = (doc_id, source_hash)
    with _derived_lock:
        cached = _extraction_cache.get(key)
        if cached is None or not _is_cache_valid(cached):
            return None
        if include_ocr and not cached["include_ocr"]:
            return None
        if include_source and not cached.get("include_source", False):
            return None
        if ocr_backend is not None and cached["result"].get("ocr_backend") != ocr_backend:
            return None
        _extraction_cache.move_to_end(key)
        return cached["result"]


def cache_ocr_result(
    doc_id: str,
    source_hash: Optional[str],
    result: Dict[str, Any],
    include_ocr: bool = True,
    include_source: bool = False,
) -> None:
    """
    Cache an extraction result for one version of a document.

    Writes L1 (keyed by doc_id and source hash) and writes the text through to
    the index for full-text search.
    """
    if source_hash:
        key = (doc_id, source_hash)
        with _derived_lock:
            _extraction_cache[key] = {
                "result": result,
                "include_ocr": include_ocr,
                "include_source": include_source,
                "timestamp": time.time(),
            }
            _extraction_cache.move_to_end(key)
            _trim(_extraction_cache, _MAX_EXTRACTION_CACHE_SIZE)

    try:
        from rm_mcp.index import get_instance

        index = get_instance()
        if index is not None:
            index.store_extraction_result(doc_id, result)
    except Exception:
        logger.warning("Could not persist extraction result to the index", exc_info=True)
