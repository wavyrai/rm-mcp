"""
reMarkable Cloud Sync Client

A replacement for rmapy that uses the current reMarkable sync API (v3/v4).
rmapy is abandoned and uses deprecated endpoints that return 500 errors.

Based on the protocol used by ddvk/rmapi.
"""

import base64
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from rm_mcp.blobstore import get_default_store
from rm_mcp.config import env_int
from rm_mcp.models import Document, Folder  # noqa: F401

logger = logging.getLogger(__name__)

# Maximum number of parallel workers for fetching document metadata
_PARALLEL_WORKERS = env_int("REMARKABLE_PARALLEL_WORKERS", 5, minimum=1)

# API endpoints
# Note: my.remarkable.com endpoints redirect to doesnotexist.remarkable.com
# So we use webapp-prod.cloud.remarkable.engineering for auth
AUTH_HOST = "https://webapp-prod.cloud.remarkable.engineering"
DEVICE_TOKEN_URL = f"{AUTH_HOST}/token/json/2/device/new"
USER_TOKEN_URL = f"{AUTH_HOST}/token/json/2/user/new"

SYNC_HOST = "https://internal.cloud.remarkable.com"
ROOT_URL = f"{SYNC_HOST}/sync/v4/root"
FILES_URL = f"{SYNC_HOST}/sync/v3/files"

# Every blob fetch must name the file it is fetching. The API validates the
# extension and rejects anything else with HTTP 400 "unexpected 'rm-filename'
# http header" — including requests that omit the header entirely.
RM_FILENAME_HEADER = "rm-filename"
ROOT_INDEX_FILENAME = "root.docSchema"
ROOT_PUT_URL = f"{SYNC_HOST}/sync/v3/root"

# Index files are line-oriented text. The first line is the schema version, the
# second summarises the index ("." for the root, the document UUID for a
# document index), and the rest are entries.
INDEX_SCHEMA_VERSION = "4"
ROOT_INDEX_OWNER = "."

# Uploads must carry a CRC32C (Castagnoli) checksum of the body; without it the
# API answers 400 "missing checksum".
_CRC32C_POLY = 0x82F63B78
_CRC32C_TABLE = []
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = (_crc >> 1) ^ (_CRC32C_POLY if _crc & 1 else 0)
    _CRC32C_TABLE.append(_crc)


def crc32c(data: bytes) -> int:
    """CRC32C (Castagnoli) checksum, as used by Google Cloud Storage."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = (crc >> 8) ^ _CRC32C_TABLE[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFFFFFF


def crc32c_header(data: bytes) -> str:
    """Format a body checksum for the `x-goog-hash` header."""
    return "crc32c=" + base64.b64encode(crc32c(data).to_bytes(4, "big")).decode()


def blob_hash(content: bytes) -> str:
    """Content address of a blob: every hash in this protocol is this."""
    return hashlib.sha256(content).hexdigest()


def _index_filename(doc_id: str) -> str:
    """Filename under which a document's blob index is stored."""
    return f"{doc_id}.docSchema"


def serialize_index(entries: List[Dict[str, Any]], owner: str) -> bytes:
    """Render index entries back into their on-the-wire form.

    The hash of an index is the SHA-256 of exactly these bytes, so this has to
    reproduce the server's formatting precisely. Verified byte-for-byte against
    a live account's root index and document indexes.

    Args:
        entries: Index entries, as returned by `_parse_index`.
        owner: `.` for the root index, or the document UUID for a document index.
    """
    total_size = sum(int(e["size"]) for e in entries)
    lines = [INDEX_SCHEMA_VERSION, f"0:{owner}:{len(entries)}:{total_size}"]
    lines += [f"{e['hash']}:{e['type']}:{e['id']}:{e['subfiles']}:{e['size']}" for e in entries]
    return ("\n".join(lines) + "\n").encode("utf-8")


def serialize_metadata(metadata: Dict[str, Any]) -> bytes:
    """Render a document's .metadata file the way the tablet writes it."""
    return (json.dumps(metadata, indent=4, sort_keys=True) + "\n").encode("utf-8")


def parse_index(content: bytes) -> List[Dict[str, Any]]:
    """Parse an index file into entries.

    Index files start with a schema version line. Schema 4 adds a summary
    line describing the index itself (four fields) before the entries,
    which have five.
    """
    lines = content.decode("utf-8").strip().split("\n")
    entries = []

    # First line is schema version
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < 5:
            # Schema 4 summary line, not an entry
            logger.debug("Skipping non-entry index line: %s", line[:100])
            continue
        try:
            entries.append(
                {
                    "hash": parts[0],
                    "type": parts[1],
                    "id": parts[2],
                    "subfiles": int(parts[3]),
                    "size": int(parts[4]),
                }
            )
        except (ValueError, IndexError):
            logger.warning("Skipping malformed index line: %s", line[:100])

    return entries


# Sentinel: "use the process-wide default" for the client's blob and state stores.
_USE_DEFAULT: Any = object()


class RootConflict(RuntimeError):
    """Raised when the library changed between reading and writing the root.

    Nothing has been modified when this is raised: the root update is the only
    step that changes anything, and it was rejected.
    """


class RemarkableClient:
    """Client for reMarkable Cloud sync API."""

    def __init__(
        self,
        device_token: str = "",
        user_token: str = "",
        blob_store: Any = _USE_DEFAULT,
        state_store: Any = _USE_DEFAULT,
    ):
        """
        Args:
            device_token: Long-lived device token.
            user_token: Short-lived user token (renewed from the device token).
            blob_store: Content-addressed blob cache (`blobstore.BlobStore`);
                defaults to the process-wide store, None disables it.
            state_store: Where the last synced library state persists (the
                `index.DocumentIndex`); defaults to the index singleton, None
                keeps sync state in memory only.
        """
        self.device_token = device_token
        self.user_token = user_token
        self._documents: List[Document] = []
        self._documents_by_id: Dict[str, Document] = {}
        self._token_lock = threading.Lock()
        self._blob_store_arg = blob_store
        self._state_store_arg = state_store

        # Incremental sync state. `_records` holds, per document, the last
        # version fetched successfully; `_synced_root_hash` is the root hash
        # those records fully reflect (None when the last sync left documents
        # behind, which forces a re-diff on the next call).
        self._sync_lock = threading.Lock()
        self._state_loaded = False
        self._records: Dict[str, Dict[str, Any]] = {}
        self._synced_root_hash: Optional[str] = None
        self._stale_ids: set = set()

        # Connection-pooling session with retry logic
        self._session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            # 429 belongs here: without it a rate-limited library fails hard
            # instead of backing off and succeeding.
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=_PARALLEL_WORKERS * 2,
            pool_maxsize=_PARALLEL_WORKERS * 2,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def renew_token(self) -> str:
        """Exchange device token for a fresh user token."""
        if not self.device_token:
            raise RuntimeError("No device token available")

        headers = {"Authorization": f"Bearer {self.device_token}"}

        try:
            response = self._session.post(USER_TOKEN_URL, headers=headers, timeout=30)
            if response.status_code == 200 and response.text:
                self.user_token = response.text.strip()
                return self.user_token
        except requests.RequestException as e:
            raise RuntimeError(f"Network error during token renewal: {e}")

        raise RuntimeError(
            f"Failed to renew user token (HTTP {response.status_code}).\n"
            "Re-authenticate by running: uvx rm-mcp --setup"
        )

    def _request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        """Make an authenticated request using the pooled session."""
        if not self.user_token:
            self.renew_token()

        extra = dict(headers or {})
        request_headers = {"Authorization": f"Bearer {self.user_token}", **extra}
        response = self._session.request(
            method, url, headers=request_headers, data=data, json=json_body, timeout=60
        )

        if response.status_code == 401:
            # Token expired, try to renew (thread-safe)
            with self._token_lock:
                # Re-check: another thread may have already renewed
                current_auth = f"Bearer {self.user_token}"
                if request_headers["Authorization"] == current_auth:
                    self.renew_token()
            request_headers = {"Authorization": f"Bearer {self.user_token}", **extra}
            response = self._session.request(
                method, url, headers=request_headers, data=data, json=json_body, timeout=60
            )

        return response

    # -----------------------------------------------------------------
    # Write operations
    # -----------------------------------------------------------------

    def get_root_info(self) -> tuple:
        """Fetch the current root hash and generation.

        The generation is the library's optimistic-concurrency token: a root
        update is only accepted if it names the generation it was based on, so
        a concurrent change from the tablet cannot be silently overwritten.

        Returns:
            Tuple of (root_hash, generation).
        """
        response = self._request(ROOT_URL)
        response.raise_for_status()
        data = response.json()
        if "hash" not in data:
            raise RuntimeError(f"Unexpected root response: {data}")
        return data["hash"], data.get("generation", 0)

    def upload_blob(self, content: bytes, filename: str) -> str:
        """Upload a blob and return its content hash.

        Blobs are content-addressed and immutable, so this is safe in
        isolation: an uploaded blob changes nothing until a root update
        references it.

        Args:
            content: Raw bytes to store.
            filename: Name the blob is stored under (needs a known extension).

        Returns:
            The blob's SHA-256 hash.
        """
        digest = blob_hash(content)
        response = self._request(
            f"{FILES_URL}/{digest}",
            method="PUT",
            headers={
                RM_FILENAME_HEADER: filename,
                "x-goog-hash": crc32c_header(content),
                "content-type": "application/octet-stream",
            },
            data=content,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Failed to upload {filename} (HTTP {response.status_code}): {response.text[:200]}"
            )
        return digest

    def put_root(self, root_hash: str, generation: int, broadcast: bool = True) -> int:
        """Point the library at a new root index.

        This is the single commit point for any change: everything else is
        content-addressed uploads that nothing references yet.

        Args:
            root_hash: Hash of the new root index blob (already uploaded).
            generation: Generation this change was based on.
            broadcast: Whether to notify devices to sync.

        Returns:
            The new generation.

        Raises:
            RootConflict: If the library changed underneath us.
        """
        response = self._request(
            ROOT_PUT_URL,
            method="PUT",
            headers={RM_FILENAME_HEADER: "roothash"},
            json_body={
                "broadcast": broadcast,
                "hash": root_hash,
                "generation": generation,
            },
        )

        if response.status_code in (409, 412):
            raise RootConflict(
                "The library changed while this update was being prepared. "
                "Nothing was modified — retry the operation."
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Failed to update root (HTTP {response.status_code}): {response.text[:200]}"
            )

        data = response.json()
        if data.get("hash") != root_hash:
            raise RuntimeError("Root hash mismatch after update — aborting.")
        return data.get("generation", generation + 1)

    def get_index(self, index_hash: str, owner_id: str) -> List[Dict[str, Any]]:
        """Fetch and parse an index blob (the root's, or a document's)."""
        filename = (
            ROOT_INDEX_FILENAME if owner_id == ROOT_INDEX_OWNER else _index_filename(owner_id)
        )
        return self._parse_index(self._get_file(index_hash, filename))

    def _get_file(self, file_hash: str, filename: str) -> bytes:
        """Download a file by its hash.

        Args:
            file_hash: Content hash of the blob to fetch.
            filename: Name the blob is stored under. The sync API requires an
                `rm-filename` header carrying a name with a recognised
                extension (``root.docSchema``, ``<uuid>.docSchema``,
                ``<uuid>.metadata``, ...). Requests without it are rejected
                with HTTP 400 regardless of the hash.
        """
        store = self._blob_store
        if store is not None:
            cached = store.get(file_hash)
            if cached is not None:
                return cached

        response = self._request(f"{FILES_URL}/{file_hash}", headers={RM_FILENAME_HEADER: filename})
        response.raise_for_status()
        data = response.content
        # Blobs are addressed by the SHA-256 of their bytes. Anything else is a
        # truncated or corrupted transfer and must not be used or cached.
        if blob_hash(data) != file_hash:
            raise RuntimeError(
                f"Blob {filename} (hash={file_hash}) failed its hash check; not using it."
            )
        if store is not None:
            store.put(file_hash, data)
        return data

    @property
    def _blob_store(self):
        if self._blob_store_arg is _USE_DEFAULT:
            return get_default_store()
        return self._blob_store_arg

    @property
    def _state_store(self):
        if self._state_store_arg is _USE_DEFAULT:
            from rm_mcp.index import get_instance

            return get_instance()
        return self._state_store_arg

    def _parse_index(self, content: bytes) -> List[Dict[str, Any]]:
        """Parse an index file into entries. See `parse_index`."""
        return parse_index(content)

    def get_root_hash(self) -> str:
        """Fetch the current root hash from the cloud.

        This is a lightweight check (single HTTP request) to determine
        if the document library has changed since the last full fetch.

        Returns:
            The root hash string

        Raises:
            RuntimeError: If the API response is invalid or empty
        """
        response = self._request(ROOT_URL)
        response.raise_for_status()

        if not response.text or not response.text.strip():
            raise RuntimeError(
                "Empty response from reMarkable API. Your token may have expired.\n"
                "Re-authenticate by running: uvx rm-mcp --setup"
            )

        try:
            root_data = response.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Invalid JSON from reMarkable API: {e}\nResponse was: {response.text[:200]}"
            )

        if "hash" not in root_data:
            raise RuntimeError(
                f"Unexpected API response format: {root_data}\nThe reMarkable API may have changed."
            )

        return root_data["hash"]

    # -----------------------------------------------------------------
    # Incremental sync
    # -----------------------------------------------------------------

    @property
    def synced_root_hash(self) -> Optional[str]:
        """Root hash the in-memory library fully reflects (None if incomplete)."""
        return self._synced_root_hash

    @property
    def sync_complete(self) -> bool:
        """False when the last sync could not fetch some documents."""
        return self._synced_root_hash is not None

    def _fetch_record(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch one document's blob index and metadata. Raises on any failure.

        A failure must not be papered over: a document returned without its
        metadata would show up under its UUID, and one silently dropped would
        look deleted.
        """
        doc_id = entry["id"]
        files = self._parse_index(self._get_file(entry["hash"], _index_filename(doc_id)))
        metadata: Dict[str, Any] = {}
        for blob in files:
            if blob["id"].endswith(".metadata"):
                metadata = json.loads(self._get_file(blob["hash"], blob["id"]).decode("utf-8"))
        return {"entry": dict(entry), "files": files, "metadata": metadata}

    @staticmethod
    def _document_from_record(record: Dict[str, Any], stale: bool) -> Optional[Document]:
        """Build a Document from a sync record (None for deleted documents)."""
        metadata = record.get("metadata") or {}
        if metadata.get("deleted", False):
            return None
        entry = record["entry"]

        last_modified = None
        if "lastModified" in metadata:
            try:
                ts = int(metadata["lastModified"]) / 1000  # ms to seconds
                last_modified = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError):
                pass

        return Document(
            id=entry["id"],
            hash=entry["hash"],
            name=metadata.get("visibleName", entry["id"]),
            doc_type=metadata.get("type", "DocumentType"),
            parent=metadata.get("parent", ""),
            deleted=False,
            pinned=metadata.get("pinned", False),
            last_modified=last_modified,
            size=entry.get("size", 0),
            files=list(record.get("files") or []),
            stale=stale,
        )

    def _load_state_once(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        store = self._state_store
        if store is None:
            return
        try:
            root_hash, records = store.load_sync_state()
        except Exception:
            logger.warning("Could not load persisted sync state; doing a full sync", exc_info=True)
            return
        self._records = records
        self._synced_root_hash = root_hash if records else None
        self._rebuild_documents()

    def _rebuild_documents(self) -> None:
        documents = []
        for doc_id, record in self._records.items():
            doc = self._document_from_record(record, stale=doc_id in self._stale_ids)
            if doc is not None:
                documents.append(doc)
        self._documents = documents
        self._documents_by_id = {d.id: d for d in documents}

    def _fetch_records(
        self, entries: Iterable[Dict[str, Any]]
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        """Fetch records in parallel. Returns (records by id, ids that failed)."""
        fetched: Dict[str, Dict[str, Any]] = {}
        failed: List[str] = []
        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as executor:
            futures = {executor.submit(self._fetch_record, e): e for e in entries}
            for future in as_completed(futures):
                entry = futures[future]
                try:
                    fetched[entry["id"]] = future.result()
                except Exception as e:
                    failed.append(entry["id"])
                    logger.warning(
                        "Could not fetch document %s (hash=%s): %s",
                        entry.get("id", "unknown"),
                        entry.get("hash", "unknown"),
                        e,
                    )
        return fetched, failed

    def _sync_to(self, root_hash: str) -> None:
        """Bring the library up to `root_hash`, fetching only what changed."""
        try:
            entries = self._parse_index(self._get_file(root_hash, ROOT_INDEX_FILENAME))
        except Exception as e:
            raise RuntimeError(f"Failed to parse root index (hash={root_hash}): {e}") from e

        current = {e["id"]: e for e in entries}
        removed = [doc_id for doc_id in self._records if doc_id not in current]
        changed = [
            e
            for e in entries
            if self._records.get(e["id"], {}).get("entry", {}).get("hash") != e["hash"]
        ]

        fetched, failed = self._fetch_records(changed)

        for doc_id in removed:
            self._records.pop(doc_id, None)
        replaced = [doc_id for doc_id in fetched if doc_id in self._records]
        self._records.update(fetched)
        # A document that failed keeps its previous version, marked stale. One
        # with no previous version cannot be shown yet; it is retried next sync.
        self._stale_ids = {doc_id for doc_id in failed if doc_id in self._records}
        complete = not failed
        self._synced_root_hash = root_hash if complete else None
        self._rebuild_documents()

        if failed:
            logger.warning(
                "Sync left %d document(s) at their previous version; will retry", len(failed)
            )
        logger.debug(
            "Synced root %s: %d fetched, %d removed, %d unchanged",
            root_hash,
            len(fetched),
            len(removed),
            len(entries) - len(changed),
        )

        store = self._state_store
        if store is None:
            return
        try:
            store.save_sync_state(root_hash if complete else None, fetched, removed)
        except Exception:
            logger.warning("Could not persist sync state", exc_info=True)
        # Searchable text of a changed document came from its old version.
        if hasattr(store, "mark_document_version"):
            for doc_id in replaced:
                try:
                    store.mark_document_version(doc_id, fetched[doc_id]["entry"]["hash"])
                except Exception:
                    logger.warning("Could not invalidate index for %s", doc_id, exc_info=True)

    def get_meta_items(
        self,
        limit: Optional[int] = None,
        root_hash: Optional[str] = None,
    ) -> List[Document]:
        """
        Return the library's documents and folders, synced incrementally.

        The root index lists every document with the hash of its current
        version. Only documents whose hash differs from the last known state
        (kept in memory and persisted in the index database) are fetched, so an
        unchanged library costs one request and a single edit costs four: the
        root pointer, the root index, the document's index and its metadata.

        Args:
            limit: Maximum number of documents to return. If None, returns all.
            root_hash: Pre-fetched root hash. If provided, skips the root hash
                API call (avoids a duplicate request when the caller already has it).
        """
        with self._sync_lock:
            if root_hash is None:
                root_hash = self.get_root_hash()
            self._load_state_once()
            if root_hash != self._synced_root_hash:
                self._sync_to(root_hash)
            documents = list(self._documents)
        return documents[:limit] if limit is not None else documents

    def download(self, doc: Document) -> bytes:
        """Download a document's content as a zip file.

        Raises if any blob cannot be fetched: an archive with a page silently
        missing reads as a document with less in it.
        """
        import io
        import zipfile

        blob_content = self._get_file(doc.hash, _index_filename(doc.id))
        blob_entries = self._parse_index(blob_content)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in blob_entries:
                try:
                    file_content = self._get_file(entry["hash"], entry["id"])
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to download {entry['id']} (hash={entry['hash']}) "
                        f"for document {doc.id}: {e}"
                    ) from e
                zf.writestr(entry["id"], file_content)

        zip_buffer.seek(0)
        return zip_buffer.read()


def register_device(one_time_code: str) -> Dict[str, str]:
    """
    Register a new device with reMarkable cloud.

    Args:
        one_time_code: Code from https://my.remarkable.com/device/apps/connect

    Returns:
        Dict with devicetoken and usertoken keys
    """
    from uuid import uuid4

    body = {
        "code": one_time_code,
        "deviceDesc": "desktop-linux",
        "deviceID": str(uuid4()),
    }

    try:
        response = requests.post(DEVICE_TOKEN_URL, json=body, timeout=30)
        if response.status_code == 200 and response.text:
            device_token = response.text.strip()
            return {"devicetoken": device_token, "usertoken": ""}
    except requests.RequestException as e:
        raise RuntimeError(f"Network error during registration: {e}")

    raise RuntimeError(
        f"Registration failed (HTTP {response.status_code}). This usually means:\n"
        "  1. The code has expired (codes are single-use)\n"
        "  2. The code was already used\n"
        "  3. The code was typed incorrectly\n\n"
        "Get a new code from: https://my.remarkable.com/device/apps/connect"
    )


def load_client_from_token(token_data: str) -> RemarkableClient:
    """
    Create a client from a token string.

    Args:
        token_data: Either:
            - JSON string with devicetoken and optional usertoken
            - Raw JWT device token (legacy format from rmapy)

    Returns:
        Configured RemarkableClient
    """
    token_data = token_data.strip()

    # Try to parse as JSON first
    if token_data.startswith("{"):
        try:
            data = json.loads(token_data)
            return RemarkableClient(
                device_token=data.get("devicetoken", ""),
                user_token=data.get("usertoken", ""),
            )
        except json.JSONDecodeError:
            pass

    # Treat as raw device token (legacy rmapy format - just the JWT)
    # JWT tokens start with "eyJ" (base64 encoded '{"')
    if token_data.startswith("eyJ"):
        return RemarkableClient(device_token=token_data, user_token="")

    # Never echo the token itself — this message reaches logs and tool output.
    raise ValueError(
        "Invalid token format. Expected either a JSON object with a "
        "'devicetoken' key, or a raw JWT device token.\n"
        f"Got {len(token_data)} characters that match neither shape.\n"
        "Re-authenticate by running: uvx rm-mcp --setup"
    )
