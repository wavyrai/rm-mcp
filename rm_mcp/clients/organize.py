"""
Library mutations: metadata edits (rename, move), new documents and folders,
and restoring earlier document versions (undo).

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

## The root guard

Before the new root index is uploaded and committed, `verify_root_change`
parses the old and new root index bytes and checks that they differ in exactly
the intended entries: the added ids (a new document or folder) and a changed
hash/size/subfile count for the touched ids. An entry lost, added, retyped or
altered anywhere else aborts the batch with nothing committed.

## Recovery

Because blobs are immutable, the previous root hash remains a complete,
valid snapshot of the library. Every commit returns and logs the root hash it
replaced, and the undo record keeps it.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from rm_mcp.clients.cloud import (
    ROOT_INDEX_FILENAME,
    ROOT_INDEX_OWNER,
    RootConflict,
    _index_filename,
    parse_index,
    serialize_index,
    serialize_metadata,
)

logger = logging.getLogger(__name__)

# A conflicting write from the tablet is transient; re-read and re-apply.
_MAX_COMMIT_ATTEMPTS = 3


class RootGuardError(RuntimeError):
    """The new root index differs from the old one beyond the intended change."""


class ChangedSinceRead(RuntimeError):
    """A document the change is based on is different now. Nothing was committed."""


@dataclass
class MetadataEdit:
    """Set metadata fields of one existing document or folder."""

    doc_id: str
    fields: Dict[str, Any]
    expected_hash: Optional[str] = None  # the version the change was planned against


@dataclass
class NewDocument:
    """A document or folder that does not exist yet: its files, uploaded as given."""

    doc_id: str
    files: List[Tuple[str, bytes]] = field(default_factory=list)


@dataclass
class Restore:
    """Point one entry back at an earlier version (undo)."""

    doc_id: str
    entry: Dict[str, Any]  # the root entry to restore
    expected_hash: str  # the entry's hash now; anything else means it changed since


def _entry_key(e: Dict[str, Any]) -> Tuple:
    return (e["hash"], str(e["type"]), e["id"], int(e["subfiles"]), int(e["size"]))


def verify_root_change(
    old_body: bytes, new_body: bytes, added: Iterable[str], changed: Iterable[str]
) -> None:
    """Refuse a root index that differs from the old one beyond the intended entries.

    Allowed: the ids in `added` are new; the ids in `changed` keep their id and
    type and may change hash, subfile count and size. Every other entry must be
    byte-for-byte the same, nothing may disappear, and the index must be sorted
    by id with no duplicates (the way it is stored).
    """
    added, changed = set(added), set(changed)
    old = parse_index(old_body)
    new = parse_index(new_body)
    old_by_id = {e["id"]: e for e in old}
    new_ids = [e["id"] for e in new]
    new_by_id = {e["id"]: e for e in new}

    problems: List[str] = []
    if len(new_ids) != len(new_by_id):
        problems.append("duplicate ids in the new index")
    if new_ids != sorted(new_ids):
        problems.append("the new index is not sorted by id")
    header = new_body.decode("utf-8").split("\n")[:2]
    total = sum(int(e["size"]) for e in new)
    if header[1:] != [f"0:{ROOT_INDEX_OWNER}:{len(new)}:{total}"]:
        problems.append("the new index header does not match its entries")

    lost = set(old_by_id) - set(new_by_id)
    if lost:
        problems.append(f"entries removed: {sorted(lost)}")
    appeared = set(new_by_id) - set(old_by_id)
    if appeared != added:
        problems.append(f"entries added {sorted(appeared)}, expected exactly {sorted(added)}")
    for doc_id, entry in new_by_id.items():
        before = old_by_id.get(doc_id)
        if before is None:
            continue
        if doc_id in changed:
            if str(entry["type"]) != str(before["type"]):
                problems.append(f"entry {doc_id} changed type")
        elif _entry_key(entry) != _entry_key(before):
            problems.append(f"unrelated entry {doc_id} changed")
    if problems:
        raise RootGuardError(
            "Refused to commit: the new root index is not the intended change ("
            + "; ".join(problems)
            + "). Nothing was committed."
        )


class LibraryWriter:
    """Applies changes to a reMarkable library, one root commit per batch."""

    def __init__(self, client):
        self.client = client
        self._uploaded: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # -----------------------------------------------------------------
    # Building blocks
    # -----------------------------------------------------------------

    def _read_metadata(self, doc_entries: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], str]:
        """Read a document's metadata blob. Returns (metadata, filename)."""
        for entry in doc_entries:
            if entry["id"].endswith(".metadata"):
                raw = self.client._get_file(entry["hash"], entry["id"])
                return json.loads(raw.decode("utf-8")), entry["id"]
        raise RuntimeError("Document has no .metadata file — refusing to modify it.")

    def _edited_entry(self, root_entry: Dict[str, Any], edit: MetadataEdit) -> Dict[str, Any]:
        """Upload the edited metadata and document index; return the new root entry.

        Uploads are content-addressed, so a retry of the same edit on the same
        version reuses them.
        """
        key = (root_entry["hash"], json.dumps(edit.fields, sort_keys=True))
        if key in self._uploaded:
            return self._uploaded[key]
        doc_id = edit.doc_id
        doc_entries = self.client.get_index(root_entry["hash"], doc_id)
        metadata, meta_filename = self._read_metadata(doc_entries)

        # `lastModified` is deliberately left alone. It answers "when did I
        # last work in this document", and renaming or moving is not that —
        # bumping it would flatten the recently-used ordering for every item
        # touched in a bulk reorganisation. Sync does not depend on it: the
        # tablet picks up the change because the document's hash changed.
        metadata.update(edit.fields)

        meta_body = serialize_metadata(metadata)
        meta_hash = self.client.upload_blob(meta_body, meta_filename)
        updated = [
            {**e, "hash": meta_hash, "size": len(meta_body)} if e["id"] == meta_filename else e
            for e in doc_entries
        ]
        doc_body = serialize_index(updated, doc_id)
        doc_hash = self.client.upload_blob(doc_body, _index_filename(doc_id))
        result = {
            **root_entry,
            "hash": doc_hash,
            "subfiles": len(updated),
            "size": sum(int(x["size"]) for x in updated),
        }
        self._uploaded[key] = result
        return result

    def _upload_new(self, new: NewDocument) -> Dict[str, Any]:
        """Upload a new document's files and index; return its root entry."""
        files = sorted(new.files, key=lambda f: f[0])
        entries = [
            {
                "hash": self.client.upload_blob(data, name),
                "type": "0",
                "id": name,
                "subfiles": 0,
                "size": len(data),
            }
            for name, data in files
        ]
        doc_body = serialize_index(entries, new.doc_id)
        doc_hash = self.client.upload_blob(doc_body, _index_filename(new.doc_id))
        # Root entries carry type "0", subfiles = number of files in the
        # document index, and size = their total — verified against a live
        # library where this holds for all 173 entries.
        return {
            "hash": doc_hash,
            "type": "0",
            "id": new.doc_id,
            "subfiles": len(entries),
            "size": sum(e["size"] for e in entries),
        }

    # -----------------------------------------------------------------
    # Commit
    # -----------------------------------------------------------------

    def _commit(
        self,
        old_body: bytes,
        new_entries: List[Dict[str, Any]],
        generation: int,
        added: Iterable[str],
        changed: Iterable[str],
    ) -> Tuple[str, int]:
        """Guard, upload the new root index and point the library at it."""
        # Entries are stored sorted by document id.
        ordered = sorted(new_entries, key=lambda e: e["id"])
        body = serialize_index(ordered, ROOT_INDEX_OWNER)
        verify_root_change(old_body, body, added, changed)
        root_hash = self.client.upload_blob(body, ROOT_INDEX_FILENAME)
        new_generation = self.client.put_root(root_hash, generation)
        return root_hash, new_generation

    def _run(
        self,
        build: Callable[[Dict[str, Dict[str, Any]]], Tuple[List[Dict[str, Any]], Set[str]]],
        added: Set[str],
    ) -> Dict[str, Any]:
        """Read the root, build the new entries, guard and commit; retry on conflict.

        `build(entries_by_id)` returns (new entry list, ids it changed) and
        raises ChangedSinceRead when a planned-against version is gone. A
        rejected commit changed nothing, so it is safe to re-read and rebuild.
        """
        last_error: Optional[RootConflict] = None
        for attempt in range(1, _MAX_COMMIT_ATTEMPTS + 1):
            previous_root, generation = self.client.get_root_info()
            old_body = self.client._get_file(previous_root, ROOT_INDEX_FILENAME)
            by_id = {e["id"]: e for e in parse_index(old_body)}
            new_entries, changed = build(by_id)
            try:
                new_root, new_generation = self._commit(
                    old_body, new_entries, generation, added, changed
                )
            except RootConflict as exc:
                last_error = exc
                logger.warning("Root conflict on attempt %d — re-reading and retrying", attempt)
                continue
            logger.info(
                "Library updated: root %s -> %s (generation %d). "
                "The previous root %s remains a complete snapshot.",
                previous_root[:12],
                new_root[:12],
                new_generation,
                previous_root,
            )
            new_by_id = {e["id"]: e for e in new_entries}
            return {
                "previous_root": previous_root,
                "root": new_root,
                "generation": new_generation,
                "before": {i: by_id[i] for i in changed if i in by_id},
                "after": {i: new_by_id[i] for i in set(changed) | added},
            }
        raise last_error or RuntimeError("Could not commit the change.")

    # -----------------------------------------------------------------
    # Batches
    # -----------------------------------------------------------------

    def apply(
        self, edits: List[MetadataEdit], new_docs: Optional[List[NewDocument]] = None
    ) -> Dict[str, Any]:
        """Commit metadata edits and new documents/folders in one root commit."""
        new_docs = list(new_docs or [])
        ids = [e.doc_id for e in edits] + [n.doc_id for n in new_docs]
        if len(ids) != len(set(ids)):
            raise ValueError("A batch may touch each document once.")
        new_entries = [self._upload_new(n) for n in new_docs]
        added = {n.doc_id for n in new_docs}

        def build(by_id):
            for edit in edits:
                current = by_id.get(edit.doc_id)
                if current is None:
                    raise RuntimeError(f"Document {edit.doc_id} is not in the library index.")
                if edit.expected_hash and current["hash"] != edit.expected_hash:
                    raise ChangedSinceRead(
                        f"{edit.doc_id} changed since the plan was made (on the tablet or "
                        "elsewhere). Nothing was committed; plan again."
                    )
            for doc_id in added:
                if doc_id in by_id:
                    raise RuntimeError(f"Id {doc_id} already exists; nothing was committed.")
            replaced = {e.doc_id: self._edited_entry(by_id[e.doc_id], e) for e in edits}
            entries = [replaced.get(i, e) for i, e in by_id.items()] + new_entries
            return entries, set(replaced)

        return self._run(build, added)

    def restore(self, restores: List[Restore]) -> Dict[str, Any]:
        """Point entries back at earlier versions, if they are still the expected ones."""

        def build(by_id):
            for r in restores:
                current = by_id.get(r.doc_id)
                if current is None or current["hash"] != r.expected_hash:
                    raise ChangedSinceRead(
                        f"{r.doc_id} changed since the batch was applied; undo refused. "
                        "Nothing was committed."
                    )
                # The earlier version must still be readable before we point at it.
                self.client.get_index(r.entry["hash"], r.doc_id)
            replaced = {r.doc_id: dict(r.entry) for r in restores}
            entries = [replaced.get(i, e) for i, e in by_id.items()]
            return entries, set(replaced)

        return self._run(build, set())

    # -----------------------------------------------------------------
    # Single operations (used by tests and simple callers)
    # -----------------------------------------------------------------

    def rename(self, doc_id: str, new_name: str) -> Dict[str, Any]:
        """Change a document or folder's visible name."""
        return self.apply([MetadataEdit(doc_id, {"visibleName": new_name})])

    def move(self, doc_id: str, new_parent_id: str) -> Dict[str, Any]:
        """Move a document or folder into another folder ("" is the root)."""
        return self.apply([MetadataEdit(doc_id, {"parent": new_parent_id})])

    def create_folder(self, name: str, parent_id: str = "") -> Dict[str, Any]:
        """Create an empty folder. A folder is a single .metadata blob."""
        folder = new_folder(name, parent_id)
        result = self.apply([], [folder])
        result["folder_id"] = folder.doc_id
        return result


def now_ms() -> str:
    return str(int(time.time() * 1000))


def new_folder(name: str, parent_id: str, folder_id: Optional[str] = None) -> NewDocument:
    """A folder as the tablet stores it: one .metadata blob."""
    folder_id = folder_id or str(uuid.uuid4())
    now = now_ms()
    metadata = {
        "createdTime": now,
        "lastModified": now,
        "parent": parent_id,
        "pinned": False,
        "type": "CollectionType",
        "visibleName": name,
    }
    return NewDocument(folder_id, [(f"{folder_id}.metadata", serialize_metadata(metadata))])


def new_document(
    name: str, parent_id: str, data: bytes, kind: str, page_count: int, doc_id: Optional[str] = None
) -> NewDocument:
    """A PDF or EPUB document with the blob set rmapi uploads: file, .content, .metadata.

    The .content mirrors rmapi's (ddvk/rmapi archive.createZipContent) plus the
    page count; the tablet adds cPages when it first opens the document.
    """
    if kind not in ("pdf", "epub"):
        raise ValueError(f"Unsupported document type: {kind}")
    doc_id = doc_id or str(uuid.uuid4())
    now = now_ms()
    content = {
        "dummyDocument": False,
        "extraMetadata": {
            "LastFinelinerv2Size": "1",
            "LastPen": "Finelinerv2",
            "LastTool": "Finelinerv2",
        },
        "fileType": kind,
        "fontName": "",
        "lastOpenedPage": 0,
        "lineHeight": -1,
        "margins": 180,
        "orientation": "portrait",
        "pageCount": page_count,
        "textScale": 1,
    }
    metadata = {
        "createdTime": now,
        "lastModified": now,
        "lastOpened": "0",
        "lastOpenedPage": 0,
        "parent": parent_id,
        "pinned": False,
        "type": "DocumentType",
        "visibleName": name,
    }
    return NewDocument(
        doc_id,
        [
            (f"{doc_id}.{kind}", data),
            (f"{doc_id}.content", (json.dumps(content, indent=4, sort_keys=True) + "\n").encode()),
            (f"{doc_id}.metadata", serialize_metadata(metadata)),
        ],
    )
