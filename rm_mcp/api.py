"""
reMarkable Cloud API client helpers.
"""

import logging
import threading
from typing import Optional

from rm_mcp import auth
from rm_mcp.models import RemarkableClientProtocol

logger = logging.getLogger(__name__)

# --- Singleton client ---
_client_singleton: Optional[RemarkableClientProtocol] = None
# Where the singleton's token came from, and the token file's (mtime, size)
# when it was read. A client built from our token file is rebuilt when
# `rm-mcp setup` replaces the file, so a running server picks up the new login.
_client_source: Optional[auth.TokenSource] = None
_client_signature: Optional[tuple] = None
_lock = threading.Lock()


def get_rmapi() -> Optional[RemarkableClientProtocol]:
    """
    Get or initialize the reMarkable API client.

    The token is read from rm-mcp's token file, then REMARKABLE_TOKEN, then the
    legacy ~/.rmapi (see rm_mcp.auth). Returns None if there is no token.
    """
    global _client_singleton, _client_source, _client_signature

    with _lock:
        if _client_singleton is not None and not _token_file_replaced():
            return _client_singleton

        from rm_mcp.clients.cloud import load_client_from_token

        source = auth.find_token()
        if source is None:
            _client_singleton = _client_source = _client_signature = None
            return None
        try:
            client = load_client_from_token(source.data)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize reMarkable client: {e}")
        if source.kind == "file":
            client.on_token_renewed = _save_renewed_user_token
        _client_singleton, _client_source = client, source
        _client_signature = auth.file_signature(source.path) if source.path else None
        return client


def current_source() -> Optional[auth.TokenSource]:
    """Where the active (or next) client's token comes from."""
    return _client_source or auth.find_token()


def _token_file_replaced() -> bool:
    """True when the client came from our token file and setup has replaced it since."""
    global _client_signature
    if _client_source is None or _client_source.kind != "file":
        return False
    signature = auth.file_signature(_client_source.path)
    if signature == _client_signature:
        return False
    try:
        current = auth.parse(auth._read(_client_source.path))["devicetoken"]
    except ValueError:
        return True
    if current != getattr(_client_singleton, "device_token", None):
        return True
    _client_signature = signature
    return False


def _save_renewed_user_token(user_token: str) -> None:
    global _client_signature
    path = _client_source.path if _client_source is not None else None
    if path is None:
        return
    auth.save_user_token(user_token, path)
    _client_signature = auth.file_signature(path)


def register_and_get_token(one_time_code: str) -> str:
    """
    Register with reMarkable using a one-time code and save the token to
    rm-mcp's own token file (never ~/.rmapi). Returns the file's path.

    Get a code from: https://my.remarkable.com/device/apps/connect
    """
    from rm_mcp.clients.cloud import register_device

    token_data = register_device(one_time_code, auth.device_desc())
    return str(auth.save_token(token_data))


def get_file_type(client, doc) -> str:
    """
    Get the file type (pdf, epub, notebook) for a document.

    The document's blob list is authoritative: a document containing a .pdf
    blob is a PDF whatever it happens to be called. Plenty of documents are
    named without an extension — an arXiv paper saved as "2304.03442" is a PDF,
    and calling it a notebook sends reads down the OCR path for text that is
    already there.

    Args:
        client: The reMarkable API client
        doc: The document to check

    Returns:
        File type string: 'pdf', 'epub', or 'notebook'
    """
    # Blob names come from the sync index and are already loaded — no extra
    # request is needed to consult them.
    files = getattr(doc, "files", None)
    if isinstance(files, (list, tuple)):
        for entry in files:
            blob_name = (entry.get("id") if isinstance(entry, dict) else "") or ""
            lowered = blob_name.lower()
            if lowered.endswith(".pdf"):
                return "pdf"
            if lowered.endswith(".epub"):
                return "epub"

    # Next, the .content file's fileType, read at sync time.
    content_type = getattr(doc, "file_type", None)
    if content_type in ("pdf", "epub"):
        return content_type

    # Fall back to the visible name when neither says.
    name = doc.VissibleName.lower()
    if name.endswith(".pdf"):
        return "pdf"
    elif name.endswith(".epub"):
        return "epub"

    return "notebook"
