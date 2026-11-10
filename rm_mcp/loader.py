"""
Startup work: fetch the library once, record it in the index, then make every
document's page text searchable in the background.

Indexing reads each document the same way remarkable_read does
(``reader.document_pages``), so search and read can never disagree. Documents
whose current version is already indexed are skipped, so a restart downloads
only what changed.
"""

import asyncio
import logging
from typing import Optional

from rm_mcp.config import env_bool
from rm_mcp.paths import _get_root_path

logger = logging.getLogger(__name__)

_MAX_FETCH_ATTEMPTS = 3
# Documents above this size are skipped: they are held in memory to read.
MAX_CONTENT_INDEX_BYTES = 100 * 1024 * 1024
CONTENT_INDEX_PAUSE_SECONDS = 1.0


async def load_library(
    shutdown_event: asyncio.Event, pause_seconds: Optional[float] = None
) -> None:
    from rm_mcp import reader
    from rm_mcp.api import get_rmapi
    from rm_mcp.cache import set_cached_collection
    from rm_mcp.index import get_instance

    pause = CONTENT_INDEX_PAUSE_SECONDS if pause_seconds is None else pause_seconds
    client = get_rmapi()
    loop = asyncio.get_running_loop()

    items = None
    for attempt in range(1, _MAX_FETCH_ATTEMPTS + 1):
        if shutdown_event.is_set():
            return
        try:
            items = await loop.run_in_executor(None, client.get_meta_items)
            break
        except Exception as e:
            logger.warning("Error fetching the library (attempt %d): %s", attempt, e)
            if attempt == _MAX_FETCH_ATTEMPTS:
                logger.error("Background loader stopping after %d attempts", attempt)
                return
            await asyncio.sleep(2**attempt)
    if not items:
        logger.info("Background loader: library is empty")
        return

    # Publish the complete collection once, so a tool called during startup
    # never sees (and caches) a partial library.
    set_cached_collection(client, items, root_hash=getattr(client, "synced_root_hash", None))
    snap = reader.snapshot()
    docs = snap.documents()

    index = get_instance()
    if index is None:
        return
    # Only documents inside the root are indexed: anything else would become
    # reachable through search.
    for entry in docs:
        try:
            if entry.hash:
                index.needs_reindex(entry.id, entry.hash)
            index.upsert_document(
                doc_id=entry.id,
                name=entry.name,
                path=entry.path,
                file_type=entry.kind,
                modified_at=entry.modified.isoformat() if entry.modified else None,
            )
        except Exception:
            logger.warning("Index metadata write failed for %s", entry.id, exc_info=True)
    try:
        await loop.run_in_executor(None, index.prune_missing, [e.id for e in docs])
    except Exception:
        logger.warning("Index prune failed", exc_info=True)

    root = _get_root_path()
    logger.info(
        "Library loaded: %d documents%s", len(docs), f" under {root}" if root != "/" else ""
    )
    if not env_bool("REMARKABLE_INDEX_CONTENT", True):
        return

    todo, too_big = [], []
    for entry in docs:
        if not reader.needs_indexing(entry):
            continue
        size = getattr(snap.items.get(entry.id), "size", 0) or 0
        (too_big if size > MAX_CONTENT_INDEX_BYTES else todo).append((size, entry))
    for size, entry in too_big:
        logger.info(
            "Content indexing skipped '%s': %d MB exceeds the %d MB limit",
            entry.name,
            size // (1024 * 1024),
            MAX_CONTENT_INDEX_BYTES // (1024 * 1024),
        )
    # Smallest first: coverage grows fastest, and an interrupted pass resumes.
    todo.sort(key=lambda pair: pair[0])
    done = failed = 0
    for _size, entry in todo:
        if shutdown_event.is_set():
            logger.info(
                "Content indexing stopped (%d/%d done; resumes next start)", done, len(todo)
            )
            return
        try:
            await loop.run_in_executor(None, reader.document_pages, snap, entry)
            done += 1
        except Exception:
            failed += 1
            logger.warning("Content indexing failed for '%s'", entry.name, exc_info=True)
        await asyncio.sleep(pause)
    if todo:
        logger.info("Content indexing complete: %d indexed, %d failed", done, failed)


def start_background_loader() -> Optional[asyncio.Task]:
    shutdown_event = asyncio.Event()
    task = asyncio.create_task(load_library(shutdown_event))
    task.shutdown_event = shutdown_event  # type: ignore[attr-defined]
    return task


async def stop_background_loader(task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    task.shutdown_event.set()
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
