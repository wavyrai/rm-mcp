"""Organising tools — rename, move, and create folders.

These are the only tools that change anything. Each one commits a single root
update, so a change either lands completely or not at all, and the root hash it
replaced is returned so the change can be undone.
"""

import logging
from typing import Optional

from mcp.types import ToolAnnotations

from rm_mcp.config import env_bool
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers

logger = logging.getLogger(__name__)

_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,  # nothing is deleted; renames and moves reverse
    "idempotentHint": False,
    "openWorldHint": False,
}

RENAME_ANNOTATIONS = ToolAnnotations(title="Rename reMarkable Item", **_WRITE_ANNOTATIONS)
MOVE_ANNOTATIONS = ToolAnnotations(title="Move reMarkable Item", **_WRITE_ANNOTATIONS)
CREATE_FOLDER_ANNOTATIONS = ToolAnnotations(title="Create reMarkable Folder", **_WRITE_ANNOTATIONS)


def _writes_disabled(compact: bool) -> Optional[str]:
    """Return an error response when the server is configured read-only."""
    if env_bool("REMARKABLE_READ_ONLY"):
        return _helpers.make_error(
            error_type="writes_disabled",
            message="This server is configured read-only.",
            suggestion="Unset REMARKABLE_READ_ONLY to allow renaming, moving and creating folders.",
            compact=compact,
        )
    return None


def _find_item(name: str, collection, items_by_id, root: str, folders_only: bool = False):
    """Resolve a name or path to a single item, honouring the root path.

    Returns (item, full_path) or (None, error_json).
    """
    resolved = _helpers._resolve_root_path(name) if name.startswith("/") else name
    wanted = resolved.lower().strip("/")

    path_matches, name_matches = [], []
    for item in collection:
        if folders_only and not item.is_folder:
            continue
        if getattr(item, "Parent", "") == "trash":
            continue
        item_path = _helpers.get_item_path(item, items_by_id)
        if not _helpers._is_within_root(item_path, root):
            continue
        if item_path.lower().strip("/") == wanted:
            path_matches.append((item, item_path))
        elif item.VissibleName.lower() == wanted:
            name_matches.append((item, item_path))

    matches = path_matches or name_matches
    if not matches:
        kind = "Folder" if folders_only else "Item"
        return None, _helpers.make_error(
            error_type="not_found",
            message=f"{kind} not found: '{name}'",
            suggestion="Use remarkable_browse('/') to see what is there.",
        )
    if len(matches) > 1:
        paths = sorted(_helpers._apply_root_filter(p) for _i, p in matches)
        return None, _helpers.make_error(
            error_type="ambiguous_document",
            message=f"{len(matches)} items are named '{name}'. Use a full path.",
            suggestion=f"For example: '{paths[0]}'",
            did_you_mean=paths,
        )
    return matches[0]


def _writer(client):
    from rm_mcp.clients.organize import LibraryWriter

    return LibraryWriter(client)


@mcp.tool(annotations=RENAME_ANNOTATIONS)
async def remarkable_rename(item: str, new_name: str, compact_output: bool = False) -> str:
    """
    <usecase>Rename a document or folder on your reMarkable.</usecase>
    <instructions>
    Changes the visible name. Contents, annotations and page data are untouched.

    The change is committed as a single atomic update, and the response includes
    the previous library state so it can be undone.
    </instructions>
    <parameters>
    - item: Current name or full path of the document or folder
    - new_name: The new name (just the name, not a path)
    </parameters>
    <examples>
    - remarkable_rename("Meeting Notes", "Q3 Planning")
    - remarkable_rename("/Work/Draft", "Final Report")
    </examples>
    """
    compact = _helpers.is_compact(compact_output)
    blocked = _writes_disabled(compact)
    if blocked:
        return blocked

    try:
        new_name = new_name.strip()
        if not new_name:
            return _helpers.make_error(
                error_type="invalid_name",
                message="The new name is empty.",
                suggestion="Provide a name with at least one character.",
                compact=compact,
            )
        if "/" in new_name:
            return _helpers.make_error(
                error_type="invalid_name",
                message=f"'{new_name}' contains a '/', which is part of a path, not a name.",
                suggestion="Use remarkable_move() to change where something lives.",
                compact=compact,
            )

        client, collection = await _helpers.run_blocking(_helpers.get_cached_collection)
        items_by_id = _helpers.get_items_by_id(collection)
        root = _helpers._get_root_path()

        target, path_or_error = _find_item(item, collection, items_by_id, root)
        if target is None:
            return path_or_error
        old_path = path_or_error

        result = await _helpers.run_blocking(_writer(client).rename, target.ID, new_name)
        _helpers.invalidate_after_write()

        return _helpers.make_response(
            {
                "renamed": True,
                "from": _helpers._apply_root_filter(old_path),
                "name": new_name,
                "kind": "folder" if target.is_folder else "document",
                "previous_root": result["previous_root"],
            },
            f"Renamed to '{new_name}'. It may take a moment to appear on the tablet.",
            compact=compact,
        )
    except Exception as e:
        return _helpers.make_error(
            error_type="rename_failed",
            message=str(e),
            suggestion=_helpers.suggest_for_error(e),
            compact=compact,
        )


@mcp.tool(annotations=MOVE_ANNOTATIONS)
async def remarkable_move(item: str, destination: str, compact_output: bool = False) -> str:
    """
    <usecase>Move a document or folder into another folder.</usecase>
    <instructions>
    Changes where something lives. Use "/" as the destination to move it to the
    top level. The item keeps its name and contents.
    </instructions>
    <parameters>
    - item: Name or full path of the document or folder to move
    - destination: Path of the destination folder, or "/" for the top level
    </parameters>
    <examples>
    - remarkable_move("Q3 Planning", "/Work")
    - remarkable_move("/Inbox/Receipt.pdf", "/Financial")
    - remarkable_move("Stray Note", "/")
    </examples>
    """
    compact = _helpers.is_compact(compact_output)
    blocked = _writes_disabled(compact)
    if blocked:
        return blocked

    try:
        client, collection = await _helpers.run_blocking(_helpers.get_cached_collection)
        items_by_id = _helpers.get_items_by_id(collection)
        root = _helpers._get_root_path()

        target, path_or_error = _find_item(item, collection, items_by_id, root)
        if target is None:
            return path_or_error
        old_path = path_or_error

        # Destination: "/" means the configured root, which is "" on the device
        # unless a root path is set.
        if destination.strip() in ("/", ""):
            if root == "/":
                dest_id, dest_path = "", "/"
            else:
                dest_folder, dest_result = _find_item(
                    root, collection, items_by_id, root, folders_only=True
                )
                if dest_folder is None:
                    return dest_result
                dest_id, dest_path = dest_folder.ID, dest_result
        else:
            dest_folder, dest_result = _find_item(
                destination, collection, items_by_id, root, folders_only=True
            )
            if dest_folder is None:
                return dest_result
            dest_id, dest_path = dest_folder.ID, dest_result

        if dest_id == target.ID:
            return _helpers.make_error(
                error_type="invalid_move",
                message="A folder cannot be moved into itself.",
                suggestion="Choose a different destination.",
                compact=compact,
            )
        if target.Parent == dest_id:
            return _helpers.make_error(
                error_type="already_there",
                message=f"'{target.VissibleName}' is already in that folder.",
                suggestion="Nothing to do.",
                compact=compact,
            )
        # Moving a folder inside its own subtree would detach it from the library.
        if target.is_folder and dest_id:
            ancestor = dest_id
            seen = set()
            while ancestor and ancestor not in seen:
                seen.add(ancestor)
                if ancestor == target.ID:
                    return _helpers.make_error(
                        error_type="invalid_move",
                        message=(
                            f"'{target.VissibleName}' cannot be moved into its own subfolder."
                        ),
                        suggestion="Choose a destination outside it.",
                        compact=compact,
                    )
                parent_item = items_by_id.get(ancestor)
                ancestor = getattr(parent_item, "Parent", "") if parent_item else ""

        result = await _helpers.run_blocking(_writer(client).move, target.ID, dest_id)
        _helpers.invalidate_after_write()

        return _helpers.make_response(
            {
                "moved": True,
                "item": target.VissibleName,
                "from": _helpers._apply_root_filter(old_path),
                "to": _helpers._apply_root_filter(dest_path),
                "kind": "folder" if target.is_folder else "document",
                "previous_root": result["previous_root"],
            },
            f"Moved '{target.VissibleName}' to {_helpers._apply_root_filter(dest_path)}.",
            compact=compact,
        )
    except Exception as e:
        return _helpers.make_error(
            error_type="move_failed",
            message=str(e),
            suggestion=_helpers.suggest_for_error(e),
            compact=compact,
        )


@mcp.tool(annotations=CREATE_FOLDER_ANNOTATIONS)
async def remarkable_create_folder(path: str, compact_output: bool = False) -> str:
    """
    <usecase>Create a new empty folder on your reMarkable.</usecase>
    <instructions>
    Creates one folder. The parent folder must already exist — create nested
    folders one level at a time.
    </instructions>
    <parameters>
    - path: Full path of the folder to create, e.g. "/Work/Archive", or just a
      name to create it at the top level
    </parameters>
    <examples>
    - remarkable_create_folder("Receipts")
    - remarkable_create_folder("/Work/Archive")
    </examples>
    """
    compact = _helpers.is_compact(compact_output)
    blocked = _writes_disabled(compact)
    if blocked:
        return blocked

    try:
        client, collection = await _helpers.run_blocking(_helpers.get_cached_collection)
        items_by_id = _helpers.get_items_by_id(collection)
        root = _helpers._get_root_path()

        cleaned = path.strip().rstrip("/")
        if not cleaned or cleaned == "/":
            return _helpers.make_error(
                error_type="invalid_name",
                message="No folder name given.",
                suggestion="Pass a name, e.g. remarkable_create_folder('Receipts').",
                compact=compact,
            )

        if "/" in cleaned.strip("/"):
            parent_path, _, name = cleaned.rstrip("/").rpartition("/")
            parent_path = parent_path or "/"
        else:
            parent_path, name = "/", cleaned.strip("/")

        if not name:
            return _helpers.make_error(
                error_type="invalid_name",
                message="No folder name given.",
                suggestion="Include a name after the final '/'.",
                compact=compact,
            )

        # Resolve the parent folder
        if parent_path == "/":
            if root == "/":
                parent_id, parent_display = "", "/"
            else:
                parent_folder, parent_result = _find_item(
                    root, collection, items_by_id, root, folders_only=True
                )
                if parent_folder is None:
                    return parent_result
                parent_id, parent_display = parent_folder.ID, parent_result
        else:
            parent_folder, parent_result = _find_item(
                parent_path, collection, items_by_id, root, folders_only=True
            )
            if parent_folder is None:
                return parent_result
            parent_id, parent_display = parent_folder.ID, parent_result

        # Refuse to create a duplicate name in the same folder
        for item in collection:
            if (
                item.is_folder
                and getattr(item, "Parent", "") == parent_id
                and item.VissibleName.lower() == name.lower()
            ):
                return _helpers.make_error(
                    error_type="already_exists",
                    message=f"A folder called '{item.VissibleName}' is already there.",
                    suggestion="Pick a different name, or use the existing folder.",
                    compact=compact,
                )

        result = await _helpers.run_blocking(_writer(client).create_folder, name, parent_id)
        _helpers.invalidate_after_write()

        full = "/" + name if parent_display == "/" else f"{parent_display}/{name}"
        return _helpers.make_response(
            {
                "created": True,
                "name": name,
                "path": _helpers._apply_root_filter(full),
                "folder_id": result["folder_id"],
                "previous_root": result["previous_root"],
            },
            f"Created folder '{name}'. Move things into it with remarkable_move().",
            compact=compact,
        )
    except Exception as e:
        return _helpers.make_error(
            error_type="create_folder_failed",
            message=str(e),
            suggestion=_helpers.suggest_for_error(e),
            compact=compact,
        )
