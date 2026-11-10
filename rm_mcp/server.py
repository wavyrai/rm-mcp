"""
reMarkable MCP Server initialization.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import unquote

from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)


def normalize_resource_uri(uri: str) -> str:
    """Map a client-sent resource URI onto the form resources are registered under.

    Resources are registered with the document's display path as-is, spaces
    included. VS Code appends ``?version=...`` for cache busting and may send
    the path either raw or percent-encoded, so strip the query and decode the
    path before lookup.
    """
    if "?" in uri:
        uri = uri.split("?", 1)[0]
    if ":///" in uri:
        scheme_end = uri.index(":///") + 4
        uri = uri[:scheme_end] + unquote(uri[scheme_end:])
    return uri


class RemarkableMCP(MCPServer):
    """MCP server that tolerates VS Code's resource URI quirks."""

    async def read_resource(self, uri, context=None):
        normalized = normalize_resource_uri(str(uri))
        if normalized != str(uri):
            logger.debug("Normalized resource URI %s -> %s", uri, normalized)
        return await super().read_resource(normalized, context)


def _build_instructions() -> str:
    """Build server instructions based on current configuration."""
    from rm_mcp.paths import _get_root_path

    base = """# reMarkable MCP Server

Documents are addressed by a stable id (UUID). Paths and names are accepted and
resolved, and every result carries the id: pass the id on.

## Reading (never changes anything)

- `remarkable_find(query?, folder?, type?, modified_after?)` - search names, typed text,
  PDF/EPUB text, highlights and saved transcripts; no query = recent documents
- `remarkable_list(folder?, depth?, sort?)` - the folder tree with ids
- `remarkable_read(id, pages="1-5")` - text per tablet page
- `remarkable_view(id, pages="1")` - page images with the ink (max 4 per call)
- `remarkable_annotations(id)` - highlights and pages with handwriting
- `remarkable_status()` - connection, index and settings

## Handwriting

Handwritten pages have no text until someone reads them. remarkable_read marks
them `handwriting: true, transcript: null`. Look at them with remarkable_view,
then save what you read with `remarkable_save_transcript(id, page, text)`. It
writes only to the local index; find and read include it from then on, until
the page is edited on the tablet.

## Organising (changes the library's structure)

- `remarkable_rename(item, new_name)`, `remarkable_move(item, destination)`,
  `remarkable_create_folder(path)`

These never delete anything and never touch a document's pages. Confirm with
the user before reorganising anything they did not explicitly ask you to.

## Resources

- `remarkable://doc/{id}` - a document's text, page by page
- `remarkable://doc/{id}/page/{n}.png` - page n as an image
"""

    root = _get_root_path()
    if root != "/":
        base += f"""
## Scope

This server is restricted to the '{root}' folder. Paths in requests and
responses are relative to it, and documents outside it are not accessible.
"""

    return base


@asynccontextmanager
async def lifespan(app: MCPServer) -> AsyncIterator[None]:
    """Lifespan context manager for the MCP server."""
    # Initialize the persistent document index (L2 cache)
    from rm_mcp import index as _index_mod
    from rm_mcp.loader import start_background_loader, stop_background_loader

    idx = _index_mod.initialize()
    if idx is not None:
        # Allow forced rebuild via env var
        if os.environ.get("REMARKABLE_INDEX_REBUILD"):
            logger.info("REMARKABLE_INDEX_REBUILD set — clearing index")
            idx.clear()

    # Check if authenticated before starting background loader
    from rm_mcp.api import get_rmapi

    client = get_rmapi()
    if client is not None:
        logger.info("Cloud mode: starting background loader...")
        task = start_background_loader()
    else:
        logger.warning("Background loader skipped (not authenticated)")
        task = None

    try:
        yield
    finally:
        # Stop background loader on shutdown (if running)
        await stop_background_loader(task)
        # Close the document index
        _index_mod.close()


def _package_version() -> str:
    from rm_mcp import __version__

    return __version__


# Everything after the name is keyword-only by convention: in SDK 2.x the second
# positional parameter is `title`, so a positional instructions string would
# silently become the title.
mcp = RemarkableMCP(
    name="rm-mcp",
    title="reMarkable",
    instructions=_build_instructions(),
    version=_package_version(),
    lifespan=lifespan,
)

# Import tools, resources, and prompts to register them
from rm_mcp import (  # noqa: E402
    prompts,  # noqa: F401
    resources,  # noqa: F401
    tools,  # noqa: F401
)


def run():
    """Run the MCP server."""
    mcp.run(transport="stdio")
