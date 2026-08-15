"""
Library mutations: rename, move, and create folder.

## How a change is made

The library is a Merkle tree. Every blob is addressed by the SHA-256 of its
content, a document's index lists its blobs, and the root index lists every
document. Changing anything means rewriting that chain upwards:

    .metadata          edit and upload      -> new blob hash
    <doc>.docSchema    relist and upload    -> new document hash
    root.docSchema     relist and upload    -> new root hash
    PUT /sync/v3/root  commit               <- the only step that changes anything

Everything before the final commit is an upload of content nothing references
yet, so a failure partway through leaves the library exactly as it was. The
commit carries the generation the change was based on, so a concurrent edit
from the tablet is rejected rather than overwritten.

## Recovery

Because blobs are immutable, the previous root hash remains a complete,
valid snapshot of the library. Every mutation returns the root hash it
replaced, and logs it, so any change can be undone by pointing the root back
at it.
"""

import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from rm_mcp.clients.cloud import (
    ROOT_INDEX_FILENAME,
    ROOT_INDEX_OWNER,
    RootConflict,
    _index_filename,
    serialize_index,
    serialize_metadata,
)

logger = logging.getLogger(__name__)

# A conflicting write from the tablet is transient; re-read and re-apply.
_MAX_COMMIT_ATTEMPTS = 3


class LibraryWriter:
    """Applies structural changes to a reMarkable library."""

    def __init__(self, client):
        self.client = client

    # -----------------------------------------------------------------
    # Metadata helpers
    # -----------------------------------------------------------------

    def _read_metadata(self, doc_entries: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], str]:
        """Read a document's metadata blob. Returns (metadata, filename)."""
        for entry in doc_entries:
            if entry["id"].endswith(".metadata"):
                raw = self.client._get_file(entry["hash"], entry["id"])
                return json.loads(raw.decode("utf-8")), entry["id"]
        raise RuntimeError("Document has no .metadata file — refusing to modify it.")

    def _rewrite_document(
        self,
        root_entries: List[Dict[str, Any]],
        doc_id: str,
        mutate: Callable[[Dict[str, Any]], None],
    ) -> List[Dict[str, Any]]:
        """Apply a metadata change to one document and relist its index.

        Returns the updated root entries. Nothing is committed here.
        """
        root_entry = next((e for e in root_entries if e["id"] == doc_id), None)
        if root_entry is None:
            raise RuntimeError(f"Document {doc_id} is not in the library index.")

        doc_entries = self.client.get_index(root_entry["hash"], doc_id)
        metadata, meta_filename = self._read_metadata(doc_entries)

        mutate(metadata)
        # The tablet uses this to decide which side of a sync is newer.
        metadata["lastModified"] = str(int(time.time() * 1000))

        meta_body = serialize_metadata(metadata)
        meta_hash = self.client.upload_blob(meta_body, meta_filename)

        updated_entries = [
            {**e, "hash": meta_hash, "size": len(meta_body)} if e["id"] == meta_filename else e
            for e in doc_entries
        ]

        doc_body = serialize_index(updated_entries, doc_id)
        doc_hash = self.client.upload_blob(doc_body, _index_filename(doc_id))

        return [
            {
                **e,
                "hash": doc_hash,
                "subfiles": len(updated_entries),
                "size": sum(int(x["size"]) for x in updated_entries),
            }
            if e["id"] == doc_id
            else e
            for e in root_entries
        ]

    # -----------------------------------------------------------------
    # Commit
    # -----------------------------------------------------------------

    def _commit(self, root_entries: List[Dict[str, Any]], generation: int) -> Tuple[str, int]:
        """Upload the new root index and point the library at it."""
        # Entries are stored sorted by document id.
        ordered = sorted(root_entries, key=lambda e: e["id"])
        body = serialize_index(ordered, ROOT_INDEX_OWNER)
        root_hash = self.client.upload_blob(body, ROOT_INDEX_FILENAME)
        new_generation = self.client.put_root(root_hash, generation)
        return root_hash, new_generation

    def _apply(
        self, operation: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        """Run an operation against a fresh tree and commit it.

        Retries from a fresh read if the library changed underneath us; the
        rejected attempt changed nothing.
        """
        last_error: Optional[RootConflict] = None

        for attempt in range(1, _MAX_COMMIT_ATTEMPTS + 1):
            previous_root, generation = self.client.get_root_info()
            root_entries = self.client.get_index(previous_root, ROOT_INDEX_OWNER)

            updated = operation(root_entries)

            try:
                new_root, new_generation = self._commit(updated, generation)
            except RootConflict as exc:
                last_error = exc
                logger.warning("Root conflict on attempt %d — re-reading and retrying", attempt)
                continue

            logger.info(
                "Library updated: root %s -> %s (generation %d). "
                "The previous root remains a complete snapshot.",
                previous_root[:12],
                new_root[:12],
                new_generation,
            )
            return {
                "previous_root": previous_root,
                "root": new_root,
                "generation": new_generation,
            }

        raise last_error or RuntimeError("Could not commit the change.")

    # -----------------------------------------------------------------
    # Operations
    # -----------------------------------------------------------------

    def rename(self, doc_id: str, new_name: str) -> Dict[str, Any]:
        """Change a document or folder's visible name."""

        def op(root_entries):
            return self._rewrite_document(
                root_entries, doc_id, lambda m: m.__setitem__("visibleName", new_name)
            )

        return self._apply(op)

    def move(self, doc_id: str, new_parent_id: str) -> Dict[str, Any]:
        """Move a document or folder into another folder ("" is the root)."""

        def op(root_entries):
            return self._rewrite_document(
                root_entries, doc_id, lambda m: m.__setitem__("parent", new_parent_id)
            )

        return self._apply(op)

    def create_folder(self, name: str, parent_id: str = "") -> Dict[str, Any]:
        """Create an empty folder. A folder is a single .metadata blob."""
        folder_id = str(uuid.uuid4())
        now = str(int(time.time() * 1000))
        metadata = {
            "createdTime": now,
            "lastModified": now,
            "parent": parent_id,
            "pinned": False,
            "type": "CollectionType",
            "visibleName": name,
        }

        def op(root_entries):
            meta_body = serialize_metadata(metadata)
            meta_filename = f"{folder_id}.metadata"
            meta_hash = self.client.upload_blob(meta_body, meta_filename)

            doc_entries = [
                {
                    "hash": meta_hash,
                    "type": "0",
                    "id": meta_filename,
                    "subfiles": 0,
                    "size": len(meta_body),
                }
            ]
            doc_body = serialize_index(doc_entries, folder_id)
            doc_hash = self.client.upload_blob(doc_body, _index_filename(folder_id))

            # Root entries carry type "0", subfiles = number of files in the
            # document index, and size = their total — verified against a live
            # library where this holds for all 173 entries.
            return root_entries + [
                {
                    "hash": doc_hash,
                    "type": "0",
                    "id": folder_id,
                    "subfiles": len(doc_entries),
                    "size": len(meta_body),
                }
            ]

        result = self._apply(op)
        result["folder_id"] = folder_id
        return result
