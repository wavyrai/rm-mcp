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

logger = logging.getLogger(__name__)

# --- Singleton client ---
_client_singleton: Optional[RemarkableClientProtocol] = None


def get_rmapi() -> Optional[RemarkableClientProtocol]:
    """
    Get or initialize the reMarkable API client.

    Uses a singleton pattern so the client is only created once per process.
    Returns None if no token is configured (unauthenticated mode).
    """
    global _client_singleton

    # Return cached client if available
    if _client_singleton is not None:
        return _client_singleton

    # Cloud API mode
    from rm_mcp.clients.cloud import load_client_from_token

    # If token is provided via environment, use it
    if REMARKABLE_TOKEN:
        _client_singleton = load_client_from_token(REMARKABLE_TOKEN)
        return _client_singleton

    # Load from file
    rmapi_file = Path.home() / ".rmapi"
    if not rmapi_file.exists():
        return None

    try:
        token_json = rmapi_file.read_text()
        _client_singleton = load_client_from_token(token_json)
        return _client_singleton
    except Exception as e:
        raise RuntimeError(f"Failed to initialize reMarkable client: {e}")


def register_and_get_token(one_time_code: str) -> str:
    """
    Register with reMarkable using a one-time code and return the token.

    Get a code from: https://my.remarkable.com/device/apps/connect
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
