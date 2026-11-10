"""remarkable_view — page images with the ink, for reading handwriting and drawings."""

import base64
from typing import List, Literal, Union

from mcp.types import ImageContent, TextContent

from rm_mcp import reader
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers


def view_blocks(id: str, pages: str, size: str) -> List[Union[TextContent, ImageContent]]:
    info, images = reader.view(id, pages, size)
    shown = ", ".join(str(n) for n in info["shown"])
    text = (
        f"'{info['name']}' (id {info['id']}, {info['path']}): page(s) {shown} of "
        f"{info['page_count']}, size {size}."
    )
    if info["left_out"]:
        left = reader._ranges(info["left_out"])
        text += (
            f" At most {reader.MAX_VIEW_PAGES} pages and about 1 MB per call, so pages {left} "
            f"were left out: call remarkable_view(id='{info['id']}', pages='{left}')."
        )
    text += (
        " You can read handwriting in these images. To keep what you read, call "
        "remarkable_save_transcript(id, page, text) per page: remarkable_find and "
        "remarkable_read then include it, until the page is edited."
    )
    blocks: List[Union[TextContent, ImageContent]] = [TextContent(type="text", text=text)]
    for png in images:
        blocks.append(
            ImageContent(
                type="image", data=base64.b64encode(png).decode("ascii"), mime_type="image/png"
            )
        )
    return blocks


@mcp.tool(title="View reMarkable Pages", annotations=_helpers.read_only("View reMarkable Pages"))
async def remarkable_view(
    id: str,
    pages: str = "1",
    size: Literal["normal", "large"] = "normal",
) -> List[Union[TextContent, ImageContent]]:
    """Page images (PNG) of the real page: the PDF/EPUB page with the ink, or the notebook page.

    Use it for handwriting, drawings and anything text extraction misses. The
    background is opaque paper colour (REMARKABLE_BACKGROUND_COLOR). At most 4
    pages and about 1 MB per call; pages that do not fit are named in the text.

    Args:
        id: Document id; a path or name also works.
        pages: '1', '2-3', '1,4'. Default '1'.
        size: 'normal' (60% of the device resolution) or 'large' (full 1404x1872).
    """
    return await _helpers.run_blocking(view_blocks, id, pages, size)
