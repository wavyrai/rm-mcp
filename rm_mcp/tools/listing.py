"""remarkable_list — the folder tree with ids."""

from typing import Literal, Optional

from rm_mcp import reader
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers
from rm_mcp.tools.schemas import ListResult


def list_result(**kwargs) -> ListResult:
    data = reader.list_folder(**kwargs)
    shown = len(data["items"])
    hint = f"{shown} of {data['total']} item(s) in {data['folder']['path']}."
    if data["next_cursor"]:
        hint += f" More: pass cursor='{data['next_cursor']}'."
    if not data["total"]:
        hint += " The folder is empty."
    return ListResult(**data, hint=hint)


@mcp.tool(title="List reMarkable Folder", annotations=_helpers.read_only("List reMarkable Folder"))
async def remarkable_list(
    folder: Optional[str] = "/",
    depth: int = 1,
    sort: Literal["name", "modified"] = "name",
    limit: int = 100,
    cursor: Optional[str] = None,
) -> ListResult:
    """List a folder: folders first, then documents, each with id, type, page count and dates.

    Args:
        folder: Folder path or id; '/' is the top of the library.
        depth: 1 lists the folder itself, 2 also lists inside its subfolders, and so on.
        sort: 'name' (A-Z) or 'modified' (newest first), within each folder.
        limit: Items per call (default 100, max 500).
        cursor: next_cursor from the previous call, to continue.
    """
    return await _helpers.run_blocking(
        list_result, folder=folder, depth=depth, sort=sort, limit=limit, cursor=cursor
    )
