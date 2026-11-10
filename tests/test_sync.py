"""
Incremental sync, the content-addressed blob store and hash-keyed derived caches.

The cloud is faked in-process at the HTTP session level, so the real client
code runs end to end and every request is counted by URL.
"""

import hashlib
import io
import json
import zipfile
from collections import Counter
from typing import Dict, Optional

import pytest

import rm_mcp.cache as cache_mod
import rm_mcp.index as index_mod
from rm_mcp.blobstore import BlobHashMismatch, BlobStore
from rm_mcp.clients.cloud import FILES_URL, ROOT_URL, RemarkableClient, serialize_index


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b""):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeCloud:
    """An in-process reMarkable sync API: a blob store plus a root pointer."""

    def __init__(self):
        self.blobs: Dict[str, bytes] = {}
        self.entries: Dict[str, dict] = {}  # doc_id -> root index entry
        self.root_hash = ""
        self.requests: Counter = Counter()
        self.failing: set = set()  # blob hashes that answer 500
        self.corrupt: set = set()  # blob hashes whose bytes come back altered

    # --- building the library ---

    def _put(self, data: bytes) -> str:
        digest = sha(data)
        self.blobs[digest] = data
        return digest

    def set_doc(
        self,
        doc_id: str,
        name: str,
        parent: str = "",
        pages: Optional[Dict[str, bytes]] = None,
        folder: bool = False,
    ) -> None:
        pages = pages if pages is not None else {"p1": f"strokes of {doc_id}".encode()}
        metadata = json.dumps(
            {
                "visibleName": name,
                "type": "CollectionType" if folder else "DocumentType",
                "parent": parent,
                "lastModified": "1750000000000",
            }
        ).encode()
        content = json.dumps({"cPages": {"pages": [{"id": pid} for pid in pages]}}).encode()
        files = [(f"{doc_id}.metadata", metadata), (f"{doc_id}.content", content)]
        files += [(f"{doc_id}/{pid}.rm", data) for pid, data in pages.items()]
        doc_entries = [
            {"hash": self._put(data), "type": "0", "id": fid, "subfiles": 0, "size": len(data)}
            for fid, data in files
        ]
        index_bytes = serialize_index(doc_entries, doc_id)
        self.entries[doc_id] = {
            "hash": self._put(index_bytes),
            "type": "0",
            "id": doc_id,
            "subfiles": len(doc_entries),
            "size": sum(e["size"] for e in doc_entries),
        }

    def remove_doc(self, doc_id: str) -> None:
        del self.entries[doc_id]

    def commit(self) -> None:
        self.root_hash = self._put(serialize_index(list(self.entries.values()), "."))

    def doc_index_hash(self, doc_id: str) -> str:
        return self.entries[doc_id]["hash"]

    # --- the session surface requests uses ---

    def mount(self, *args, **kwargs):
        pass

    def request(self, method, url, headers=None, timeout=None, data=None, json=None):
        self.requests[url] += 1
        if url == ROOT_URL:
            body = _json_dumps({"hash": self.root_hash, "generation": 1})
            return FakeResponse(content=body.encode())
        digest = url.rsplit("/", 1)[-1]
        if digest in self.failing:
            return FakeResponse(status_code=500, content=b"transient")
        if digest not in self.blobs:
            return FakeResponse(status_code=404, content=b"not found")
        payload = self.blobs[digest]
        if digest in self.corrupt:
            payload = payload + b"tampered"
        return FakeResponse(content=payload)

    # --- counting ---

    def total(self) -> int:
        return sum(self.requests.values())

    def reset_counts(self) -> None:
        self.requests.clear()


def _json_dumps(value) -> str:
    return json.dumps(value)


def blob_url(digest: str) -> str:
    return f"{FILES_URL}/{digest}"


def metadata_hash(cloud: FakeCloud, doc_id: str) -> str:
    index = cloud.blobs[cloud.doc_index_hash(doc_id)].decode()
    for line in index.splitlines():
        parts = line.split(":")
        if len(parts) == 5 and parts[2].endswith(".metadata"):
            return parts[0]
    raise AssertionError("no metadata entry")


def make_library(n: int) -> FakeCloud:
    cloud = FakeCloud()
    for i in range(n):
        cloud.set_doc(f"doc-{i:04d}", f"Note {i}")
    cloud.commit()
    return cloud


@pytest.fixture
def state(tmp_path):
    index = index_mod.DocumentIndex(str(tmp_path / "state.db"))
    yield index
    index.close()


def make_client(cloud: FakeCloud, state, tmp_path, blob_dir: str = "blobs") -> RemarkableClient:
    client = RemarkableClient(
        device_token="",
        user_token="token",
        blob_store=BlobStore(tmp_path / blob_dir, max_bytes=500 * 1024 * 1024),
        state_store=state,
    )
    client._session = cloud
    return client


def by_id(docs) -> Dict[str, object]:
    return {d.id: d for d in docs}


# =============================================================================
# Incremental sync
# =============================================================================


class TestIncrementalSync:
    def test_cold_start_fetches_every_blob_once(self, tmp_path, state):
        cloud = make_library(1000)
        client = make_client(cloud, state, tmp_path)

        docs = client.get_meta_items()

        assert len(docs) == 1000
        # root pointer + root index + (document index + metadata) per document
        assert cloud.total() == 2 + 2 * 1000
        assert max(cloud.requests.values()) == 1

    def test_one_changed_document_costs_only_its_own_blobs(self, tmp_path, state):
        cloud = make_library(1000)
        client = make_client(cloud, state, tmp_path)
        client.get_meta_items()
        cloud.reset_counts()

        cloud.set_doc("doc-0500", "Renamed note")
        cloud.commit()
        docs = client.get_meta_items()

        assert by_id(docs)["doc-0500"].name == "Renamed note"
        # The root pointer, the new root index, then only this document's
        # index and metadata. Nothing for the other 999.
        assert cloud.requests == Counter(
            {
                ROOT_URL: 1,
                blob_url(cloud.root_hash): 1,
                blob_url(cloud.doc_index_hash("doc-0500")): 1,
                blob_url(metadata_hash(cloud, "doc-0500")): 1,
            }
        )
        assert cloud.total() <= 4

    def test_unchanged_root_costs_one_request(self, tmp_path, state):
        cloud = make_library(50)
        client = make_client(cloud, state, tmp_path)
        client.get_meta_items()
        cloud.reset_counts()

        docs = client.get_meta_items()

        assert len(docs) == 50
        assert cloud.total() == 1

    def test_restart_with_persisted_state_and_no_changes_costs_one_request(self, tmp_path, state):
        cloud = make_library(1000)
        make_client(cloud, state, tmp_path).get_meta_items()
        cloud.reset_counts()

        # A new process: fresh client, fresh (empty) blob store, same SQLite db.
        restarted = make_client(cloud, state, tmp_path, blob_dir="other-blobs")
        docs = restarted.get_meta_items()

        assert len(docs) == 1000
        assert cloud.requests == Counter({ROOT_URL: 1})
        assert by_id(docs)["doc-0007"].name == "Note 7"

    def test_restart_with_one_change_is_incremental(self, tmp_path, state):
        cloud = make_library(200)
        make_client(cloud, state, tmp_path).get_meta_items()
        cloud.set_doc("doc-0003", "Edited while we were down")
        cloud.commit()
        cloud.reset_counts()

        docs = make_client(cloud, state, tmp_path).get_meta_items()

        assert by_id(docs)["doc-0003"].name == "Edited while we were down"
        assert cloud.total() <= 4

    def test_deleted_document_disappears(self, tmp_path, state):
        cloud = make_library(20)
        client = make_client(cloud, state, tmp_path)
        client.get_meta_items()

        cloud.remove_doc("doc-0011")
        cloud.commit()
        docs = client.get_meta_items()

        assert "doc-0011" not in by_id(docs)
        assert len(docs) == 19
        # ...and stays gone after a restart
        restarted = make_client(cloud, state, tmp_path).get_meta_items()
        assert "doc-0011" not in by_id(restarted)
        assert client.get_doc("doc-0011") is None

    def test_transient_metadata_failure_keeps_the_previous_version(self, tmp_path, state):
        cloud = make_library(20)
        client = make_client(cloud, state, tmp_path)
        client.get_meta_items()

        cloud.set_doc("doc-0004", "New name")
        cloud.commit()
        cloud.failing.add(metadata_hash(cloud, "doc-0004"))
        docs = by_id(client.get_meta_items())

        assert docs["doc-0004"].name == "Note 4"
        assert docs["doc-0004"].stale is True
        assert len(docs) == 20

        # Once the cloud recovers, the next sync retries the document even
        # though the root hash has not moved.
        cloud.failing.clear()
        docs = by_id(client.get_meta_items())
        assert docs["doc-0004"].name == "New name"
        assert docs["doc-0004"].stale is False

    def test_transient_index_failure_keeps_the_previous_version(self, tmp_path, state):
        cloud = make_library(20)
        client = make_client(cloud, state, tmp_path)
        client.get_meta_items()

        cloud.set_doc("doc-0009", "Changed")
        cloud.commit()
        cloud.failing.add(cloud.doc_index_hash("doc-0009"))
        docs = by_id(client.get_meta_items())

        assert docs["doc-0009"].name == "Note 9"
        assert docs["doc-0009"].stale is True

    def test_failed_document_is_retried_after_a_restart(self, tmp_path, state):
        cloud = make_library(10)
        client = make_client(cloud, state, tmp_path)
        client.get_meta_items()
        cloud.set_doc("doc-0002", "Retry me")
        cloud.commit()
        cloud.failing.add(metadata_hash(cloud, "doc-0002"))
        client.get_meta_items()

        cloud.failing.clear()
        docs = by_id(make_client(cloud, state, tmp_path).get_meta_items())
        assert docs["doc-0002"].name == "Retry me"

    def test_changed_document_loses_its_old_searchable_text(self, tmp_path, state):
        cloud = make_library(5)
        client = make_client(cloud, state, tmp_path)
        client.get_meta_items()
        state.upsert_document("doc-0001", doc_hash=cloud.doc_index_hash("doc-0001"))
        state.upsert_page("doc-0001", 0, "words that get deleted", "typed_text")

        cloud.set_doc("doc-0001", "Rewritten")
        cloud.commit()
        client.get_meta_items()

        assert state.search("deleted") == []
        assert state.get_document_hash("doc-0001") == cloud.doc_index_hash("doc-0001")

    def test_rename_and_move_keep_the_id(self, tmp_path, state):
        from rm_mcp.library import Library

        cloud = FakeCloud()
        cloud.set_doc("folder-a", "Work", folder=True)
        cloud.set_doc("folder-b", "Archive", folder=True)
        cloud.set_doc("n1", "Draft", parent="folder-a")
        cloud.commit()
        client = make_client(cloud, state, tmp_path)
        before = Library(client.get_meta_items()).get("n1")

        cloud.set_doc("n1", "Final", parent="folder-b")
        cloud.commit()
        after = Library(client.get_meta_items()).get("n1")

        assert before.path == "/Work/Draft"
        assert after.path == "/Archive/Final"
        assert after.id == before.id == "n1"
        assert after.name == "Final"
        assert after.kind == "notebook"
        assert after.hash == cloud.doc_index_hash("n1")
        assert after.hash != before.hash


class TestCollectionCacheUsesIncrementalSync:
    def test_ttl_expiry_with_one_change(self, tmp_path, state, monkeypatch):
        import rm_mcp.api as api_mod

        cloud = make_library(300)
        client = make_client(cloud, state, tmp_path)
        monkeypatch.setattr(api_mod, "_client_singleton", client)
        monkeypatch.setattr(cache_mod, "_CACHE_TTL_SECONDS", 0)
        cache_mod.invalidate_collection_cache()
        try:
            cache_mod.get_cached_collection()
            cloud.set_doc("doc-0100", "Changed")
            cloud.commit()
            cloud.reset_counts()

            _client, collection = cache_mod.get_cached_collection()

            assert by_id(collection)["doc-0100"].name == "Changed"
            assert cloud.total() <= 4
        finally:
            cache_mod.invalidate_collection_cache()


# =============================================================================
# Downloads
# =============================================================================


class TestDownload:
    def test_download_raises_when_a_blob_fails(self, tmp_path, state):
        cloud = FakeCloud()
        cloud.set_doc("nb", "Notebook", pages={"p1": b"one", "p2": b"two"})
        cloud.commit()
        client = make_client(cloud, state, tmp_path)
        doc = client.get_meta_items()[0]

        cloud.failing.add(sha(b"two"))
        with pytest.raises(RuntimeError):
            client.download(doc)

    def test_download_reads_blobs_from_the_store(self, tmp_path, state):
        cloud = FakeCloud()
        cloud.set_doc("nb", "Notebook", pages={"p1": b"one"})
        cloud.commit()
        client = make_client(cloud, state, tmp_path)
        doc = client.get_meta_items()[0]
        client.download(doc)
        cloud.reset_counts()

        raw = client.download(doc)

        assert cloud.total() == 0
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            assert zf.read("nb/p1.rm") == b"one"

    def test_corrupted_network_bytes_are_rejected(self, tmp_path, state):
        cloud = FakeCloud()
        cloud.set_doc("nb", "Notebook", pages={"p1": b"one"})
        cloud.commit()
        client = make_client(cloud, state, tmp_path)
        doc = client.get_meta_items()[0]

        cloud.corrupt.add(sha(b"one"))
        with pytest.raises(RuntimeError):
            client.download(doc)


# =============================================================================
# Content-addressed blob store
# =============================================================================


class TestBlobStore:
    def test_round_trip(self, tmp_path):
        store = BlobStore(tmp_path / "s", max_bytes=1024)
        store.put(sha(b"hello"), b"hello")
        assert store.get(sha(b"hello")) == b"hello"

    def test_put_rejects_bytes_that_do_not_match_the_hash(self, tmp_path):
        store = BlobStore(tmp_path / "s", max_bytes=1024)
        with pytest.raises(BlobHashMismatch):
            store.put(sha(b"hello"), b"goodbye")
        assert store.get(sha(b"hello")) is None

    def test_corrupted_file_on_disk_is_never_served(self, tmp_path):
        store = BlobStore(tmp_path / "s", max_bytes=1024)
        digest = sha(b"hello")
        store.put(digest, b"hello")
        path = store.path_for(digest)
        path.write_bytes(b"hellO")

        assert store.get(digest) is None
        assert not path.exists()

    def test_evicts_least_recently_used_past_the_cap(self, tmp_path):
        store = BlobStore(tmp_path / "s", max_bytes=25)
        blobs = [bytes([65 + i]) * 10 for i in range(3)]
        store.put(sha(blobs[0]), blobs[0])
        store.put(sha(blobs[1]), blobs[1])
        store.get(sha(blobs[0]))  # 0 is now more recent than 1
        store.put(sha(blobs[2]), blobs[2])

        assert store.get(sha(blobs[1])) is None
        assert store.get(sha(blobs[0])) == blobs[0]
        assert store.get(sha(blobs[2])) == blobs[2]
        assert store.total_bytes <= 25

    def test_survives_a_restart(self, tmp_path):
        BlobStore(tmp_path / "s", max_bytes=1024).put(sha(b"kept"), b"kept")
        assert BlobStore(tmp_path / "s", max_bytes=1024).get(sha(b"kept")) == b"kept"

    def test_cap_from_config(self, tmp_path, monkeypatch):
        from rm_mcp import blobstore

        monkeypatch.setenv("REMARKABLE_BLOB_CACHE_MB", "7")
        blobstore.reset_default_store()
        assert blobstore.get_default_store().max_bytes == 7 * 1024 * 1024


# =============================================================================
# Hash-keyed derived caches
# =============================================================================


def notebook_zip(pages: Dict[str, bytes], typed: Optional[str] = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            "doc.content", json.dumps({"cPages": {"pages": [{"id": pid} for pid in pages]}})
        )
        for pid, data in pages.items():
            zf.writestr(f"doc/{pid}.rm", data)
        if typed:
            zf.writestr("doc/notes.txt", typed)
    return buffer.getvalue()


@pytest.fixture
def fresh_index(tmp_path):
    saved = index_mod._instance
    index_mod._instance = None
    index = index_mod.initialize(str(tmp_path / "index.db"))
    cache_mod.clear_extraction_cache()
    yield index
    cache_mod.clear_extraction_cache()
    index_mod.close()
    index_mod._instance = saved


class TestHashKeyedCaches:
    def test_edited_page_ocr_is_not_served(self, fresh_index):
        cache_mod.cache_page_ocr("doc", 1, "sampling", "old text", page_hash=sha(b"v1"))

        assert cache_mod.get_cached_page_ocr("doc", 1, "sampling", page_hash=sha(b"v2")) is None
        assert (
            cache_mod.get_cached_page_ocr("doc", 1, "sampling", page_hash=sha(b"v1")) == "old text"
        )

    def test_edited_page_ocr_is_not_served_after_restart(self, fresh_index):
        cache_mod.cache_page_ocr("doc", 1, "sampling", "old text", page_hash=sha(b"v1"))
        cache_mod.clear_extraction_cache()  # drop L1, as a restart would

        assert cache_mod.get_cached_page_ocr("doc", 1, "sampling", page_hash=sha(b"v2")) is None
        assert (
            cache_mod.get_cached_page_ocr("doc", 1, "sampling", page_hash=sha(b"v1")) == "old text"
        )

    def test_page_ocr_survives_edits_to_other_pages(self, fresh_index):
        cache_mod.cache_page_ocr("doc", 2, "sampling", "page two", page_hash=sha(b"p2"))
        cache_mod.clear_extraction_cache()
        # Page 1 was edited, page 2 was not: page 2 is still served.
        assert (
            cache_mod.get_cached_page_ocr("doc", 2, "sampling", page_hash=sha(b"p2")) == "page two"
        )

    def test_page_hashes_follow_page_bytes(self, tmp_path):
        from rm_mcp.extract.notebook import get_page_hashes

        path = tmp_path / "a.zip"
        path.write_bytes(notebook_zip({"p1": b"first", "p2": b"second"}))
        assert get_page_hashes(path) == [sha(b"first"), sha(b"second")]

    def test_edited_document_extraction_is_not_served(self, tmp_path, fresh_index):
        from rm_mcp.extract.notebook import extract_text_from_document_zip

        v1 = tmp_path / "v1.zip"
        v1.write_bytes(notebook_zip({"p1": b"x"}, typed="first draft"))
        v2 = tmp_path / "v2.zip"
        v2.write_bytes(notebook_zip({"p1": b"x"}, typed="second draft"))

        first = extract_text_from_document_zip(v1, doc_id="same-doc")
        second = extract_text_from_document_zip(v2, doc_id="same-doc")

        assert "first draft" in first["typed_text"]
        assert "second draft" in second["typed_text"]

    def test_unchanged_document_extraction_is_served_from_cache(
        self, tmp_path, fresh_index, monkeypatch
    ):
        from rm_mcp.extract import notebook

        v1 = tmp_path / "v1.zip"
        v1.write_bytes(notebook_zip({"p1": b"x"}, typed="draft"))
        notebook.extract_text_from_document_zip(v1, doc_id="d")

        def boom(*args, **kwargs):
            raise AssertionError("re-extracted an unchanged document")

        monkeypatch.setattr(notebook, "_get_ordered_rm_files", boom)
        assert "draft" in notebook.extract_text_from_document_zip(v1, doc_id="d")["typed_text"]
