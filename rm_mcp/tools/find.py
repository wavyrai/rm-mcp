"""remarkable_find — one search over names, page text and saved transcripts."""

from typing import Literal, Optional

from rm_mcp import reader
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers
from rm_mcp.tools.schemas import FindResult


def find_result(**kwargs) -> FindResult:
    data = reader.find(**kwargs)
    results, cov = data["results"], data["index_coverage"]
    if not data["query"]:
        hint = f"{len(results)} most recently modified of {data['total']} documents."
    elif results:
        hint = f"{data['total']} document(s) match '{data['query']}'."
    else:
        hint = f"Nothing matches '{data['query']}'."
    if results:
        first = results[0]
        page = first["hits"][0]["page"] if first["hits"] else 1
        hint += f" Read one with remarkable_read(id='{first['id']}', pages='{page}')."
    if data["query"] and cov["indexed"] < cov["total"]:
        hint += (
            f" Page text is searchable for {cov['indexed']}/{cov['total']} documents in scope; "
            "the rest are indexed in the background or when read."
        )
    return FindResult(**data, hint=hint)


@mcp.tool(
    title="Find reMarkable Documents", annotations=_helpers.read_only("Find reMarkable Documents")
)
async def remarkable_find(
    query: Optional[str] = None,
    folder: Optional[str] = None,
    type: Optional[Literal["notebook", "pdf", "epub"]] = None,
    modified_after: Optional[str] = None,
    limit: int = 20,
) -> FindResult:
    """Search the library: names, typed text, PDF/EPUB text, highlights and saved transcripts.

    Every result carries the document's id, path, type, modified date and page
    count, plus hits [{page, snippet}] with tablet page numbers. Without a query
    it returns the most recently modified documents. Handwriting is searchable
    once a transcript is saved with remarkable_save_transcript.

    Args:
        query: Words to look for. Omit to list recent documents.
        folder: Only documents under this folder (path or id).
        type: Only notebooks, PDFs or EPUBs.
        modified_after: ISO date or datetime, e.g. '2026-11-01'.
        limit: Maximum results (default 20, max 100).
    """
    return await _helpers.run_blocking(
        find_result,
        query=query,
        folder=folder,
        kind=type,
        modified_after=modified_after,
        limit=limit,
    )
