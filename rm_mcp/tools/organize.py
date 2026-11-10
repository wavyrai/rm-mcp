"""Deprecated organising tools: rename, move and create folder.

Each is a one-change remarkable_organize batch applied at once (no dry run),
with the 0.7 response shape plus a ``deprecated`` note. They go through the
same planner, write scope, root guard and undo log as remarkable_organize, and
return its undo token. They will be removed in the next release.
"""

import logging
from typing import Any, Dict

from mcp.types import ToolAnnotations

from rm_mcp import writes
from rm_mcp.organizer import PlanError
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers
from rm_mcp.write_scope import WriteRefused

logger = logging.getLogger(__name__)

_WRITE_ANNOTATIONS = {
    "read_only_hint": False,
    "destructive_hint": False,  # nothing is deleted; renames and moves reverse
    "idempotent_hint": False,
    "open_world_hint": False,
}

RENAME_ANNOTATIONS = ToolAnnotations(
    title="Rename reMarkable Item (deprecated)", **_WRITE_ANNOTATIONS
)
MOVE_ANNOTATIONS = ToolAnnotations(title="Move reMarkable Item (deprecated)", **_WRITE_ANNOTATIONS)
CREATE_FOLDER_ANNOTATIONS = ToolAnnotations(
    title="Create reMarkable Folder (deprecated)", **_WRITE_ANNOTATIONS
)

_NOTE = "{name} is deprecated: use remarkable_organize(changes=[{change}], dry_run=false)."


def _apply(change: Dict[str, Any], failure: str, compact: bool, respond):
    note = _NOTE.format(
        name=f"remarkable_{change['op']}", change="{op: '" + change["op"] + "', ...}"
    )
    try:
        client, plan = writes.organize_plan([change])
        result = writes.apply_plan(client, plan)
        data, message = respond(plan.steps[0], plan, result)
        data["undo_token"] = result["undo_token"]
        data["previous_root"] = result["previous_root"]
        data["deprecated"] = note
        return _helpers.make_response(data, message, compact=compact)
    except (PlanError, WriteRefused) as e:
        did_you_mean = getattr(e, "did_you_mean", None) or None
        return _helpers.make_error(
            error_type=e.kind,
            message=str(e),
            suggestion="Use a full path or an id."
            if did_you_mean
            else "Use remarkable_list() to see what is there.",
            compact=compact,
            did_you_mean=did_you_mean,
        )
    except Exception as e:
        return _helpers.make_error(
            error_type=failure,
            message=str(e),
            suggestion=_helpers.suggest_for_error(e),
            compact=compact,
        )


@mcp.tool(title=RENAME_ANNOTATIONS.title, annotations=RENAME_ANNOTATIONS)
async def remarkable_rename(item: str, new_name: str, compact_output: bool = False) -> str:
    """Deprecated: use remarkable_organize. Renames a document or folder at once."""
    compact = _helpers.is_compact(compact_output)

    def respond(step, plan, result):
        name = new_name.strip()
        return (
            {
                "renamed": True,
                "from": plan.show(step.before),
                "name": name,
                "kind": step.kind,
            },
            f"Renamed to '{name}'. It may take a moment to appear on the tablet.",
        )

    change = {"op": "rename", "item": item, "name": new_name}
    return await _helpers.run_blocking(_apply, change, "rename_failed", compact, respond)


@mcp.tool(title=MOVE_ANNOTATIONS.title, annotations=MOVE_ANNOTATIONS)
async def remarkable_move(item: str, destination: str, compact_output: bool = False) -> str:
    """Deprecated: use remarkable_organize. Moves an item into a folder ("/" = top) at once."""
    compact = _helpers.is_compact(compact_output)

    def respond(step, plan, result):
        to = plan.show(step.after.rsplit("/", 1)[0] or "/")
        return (
            {
                "moved": True,
                "item": step.after.rsplit("/", 1)[-1],
                "from": plan.show(step.before),
                "to": to,
                "kind": step.kind,
            },
            f"Moved to {to}.",
        )

    change = {"op": "move", "item": item, "to": destination or "/"}
    return await _helpers.run_blocking(_apply, change, "move_failed", compact, respond)


@mcp.tool(title=CREATE_FOLDER_ANNOTATIONS.title, annotations=CREATE_FOLDER_ANNOTATIONS)
async def remarkable_create_folder(path: str, compact_output: bool = False) -> str:
    """Deprecated: use remarkable_organize. Creates a folder at once.

    '/A/New' needs /A to exist; 'New' goes under the default write folder.
    """
    compact = _helpers.is_compact(compact_output)

    def respond(step, plan, result):
        last = plan.steps[-1]
        return (
            {
                "created": True,
                "name": last.after.rsplit("/", 1)[-1],
                "path": plan.show(last.after),
                "folder_id": last.id,
            },
            "Created folder. Move things into it with remarkable_organize().",
        )

    change = {"op": "create_folder", "path": path}
    return await _helpers.run_blocking(_apply, change, "create_folder_failed", compact, respond)
