"""
reMarkable transport backends.

Provides cloud (sync API) client implementation.
"""

from rm_mcp.clients.cloud import (  # noqa: F401
    RemarkableClient,
    load_client_from_file,
    load_client_from_token,
    register_device,
)
