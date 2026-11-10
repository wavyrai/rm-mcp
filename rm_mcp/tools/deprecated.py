"""Tool names from 0.7 and earlier, kept for one release as thin aliases.

Each maps onto a v2 tool and says which one in a ``deprecated`` field (or the
first text block for images). They will be removed in the next release.
"""

import json
from typing import List, Optional, Union

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from rm_mcp import reader
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers
from rm_mcp.tools.find import find_result
from rm_mcp.tools.listing import list_result
from rm_mcp.tools.schemas import ReadResult
from rm_mcp.tools.view import view_blocks


def _with_note(model, note: str) -> str:
    data = {"deprecated": note, **model.model_dump(mode="json")}
    return json.dumps(data, indent=2)


@mcp.tool(
    title="Search reMarkable Documents (deprecated)",
    annotations=_helpers.deprecated("Search reMarkable Documents"),
    structured_output=False,
)
async def remarkable_search(
    query: str, grep: Optional[str] = None, limit: int = 5, include_ocr: bool = False
) -> str:
    """Deprecated: use remarkable_find(query). Kept for one release."""
    result = await _helpers.run_blocking(find_result, query=query, limit=limit)
    return _with_note(result, "remarkable_search is deprecated: use remarkable_find(query).")


@mcp.tool(
    title="Recent reMarkable Documents (deprecated)",
    annotations=_helpers.deprecated("Recent reMarkable Documents"),
    structured_output=False,
)
async def remarkable_recent(limit: int = 10, include_preview: bool = False) -> str:
    """Deprecated: use remarkable_find() with no query. Kept for one release."""
    result = await _helpers.run_blocking(find_result, limit=limit)
    return _with_note(result, "remarkable_recent is deprecated: use remarkable_find().")


def _browse(path: str) -> str:
    try:
        result = list_result(folder=path)
        note = "remarkable_browse is deprecated: use remarkable_list(folder)."
    except ToolError:
        # Old browse redirected a document path to its first page.
        snap = reader.snapshot()
        entry = reader.resolve_document(snap, path)
        result = ReadResult(**reader.read(entry.id, "1"))
        note = "remarkable_browse is deprecated: use remarkable_read(id) for documents."
    return _with_note(result, note)


@mcp.tool(
    title="Browse reMarkable Library (deprecated)",
    annotations=_helpers.deprecated("Browse reMarkable Library"),
    structured_output=False,
)
async def remarkable_browse(path: str = "/") -> str:
    """Deprecated: use remarkable_list(folder). Kept for one release."""
    return await _helpers.run_blocking(_browse, path)


@mcp.tool(
    title="Get reMarkable Page Image (deprecated)",
    annotations=_helpers.deprecated("Get reMarkable Page Image"),
)
async def remarkable_image(
    document: str,
    page: int = 1,
    background: Optional[str] = None,
    output_format: str = "png",
    compatibility: bool = False,
    include_ocr: bool = False,
) -> List[Union[TextContent, ImageContent]]:
    """Deprecated: use remarkable_view(id, pages). Kept for one release; PNG only."""
    blocks = await _helpers.run_blocking(view_blocks, document, str(page), "normal")
    note = "remarkable_image is deprecated: use remarkable_view(id, pages)."
    if output_format.lower() != "png" or background or compatibility or include_ocr:
        note += " output_format, background, compatibility and include_ocr are ignored."
    return [TextContent(type="text", text=note), *blocks]
