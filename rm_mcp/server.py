"""
reMarkable MCP Server initialization.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import quote, unquote

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


class RemarkableMCP(FastMCP):
    """Custom FastMCP server that handles VS Code's URI quirks.

    VS Code:
    - Appends ?version=... to resource URIs for cache busting
    - May send URIs with spaces or URL-encoded (%20)

    Pydantic's AnyUrl stores URIs with URL-encoded paths, so we need to
    normalize incoming URIs to match.
    """

    async def read_resource(self, uri):
        """Read a resource, normalizing the URI for lookup.

        Handles:
        - Query parameters: ?version=timestamp -> stripped
        - Spaces in path: encode to %20 to match stored URIs
        """
        uri_str = str(uri)

        # Strip query parameters (e.g., ?version=1764625282944)
        if "?" in uri_str:
            uri_str = uri_str.split("?")[0]
            logger.debug("Stripped query params from resource URI")

        # Normalize path encoding - Pydantic AnyUrl stores with %20 for spaces
        # VS Code may send either spaces or %20, so normalize to %20
        if ":///" in uri_str:
            scheme_end = uri_str.index(":///") + 4
            scheme = uri_str[:scheme_end]
            path = uri_str[scheme_end:]

            # First decode any existing encoding, then re-encode consistently
            # This handles both "November 2025" and "November%202025" inputs
            decoded_path = unquote(path)
            # quote with safe='/' preserves path separators but encodes spaces
            encoded_path = quote(decoded_path, safe="/:")
            uri_str = scheme + encoded_path
            logger.debug(f"Normalized resource URI path: {path} -> {encoded_path}")

        return await super().read_resource(uri_str)


def _build_instructions() -> str:
    """Build server instructions based on current configuration."""
    return """# reMarkable MCP Server

Access documents from your reMarkable tablet. All operations are read-only.

## Available Tools

- `remarkable_browse(path)` - Browse folders (auto-redirects to read for documents)
- `remarkable_read(document, page, pages, grep)` - Read document content
- `remarkable_recent(limit)` - Get recently modified documents
- `remarkable_search(query, grep, limit)` - Search by name and indexed content
- `remarkable_status()` - Check connection and diagnose issues
- `remarkable_image(document, page, include_ocr)` - Get a PNG image with optional OCR

## Recommended Workflows

### Finding and Reading Documents
1. Use `remarkable_search("keyword")` to find documents by name or content
2. Use `remarkable_read("Document Name")` to get content
3. Use `remarkable_read("Document", pages="all")` to get complete content in one call
4. Use `remarkable_read("Document", grep="pattern")` to search within a document (auto-redirects to matching page)

### Getting Page Images
Use `remarkable_image` when you need visual context:
- Hand-drawn diagrams, sketches, or UI mockups
- Content that text extraction might miss

### Key Features
- **Multi-page read**: Use `pages="all"` or `pages="1-3"` to get multiple pages in one call
- **Grep auto-redirect**: grep automatically finds and returns the matching page
- **Full-text search**: Reading a document indexes its content for future search
- **Compact mode**: Use `compact_output=True` on any tool to omit hints and reduce response size
- **Auto-OCR opt-out**: Use `auto_ocr=False` to skip automatic OCR on empty notebooks

## MCP Resources

Documents are registered as resources for direct access:
- `remarkable:///{path}.txt` - Get full extracted text content in one request
- `remarkableimg:///{path}.page-{N}.png` - Get PNG image of page N (notebooks only)

## OCR (Sampling Mode Active)

OCR is configured to use this client's AI model via MCP sampling.
Use `remarkable_image("Document", include_ocr=True)` to extract text from images.
This requires no external API keys - it uses your client's capabilities.
"""


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[None]:
    """Lifespan context manager for the MCP server."""
    from rm_mcp.resources import (
        start_background_loader,
        stop_background_loader,
    )

    # Initialize the persistent document index (L2 cache)
    from rm_mcp import index as _index_mod

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


# Initialize FastMCP server with lifespan and instructions
mcp = RemarkableMCP("rm-mcp", instructions=_build_instructions(), lifespan=lifespan)

# Import tools, resources, and prompts to register them
from rm_mcp import (  # noqa: E402
    prompts,  # noqa: F401
    resources,  # noqa: F401
    tools,  # noqa: F401
)


def run():
    """Run the MCP server."""
    mcp.run()
