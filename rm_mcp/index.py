"""
Persistent SQLite FTS5 index for reMarkable documents.

Provides an L2 cache layer between in-memory caches (L1) and the reMarkable Cloud (L3).
Survives restarts and enables full-text content search across previously-read documents.

DB location: ~/.cache/rm-mcp/index.db (override via REMARKABLE_INDEX_PATH env var)
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Default database path
_DEFAULT_DB_DIR = Path.home() / ".cache" / "rm-mcp"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "index.db"

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    doc_hash TEXT,
    name TEXT,
    path TEXT,
    file_type TEXT,
    modified_at TEXT,
    page_count INTEGER,
    indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    doc_id TEXT,
    page_number INTEGER,
    content_type TEXT,
    content TEXT,
    ocr_backend TEXT,
    indexed_at TEXT,
    PRIMARY KEY (doc_id, page_number, content_type),
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    doc_id,
    content,
    tokenize='porter unicode61'
);
"""

# Singleton instance
_instance: Optional["DocumentIndex"] = None
_instance_lock = threading.Lock()


def get_instance() -> Optional["DocumentIndex"]:
    """Get the singleton DocumentIndex instance.

    Returns None if initialization failed (graceful degradation).
    """
    return _instance


def initialize(db_path: Optional[str] = None) -> Optional["DocumentIndex"]:
    """Initialize the singleton DocumentIndex.

    Args:
        db_path: Path to SQLite database. If None, uses REMARKABLE_INDEX_PATH
                 env var or default ~/.cache/rm-mcp/index.db.
                 Use ":memory:" for in-memory database (testing).

    Returns:
        DocumentIndex instance or None if initialization failed.
    """
    global _instance
    with _instance_lock:
        if _instance is not None:
            return _instance
        try:
            _instance = DocumentIndex(db_path)
            return _instance
        except Exception as e:
            logger.warning(f"Failed to initialize document index: {e}")
            return None


def close():
    """Close and discard the singleton instance."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.close()
            _instance = None


class DocumentIndex:
    """Thread-safe SQLite FTS5 index for reMarkable documents.

    Uses thread-local connections and WAL mode for safe concurrent access.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = os.environ.get("REMARKABLE_INDEX_PATH")
        if db_path is None:
            db_path = str(_DEFAULT_DB_PATH)

        self._db_path = db_path
        self._local = threading.local()
        self._connections: List[sqlite3.Connection] = []

        # A plain ":memory:" database is private to the connection that opened
        # it, and connections here are thread-local — so every thread would get
        # its own empty database. Shared-cache URI form keeps one database that
        # all threads of this process see.
        self._is_memory = db_path == ":memory:"
        if self._is_memory:
            self._connect_path = f"file:rm-mcp-{id(self):x}?mode=memory&cache=shared"
            self._use_uri = True
            # Hold an extra connection open: a shared-cache in-memory database
            # is destroyed as soon as the last connection to it closes.
            self._keepalive = sqlite3.connect(self._connect_path, uri=True)
        else:
            self._connect_path = db_path
            self._use_uri = False
            self._keepalive = None
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialize schema on the creating thread
        conn = self._get_connection()
        conn.executescript(_SCHEMA_SQL)

        # Schema versioning: detect stale DB and rebuild if needed
        row = conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'").fetchone()
        stored_version = int(row["value"]) if row else 0

        if stored_version < _SCHEMA_VERSION:
            if stored_version > 0:
                logger.info(
                    f"Schema version {stored_version} → {_SCHEMA_VERSION}, rebuilding index"
                )
                conn.execute("DELETE FROM pages_fts")
                conn.execute("DELETE FROM pages")
                conn.execute("DELETE FROM documents")
            conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )

        conn.commit()
        logger.info(f"Document index initialized: {db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._connect_path, uri=self._use_uri)
            if not self._is_memory:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            self._connections.append(conn)
        return conn

    def _ensure_document_row(self, conn: sqlite3.Connection, doc_id: str) -> None:
        """Make sure a parent row exists before writing pages.

        `pages` has a foreign key onto `documents`, so page writes for a
        document the background loader has not registered yet would otherwise
        fail with an IntegrityError that the callers swallow — silently
        disabling the persistent cache.
        """
        conn.execute(
            "INSERT OR IGNORE INTO documents (doc_id, indexed_at) VALUES (?, ?)",
            (doc_id, datetime.now(timezone.utc).isoformat()),
        )

    # -----------------------------------------------------------------
    # Document operations
    # -----------------------------------------------------------------

    def upsert_document(
        self,
        doc_id: str,
        doc_hash: Optional[str] = None,
        name: Optional[str] = None,
        path: Optional[str] = None,
        file_type: Optional[str] = None,
        modified_at: Optional[str] = None,
        page_count: Optional[int] = None,
    ) -> None:
        """Insert or update document metadata."""
        conn = self._get_connection()
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO documents
                    (doc_id, doc_hash, name, path, file_type, modified_at, page_count, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    doc_hash = COALESCE(excluded.doc_hash, documents.doc_hash),
                    name = COALESCE(excluded.name, documents.name),
                    path = COALESCE(excluded.path, documents.path),
                    file_type = COALESCE(excluded.file_type, documents.file_type),
                    modified_at = COALESCE(excluded.modified_at, documents.modified_at),
                    page_count = COALESCE(excluded.page_count, documents.page_count),
                    indexed_at = excluded.indexed_at
                """,
                (doc_id, doc_hash, name, path, file_type, modified_at, page_count, now),
            )

    def get_document_hash(self, doc_id: str) -> Optional[str]:
        """Get the stored hash for a document."""
        conn = self._get_connection()
        row = conn.execute("SELECT doc_hash FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return row["doc_hash"] if row else None

    def needs_reindex(self, doc_id: str, current_hash: str) -> bool:
        """Check if a document needs re-indexing based on hash comparison.

        If the document's hash has changed, deletes stale page content
        and returns True. Returns True for new (unindexed) documents.
        """
        stored_hash = self.get_document_hash(doc_id)
        if stored_hash is None:
            return True
        if stored_hash != current_hash:
            # Hash changed — clear stale pages and FTS entries
            conn = self._get_connection()
            with conn:
                # Delete FTS entries for this document's pages
                conn.execute(
                    "DELETE FROM pages_fts "
                    "WHERE rowid IN (SELECT rowid FROM pages WHERE doc_id = ?)",
                    (doc_id,),
                )
                conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
            logger.debug(f"Cleared stale pages for document {doc_id}")
            return True
        return False

    # -----------------------------------------------------------------
    # Page operations
    # -----------------------------------------------------------------

    def upsert_page(
        self,
        doc_id: str,
        page_number: int,
        content: str,
        content_type: str = "typed_text",
        ocr_backend: Optional[str] = None,
    ) -> None:
        """Insert or update page content and sync FTS index."""
        conn = self._get_connection()
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            self._ensure_document_row(conn, doc_id)
            self._write_page(conn, doc_id, page_number, content_type, content, ocr_backend, now)

    def _write_page(
        self,
        conn: sqlite3.Connection,
        doc_id: str,
        page_number: int,
        content_type: str,
        content: str,
        ocr_backend: Optional[str],
        now: str,
    ) -> None:
        """Write one page row and keep its FTS entry in sync.

        Must be called inside a transaction.
        """
        # Drop the stale FTS entry first — fts5 has no upsert by rowid.
        existing = conn.execute(
            "SELECT rowid FROM pages WHERE doc_id = ? AND page_number = ? AND content_type = ?",
            (doc_id, page_number, content_type),
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM pages_fts WHERE rowid = ?", (existing[0],))

        conn.execute(
            """
            INSERT INTO pages (doc_id, page_number, content_type, content, ocr_backend, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id, page_number, content_type) DO UPDATE SET
                content = excluded.content,
                ocr_backend = excluded.ocr_backend,
                indexed_at = excluded.indexed_at
            """,
            (doc_id, page_number, content_type, content, ocr_backend, now),
        )

        # Get the rowid of the upserted row and insert into FTS
        row = conn.execute(
            "SELECT rowid FROM pages WHERE doc_id = ? AND page_number = ? AND content_type = ?",
            (doc_id, page_number, content_type),
        ).fetchone()
        if row:
            conn.execute(
                "INSERT INTO pages_fts(rowid, doc_id, content) VALUES (?, ?, ?)",
                (row[0], doc_id, content),
            )

    def get_page_ocr(
        self, doc_id: str, page_number: int, backend: str = "sampling"
    ) -> Optional[str]:
        """Get stored OCR text for a specific page."""
        conn = self._get_connection()
        row = conn.execute(
            """
            SELECT content FROM pages
            WHERE doc_id = ? AND page_number = ? AND content_type = 'ocr'
            AND (ocr_backend = ? OR ocr_backend IS NULL)
            """,
            (doc_id, page_number, backend),
        ).fetchone()
        return row["content"] if row else None

    def store_extraction_result(
        self,
        doc_id: str,
        result: Dict[str, Any],
    ) -> None:
        """Store a full extraction result (typed text, highlights, OCR) as pages.

        All writes are done in a single transaction for atomicity.

        Args:
            doc_id: Document ID
            result: Extraction result dict with keys like typed_text, highlights,
                    handwritten_text, pages, ocr_backend
        """
        ocr_backend = result.get("ocr_backend")
        conn = self._get_connection()
        now = datetime.now(timezone.utc).isoformat()

        parts = []  # (page_number, content_type, content, ocr_backend)

        typed_text = result.get("typed_text", [])
        if typed_text:
            parts.append((0, "typed_text", "\n\n".join(typed_text), None))

        highlights = result.get("highlights", [])
        if highlights:
            parts.append((0, "highlight", "\n\n".join(highlights), None))

        source_text = result.get("source_text")
        if source_text and source_text.strip():
            parts.append((0, "source_text", source_text, None))

        # Handwritten text arrives as one entry per page, in page order — keep
        # that granularity so get_page_ocr() can serve a single page later.
        handwritten = result.get("handwritten_text") or []
        for page_index, page_text in enumerate(handwritten, start=1):
            if page_text and page_text.strip():
                parts.append((page_index, "ocr", page_text, ocr_backend))

        # Nothing to write — return before opening a transaction. Opening one
        # and returning without committing would leave this connection holding
        # the write lock, stalling every other thread's writes.
        if not parts:
            return

        with conn:
            self._ensure_document_row(conn, doc_id)
            for page_number, content_type, content, backend in parts:
                self._write_page(conn, doc_id, page_number, content_type, content, backend, now)

    # -----------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Full-text search across indexed page content.

        Uses FTS5 MATCH with bm25 ranking and snippet context.
        Deduplicates by doc_id (keeps the best-ranked match per document).

        Returns:
            List of dicts with keys: doc_id, name, path, file_type, snippet, rank
        """
        conn = self._get_connection()
        # Fetch more rows than needed to allow for dedup, but cap to avoid
        # pulling the entire table into memory.
        fetch_limit = limit * 5
        try:
            rows = conn.execute(
                """
                SELECT
                    d.doc_id,
                    d.name,
                    d.path,
                    d.file_type,
                    d.modified_at,
                    snippet(pages_fts, 1, '>>>', '<<<', '...', 40) AS snippet,
                    bm25(pages_fts) AS rank
                FROM pages_fts
                JOIN documents d ON d.doc_id = pages_fts.doc_id
                WHERE pages_fts.content MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, fetch_limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Invalid FTS5 query syntax (e.g. unmatched quotes)
            return []

        # Deduplicate by doc_id, keeping the best-ranked (first) match
        seen: set = set()
        results: List[Dict[str, Any]] = []
        for row in rows:
            doc_id = row["doc_id"]
            if doc_id not in seen:
                seen.add(doc_id)
                results.append(dict(row))
                if len(results) >= limit:
                    break
        return results

    # -----------------------------------------------------------------
    # Preview / snippet helpers
    # -----------------------------------------------------------------

    def get_preview(self, doc_id: str, max_chars: int = 200) -> Optional[str]:
        """Get text preview from indexed pages. Prefers typed_text > highlight > ocr > source."""
        conn = self._get_connection()
        for content_type in ("typed_text", "highlight", "ocr", "source_text"):
            row = conn.execute(
                "SELECT content FROM pages WHERE doc_id = ? AND content_type = ? LIMIT 1",
                (doc_id, content_type),
            ).fetchone()
            if row and row["content"]:
                text = row["content"].strip()
                if text:
                    return text[:max_chars]
        return None

    def get_content_snippet(self, doc_id: str, max_chars: int = 2000) -> Optional[str]:
        """Get concatenated content for search previews."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT content FROM pages WHERE doc_id = ? ORDER BY page_number, content_type",
            (doc_id,),
        ).fetchall()
        if not rows:
            return None
        parts = [r["content"] for r in rows if r["content"]]
        if not parts:
            return None
        combined = "\n\n".join(parts)
        return combined[:max_chars]

    def get_page_count(self, doc_id: str) -> Optional[int]:
        """Get the stored page count for a document, if known."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT page_count FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return row["page_count"] if row and row["page_count"] else None

    def prune_missing(self, known_doc_ids: Iterable[str]) -> int:
        """Delete indexed data for documents that are no longer in the library.

        Without this, deleted and trashed documents keep their content
        searchable forever.

        Args:
            known_doc_ids: IDs of every document currently visible to the server.

        Returns:
            Number of document rows removed.
        """
        keep = set(known_doc_ids)
        conn = self._get_connection()
        stale = [
            row["doc_id"]
            for row in conn.execute("SELECT doc_id FROM documents").fetchall()
            if row["doc_id"] not in keep
        ]
        if not stale:
            return 0

        with conn:
            for doc_id in stale:
                conn.execute(
                    "DELETE FROM pages_fts "
                    "WHERE rowid IN (SELECT rowid FROM pages WHERE doc_id = ?)",
                    (doc_id,),
                )
                conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
                conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        logger.info("Pruned %d document(s) no longer in the library", len(stale))
        return len(stale)

    def get_indexed_document_count(self) -> int:
        """Count documents that have at least one indexed page."""
        conn = self._get_connection()
        row = conn.execute("SELECT COUNT(DISTINCT doc_id) FROM pages").fetchone()
        return row[0] if row else 0

    # -----------------------------------------------------------------
    # Management
    # -----------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        conn = self._get_connection()
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        page_count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

        db_size = 0
        if self._db_path != ":memory:":
            try:
                db_size = Path(self._db_path).stat().st_size
            except OSError:
                pass

        return {
            "index_documents": doc_count,
            "index_pages": page_count,
            "index_size": db_size,
            "index_path": self._db_path,
        }

    def rebuild(self) -> None:
        """Rebuild the FTS index from the pages table."""
        conn = self._get_connection()
        with conn:
            conn.execute("DELETE FROM pages_fts")
            conn.execute(
                "INSERT INTO pages_fts(rowid, doc_id, content) "
                "SELECT rowid, doc_id, content FROM pages"
            )

    def clear(self) -> None:
        """Clear all indexed data."""
        conn = self._get_connection()
        with conn:
            conn.execute("DELETE FROM pages_fts")
            conn.execute("DELETE FROM pages")
            conn.execute("DELETE FROM documents")
        logger.info("Document index cleared")

    def close(self) -> None:
        """Close every connection this index opened, on any thread."""
        for conn in self._connections:
            try:
                conn.close()
            except Exception:
                logger.debug("Failed to close an index connection", exc_info=True)
        self._connections.clear()
        self._local = threading.local()
        if self._keepalive is not None:
            self._keepalive.close()
            self._keepalive = None

    @property
    def db_path(self) -> str:
        return self._db_path
