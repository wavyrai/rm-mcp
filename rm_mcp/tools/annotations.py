"""remarkable_annotations — highlights and handwritten pages."""

from typing import Optional

from rm_mcp import reader
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers
from rm_mcp.tools.schemas import AnnotationsResult


@mcp.tool(title="reMarkable Annotations", annotations=_helpers.read_only("reMarkable Annotations"))
async def remarkable_annotations(id: str, pages: Optional[str] = None) -> AnnotationsResult:
    """Highlights (with their text) and the pages carrying handwriting or drawings.

    Both highlight formats are read: in-page highlights from current firmware
    and the older per-page .json files. Page numbers are tablet pages.

    Args:
        id: Document id; a path or name also works.
        pages: Limit to these pages ('2-5', 'all'). Default: every page.
    """
    return AnnotationsResult(**await _helpers.run_blocking(reader.annotations, id, pages))
