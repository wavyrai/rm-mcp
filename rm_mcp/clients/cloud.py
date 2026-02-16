"""
reMarkable Cloud Sync Client

A replacement for rmapy that uses the current reMarkable sync API (v3/v4).
rmapy is abandoned and uses deprecated endpoints that return 500 errors.

Based on the protocol used by ddvk/rmapi.
"""

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from rm_mcp.models import Document, Folder  # noqa: F401

logger = logging.getLogger(__name__)

# Maximum number of parallel workers for fetching document metadata
_PARALLEL_WORKERS = int(os.environ.get("REMARKABLE_PARALLEL_WORKERS", "5"))

# API endpoints
# Note: my.remarkable.com endpoints redirect to doesnotexist.remarkable.com
# So we use webapp-prod.cloud.remarkable.engineering for auth
AUTH_HOST = "https://webapp-prod.cloud.remarkable.engineering"
DEVICE_TOKEN_URL = f"{AUTH_HOST}/token/json/2/device/new"
USER_TOKEN_URL = f"{AUTH_HOST}/token/json/2/user/new"

SYNC_HOST = "https://internal.cloud.remarkable.com"
ROOT_URL = f"{SYNC_HOST}/sync/v4/root"
FILES_URL = f"{SYNC_HOST}/sync/v3/files"


class RemarkableClient:
    """Client for reMarkable Cloud sync API."""

    def __init__(self, device_token: str = "", user_token: str = ""):
        self.device_token = device_token
        self.user_token = user_token
        self._documents: List[Document] = []
        self._documents_by_id: Dict[str, Document] = {}
        self._token_lock = threading.Lock()

        # Connection-pooling session with retry logic
        self._session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=10,
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

    def _request(self, url: str, method: str = "GET") -> requests.Response:
        """Make an authenticated request using the pooled session."""
        if not self.user_token:
            self.renew_token()

        headers = {"Authorization": f"Bearer {self.user_token}"}
        response = self._session.request(method, url, headers=headers, timeout=60)

        if response.status_code == 401:
            # Token expired, try to renew (thread-safe)
            with self._token_lock:
                # Re-check: another thread may have already renewed
                current_auth = f"Bearer {self.user_token}"
                if headers["Authorization"] == current_auth:
                    self.renew_token()
            headers = {"Authorization": f"Bearer {self.user_token}"}
            response = self._session.request(method, url, headers=headers, timeout=60)

        return response

    def _get_file(self, file_hash: str) -> bytes:
        """Download a file by its hash."""
        response = self._request(f"{FILES_URL}/{file_hash}")
        response.raise_for_status()
        return response.content

    def _parse_index(self, content: bytes) -> List[Dict[str, Any]]:
        """Parse an index file into entries."""
        lines = content.decode("utf-8").strip().split("\n")
        entries = []

        # First line is schema version
        for line in lines[1:]:
            try:
                parts = line.split(":")
                if len(parts) >= 5:
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

    def _fetch_document_meta(self, entry: Dict[str, Any]) -> Optional[Document]:
        """Fetch metadata for a single document entry.

        Args:
            entry: Index entry with hash, id, size keys

        Returns:
            Document if successful, None if skipped/failed
        """
        doc_id = entry["id"]
        doc_hash = entry["hash"]

        # Fetch the document's blob index
        try:
            blob_content = self._get_file(doc_hash)
            blob_entries = self._parse_index(blob_content)
        except Exception:
            logger.debug("Failed to fetch blob index for document %s (hash=%s)", doc_id, doc_hash)
            return None

        # Find and fetch the metadata file
        metadata = {}
        files = []

        for blob_entry in blob_entries:
            files.append(blob_entry)
            if blob_entry["id"].endswith(".metadata"):
                try:
                    meta_content = self._get_file(blob_entry["hash"])
                    metadata = json.loads(meta_content.decode("utf-8"))
                except Exception:
                    logger.warning(
                        "Failed to fetch/parse metadata for document %s (blob hash=%s)",
                        doc_id, blob_entry["hash"],
                    )

        # Skip deleted documents
        if metadata.get("deleted", False):
            return None

        # Parse last modified timestamp
        last_modified = None
        if "lastModified" in metadata:
            try:
                ts = int(metadata["lastModified"]) / 1000  # Convert ms to seconds
                last_modified = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError):
                pass

        return Document(
            id=doc_id,
            hash=doc_hash,
            name=metadata.get("visibleName", doc_id),
            doc_type=metadata.get("type", "DocumentType"),
            parent=metadata.get("parent", ""),
            deleted=metadata.get("deleted", False),
            pinned=metadata.get("pinned", False),
            last_modified=last_modified,
            size=entry["size"],
            files=files,
        )

    def get_meta_items(
        self,
        limit: Optional[int] = None,
        root_hash: Optional[str] = None,
    ) -> List[Document]:
        """
        Fetch documents and folders from the cloud.

        Uses parallel fetching with ThreadPoolExecutor for ~3-5x speedup
        on large libraries (connection pooling + concurrent requests).

        Args:
            limit: Maximum number of documents to fetch. If None, fetches all.
            root_hash: Pre-fetched root hash. If provided, skips the root hash
                API call (avoids a duplicate request when the caller already has it).

        Returns a list of Document objects (compatible with rmapy Collection).
        """
        if root_hash is None:
            # Fetch the root hash from the API
            response = self._request(ROOT_URL)
            response.raise_for_status()

            # Handle empty or invalid JSON response
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

            root_hash = root_data["hash"]

        # Get root index
        try:
            root_index = self._get_file(root_hash)
            entries = self._parse_index(root_index)
        except Exception as e:
            raise RuntimeError(f"Failed to parse root index (hash={root_hash}): {e}") from e

        if limit is not None:
            entries = entries[:limit]

        # Fetch document metadata in parallel
        documents = []
        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as executor:
            futures = {
                executor.submit(self._fetch_document_meta, entry): entry
                for entry in entries
            }
            for future in as_completed(futures):
                try:
                    doc = future.result()
                    if doc is not None:
                        documents.append(doc)
                except Exception:
                    entry = futures[future]
                    logger.warning(
                        "Failed to fetch metadata for entry %s (hash=%s)",
                        entry.get("id", "unknown"), entry.get("hash", "unknown"),
                    )

        self._documents = documents
        self._documents_by_id = {d.id: d for d in documents}

        return documents

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        if not self._documents_by_id:
            self.get_meta_items()
        return self._documents_by_id.get(doc_id)

    def download(self, doc: Document) -> bytes:
        """Download a document's content as a zip file."""
        # The document blob contains all the files
        # We need to fetch each file and create a zip
        import io
        import zipfile

        blob_content = self._get_file(doc.hash)
        blob_entries = self._parse_index(blob_content)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in blob_entries:
                file_id = entry["id"]
                file_hash = entry["hash"]

                # Download the file
                try:
                    file_content = self._get_file(file_hash)
                    zf.writestr(file_id, file_content)
                except Exception:
                    logger.warning(
                        "Failed to download file %s (hash=%s) for document %s",
                        file_id, file_hash, doc.id,
                    )
                    continue

        zip_buffer.seek(0)
        return zip_buffer.read()


def register_device(one_time_code: str) -> Dict[str, str]:
    """
    Register a new device with reMarkable cloud.

    Args:
        one_time_code: Code from https://my.remarkable.com/device/browser/connect

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
        "Get a new code from: https://my.remarkable.com/device/browser/connect"
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

    raise ValueError(
        f"Invalid token format. Expected JSON or JWT token.\n"
        f"Token starts with: {token_data[:20]}..."
    )


def load_client_from_file(token_file: Path = Path.home() / ".rmapi") -> RemarkableClient:
    """
    Load a client from a token file.

    Args:
        token_file: Path to JSON token file (default: ~/.rmapi)

    Returns:
        Configured RemarkableClient
    """
    if not token_file.exists():
        raise RuntimeError(
            f"Token file not found: {token_file}\n"
            "Run: uvx rm-mcp --setup"
        )

    token_json = token_file.read_text()
    return load_client_from_token(token_json)
