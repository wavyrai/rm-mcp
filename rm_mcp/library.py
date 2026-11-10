"""
Stable-ID view of the library.

Documents and folders are identified by their UUID, which survives renames and
moves; names and paths are derived from the current collection every time. The
`hash` is the content address of the document's current version (its blob
index), so anything derived from a document can be keyed by it.
"""

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Iterator, Optional

from rm_mcp.api import get_file_type
from rm_mcp.paths import get_item_path, get_items_by_id


@dataclass(frozen=True)
class LibraryEntry:
    id: str
    path: str  # full path from the library root (not root-filtered)
    name: str
    kind: str  # "folder", "notebook", "pdf" or "epub"
    hash: str
    modified: Optional[datetime]
    page_count: Optional[int]  # from the synced .content, else the index, else None
    stale: bool = False  # the latest version could not be fetched yet
    parent: str = ""  # parent folder id; "" for the top level, "trash" when trashed

    @property
    def is_folder(self) -> bool:
        return self.kind == "folder"


def _known_page_count(doc_id: str) -> Optional[int]:
    try:
        from rm_mcp.index import get_instance

        index = get_instance()
        return index.get_page_count(doc_id) if index is not None else None
    except Exception:
        return None


def _page_count(item) -> Optional[int]:
    synced = getattr(item, "page_count", None)
    return synced if isinstance(synced, int) else _known_page_count(item.ID)


class Library:
    """Immutable id → entry map over one snapshot of the collection."""

    def __init__(self, items: Iterable):
        items = list(items)
        by_id = get_items_by_id(items)
        self._entries: Dict[str, LibraryEntry] = {}
        for item in items:
            self._entries[item.ID] = LibraryEntry(
                id=item.ID,
                path=get_item_path(item, by_id),
                name=item.VissibleName,
                kind="folder" if item.is_folder else get_file_type(None, item),
                hash=getattr(item, "hash", "") or "",
                modified=item.ModifiedClient,
                page_count=None if item.is_folder else _page_count(item),
                stale=bool(getattr(item, "stale", False)),
                parent=getattr(item, "Parent", "") or "",
            )

    def get(self, doc_id: str) -> Optional[LibraryEntry]:
        return self._entries.get(doc_id)

    def __contains__(self, doc_id: str) -> bool:
        return doc_id in self._entries

    def __iter__(self) -> Iterator[LibraryEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)


_snapshot_lock = threading.Lock()
_snapshot: Optional[tuple] = None  # (collection identity, Library)


def get_library() -> Library:
    """The library for the current cached collection, rebuilt only when it changes."""
    from rm_mcp.cache import get_cached_collection

    _client, collection = get_cached_collection()
    return library_for(collection)


def library_for(collection) -> Library:
    """The library for this collection snapshot, built once per snapshot."""
    global _snapshot
    with _snapshot_lock:
        if _snapshot is not None and _snapshot[0] is collection:
            return _snapshot[1]
    library = Library(collection)
    with _snapshot_lock:
        _snapshot = (collection, library)
    return library
