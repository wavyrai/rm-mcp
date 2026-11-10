"""remarkable_read — text per tablet page."""

from typing import Annotated, Optional

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from rm_mcp import reader
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers
from rm_mcp.tools.schemas import ReadResult

_OLD = "(deprecated) "


@mcp.tool(
    title="Read reMarkable Document", annotations=_helpers.read_only("Read reMarkable Document")
)
async def remarkable_read(
    id: Optional[str] = None,
    pages: Optional[str] = None,
    document: Annotated[Optional[str], Field(description=_OLD + "use id")] = None,
    page: Annotated[Optional[int], Field(description=_OLD + "use pages")] = None,
    grep: Annotated[
        Optional[str], Field(description=_OLD + "keep only pages matching this regex")
    ] = None,
    content_type: Annotated[
        Optional[str], Field(description=_OLD + "use remarkable_annotations")
    ] = None,
) -> ReadResult:
    """Read a document's text, one entry per tablet page.

    Text is typed text plus the PDF/EPUB text under the page, and any saved
    transcript of its handwriting. A handwritten page with no transcript comes
    back as {"handwriting": true, "transcript": null}: look at it with
    remarkable_view and save what you read with remarkable_save_transcript.
    Page numbers are the tablet's, so pages inserted into a PDF count. Output
    is capped (REMARKABLE_MAX_OUTPUT_CHARS); cut pages are listed in cut_pages.

    Args:
        id: Document id (from remarkable_find/remarkable_list); a path or name also works.
        pages: '3', '1-5', '2,4', '7-' or 'all'. Default '1-5'.
    """
    notes = []
    if document is not None:
        notes.append("document=")
        id = id or document
    if page is not None:
        notes.append("page=")
        pages = pages or str(page)
    if grep is not None:
        notes.append("grep=")
        pages = pages or "all"
    if content_type not in (None, "text"):
        raise ToolError(
            "content_type='annotations' moved to its own tool: "
            f"call remarkable_annotations(id='{id or document}')."
        )
    if not id:
        raise ToolError("No document given. Pass id (from remarkable_find or remarkable_list).")
    data = await _helpers.run_blocking(reader.read, id, pages or "1-5", grep)
    result = ReadResult(**data)
    if notes:
        result.deprecated = (
            f"deprecated: {', '.join(notes)} will be removed; use remarkable_read(id, pages) "
            "and remarkable_find for searching."
        )
    return result
