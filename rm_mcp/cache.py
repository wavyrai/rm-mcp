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
        raise RuntimeError("Not authenticated. Run: uvx rm-mcp@latest setup")
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
# Page transcripts
# =============================================================================
#
# A transcript is text an agent wrote for a handwritten page after looking at
# it (remarkable_save_transcript); older versions stored sampling OCR the same
# way. It is keyed by (page_hash, backend), where page_hash is the content hash
# of everything the page renders from (extract.pages.page_content_hashes), so
# editing a page drops its transcript and editing another page keeps it.
#
# Written from worker threads and read from the loop, so access holds a lock.

TRANSCRIPT_BACKEND = "transcript"
# Read as a fallback: OCR saved by versions that transcribed through sampling.
LEGACY_OCR_BACKEND = "sampling"

PAGE_OCR_TTL_SECONDS = 300
_MAX_PAGE_OCR_CACHE_SIZE = 200

_derived_lock = threading.RLock()

# Key: (page_hash, backend). Value: {"text": str, "timestamp": float}
_page_ocr_cache: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()


def clear_page_ocr_cache() -> None:
    """Drop the in-memory transcript cache (the index keeps its copy)."""
    with _derived_lock:
        _page_ocr_cache.clear()


def get_cached_page_ocr(
    doc_id: str,
    page: int,
    backend: str,
    page_hash: Optional[str] = None,
) -> Optional[str]:
    """
    Get the stored text for exactly these page contents.

    Checks L1 (in-memory) first, then L2 (the index's content-addressed
    page_ocr table). Without a page hash there is no way to know stored text
    matches the page, so nothing is returned.
    """
    if not page_hash:
        return None
    cache_key = (page_hash, backend)
    with _derived_lock:
        cached = _page_ocr_cache.get(cache_key)
        if cached is not None:
            if (time.time() - cached["timestamp"]) < PAGE_OCR_TTL_SECONDS:
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
                logger.debug("L2 hit for page text: %s p%d", doc_id, page)
                return text
    except Exception:
        logger.warning("L2 read failed for page text", exc_info=True)

    return None


def cache_page_ocr(
    doc_id: str,
    page: int,
    backend: str,
    text: str,
    page_hash: str,
) -> None:
    """
    Store text for a page under its content hash, and make it searchable.

    Raises if the index cannot store it: a transcript the caller was told is
    saved must not exist only in memory.
    """
    from rm_mcp.index import get_instance

    index = get_instance()
    if index is None:
        raise RuntimeError("The local index is unavailable, so the transcript cannot be stored.")
    index.put_page_ocr_by_hash(page_hash, backend, text)
    index.upsert_page(doc_id, page, text, "ocr", backend)
    with _derived_lock:
        _page_ocr_cache[(page_hash, backend)] = {"text": text, "timestamp": time.time()}
        _page_ocr_cache.move_to_end((page_hash, backend))
        _trim(_page_ocr_cache, _MAX_PAGE_OCR_CACHE_SIZE)


def get_page_transcript(doc_id: str, page: int, page_hash: Optional[str]) -> Optional[str]:
    """The saved transcript of a page, falling back to legacy sampling OCR."""
    for backend in (TRANSCRIPT_BACKEND, LEGACY_OCR_BACKEND):
        text = get_cached_page_ocr(doc_id, page, backend, page_hash=page_hash)
        if text is not None:
            return text
    return None


def _trim(cache: "OrderedDict", max_size: int) -> None:
    while len(cache) > max_size:
        cache.popitem(last=False)
