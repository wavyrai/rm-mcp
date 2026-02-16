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
from typing import Any, Dict, List, Optional

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

        # Ensure parent directory exists (skip for :memory:)
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialize schema on the creating thread
        conn = self._get_connection()
        conn.executescript(_SCHEMA_SQL)

        # Schema versioning: detect stale DB and rebuild if needed
        row = conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()
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
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

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
        conn.execute(
            """
            INSERT INTO documents (doc_id, doc_hash, name, path, file_type, modified_at, page_count, indexed_at)
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
        conn.commit()

    def get_document_hash(self, doc_id: str) -> Optional[str]:
        """Get the stored hash for a document."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT doc_hash FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
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
            # Delete FTS entries for this document's pages
            conn.execute(
                "DELETE FROM pages_fts WHERE rowid IN "
                "(SELECT rowid FROM pages WHERE doc_id = ?)",
                (doc_id,),
            )
            conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
            conn.commit()
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

        # Check if row already exists (for FTS cleanup)
        existing = conn.execute(
            "SELECT rowid FROM pages WHERE doc_id = ? AND page_number = ? AND content_type = ?",
            (doc_id, page_number, content_type),
        ).fetchone()

        if existing:
            old_rowid = existing[0]
            # Delete old FTS entry
            conn.execute(
                "DELETE FROM pages_fts WHERE rowid = ?", (old_rowid,)
            )

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

        conn.commit()

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

        handwritten = result.get("handwritten_text", [])
        if handwritten:
            parts.append((0, "ocr", "\n\n".join(handwritten), ocr_backend))

        if not parts:
            return

        for page_number, content_type, content, backend in parts:
            # Clean up existing FTS entry
            existing = conn.execute(
                "SELECT rowid FROM pages WHERE doc_id = ? AND page_number = ? AND content_type = ?",
                (doc_id, page_number, content_type),
            ).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM pages_fts WHERE rowid = ?", (existing[0],)
                )

            # Upsert the page
            conn.execute(
                """
                INSERT INTO pages (doc_id, page_number, content_type, content, ocr_backend, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id, page_number, content_type) DO UPDATE SET
                    content = excluded.content,
                    ocr_backend = excluded.ocr_backend,
                    indexed_at = excluded.indexed_at
                """,
                (doc_id, page_number, content_type, content, backend, now),
            )

            # Insert new FTS entry
            row = conn.execute(
                "SELECT rowid FROM pages WHERE doc_id = ? AND page_number = ? AND content_type = ?",
                (doc_id, page_number, content_type),
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO pages_fts(rowid, doc_id, content) VALUES (?, ?, ?)",
                    (row[0], doc_id, content),
                )

        conn.commit()

    # -----------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------

    def search(
        self, query: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
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
        """Get text preview from indexed pages. Prefers typed_text > highlight > ocr."""
        conn = self._get_connection()
        for content_type in ("typed_text", "highlight", "ocr"):
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

    def get_indexed_document_count(self) -> int:
        """Count documents that have at least one indexed page."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM pages"
        ).fetchone()
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
        """Rebuild the FTS index."""
        conn = self._get_connection()
        conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('rebuild')")
        conn.commit()

    def clear(self) -> None:
        """Clear all indexed data."""
        conn = self._get_connection()
        conn.execute("DELETE FROM pages_fts")
        conn.execute("DELETE FROM pages")
        conn.execute("DELETE FROM documents")
        conn.commit()
        logger.info("Document index cleared")

    def close(self) -> None:
        """Close the thread-local connection (if any)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @property
    def db_path(self) -> str:
        return self._db_path
