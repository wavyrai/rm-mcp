"""
reMarkable Cloud API client helpers.
"""

import json as json_module
import logging
import os
from pathlib import Path
from typing import Optional

from rm_mcp.models import RemarkableClientProtocol

# Configuration - check env var first, then fall back to file
REMARKABLE_TOKEN = os.environ.get("REMARKABLE_TOKEN")
REMARKABLE_CONFIG_DIR = Path.home() / ".remarkable"
REMARKABLE_TOKEN_FILE = REMARKABLE_CONFIG_DIR / "token"
CACHE_DIR = REMARKABLE_CONFIG_DIR / "cache"

logger = logging.getLogger(__name__)

# --- Singleton client ---
_client_singleton: Optional[RemarkableClientProtocol] = None

# Track whether we've already written the token file for this process
_token_file_written = False


def get_rmapi() -> RemarkableClientProtocol:
    """
    Get or initialize the reMarkable API client.

    Uses a singleton pattern so the client is only created once per process.
    """
    global _client_singleton, _token_file_written

    # Return cached client if available
    if _client_singleton is not None:
        return _client_singleton

    # Cloud API mode
    from rm_mcp.clients.cloud import load_client_from_token

    # If token is provided via environment, use it
    if REMARKABLE_TOKEN:
        # Save to ~/.rmapi for compatibility (only once per process)
        if not _token_file_written:
            rmapi_file = Path.home() / ".rmapi"
            fd = os.open(str(rmapi_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, REMARKABLE_TOKEN.encode())
            finally:
                os.close(fd)
            _token_file_written = True
        _client_singleton = load_client_from_token(REMARKABLE_TOKEN)
        return _client_singleton

    # Load from file
    rmapi_file = Path.home() / ".rmapi"
    if not rmapi_file.exists():
        raise RuntimeError(
            "No reMarkable token found. Register first:\n"
            "  uvx rm-mcp --register <code>\n\n"
            "Get a code from: https://my.remarkable.com/device/browser/connect"
        )

    try:
        token_json = rmapi_file.read_text()
        _client_singleton = load_client_from_token(token_json)
        return _client_singleton
    except Exception as e:
        raise RuntimeError(f"Failed to initialize reMarkable client: {e}")


def ensure_config_dir():
    """Ensure configuration directory exists."""
    REMARKABLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def register_and_get_token(one_time_code: str) -> str:
    """
    Register with reMarkable using a one-time code and return the token.

    Get a code from: https://my.remarkable.com/device/browser/connect
    """
    from rm_mcp.clients.cloud import register_device

    try:
        token_data = register_device(one_time_code)

        # Save to ~/.rmapi for compatibility
        rmapi_file = Path.home() / ".rmapi"
        token_json = json_module.dumps(token_data)
        fd = os.open(str(rmapi_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token_json.encode())
        finally:
            os.close(fd)

        return token_json
    except Exception as e:
        raise RuntimeError(str(e))


def get_file_type(client, doc) -> str:
    """
    Get the file type (pdf, epub, notebook) for a document.

    Args:
        client: The reMarkable API client
        doc: The document to check

    Returns:
        File type string: 'pdf', 'epub', or 'notebook'
    """
    # Infer from document name
    name = doc.VissibleName.lower()
    if name.endswith(".pdf"):
        return "pdf"
    elif name.endswith(".epub"):
        return "epub"

    return "notebook"
