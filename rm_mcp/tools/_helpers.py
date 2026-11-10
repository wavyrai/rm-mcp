"""
Shared helpers for MCP tool modules.

The organising tools reach these through ``_helpers.X`` so a test can patch one
name (e.g. ``rm_mcp.tools._helpers.get_cached_collection``) for all of them.
"""

import functools
import logging
import os
from typing import Any, Callable

import anyio
from mcp.types import ToolAnnotations

from rm_mcp.cache import get_cached_collection  # noqa: F401
from rm_mcp.paths import (  # noqa: F401
    _apply_root_filter,
    _get_root_path,
    _is_within_root,
    _resolve_root_path,
    get_item_path,
    get_items_by_id,
)
from rm_mcp.responses import make_error, make_response  # noqa: F401

logger = logging.getLogger(__name__)


def is_compact(compact_output: bool = False) -> bool:
    """Check parameter or REMARKABLE_COMPACT env var."""
    return compact_output or os.environ.get("REMARKABLE_COMPACT", "").lower() in (
        "1",
        "true",
        "yes",
    )


async def run_blocking(func: Callable[..., Any], *args, **kwargs) -> Any:
    """Run a blocking (network/CPU-bound) callable off the event loop."""
    return await anyio.to_thread.run_sync(functools.partial(func, *args, **kwargs))


def suggest_for_error(e: Exception) -> str:
    """Generate a context-aware suggestion based on error content."""
    msg = str(e).lower()
    if "not authenticated" in msg or "no device token" in msg:
        return "Run: uvx rm-mcp@latest setup to authenticate."
    if "re-authenticate" in msg or ("token" in msg and ("expired" in msg or "401" in msg)):
        return "Your token may have expired. Run: uvx rm-mcp@latest setup to re-authenticate."
    if "network error" in msg or "connection" in msg or "timeout" in msg:
        return "Check your internet connection and try again."
    if "empty response" in msg:
        return (
            "The reMarkable API returned an empty response. "
            "Your token may have expired. Run: uvx rm-mcp@latest setup"
        )
    return "Check remarkable_status() for diagnostics."


def invalidate_after_write() -> None:
    """Drop the collection cache after a structural change (rename, move, new folder)."""
    from rm_mcp.cache import invalidate_collection_cache

    invalidate_collection_cache()


# --- Tool annotations ---

_READ_ONLY = {
    "read_only_hint": True,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": False,  # a private cloud account, not the open world
}


def read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, **_READ_ONLY)


def deprecated(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=f"{title} (deprecated)", **_READ_ONLY)
