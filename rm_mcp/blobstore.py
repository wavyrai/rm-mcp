"""
Content-addressed on-disk blob store.

The sync API addresses every blob by the SHA-256 of its bytes, so a blob seen
once never has to be fetched again: the same hash always means the same bytes.
The store keeps blobs on disk under their hash, bounded by a byte cap with
least-recently-used eviction, and verifies the hash on every read and write —
a blob whose bytes do not match its hash is never served.

Settings:
    REMARKABLE_BLOB_CACHE_DIR  location, default ~/.cache/rm-mcp/blobs
    REMARKABLE_BLOB_CACHE_MB   size cap in MB, default 500; 0 disables the store
"""

import hashlib
import logging
import os
import re
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from rm_mcp.config import env_int

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path.home() / ".cache" / "rm-mcp" / "blobs"
_DEFAULT_CAP_MB = 500
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class BlobHashMismatch(ValueError):
    """The bytes offered for a hash are not the bytes that hash names."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobStore:
    """Thread-safe, size-capped, content-addressed blob cache on disk."""

    def __init__(self, root: Path, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        # hash -> size, least recently used first
        self._entries: "OrderedDict[str, int]" = OrderedDict()
        self.root.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        found = []
        for path in self.root.glob("*/*"):
            if not _HASH_RE.match(path.name):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((stat.st_mtime, path.name, stat.st_size))
        for _mtime, digest, size in sorted(found):
            self._entries[digest] = size

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return sum(self._entries.values())

    def path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def get(self, digest: str) -> Optional[bytes]:
        """Return the blob's bytes, or None if absent or corrupted."""
        if not _HASH_RE.match(digest):
            return None
        path = self.path_for(digest)
        with self._lock:
            if digest not in self._entries:
                return None
            try:
                data = path.read_bytes()
            except OSError:
                self._entries.pop(digest, None)
                return None
            if sha256_hex(data) != digest:
                logger.warning("Blob %s failed its hash check on disk; discarding it", digest)
                self._entries.pop(digest, None)
                path.unlink(missing_ok=True)
                return None
            self._entries.move_to_end(digest)
            try:
                os.utime(path)  # keeps LRU order across restarts
            except OSError:
                pass
            return data

    def put(self, digest: str, data: bytes) -> None:
        """Store a blob. Raises BlobHashMismatch if `data` is not `digest`'s bytes."""
        if sha256_hex(data) != digest:
            raise BlobHashMismatch(f"Bytes do not match blob hash {digest}")
        if len(data) > self.max_bytes:
            return
        path = self.path_for(digest)
        with self._lock:
            if digest in self._entries:
                self._entries.move_to_end(digest)
                return
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except OSError:
                logger.warning("Could not write blob %s to the store", digest, exc_info=True)
                return
            self._entries[digest] = len(data)
            self._evict_locked()

    def _evict_locked(self) -> None:
        total = sum(self._entries.values())
        while total > self.max_bytes and self._entries:
            digest, size = self._entries.popitem(last=False)
            self.path_for(digest).unlink(missing_ok=True)
            total -= size


_default: Optional[BlobStore] = None
_default_lock = threading.Lock()
_DISABLED = object()


def get_default_store() -> Optional[BlobStore]:
    """The process-wide store, or None when disabled or unavailable."""
    global _default
    with _default_lock:
        if _default is _DISABLED:
            return None
        if _default is None:
            cap_mb = env_int("REMARKABLE_BLOB_CACHE_MB", _DEFAULT_CAP_MB, minimum=0)
            if cap_mb == 0:
                _default = _DISABLED
                return None
            root = Path(os.environ.get("REMARKABLE_BLOB_CACHE_DIR") or _DEFAULT_DIR)
            try:
                _default = BlobStore(root, cap_mb * 1024 * 1024)
            except OSError:
                logger.warning("Blob store unavailable at %s", root, exc_info=True)
                _default = _DISABLED
                return None
        return _default


def reset_default_store() -> None:
    """Forget the process-wide store (tests, config changes)."""
    global _default
    with _default_lock:
        _default = None
