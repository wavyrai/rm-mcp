"""
MCP resource templates, resolved live against the current library:

- remarkable://doc/{id}             the document's text, page by page (text/plain)
- remarkable://doc/{id}/page/{n}.png  tablet page n as an opaque PNG

Nothing is registered per document at startup; a template read looks the id
up in the current collection, so renames, moves and edits show immediately.
"""

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import Completion, ResourceTemplateReference

from rm_mcp import reader
from rm_mcp.server import mcp
from rm_mcp.tools._helpers import run_blocking


async def _resolve(func, *args):
    try:
        return await run_blocking(func, *args)
    except ToolError as e:
        raise ResourceNotFoundError(str(e)) from None


@mcp.resource(
    "remarkable://doc/{id}",
    name="document-text",
    title="reMarkable document text",
    description="Every tablet page's text and saved transcripts, by document id.",
    mime_type="text/plain",
)
async def document_text(id: str) -> str:
    return await _resolve(reader.document_text, id)


@mcp.resource(
    "remarkable://doc/{id}/page/{n}.png",
    name="document-page",
    title="reMarkable page image",
    description="Tablet page n of a document as a PNG on paper colour, ink included.",
    mime_type="image/png",
)
async def document_page(id: str, n: str) -> bytes:
    if not n.isdigit():
        raise ResourceNotFoundError(f"Page '{n}' is not a page number; use 1, 2, 3, ...")
    return await _resolve(reader.page_png, id, int(n))


@mcp.completion()
async def handle_completion(ref, argument, context):
    """Page numbers for remarkable://doc/{id}/page/{n}.png, from the indexed page count."""
    if not isinstance(ref, ResourceTemplateReference) or argument.name != "n":
        return None
    doc_id = (context.arguments or {}).get("id") if context is not None else None
    count = 10
    if doc_id:
        from rm_mcp.index import get_instance

        index = get_instance()
        count = (index.get_page_count(doc_id) if index is not None else None) or count
    values = [str(i) for i in range(1, count + 1) if str(i).startswith(argument.value or "")]
    return Completion(values=values[:100], has_more=len(values) > 100)
