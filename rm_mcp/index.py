"""
Persistent SQLite FTS5 index for reMarkable documents.

Provides an L2 cache layer between in-memory caches (L1) and the reMarkable Cloud (L3).
Survives restarts and enables full-text content search across previously-read documents.

DB location: ~/.cache/rm-mcp/index.db (override via REMARKABLE_INDEX_PATH env var)
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default database path
_DEFAULT_DB_DIR = Path.home() / ".cache" / "rm-mcp"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "index.db"

# 2: searchable rows are per tablet page ("page" text, "ocr" transcripts);
# version 1 stored whole-document rows under page 0.
_SCHEMA_VERSION = 2

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

-- Last known library state, so a restart syncs incrementally. One row per
-- document (or folder): the root-index hash of the version described by
-- `record` (JSON: root entry, blob list, metadata). The root hash it was
-- synced from lives in _meta under 'sync_root_hash'.
CREATE TABLE IF NOT EXISTS sync_documents (
    doc_id TEXT PRIMARY KEY,
    entry_hash TEXT NOT NULL,
    record TEXT NOT NULL
);

-- Content-addressed OCR cache: keyed by the hash of the page's .rm blob, so
-- an edited page can never be answered with the text of its old strokes.
CREATE TABLE IF NOT EXISTS page_ocr (
    page_hash TEXT NOT NULL,
    backend TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT,
    PRIMARY KEY (page_hash, backend)
);

-- Applied write batches, for remarkable_undo. `record` is JSON: the root hash
-- the batch replaced, and per touched document its root entry before and
-- after. Kept after use (used_at set) as a log of what changed.
CREATE TABLE IF NOT EXISTS write_log (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    record TEXT NOT NULL,
    used_at TEXT
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

    def replace_document_pages(
        self,
        doc_id: str,
        doc_hash: Optional[str],
        pages: Iterable[Tuple[int, str, str]],
        **metadata: Any,
    ) -> None:
        """Make one document version's pages the searchable ones, atomically.

        Args:
            doc_id: Document ID.
            doc_hash: The version the pages were read from.
            pages: (page_number, content_type, content) rows; content_type is
                "page" for the page's own text or "ocr" for a saved transcript.
            metadata: name, path, file_type, modified_at, page_count.
        """
        self.upsert_document(doc_id=doc_id, doc_hash=doc_hash, **metadata)
        conn = self._get_connection()
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                "DELETE FROM pages_fts WHERE rowid IN (SELECT rowid FROM pages WHERE doc_id = ?)",
                (doc_id,),
            )
            conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
            for page_number, content_type, content in pages:
                self._write_page(conn, doc_id, page_number, content_type, content, None, now)

    # -----------------------------------------------------------------
    # Content-addressed page OCR
    # -----------------------------------------------------------------

    def get_page_ocr_by_hash(self, page_hash: str, backend: str) -> Optional[str]:
        """OCR text for exactly these page bytes, if it was ever transcribed."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT content FROM page_ocr WHERE page_hash = ? AND backend = ?",
            (page_hash, backend),
        ).fetchone()
        return row["content"] if row else None

    def put_page_ocr_by_hash(self, page_hash: str, backend: str, content: str) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO page_ocr (page_hash, backend, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (page_hash, backend, content, datetime.now(timezone.utc).isoformat()),
            )

    def mark_document_version(self, doc_id: str, doc_hash: str) -> bool:
        """Record that a known document now has a new version.

        Clears its searchable pages if they came from another version, so search
        stops matching text the document no longer contains. Documents the
        index has never seen are left alone (no row is created here).

        Returns:
            True if stale pages were cleared.
        """
        conn = self._get_connection()
        row = conn.execute("SELECT doc_hash FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if row is None or row["doc_hash"] in (None, doc_hash):
            return False
        with conn:
            conn.execute(
                "DELETE FROM pages_fts WHERE rowid IN (SELECT rowid FROM pages WHERE doc_id = ?)",
                (doc_id,),
            )
            conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
            conn.execute("UPDATE documents SET doc_hash = ? WHERE doc_id = ?", (doc_hash, doc_id))
        return True

    # -----------------------------------------------------------------
    # Sync state
    # -----------------------------------------------------------------

    def load_sync_state(self) -> Tuple[Optional[str], Dict[str, Dict[str, Any]]]:
        """Return (root hash fully synced, {doc_id: record}) from the last run."""
        conn = self._get_connection()
        row = conn.execute("SELECT value FROM _meta WHERE key = 'sync_root_hash'").fetchone()
        records: Dict[str, Dict[str, Any]] = {}
        for r in conn.execute("SELECT doc_id, record FROM sync_documents").fetchall():
            try:
                records[r["doc_id"]] = json.loads(r["record"])
            except ValueError:
                logger.warning("Discarding unreadable sync record for %s", r["doc_id"])
        return (row["value"] if row else None), records

    def save_sync_state(
        self,
        root_hash: Optional[str],
        upserts: Dict[str, Dict[str, Any]],
        removed: Iterable[str],
    ) -> None:
        """Apply one sync's changes atomically.

        Args:
            root_hash: The root hash now fully reflected, or None when some
                documents could not be fetched (forces a re-diff next time).
            upserts: Records for documents fetched in this sync.
            removed: IDs of documents no longer in the library.
        """
        conn = self._get_connection()
        with conn:
            for doc_id, record in upserts.items():
                conn.execute(
                    "INSERT OR REPLACE INTO sync_documents (doc_id, entry_hash, record) "
                    "VALUES (?, ?, ?)",
                    (doc_id, record["entry"]["hash"], json.dumps(record)),
                )
            for doc_id in removed:
                conn.execute("DELETE FROM sync_documents WHERE doc_id = ?", (doc_id,))
            if root_hash is None:
                conn.execute("DELETE FROM _meta WHERE key = 'sync_root_hash'")
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES ('sync_root_hash', ?)",
                    (root_hash,),
                )

    # -----------------------------------------------------------------
    # Write log (undo)
    # -----------------------------------------------------------------

    def save_write(self, token: str, kind: str, record: Dict[str, Any]) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                "INSERT INTO write_log (token, kind, created_at, record) VALUES (?, ?, ?, ?)",
                (token, kind, datetime.now(timezone.utc).isoformat(), json.dumps(record)),
            )

    def complete_write(self, token: str, kind: str, record: Dict[str, Any]) -> None:
        """Replace a reserved row's kind and record once its batch is committed."""
        conn = self._get_connection()
        with conn:
            cur = conn.execute(
                "UPDATE write_log SET kind = ?, record = ? WHERE token = ?",
                (kind, json.dumps(record), token),
            )
        if cur.rowcount != 1:
            raise KeyError(f"No reserved write {token}")

    def get_write(self, token: str) -> Optional[Dict[str, Any]]:
        row = (
            self._get_connection()
            .execute(
                "SELECT kind, created_at, record, used_at FROM write_log WHERE token = ?",
                (token,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return {
            "kind": row["kind"],
            "created_at": row["created_at"],
            "used_at": row["used_at"],
            "record": json.loads(row["record"]),
        }

    def mark_write_undone(self, token: str) -> bool:
        """Mark a batch undone; False if it already was (single use)."""
        conn = self._get_connection()
        with conn:
            cur = conn.execute(
                "UPDATE write_log SET used_at = ? WHERE token = ? AND used_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), token),
            )
        return cur.rowcount == 1

    # -----------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------

    def search(self, query: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Full-text search over indexed pages, best match first.

        Returns one row per matching page: doc_id, page_number, content_type
        ("page" or "ocr"), snippet and rank. Callers must still check that the
        document is visible in the current library.
        """
        conn = self._get_connection()
        sql = """
            SELECT
                p.doc_id,
                p.page_number,
                p.content_type,
                snippet(pages_fts, 1, '', '', '…', 16) AS snippet,
                bm25(pages_fts) AS rank
            FROM pages_fts
            JOIN pages p ON p.rowid = pages_fts.rowid
            WHERE pages_fts.content MATCH ?
            ORDER BY rank
            LIMIT ?
            """
        try:
            rows = conn.execute(sql, (query, limit)).fetchall()
        except sqlite3.OperationalError:
            # Ordinary search terms are valid FTS5 syntax right up until they
            # are not: a hyphen ("agent-based"), a dot, an apostrophe. Retry the
            # query as a quoted phrase, which treats it as literal text.
            phrase = '"' + query.replace('"', '""') + '"'
            try:
                rows = conn.execute(sql, (phrase, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(row) for row in rows]

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

    def has_indexed_content(self, doc_id: str) -> bool:
        """Whether any page content is stored for this document.

        `needs_reindex` clears a document's pages when its hash changes, so
        "has pages" is exactly "content is indexed for the current version".
        """
        conn = self._get_connection()
        row = conn.execute("SELECT 1 FROM pages WHERE doc_id = ? LIMIT 1", (doc_id,)).fetchone()
        return row is not None

    def indexed_versions(self) -> Dict[str, Optional[str]]:
        """doc_id -> indexed version hash, for every document with searchable pages."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT d.doc_id, d.doc_hash FROM documents d "
            "WHERE EXISTS (SELECT 1 FROM pages p WHERE p.doc_id = d.doc_id)"
        ).fetchall()
        return {row["doc_id"]: row["doc_hash"] for row in rows}

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

    def clear(self) -> None:
        """Clear all indexed data, except saved transcripts (page_ocr).

        Transcripts are work an agent did and cannot be rebuilt from the
        cloud; they are keyed by page content, so they reattach on re-index.
        """
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
