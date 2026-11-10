"""The write tools: remarkable_send, remarkable_organize and remarkable_undo.

None of them deletes anything. Each write is one root commit, refused before
any upload when a path is outside the write scope (see rm_mcp/write_scope.py).
"""

from typing import List, Optional

from mcp.types import ToolAnnotations

from rm_mcp import writes
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers
from rm_mcp.tools.schemas import Change, OrganizeResult, SendResult, UndoResult

SEND_ANNOTATIONS = ToolAnnotations(
    title="Send to reMarkable",
    read_only_hint=False,
    destructive_hint=False,  # adds a document; never replaces or removes one
    idempotent_hint=False,  # sending twice makes two documents
    open_world_hint=True,  # may fetch a URL
)
ORGANIZE_ANNOTATIONS = ToolAnnotations(
    title="Organize reMarkable Library",
    read_only_hint=False,
    destructive_hint=False,  # renames, moves and new folders; undoable, nothing deleted
    idempotent_hint=False,
    open_world_hint=False,
)
UNDO_ANNOTATIONS = ToolAnnotations(
    title="Undo reMarkable Organize",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


@mcp.tool(title=SEND_ANNOTATIONS.title, annotations=SEND_ANNOTATIONS)
async def remarkable_send(
    source: str, folder: Optional[str] = None, title: Optional[str] = None
) -> SendResult:
    """Put a PDF or EPUB on the reMarkable.

    source is one of: a local file path (.pdf, .epub, or .md which is typeset),
    an http(s) URL to a PDF or EPUB (up to 100 MB, public hosts only), or
    Markdown text, which is typeset into a clean PDF sized for the tablet
    (headings, lists, code, links). The document is added in one commit; nothing
    existing is changed. There is no undo for a send.

    Args:
        source: File path, URL, or Markdown text.
        folder: Destination folder. '/A/B' is a path from the top; a path without
            a leading '/' is under the default write folder. Missing folders
            are created. Omitted: the default write folder (see remarkable_status).
        title: Name on the tablet. Default: the file name, or the first heading.
    """
    return await _helpers.run_blocking(writes.send, source, folder, title)


@mcp.tool(title=ORGANIZE_ANNOTATIONS.title, annotations=ORGANIZE_ANNOTATIONS)
async def remarkable_organize(changes: List[Change], dry_run: bool = True) -> OrganizeResult:
    """Rename, move and create folders as one batch. Dry run by default.

    With dry_run=true (the default) it returns the exact plan, each change with
    its path before and after, and changes nothing. With dry_run=false it applies
    all changes in one commit and returns an undo_token for remarkable_undo.
    Changes apply in order, so a batch can create a folder and move items into
    it. Every source and destination must be inside the write scope, or the
    whole batch is refused. Nothing is ever deleted.

    Args:
        changes: [{op: 'rename', item, name} | {op: 'move', item, to} |
            {op: 'create_folder', path}]. item is an id, path or unique name.
        dry_run: true to only plan (default); false to apply.
    """
    data = [c.model_dump() for c in changes]
    return await _helpers.run_blocking(writes.organize, data, dry_run)


@mcp.tool(title=UNDO_ANNOTATIONS.title, annotations=UNDO_ANNOTATIONS)
async def remarkable_undo(token: str) -> UndoResult:
    """Revert an applied remarkable_organize batch.

    Puts every renamed or moved item back, in one commit, but only if none of
    them changed since the batch (otherwise nothing is changed). Folders the
    batch created stay, since undo never deletes. Each token works once. A
    remarkable_send cannot be undone.

    Args:
        token: The undo_token remarkable_organize returned.
    """
    return await _helpers.run_blocking(writes.undo, token)
