"""
Integration tests that run the tools against a fake library and a real index.

test_server.py patches the client, the collection cache and the filesystem, so
it verifies that responses are assembled correctly but never exercises the
seams where behaviour actually breaks: documents with no timestamp, real SQLite
constraints, real ZIP archives, real PDF text, and more than one thread.

Everything here uses the real code paths end to end. The only thing faked is
the network.
"""

import io
import json
import json as _json  # kept accessible where a parameter named `json` shadows it
import zipfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import patch

import pytest

import rm_mcp.cache as cache_mod
import rm_mcp.index as index_mod
from rm_mcp.models import Document
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


# =============================================================================
# A fake library that behaves like the real cloud client
# =============================================================================


def make_notebook_zip(page_ids: List[str], typed_text: Optional[str] = None) -> bytes:
    """Build a document archive shaped like the ones the cloud returns."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            "doc.content",
            json.dumps({"cPages": {"pages": [{"id": pid} for pid in page_ids]}}),
        )
        for pid in page_ids:
            zf.writestr(f"doc/{pid}.rm", b"reMarkable .lines file\x00")
        if typed_text:
            zf.writestr("doc/notes.txt", typed_text)
    return buffer.getvalue()


def make_pdf_zip(text: str) -> bytes:
    """Build a document archive containing a real PDF with real text."""
    import fitz

    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 144), text, fontsize=14)
    pdf_bytes = pdf.tobytes()
    pdf.close()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("doc.content", json.dumps({"cPages": {"pages": []}}))
        zf.writestr("doc.pdf", pdf_bytes)
    return buffer.getvalue()


class FakeLibrary:
    """In-process stand-in for RemarkableClient.

    Implements the same surface the tools rely on, counts network calls so
    tests can assert on request volume, and stores real archive bytes.
    """

    def __init__(self, documents: List[Document], archives: Optional[dict] = None):
        self.documents = documents
        self.archives = archives or {}
        self.download_calls: List[str] = []
        self.meta_calls = 0
        self.root_hash_calls = 0
        self._root_hash = "root-hash-1"

    def get_root_hash(self) -> str:
        self.root_hash_calls += 1
        return self._root_hash

    def get_meta_items(self, limit=None, root_hash=None) -> List[Document]:
        self.meta_calls += 1
        items = self.documents
        return items[:limit] if limit is not None else items

    def download(self, doc) -> bytes:
        self.download_calls.append(doc.ID)
        if doc.ID not in self.archives:
            raise RuntimeError(f"No archive for {doc.ID}")
        return self.archives[doc.ID]


def doc(
    doc_id: str,
    name: str,
    parent: str = "",
    modified: Optional[datetime] = NOW,
    folder: bool = False,
    doc_hash: str = "h1",
) -> Document:
    return Document(
        id=doc_id,
        hash=doc_hash,
        name=name,
        doc_type="CollectionType" if folder else "DocumentType",
        parent=parent,
        last_modified=modified,
    )


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """Give every test a fresh index, empty caches, and default settings."""
    for var in (
        "REMARKABLE_ROOT_PATH",
        "REMARKABLE_COMPACT",
        "REMARKABLE_MAX_OUTPUT_CHARS",
        "REMARKABLE_PAGE_SIZE",
        "REMARKABLE_OCR_BACKEND",
    ):
        monkeypatch.delenv(var, raising=False)

    cache_mod.invalidate_collection_cache()
    cache_mod.clear_extraction_cache()
    _helpers.clear_download_cache()
    _helpers._rendered_image_cache.clear()
    _helpers._file_type_cache.clear()

    saved = index_mod._instance
    index_mod._instance = None
    index = index_mod.initialize(str(tmp_path / "index.db"))
    yield index
    index_mod.close()
    index_mod._instance = saved


def use_library(library: FakeLibrary):
    """Point the collection cache at a fake library."""
    return patch.object(_helpers, "get_cached_collection", lambda: (library, library.documents))


async def call(tool: str, **kwargs):
    """Call a tool through the MCP server and parse its JSON response."""
    result = await mcp.call_tool(tool, kwargs)
    payload = result[0][0] if isinstance(result[0], (list, tuple)) else result[0]
    return json.loads(payload.text)


# =============================================================================
# RM-01 — recent must survive documents with no modification time
# =============================================================================


class TestRecentWithMissingTimestamps:
    async def test_recent_handles_documents_without_timestamps(self):
        """A document with no lastModified must not break the sort."""
        library = FakeLibrary(
            [
                doc("a", "Has Timestamp", modified=NOW),
                doc("b", "No Timestamp", modified=None),
                doc("c", "Older", modified=NOW - timedelta(days=5)),
            ]
        )
        with use_library(library):
            data = await call("remarkable_recent", limit=10)

        assert data["count"] == 3
        names = [d["name"] for d in data["documents"]]
        # Newest first, and the undated document sorts last rather than raising
        assert names == ["Has Timestamp", "Older", "No Timestamp"]

    async def test_recent_handles_all_timestamps_missing(self):
        library = FakeLibrary([doc("a", "One", modified=None), doc("b", "Two", modified=None)])
        with use_library(library):
            data = await call("remarkable_recent", limit=10)
        assert data["count"] == 2

    async def test_recent_handles_naive_datetimes(self):
        """Mixing naive and aware datetimes must not raise."""
        naive = datetime(2026, 1, 1, 9, 0, 0)
        library = FakeLibrary([doc("a", "Aware", modified=NOW), doc("b", "Naive", modified=naive)])
        with use_library(library):
            data = await call("remarkable_recent", limit=10)
        assert [d["name"] for d in data["documents"]] == ["Aware", "Naive"]


# =============================================================================
# RM-02 — the root path filter must confine content search too
# =============================================================================


class TestRootPathConfinement:
    async def test_search_does_not_leak_indexed_content_outside_root(
        self, clean_state, monkeypatch
    ):
        """Indexed content outside REMARKABLE_ROOT_PATH must stay invisible."""
        index = clean_state
        index.upsert_document(
            doc_id="private",
            name="Private Diary",
            path="/Personal/Private Diary",
            file_type="notebook",
        )
        index.upsert_page("private", 1, "confidential salary negotiation", "ocr", "sampling")

        library = FakeLibrary(
            [
                doc("personal", "Personal", folder=True),
                doc("private", "Private Diary", parent="personal"),
                doc("work", "Work", folder=True),
                doc("report", "Report", parent="work"),
            ]
        )

        monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
        with use_library(library):
            data = await call("remarkable_search", query="confidential")

        assert "_error" in data, "content outside the root must not be returned"
        assert "Private Diary" not in json.dumps(data)

    async def test_search_returns_indexed_content_inside_root(self, clean_state, monkeypatch):
        """The same search inside the root still works."""
        index = clean_state
        index.upsert_document(
            doc_id="report", name="Report", path="/Work/Report", file_type="notebook"
        )
        index.upsert_page("report", 1, "confidential roadmap notes", "ocr", "sampling")

        library = FakeLibrary(
            [doc("work", "Work", folder=True), doc("report", "Report", parent="work")]
        )

        monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
        with use_library(library):
            data = await call("remarkable_search", query="confidential")

        assert data["count"] == 1
        # Paths are reported relative to the root
        assert data["documents"][0]["path"] == "/Report"

    async def test_search_drops_hits_for_deleted_documents(self, clean_state):
        """Content indexed for a document that no longer exists is not returned."""
        index = clean_state
        index.upsert_document(
            doc_id="gone", name="Deleted Note", path="/Deleted Note", file_type="notebook"
        )
        index.upsert_page("gone", 1, "vanished content", "ocr", "sampling")

        library = FakeLibrary([doc("kept", "Kept Note")])
        with use_library(library):
            data = await call("remarkable_search", query="vanished")

        assert "_error" in data


# =============================================================================
# RM-03 — index writes must actually land
# =============================================================================


class TestIndexWritesLand:
    def test_page_write_without_document_row(self, clean_state):
        """Writing a page for an unregistered document must not be dropped."""
        index = clean_state
        index.upsert_page("never-registered", 1, "hello world", "ocr", "sampling")
        assert index.get_page_ocr("never-registered", 1) == "hello world"

    def test_extraction_write_without_document_row(self, clean_state):
        index = clean_state
        index.store_extraction_result(
            "never-registered", {"typed_text": ["alpha beta"], "handwritten_text": []}
        )
        assert index.get_preview("never-registered") == "alpha beta"

    def test_handwritten_text_keeps_page_granularity(self, clean_state):
        """Per-page OCR must be retrievable per page, not as one blob."""
        index = clean_state
        index.store_extraction_result(
            "doc",
            {
                "handwritten_text": ["page one text", "", "page three text"],
                "ocr_backend": "sampling",
            },
        )
        assert index.get_page_ocr("doc", 1) == "page one text"
        assert index.get_page_ocr("doc", 3) == "page three text"

    def test_cache_layer_persists_page_ocr(self, clean_state):
        """The cache helper writes through to the index for a fresh document."""
        cache_mod.cache_page_ocr("fresh-doc", 2, "sampling", "transcribed text")
        cache_mod._page_ocr_cache.clear()  # force an L2 read
        assert cache_mod.get_cached_page_ocr("fresh-doc", 2, "sampling") == "transcribed text"

    def test_write_with_nothing_to_store_releases_the_lock(self, clean_state):
        """An empty extraction must not leave a transaction open.

        A connection that returns while holding the write lock blocks every
        other thread's writes until the busy timeout expires — five seconds of
        stall per write, with nothing in the logs to explain it.
        """
        import threading
        import time

        index = clean_state
        # Nothing extractable: the write path has no rows to insert
        index.store_extraction_result("empty-doc", {"typed_text": [], "handwritten_text": []})

        elapsed = {}

        def other_thread_write():
            start = time.monotonic()
            index.upsert_document(doc_id="other", name="Other")
            elapsed["seconds"] = time.monotonic() - start

        thread = threading.Thread(target=other_thread_write)
        thread.start()
        thread.join(timeout=10)

        assert "seconds" in elapsed, "write from another thread never completed"
        assert elapsed["seconds"] < 1.0, (
            f"write blocked for {elapsed['seconds']:.1f}s — a lock was left held"
        )

    def test_failed_write_does_not_hold_the_lock(self, clean_state):
        """A write that raises must roll back rather than keep the transaction."""
        import threading
        import time

        index = clean_state
        with pytest.raises(Exception):
            # An unbindable value fails partway through the write
            index.upsert_page("doc", 1, object(), "ocr", "sampling")

        elapsed = {}

        def other_thread_write():
            start = time.monotonic()
            index.upsert_document(doc_id="other", name="Other")
            elapsed["seconds"] = time.monotonic() - start

        thread = threading.Thread(target=other_thread_write)
        thread.start()
        thread.join(timeout=10)

        assert elapsed.get("seconds", 99) < 1.0

    def test_prune_removes_documents_no_longer_present(self, clean_state):
        index = clean_state
        index.upsert_document(doc_id="keep", name="Keep", path="/Keep")
        index.upsert_page("keep", 1, "kept content", "ocr", "sampling")
        index.upsert_document(doc_id="drop", name="Drop", path="/Drop")
        index.upsert_page("drop", 1, "dropped content", "ocr", "sampling")

        assert index.prune_missing({"keep"}) == 1
        assert index.search("dropped") == []
        assert len(index.search("kept")) == 1


# =============================================================================
# RM-09 — an in-memory index must be shared across threads
# =============================================================================


class TestInMemoryIndexAcrossThreads:
    def test_memory_index_is_visible_from_another_thread(self):
        import threading

        index = index_mod.DocumentIndex(":memory:")
        try:
            index.upsert_page("doc", 1, "written on the main thread", "ocr", "sampling")

            result = {}

            def read():
                try:
                    result["value"] = index.get_page_ocr("doc", 1)
                except Exception as exc:  # pragma: no cover - failure path
                    result["value"] = f"{type(exc).__name__}: {exc}"

            thread = threading.Thread(target=read)
            thread.start()
            thread.join()

            assert result["value"] == "written on the main thread"
        finally:
            index.close()


# =============================================================================
# RM-04 — PDF and EPUB text must actually be extracted
# =============================================================================


class TestSourceDocumentText:
    async def test_read_returns_pdf_text(self):
        """Reading a PDF returns the document's own text, not just annotations."""
        pdf_doc = doc("pdf-1", "Manual.pdf")
        library = FakeLibrary(
            [pdf_doc], {"pdf-1": make_pdf_zip("Installation requires a torque wrench")}
        )

        with use_library(library):
            data = await call("remarkable_read", document="Manual.pdf")

        assert "torque wrench" in data["content"]
        assert data["file_type"] == "pdf"

    async def test_pdf_text_is_searchable_after_reading(self, clean_state):
        """Reading a PDF indexes its text for later content search."""
        pdf_doc = doc("pdf-1", "Manual.pdf")
        library = FakeLibrary(
            [pdf_doc], {"pdf-1": make_pdf_zip("Installation requires a torque wrench")}
        )

        with use_library(library):
            await call("remarkable_read", document="Manual.pdf")
            data = await call("remarkable_search", query="torque")

        assert data["count"] == 1
        assert data["documents"][0]["match_type"] == "content"

    async def test_grep_finds_text_inside_a_pdf(self):
        pdf_doc = doc("pdf-1", "Manual.pdf")
        library = FakeLibrary(
            [pdf_doc], {"pdf-1": make_pdf_zip("Calibrate the sensor every six months")}
        )
        with use_library(library):
            data = await call("remarkable_read", document="Manual.pdf", grep="calibrate")

        assert data["grep_matches"] >= 1
        assert "Calibrate" in data["content"]


# =============================================================================
# RM-05 / RM-07 — per-page OCR must cover every page the caller asked for
# =============================================================================


def fake_ocr(pages_text: dict):
    """Return an ocr_via_sampling stand-in keyed by the rendered page marker."""

    async def _ocr(ctx, png_data, max_tokens=2000):
        marker = png_data.decode()
        return pages_text.get(marker)

    return _ocr


def fake_render(zip_path, page, background_color=None):
    """Render stand-in: encodes the page number so the OCR stub can key on it."""
    return f"page-{page}".encode()


class TestSamplingOCRCoverage:
    @pytest.fixture(autouse=True)
    def sampling_enabled(self, monkeypatch):
        monkeypatch.setenv("REMARKABLE_OCR_BACKEND", "sampling")
        monkeypatch.setattr(_helpers, "should_use_sampling_ocr", lambda ctx: True)
        monkeypatch.setattr(_helpers, "render_page_from_document_zip", fake_render)

    async def test_pages_all_transcribes_every_page(self, monkeypatch):
        """pages='all' must OCR each page, not return blanks for all but one."""
        monkeypatch.setattr(
            _helpers,
            "ocr_via_sampling",
            fake_ocr({"page-1": "first page", "page-2": "second page", "page-3": "third page"}),
        )
        library = FakeLibrary([doc("n1", "Journal")], {"n1": make_notebook_zip(["p1", "p2", "p3"])})
        with use_library(library):
            data = await call("remarkable_read", document="Journal", pages="all", include_ocr=True)

        assert data["pages"] == [1, 2, 3]
        for expected in ("first page", "second page", "third page"):
            assert expected in data["content"]

    async def test_page_range_transcribes_only_that_range(self, monkeypatch):
        """OCR is expensive, so only the requested pages are sent to the model."""
        seen = []

        async def _ocr(ctx, png_data, max_tokens=2000):
            seen.append(png_data.decode())
            return f"text for {png_data.decode()}"

        monkeypatch.setattr(_helpers, "ocr_via_sampling", _ocr)
        library = FakeLibrary(
            [doc("n1", "Journal")], {"n1": make_notebook_zip(["p1", "p2", "p3", "p4"])}
        )
        with use_library(library):
            data = await call("remarkable_read", document="Journal", pages="2-3", include_ocr=True)

        assert sorted(seen) == ["page-2", "page-3"]
        assert data["pages"] == [2, 3]

    async def test_single_page_ocr_is_cached_for_reuse(self, monkeypatch, clean_state):
        """A second read of the same page must not re-run OCR."""
        calls = []

        async def _ocr(ctx, png_data, max_tokens=2000):
            calls.append(png_data.decode())
            return "transcribed once"

        monkeypatch.setattr(_helpers, "ocr_via_sampling", _ocr)
        library = FakeLibrary([doc("n1", "Journal")], {"n1": make_notebook_zip(["p1", "p2"])})

        with use_library(library):
            await call("remarkable_read", document="Journal", page=1, include_ocr=True)
            data = await call("remarkable_read", document="Journal", page=1, include_ocr=True)

        assert len(calls) == 1
        assert "transcribed once" in data["content"]

    async def test_grep_reports_which_pages_were_searched(self, monkeypatch):
        """A grep miss must not claim the whole document lacks the term."""
        monkeypatch.setattr(_helpers, "ocr_via_sampling", fake_ocr({"page-1": "unrelated text"}))
        library = FakeLibrary([doc("n1", "Journal")], {"n1": make_notebook_zip(["p1", "p2", "p3"])})
        with use_library(library):
            data = await call(
                "remarkable_read", document="Journal", grep="missing", include_ocr=True
            )

        error = data["_error"]
        assert error["type"] == "no_grep_matches"
        # It says what it actually looked at, and how to widen the search
        assert "page(s) read so far" in error["message"]
        assert "pages='all'" in error["suggestion"]

    async def test_auto_ocr_retry_keeps_the_page_selection(self, monkeypatch):
        """The automatic OCR retry must not silently drop pages='all'."""
        monkeypatch.setattr(
            _helpers,
            "ocr_via_sampling",
            fake_ocr({"page-1": "alpha", "page-2": "beta"}),
        )
        # No typed text, so the empty-notebook path triggers the OCR retry
        library = FakeLibrary([doc("n1", "Blank")], {"n1": make_notebook_zip(["p1", "p2"])})

        with use_library(library):
            data = await call("remarkable_read", document="Blank", pages="all")

        assert data.get("_ocr_auto_enabled") is True
        assert data["pages"] == [1, 2]
        assert "alpha" in data["content"] and "beta" in data["content"]


# =============================================================================
# RM-06 / RM-13 — render caching must be correct and cheap
# =============================================================================


class TestRenderCaching:
    def test_cache_key_separates_backgrounds_and_formats(self):
        d = doc("x", "Notes", doc_hash="v1")
        white = _helpers.render_cache_key(d, 1, "png", "#FFFFFF")
        clear = _helpers.render_cache_key(d, 1, "png", "#00000000")
        svg = _helpers.render_cache_key(d, 1, "svg", "#FFFFFF")
        assert len({white, clear, svg}) == 3

    def test_cache_key_changes_when_the_document_changes(self):
        before = _helpers.render_cache_key(doc("x", "Notes", doc_hash="v1"), 1, "png", "#FFF")
        after = _helpers.render_cache_key(doc("x", "Notes", doc_hash="v2"), 1, "png", "#FFF")
        assert before != after

    def test_download_cache_avoids_repeat_downloads(self):
        d = doc("nb", "Notes")
        library = FakeLibrary([d], {"nb": make_notebook_zip(["p1", "p2"])})

        for _ in range(3):
            _helpers.download_document(library, d)

        assert len(library.download_calls) == 1

    def test_download_cache_refetches_a_changed_document(self):
        library = FakeLibrary([], {"nb": make_notebook_zip(["p1"])})
        _helpers.download_document(library, doc("nb", "Notes", doc_hash="v1"))
        _helpers.download_document(library, doc("nb", "Notes", doc_hash="v2"))
        assert len(library.download_calls) == 2

    def test_rendered_image_cache_evicts_by_size(self):
        _helpers._rendered_image_cache.clear()
        big = "x" * (8 * 1024 * 1024)
        for i in range(12):  # 96MB against a 64MB budget
            _helpers.put_rendered_image(f"key-{i}", big)

        total = sum(len(v) for v in _helpers._rendered_image_cache.values())
        assert total <= _helpers._MAX_RENDERED_IMAGE_BYTES
        # Most recent survives, oldest was evicted
        assert "key-11" in _helpers._rendered_image_cache
        assert "key-0" not in _helpers._rendered_image_cache


# =============================================================================
# RM-20 — duplicate names must not resolve arbitrarily
# =============================================================================


class TestDuplicateNames:
    async def test_duplicate_names_are_reported_not_guessed(self):
        library = FakeLibrary(
            [
                doc("work", "Work", folder=True),
                doc("personal", "Personal", folder=True),
                doc("n1", "Notes", parent="work"),
                doc("n2", "Notes", parent="personal"),
            ]
        )
        with use_library(library):
            data = await call("remarkable_read", document="Notes")

        assert data["_error"]["type"] == "ambiguous_document"
        assert set(data["_error"]["did_you_mean"]) == {"/Work/Notes", "/Personal/Notes"}

    async def test_full_path_resolves_a_duplicate_name(self):
        library = FakeLibrary(
            [
                doc("work", "Work", folder=True),
                doc("personal", "Personal", folder=True),
                doc("n1", "Notes", parent="work"),
                doc("n2", "Notes", parent="personal"),
            ],
            {
                "n1": make_notebook_zip(["p1"], typed_text="work notes"),
                "n2": make_notebook_zip(["p1"], typed_text="personal notes"),
            },
        )
        with use_library(library):
            data = await call("remarkable_read", document="/Work/Notes")

        assert "work notes" in data["content"]

    async def test_a_unique_name_still_resolves(self):
        library = FakeLibrary(
            [doc("n1", "Unique Note")],
            {"n1": make_notebook_zip(["p1"], typed_text="the only one")},
        )
        with use_library(library):
            data = await call("remarkable_read", document="Unique Note")
        assert "the only one" in data["content"]


# =============================================================================
# RM-15 — the zip guard must reject traversal, including near-miss prefixes
# =============================================================================


class TestZipExtractionSafety:
    def test_rejects_parent_directory_traversal(self, tmp_path):
        from rm_mcp.extract.notebook import _safe_extractall

        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escaped.txt", "nope")

        target = tmp_path / "target"
        target.mkdir()
        with zipfile.ZipFile(archive) as zf:
            with pytest.raises(ValueError, match="outside target directory"):
                _safe_extractall(zf, target)

    def test_rejects_absolute_sibling_prefix(self, tmp_path):
        """A sibling directory sharing the target's name prefix is not inside it."""
        from rm_mcp.extract.notebook import _safe_extractall

        target = tmp_path / "target"
        target.mkdir()
        (tmp_path / "target-evil").mkdir()

        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../target-evil/pwned.txt", "nope")

        with zipfile.ZipFile(archive) as zf:
            with pytest.raises(ValueError, match="outside target directory"):
                _safe_extractall(zf, target)

    def test_allows_normal_nested_members(self, tmp_path):
        from rm_mcp.extract.notebook import _safe_extractall

        archive = tmp_path / "ok.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("doc/page.rm", "data")

        target = tmp_path / "target"
        target.mkdir()
        with zipfile.ZipFile(archive) as zf:
            _safe_extractall(zf, target)

        assert (target / "doc" / "page.rm").read_text() == "data"


# =============================================================================
# RM-16 — bad configuration degrades instead of crashing
# =============================================================================


class TestConfigParsing:
    def test_invalid_integer_falls_back_to_default(self, monkeypatch):
        from rm_mcp.config import env_int

        monkeypatch.setenv("RM_TEST_VALUE", "not-a-number")
        assert env_int("RM_TEST_VALUE", 42) == 42

    def test_value_below_minimum_falls_back_to_default(self, monkeypatch):
        from rm_mcp.config import env_int

        monkeypatch.setenv("RM_TEST_VALUE", "0")
        assert env_int("RM_TEST_VALUE", 5, minimum=1) == 5

    def test_valid_value_is_used(self, monkeypatch):
        from rm_mcp.config import env_int

        monkeypatch.setenv("RM_TEST_VALUE", "17")
        assert env_int("RM_TEST_VALUE", 5) == 17

    def test_empty_value_uses_default(self, monkeypatch):
        from rm_mcp.config import env_int

        monkeypatch.setenv("RM_TEST_VALUE", "  ")
        assert env_int("RM_TEST_VALUE", 9) == 9

    async def test_malformed_page_size_does_not_break_reads(self, monkeypatch):
        monkeypatch.setenv("REMARKABLE_PAGE_SIZE", "enormous")
        library = FakeLibrary(
            [doc("n1", "Note")], {"n1": make_notebook_zip(["p1"], typed_text="content here")}
        )
        with use_library(library):
            data = await call("remarkable_read", document="Note")
        assert "content here" in data["content"]


# =============================================================================
# The sync API contract
# =============================================================================


class FakeResponse:
    def __init__(self, content=b"", status_code=200, text=None):
        self.content = content
        self.status_code = status_code
        self.text = text if text is not None else content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class RecordingSession:
    """Stands in for requests.Session, recording headers and serving blobs.

    Mirrors the live API's rule: a blob fetch without an `rm-filename` header
    carrying a recognised extension is rejected with HTTP 400.
    """

    VALID_SUFFIXES = (".docSchema", ".metadata", ".content", ".pagedata", ".rm", ".pdf", ".epub")

    def __init__(self, root_hash: str, blobs: dict):
        self.root_hash = root_hash
        self.blobs = blobs
        self.requests: List[tuple] = []

    def mount(self, *args, **kwargs):
        pass

    # `json` shadows the module here because that is the keyword requests uses.
    def request(self, method, url, headers=None, timeout=None, data=None, json=None):
        headers = headers or {}
        self.requests.append((url, headers.get("rm-filename")))

        if url.endswith("/sync/v4/root"):
            return FakeResponse(text=_json.dumps({"hash": self.root_hash, "generation": 1}))

        filename = headers.get("rm-filename")
        if not filename or not filename.endswith(self.VALID_SUFFIXES):
            return FakeResponse(
                status_code=400,
                text='{"message":"unexpected \'rm-filename\' http header"}',
            )

        blob_hash = url.rsplit("/", 1)[-1]
        if blob_hash not in self.blobs:
            return FakeResponse(status_code=404, text="not found")
        return FakeResponse(content=self.blobs[blob_hash])


class TestSyncApiContract:
    """The sync API rejects blob fetches that do not name the file."""

    def build_client(self):
        from rm_mcp.clients.cloud import RemarkableClient

        metadata = json.dumps(
            {"visibleName": "Live Note", "type": "DocumentType", "lastModified": "1750000000000"}
        ).encode()
        # Schema 4: a version line, a summary line, then the entries
        doc_index = (
            "4\n"
            "0:.:doc-uuid:2:200\n"
            "metahash:0:doc-uuid.metadata:0:100\n"
            "conthash:0:doc-uuid.content:0:100\n"
        ).encode()
        root_index = ("4\n0:.:1:300\ndochash:80000000:doc-uuid:2:200\n").encode()

        session = RecordingSession(
            "roothash",
            {
                "roothash": root_index,
                "dochash": doc_index,
                "metahash": metadata,
                "conthash": b'{"pages": []}',
            },
        )
        client = RemarkableClient(device_token="d", user_token="u")
        client._session = session
        return client, session

    def test_root_index_is_fetched_with_its_filename(self):
        client, session = self.build_client()
        docs = client.get_meta_items()

        assert [d.name for d in docs] == ["Live Note"]
        root_fetch = next(f for url, f in session.requests if url.endswith("roothash"))
        assert root_fetch == "root.docSchema"

    def test_document_index_is_fetched_with_its_filename(self):
        client, session = self.build_client()
        client.get_meta_items()

        doc_fetch = next(f for url, f in session.requests if url.endswith("dochash"))
        assert doc_fetch == "doc-uuid.docSchema"

    def test_each_blob_is_fetched_under_its_own_name(self):
        client, session = self.build_client()
        client.get_meta_items()

        meta_fetch = next(f for url, f in session.requests if url.endswith("metahash"))
        assert meta_fetch == "doc-uuid.metadata"

    def test_download_names_every_member(self):
        client, session = self.build_client()
        docs = client.get_meta_items()
        raw = client.download(docs[0])

        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            assert sorted(zf.namelist()) == ["doc-uuid.content", "doc-uuid.metadata"]
        # No fetch went out unnamed, so none was rejected
        blob_fetches = [f for url, f in session.requests if not url.endswith("/root")]
        assert all(f for f in blob_fetches)

    def test_schema_4_summary_line_is_not_parsed_as_an_entry(self):
        from rm_mcp.clients.cloud import RemarkableClient

        client = RemarkableClient()
        entries = client._parse_index(b"4\n0:.:173:2063325327\nabc:0:doc-uuid:4:26189276\n")

        assert len(entries) == 1
        assert entries[0]["id"] == "doc-uuid"
        assert entries[0]["size"] == 26189276


# =============================================================================
# RM-18 — errors must never carry token material
# =============================================================================


class TestTokenHandling:
    def test_invalid_token_error_does_not_echo_the_token(self):
        from rm_mcp.clients.cloud import load_client_from_token

        secret = "s3cr3t-token-material-that-must-not-leak"
        with pytest.raises(ValueError) as exc:
            load_client_from_token(secret)

        assert secret not in str(exc.value)
        assert "rm-mcp --setup" in str(exc.value)


# =============================================================================
# RM-10 — the loader must not re-read the library once per batch
# =============================================================================


class TestBackgroundLoader:
    async def test_loader_fetches_the_library_once(self, clean_state):
        import asyncio

        from rm_mcp import resources

        documents = [doc(f"d{i}", f"Note {i}") for i in range(40)]
        library = FakeLibrary(documents)

        resources._registered_docs.clear()
        resources._registered_uris.clear()
        resources._registered_img.clear()

        with patch("rm_mcp.api.get_rmapi", return_value=library):
            await resources._load_documents_background(asyncio.Event())

        # One fetch for the whole library, not one per batch
        assert library.meta_calls == 1

    async def test_loader_indexes_only_documents_inside_the_root(self, clean_state, monkeypatch):
        import asyncio

        from rm_mcp import resources

        library = FakeLibrary(
            [
                doc("work", "Work", folder=True),
                doc("personal", "Personal", folder=True),
                doc("in", "Inside", parent="work"),
                doc("out", "Outside", parent="personal"),
            ]
        )

        resources._registered_docs.clear()
        resources._registered_uris.clear()
        resources._registered_img.clear()

        monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
        with patch("rm_mcp.api.get_rmapi", return_value=library):
            await resources._load_documents_background(asyncio.Event())

        assert clean_state.get_document_hash("in") is not None
        assert clean_state.get_document_hash("out") is None

    async def test_loader_publishes_the_complete_collection(self, clean_state):
        import asyncio

        from rm_mcp import resources

        documents = [doc(f"d{i}", f"Note {i}") for i in range(30)]
        library = FakeLibrary(documents)

        resources._registered_docs.clear()
        resources._registered_uris.clear()
        resources._registered_img.clear()

        with patch("rm_mcp.api.get_rmapi", return_value=library):
            await resources._load_documents_background(asyncio.Event())

        _client, collection = cache_mod.get_cached_collection()
        assert len(collection) == 30


# =============================================================================
# Reading behaviour end to end
# =============================================================================


class TestReadingEndToEnd:
    async def test_reads_typed_text_from_a_notebook(self):
        library = FakeLibrary(
            [doc("n1", "Meeting Notes")],
            {"n1": make_notebook_zip(["p1", "p2"], typed_text="Discussed the roadmap")},
        )
        with use_library(library):
            data = await call("remarkable_read", document="Meeting Notes")
        assert "Discussed the roadmap" in data["content"]

    async def test_browse_lists_folders_and_documents(self):
        library = FakeLibrary(
            [
                doc("work", "Work", folder=True),
                doc("n1", "Report", parent="work"),
                doc("n2", "Top Level"),
            ]
        )
        with use_library(library):
            data = await call("remarkable_browse", path="/")

        assert [f["name"] for f in data["folders"]] == ["Work"]
        assert [d["name"] for d in data["documents"]] == ["Top Level"]

    async def test_browse_into_a_folder(self):
        library = FakeLibrary(
            [doc("work", "Work", folder=True), doc("n1", "Report", parent="work")]
        )
        with use_library(library):
            data = await call("remarkable_browse", path="/Work")
        assert [d["name"] for d in data["documents"]] == ["Report"]

    async def test_status_reports_document_count_and_config(self):
        library = FakeLibrary([doc("n1", "One"), doc("n2", "Two")])
        with use_library(library):
            data = await call("remarkable_status")

        assert data["authenticated"] is True
        assert data["document_count"] == 2
        assert data["config"]["root_path"] == "/"

    async def test_missing_document_suggests_alternatives(self):
        library = FakeLibrary([doc("n1", "Quarterly Report")])
        with use_library(library):
            data = await call("remarkable_read", document="Quarterly Repot")

        assert data["_error"]["type"] == "document_not_found"
        assert "Quarterly Report" in data["_error"]["did_you_mean"]

    async def test_page_out_of_range_is_explained(self):
        library = FakeLibrary(
            [doc("n1", "Note")], {"n1": make_notebook_zip(["p1"], typed_text="short")}
        )
        with use_library(library):
            data = await call("remarkable_read", document="Note", page=99)
        assert data["_error"]["type"] == "page_out_of_range"

    async def test_invalid_regex_is_reported_clearly(self):
        library = FakeLibrary(
            [doc("n1", "Note")], {"n1": make_notebook_zip(["p1"], typed_text="content")}
        )
        with use_library(library):
            data = await call("remarkable_read", document="Note", grep="[unclosed")
        assert data["_error"]["type"] == "invalid_grep"


# =============================================================================
# Page range parsing
# =============================================================================


class TestParsePages:
    @pytest.mark.parametrize(
        "spec,total,expected",
        [
            ("all", 3, [1, 2, 3]),
            ("1-3", 5, [1, 2, 3]),
            ("2,4,5", 5, [2, 4, 5]),
            ("1-2,5", 5, [1, 2, 5]),
            ("3-1", 5, []),  # reversed range yields nothing
            ("0-2", 5, [1, 2]),  # clamped to the first page
            ("4-99", 5, [4, 5]),  # clamped to the last page
            ("", 5, []),
            ("nonsense", 5, []),
            ("2,,3", 5, [2, 3]),
            ("3,1,2", 5, [1, 2, 3]),  # sorted
            ("2,2,2", 5, [2]),  # de-duplicated
            ("ALL", 3, [1, 2, 3]),  # case-insensitive
        ],
    )
    def test_parse_pages(self, spec, total, expected):
        assert _helpers.parse_pages(spec, total) == expected

    def test_parse_pages_never_returns_out_of_range_values(self):
        for spec in ("all", "1-999", "0", "-5", "1,50,100"):
            for page in _helpers.parse_pages(spec, 7):
                assert 1 <= page <= 7


# =============================================================================
# Root path helpers
# =============================================================================


class TestRootPathHelpers:
    @pytest.mark.parametrize(
        "configured,expected",
        [("", "/"), ("/", "/"), ("/Work", "/Work"), ("Work", "/Work"), ("/Work/", "/Work")],
    )
    def test_root_path_normalisation(self, monkeypatch, configured, expected):
        monkeypatch.setenv("REMARKABLE_ROOT_PATH", configured)
        assert _helpers._get_root_path() == expected

    @pytest.mark.parametrize(
        "path,root,inside",
        [
            ("/Work/Report", "/Work", True),
            ("/Work", "/Work", True),
            ("/work/report", "/Work", True),  # case-insensitive
            ("/Workshop/Report", "/Work", False),  # prefix is not containment
            ("/Personal/Report", "/Work", False),
            ("/anything", "/", True),
        ],
    )
    def test_containment(self, path, root, inside):
        assert _helpers._is_within_root(path, root) is inside

    @pytest.mark.parametrize("path", ["/Work/Report", "/Work/Deep/Nested/File"])
    def test_resolve_and_filter_round_trip(self, monkeypatch, path):
        """Filtering a path and resolving it back returns the original."""
        monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
        displayed = _helpers._apply_root_filter(path)
        assert _helpers._resolve_root_path(displayed) == path


# =============================================================================
# Write operations — rename, move, create folder
# =============================================================================


class FakeCloud:
    """A stand-in sync server that enforces the real one's rules.

    Blobs are content-addressed and immutable, uploads must carry a correct
    CRC32C, fetches must name the file, and a root update is only accepted if
    it names the generation it was based on. Getting any of those wrong here
    means getting them wrong against a real library.
    """

    def __init__(self):
        self.blobs = {}
        self.generation = 1
        self.root_hash = None
        self.uploads = []
        self.root_updates = 0
        self.conflict_once = False

    # --- helpers used to build a starting library -------------------------

    def put(self, content: bytes, name: str) -> str:
        from rm_mcp.clients.cloud import blob_hash

        digest = blob_hash(content)
        self.blobs[digest] = content
        return digest

    def seed(self, documents):
        """documents: list of (doc_id, visibleName, parent, type)."""
        from rm_mcp.clients.cloud import serialize_index, serialize_metadata

        root_entries = []
        for doc_id, name, parent, doc_type in documents:
            meta = serialize_metadata(
                {
                    "createdTime": "1700000000000",
                    "lastModified": "1700000000000",
                    "parent": parent,
                    "pinned": False,
                    "type": doc_type,
                    "visibleName": name,
                }
            )
            meta_hash = self.put(meta, f"{doc_id}.metadata")
            files = [
                {
                    "hash": meta_hash,
                    "type": "0",
                    "id": f"{doc_id}.metadata",
                    "subfiles": 0,
                    "size": len(meta),
                }
            ]
            index = serialize_index(files, doc_id)
            doc_hash = self.put(index, f"{doc_id}.docSchema")
            root_entries.append(
                {
                    "hash": doc_hash,
                    "type": "0",
                    "id": doc_id,
                    "subfiles": len(files),
                    "size": len(meta),
                }
            )
        root_entries.sort(key=lambda e: e["id"])
        root = serialize_index(root_entries, ".")
        self.root_hash = self.put(root, "root.docSchema")

    def visible_names(self):
        """Read the library back the way a tablet would."""
        from rm_mcp.clients.cloud import parse_index

        names = {}
        root = parse_index(self.blobs[self.root_hash])
        for entry in root:
            files = parse_index(self.blobs[entry["hash"]])
            for f in files:
                if f["id"].endswith(".metadata"):
                    meta = _json.loads(self.blobs[f["hash"]])
                    names[entry["id"]] = (meta["visibleName"], meta["parent"], meta["type"])
        return names

    def metadata_of(self, doc_id):
        """Full metadata dict for one document, read back from the tree."""
        from rm_mcp.clients.cloud import parse_index

        for entry in parse_index(self.blobs[self.root_hash]):
            if entry["id"] != doc_id:
                continue
            for f in parse_index(self.blobs[entry["hash"]]):
                if f["id"].endswith(".metadata"):
                    return _json.loads(self.blobs[f["hash"]])
        return None

    # --- the transport --------------------------------------------------

    def request(self, method, url, headers=None, timeout=None, data=None, json=None):
        from rm_mcp.clients.cloud import blob_hash, crc32c_header

        headers = headers or {}
        name = headers.get("rm-filename")

        if url.endswith("/sync/v4/root"):
            return FakeResponse(
                text=_json.dumps({"hash": self.root_hash, "generation": self.generation})
            )

        if url.endswith("/sync/v3/root"):
            assert name == "roothash", "root update must name roothash"
            self.root_updates += 1
            if self.conflict_once:
                self.conflict_once = False
                return FakeResponse(status_code=412, text="conflict")
            if json["generation"] != self.generation:
                return FakeResponse(status_code=412, text="generation conflict")
            if json["hash"] not in self.blobs:
                return FakeResponse(status_code=400, text="root blob not uploaded")
            self.root_hash = json["hash"]
            self.generation += 1
            return FakeResponse(
                text=_json.dumps({"hash": self.root_hash, "generation": self.generation})
            )

        digest = url.rsplit("/", 1)[-1]

        if method == "PUT":
            if not name or not name.endswith(
                (".docSchema", ".metadata", ".content", ".pagedata", ".rm", ".pdf", ".epub")
            ):
                return FakeResponse(status_code=400, text="unexpected 'rm-filename' http header")
            if headers.get("x-goog-hash") != crc32c_header(data):
                return FakeResponse(status_code=400, text="missing checksum")
            if blob_hash(data) != digest:
                return FakeResponse(status_code=400, text="hash does not match body")
            self.blobs[digest] = data
            self.uploads.append(name)
            return FakeResponse(status_code=200)

        if not name:
            return FakeResponse(status_code=400, text="unexpected 'rm-filename' http header")
        if digest not in self.blobs:
            return FakeResponse(status_code=404, text="not found")
        return FakeResponse(content=self.blobs[digest])

    def mount(self, *a, **k):
        pass


def make_client(cloud: FakeCloud):
    from rm_mcp.clients.cloud import RemarkableClient

    client = RemarkableClient(device_token="d", user_token="u")
    client._session = cloud
    return client


LIBRARY = [
    ("folder-work", "Work", "", "CollectionType"),
    ("folder-arch", "Archive", "", "CollectionType"),
    ("doc-notes", "Meeting Notes", "folder-work", "DocumentType"),
    ("doc-loose", "Loose Note", "", "DocumentType"),
]


class TestLibraryWriter:
    def setup_cloud(self):
        from rm_mcp.clients.organize import LibraryWriter

        cloud = FakeCloud()
        cloud.seed(LIBRARY)
        return cloud, LibraryWriter(make_client(cloud))

    def test_rename_changes_only_the_target(self):
        cloud, writer = self.setup_cloud()
        before = cloud.visible_names()

        result = writer.rename("doc-notes", "Q3 Planning")

        after = cloud.visible_names()
        assert after["doc-notes"][0] == "Q3 Planning"
        assert result["previous_root"] != result["root"]
        # every other document is byte-identical
        for doc_id in before:
            if doc_id != "doc-notes":
                assert after[doc_id] == before[doc_id]

    def test_rename_preserves_parent_and_type(self):
        cloud, writer = self.setup_cloud()
        writer.rename("doc-notes", "Renamed")
        name, parent, doc_type = cloud.visible_names()["doc-notes"]
        assert (parent, doc_type) == ("folder-work", "DocumentType")

    def test_rename_does_not_touch_last_modified(self):
        """Renaming is not working on a document, so its timestamp must survive.

        Bumping it would flatten the recently-used ordering for every item
        touched in a bulk reorganisation.
        """
        cloud, writer = self.setup_cloud()
        before = cloud.metadata_of("doc-notes")["lastModified"]
        writer.rename("doc-notes", "Renamed")
        assert cloud.metadata_of("doc-notes")["lastModified"] == before

    def test_move_does_not_touch_last_modified(self):
        cloud, writer = self.setup_cloud()
        before = cloud.metadata_of("doc-notes")["lastModified"]
        writer.move("doc-notes", "folder-arch")
        assert cloud.metadata_of("doc-notes")["lastModified"] == before

    def test_rename_changes_nothing_but_the_name(self):
        """Every other metadata field is byte-for-byte what it was."""
        cloud, writer = self.setup_cloud()
        before = cloud.metadata_of("doc-notes")
        writer.rename("doc-notes", "Renamed")
        after = cloud.metadata_of("doc-notes")
        assert after.pop("visibleName") == "Renamed"
        before.pop("visibleName")
        assert after == before

    def test_move_changes_parent_only(self):
        cloud, writer = self.setup_cloud()
        writer.move("doc-notes", "folder-arch")
        name, parent, _t = cloud.visible_names()["doc-notes"]
        assert (name, parent) == ("Meeting Notes", "folder-arch")

    def test_move_to_top_level(self):
        cloud, writer = self.setup_cloud()
        writer.move("doc-notes", "")
        assert cloud.visible_names()["doc-notes"][1] == ""

    def test_create_folder_appears_in_the_library(self):
        cloud, writer = self.setup_cloud()
        result = writer.create_folder("Receipts", "folder-work")
        names = cloud.visible_names()
        assert names[result["folder_id"]] == ("Receipts", "folder-work", "CollectionType")
        assert len(names) == len(LIBRARY) + 1

    def test_the_previous_root_still_describes_the_old_library(self):
        """The returned previous_root is a complete, valid snapshot."""
        from rm_mcp.clients.cloud import parse_index

        cloud, writer = self.setup_cloud()
        result = writer.rename("doc-notes", "Changed")

        # The old root blob is still there and still parses
        old_root = parse_index(cloud.blobs[result["previous_root"]])
        assert len(old_root) == len(LIBRARY)
        # Pointing the library back at it restores the old name
        cloud.root_hash = result["previous_root"]
        assert cloud.visible_names()["doc-notes"][0] == "Meeting Notes"

    def test_uploads_are_content_addressed_and_checksummed(self):
        """The fake rejects a wrong hash or checksum, so passing proves both."""
        cloud, writer = self.setup_cloud()
        writer.rename("doc-notes", "Verified")
        assert cloud.uploads  # metadata, doc index, root index
        assert any(u.endswith(".metadata") for u in cloud.uploads)
        assert any(u.endswith(".docSchema") for u in cloud.uploads)

    def test_generation_conflict_is_retried(self):
        cloud, writer = self.setup_cloud()
        cloud.conflict_once = True

        writer.rename("doc-notes", "After Conflict")

        assert cloud.root_updates == 2  # rejected once, then succeeded
        assert cloud.visible_names()["doc-notes"][0] == "After Conflict"

    def test_a_stale_generation_is_rejected_not_overwritten(self):
        """A write based on an old generation must not clobber a newer one."""
        from rm_mcp.clients.cloud import RootConflict

        cloud, writer = self.setup_cloud()
        stale_generation = cloud.generation
        # Someone else changes the library first
        writer.rename("doc-loose", "Changed Elsewhere")

        root_entries = writer.client.get_index(cloud.root_hash, ".")
        with pytest.raises(RootConflict):
            writer._commit(root_entries, stale_generation)
        # The other change survived
        assert cloud.visible_names()["doc-loose"][0] == "Changed Elsewhere"

    def test_unknown_document_is_refused(self):
        cloud, writer = self.setup_cloud()
        with pytest.raises(RuntimeError, match="not in the library index"):
            writer.rename("does-not-exist", "Nope")

    def test_round_trip_through_the_writer_is_stable(self):
        """Renaming to the same name leaves the library semantically identical."""
        cloud, writer = self.setup_cloud()
        before = cloud.visible_names()
        writer.rename("doc-notes", "Meeting Notes")
        after = cloud.visible_names()
        assert {k: (v[0], v[1], v[2]) for k, v in after.items()} == {
            k: (v[0], v[1], v[2]) for k, v in before.items()
        }


class TestOrganizingTools:
    """The MCP tools on top of the writer."""

    def setup_cloud(self):
        cloud = FakeCloud()
        cloud.seed(LIBRARY)
        return cloud, make_client(cloud)

    def library(self, client):
        docs = client.get_meta_items()
        return patch.object(_helpers, "get_cached_collection", lambda: (client, docs))

    async def test_rename_tool(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_rename", item="Meeting Notes", new_name="Q3 Planning")
        assert data["renamed"] is True
        assert cloud.visible_names()["doc-notes"][0] == "Q3 Planning"

    async def test_rename_rejects_a_path_as_a_name(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_rename", item="Meeting Notes", new_name="/Work/Elsewhere")
        assert data["_error"]["type"] == "invalid_name"
        assert cloud.visible_names()["doc-notes"][0] == "Meeting Notes"

    async def test_rename_rejects_an_empty_name(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_rename", item="Meeting Notes", new_name="   ")
        assert data["_error"]["type"] == "invalid_name"

    async def test_move_tool(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_move", item="Loose Note", destination="/Archive")
        assert data["moved"] is True
        assert cloud.visible_names()["doc-loose"][1] == "folder-arch"

    async def test_move_to_root(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_move", item="/Work/Meeting Notes", destination="/")
        assert data["moved"] is True
        assert cloud.visible_names()["doc-notes"][1] == ""

    async def test_move_into_a_document_is_refused(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_move", item="Loose Note", destination="Meeting Notes")
        assert data["_error"]["type"] == "not_found"
        assert cloud.visible_names()["doc-loose"][1] == ""

    async def test_move_a_folder_into_itself_is_refused(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_move", item="Work", destination="/Work")
        assert data["_error"]["type"] == "invalid_move"

    async def test_move_that_changes_nothing_is_reported(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_move", item="Meeting Notes", destination="/Work")
        assert data["_error"]["type"] == "already_there"
        assert cloud.root_updates == 0

    async def test_create_folder_tool(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_create_folder", path="/Work/Receipts")
        assert data["created"] is True
        assert data["path"] == "/Work/Receipts"
        assert ("Receipts", "folder-work", "CollectionType") in cloud.visible_names().values()

    async def test_create_folder_at_top_level(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_create_folder", path="Inbox")
        assert data["path"] == "/Inbox"
        assert ("Inbox", "", "CollectionType") in cloud.visible_names().values()

    async def test_create_duplicate_folder_is_refused(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_create_folder", path="Work")
        assert data["_error"]["type"] == "already_exists"
        assert cloud.root_updates == 0

    async def test_create_folder_with_missing_parent_is_refused(self):
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_create_folder", path="/Nope/Deep")
        assert data["_error"]["type"] == "not_found"
        assert cloud.root_updates == 0

    async def test_read_only_mode_blocks_every_write(self, monkeypatch):
        monkeypatch.setenv("REMARKABLE_READ_ONLY", "1")
        cloud, client = self.setup_cloud()
        with self.library(client):
            for tool, kwargs in [
                ("remarkable_rename", {"item": "Loose Note", "new_name": "X"}),
                ("remarkable_move", {"item": "Loose Note", "destination": "/Work"}),
                ("remarkable_create_folder", {"path": "New"}),
            ]:
                data = await call(tool, **kwargs)
                assert data["_error"]["type"] == "writes_disabled"
        assert cloud.root_updates == 0


class TestWritesRespectRootPath:
    """A configured root path confines writes as well as reads."""

    def setup_cloud(self):
        cloud = FakeCloud()
        cloud.seed(
            [
                ("folder-work", "Work", "", "CollectionType"),
                ("folder-priv", "Personal", "", "CollectionType"),
                ("doc-secret", "Private Diary", "folder-priv", "DocumentType"),
                ("doc-work", "Report", "folder-work", "DocumentType"),
            ]
        )
        return cloud, make_client(cloud)

    def library(self, client):
        docs = client.get_meta_items()
        return patch.object(_helpers, "get_cached_collection", lambda: (client, docs))

    async def test_cannot_rename_outside_the_root(self, monkeypatch):
        monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_rename", item="Private Diary", new_name="Exposed")
        assert data["_error"]["type"] == "not_found"
        assert cloud.visible_names()["doc-secret"][0] == "Private Diary"
        assert cloud.root_updates == 0

    async def test_cannot_move_something_outside_the_root(self, monkeypatch):
        monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_move", item="Private Diary", destination="/")
        assert data["_error"]["type"] == "not_found"
        assert cloud.root_updates == 0

    async def test_cannot_move_into_a_folder_outside_the_root(self, monkeypatch):
        monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_move", item="Report", destination="/Personal")
        assert data["_error"]["type"] == "not_found"
        assert cloud.visible_names()["doc-work"][1] == "folder-work"

    async def test_can_rename_inside_the_root(self, monkeypatch):
        monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
        cloud, client = self.setup_cloud()
        with self.library(client):
            data = await call("remarkable_rename", item="Report", new_name="Final Report")
        assert data["renamed"] is True
        assert cloud.visible_names()["doc-work"][0] == "Final Report"


class TestFileTypeDetection:
    """A document's blobs decide its type, not what it happens to be called."""

    def make(self, name, blob_names):
        d = doc("x", name)
        d.files = [
            {"id": b, "hash": "h", "type": "0", "subfiles": 0, "size": 1} for b in blob_names
        ]
        return d

    def test_pdf_without_an_extension_in_its_name(self):
        """An arXiv paper saved as '2304.03442' is still a PDF."""
        from rm_mcp.api import get_file_type

        d = self.make("2304.03442", ["x.content", "x.metadata", "x.pagedata", "x.pdf"])
        assert get_file_type(None, d) == "pdf"

    def test_epub_without_an_extension(self):
        from rm_mcp.api import get_file_type

        d = self.make("Some Book", ["x.content", "x.metadata", "x.epub"])
        assert get_file_type(None, d) == "epub"

    def test_notebook_has_no_source_blob(self):
        from rm_mcp.api import get_file_type

        d = self.make("Quick sheets", ["x.content", "x.metadata", "x/p1.rm"])
        assert get_file_type(None, d) == "notebook"

    def test_name_is_used_when_blobs_are_unknown(self):
        from rm_mcp.api import get_file_type

        d = doc("x", "Report.pdf")
        assert get_file_type(None, d) == "pdf"

    def test_a_notebook_named_like_a_pdf_follows_its_blobs(self):
        from rm_mcp.api import get_file_type

        d = self.make("notes about a pdf", ["x.content", "x.metadata", "x/p1.rm"])
        assert get_file_type(None, d) == "notebook"
