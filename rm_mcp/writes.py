"""
The write side behind remarkable_send, remarkable_organize and remarkable_undo.

Order of every write: check the scope (`write_scope.current`), load the
source, sync, plan against that snapshot (`organizer`, which checks every path
against the scope), then hand the plan to the writer, which uploads, guards
the root index and commits once. A refusal at any step before the commit
leaves the library unchanged; a refusal before the writer means zero uploads.

Synchronous; tools call these through ``run_blocking``.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver.exceptions import ToolError

from rm_mcp import organizer, sources, write_scope
from rm_mcp.clients.cloud import RootConflict
from rm_mcp.clients.organize import ChangedSinceRead, LibraryWriter, Restore, RootGuardError
from rm_mcp.paths import _apply_root_filter, _get_root_path
from rm_mcp.tools import _helpers
from rm_mcp.tools.schemas import OrganizeResult, PlannedChange, SendResult, UndoResult

logger = logging.getLogger(__name__)


def _fresh_collection():
    _helpers.invalidate_after_write()
    client, items = _helpers.get_cached_collection()
    if client is None:
        raise ToolError("Not connected to the reMarkable Cloud. Run: uvx rm-mcp --setup")
    return client, items


def _commit(fn, *args):
    try:
        return fn(*args)
    except (RootGuardError, ChangedSinceRead) as e:
        raise ToolError(str(e))
    except RootConflict as e:
        raise ToolError(f"{e} The tablet kept changing the library; try again in a moment.")
    finally:
        _helpers.invalidate_after_write()


def _index():
    from rm_mcp.index import get_instance

    return get_instance()


# =============================================================================
# organize
# =============================================================================


def organize_plan(changes: List[Dict[str, Any]]):
    scope = write_scope.current()
    client, items = _fresh_collection()
    return client, organizer.plan_organize(items, changes, scope)


def organize(changes: List[Dict[str, Any]], dry_run: bool = True) -> OrganizeResult:
    client, plan = organize_plan(changes)
    planned = [PlannedChange(**c) for c in plan.describe()]
    if dry_run:
        return OrganizeResult(
            dry_run=True,
            applied=False,
            changes=planned,
            hint="Nothing was changed. Call again with dry_run=false to apply exactly this plan.",
        )
    result = apply_plan(client, plan)
    return OrganizeResult(
        dry_run=False,
        applied=True,
        changes=planned,
        undo_token=result["undo_token"],
        previous_root=result["previous_root"],
        hint=(
            f"Applied {len(planned)} change(s) in one commit. "
            + (
                f"remarkable_undo('{result['undo_token']}') reverts it while the items are "
                "unchanged."
                if result["undo_token"]
                else "No undo token: the local index is disabled."
            )
            + " The tablet shows the change after it syncs."
        ),
    )


def apply_plan(client, plan: organizer.Plan) -> Dict[str, Any]:
    """Commit a planned organize batch and record it for undo."""
    result = _commit(LibraryWriter(client).apply, plan.edits(), plan.new_docs())
    record = {
        "previous_root": result["previous_root"],
        "root": result["root"],
        "changes": [
            {
                "op": s.op,
                "id": s.id,
                "kind": s.kind,
                "before": s.before,
                "after": s.after,
                "before_entry": result["before"].get(s.id),
                "after_entry": result["after"].get(s.id),
            }
            for s in plan.steps
        ],
    }
    token = None
    index = _index()
    if index is not None:
        token = uuid.uuid4().hex
        try:
            index.save_write(token, "organize", record)
        except Exception:
            logger.warning("Could not record batch for undo", exc_info=True)
            token = None
    return {**result, "undo_token": token}


# =============================================================================
# send
# =============================================================================


def send(source: str, folder: Optional[str] = None, title: Optional[str] = None) -> SendResult:
    scope = write_scope.current()
    loaded = sources.load(source, title)
    client, items = _fresh_collection()
    plan = organizer.plan_send(
        items, scope, folder, loaded.title, loaded.data, loaded.kind, loaded.page_count
    )
    result = _commit(LibraryWriter(client).apply, [], plan.new_docs())
    doc = plan.steps[-1]
    index = _index()
    if index is not None:
        try:
            index.save_write(
                uuid.uuid4().hex,
                "send",
                {"previous_root": result["previous_root"], "root": result["root"], "id": doc.id},
            )
        except Exception:
            logger.warning("Could not log the send", exc_info=True)
    root = _get_root_path()
    return SendResult(
        id=doc.id,
        name=loaded.title,
        path=_apply_root_filter(doc.after, root),
        type=loaded.kind,
        page_count=loaded.page_count,
        size_bytes=len(loaded.data),
        source=loaded.origin,
        created_folders=[_apply_root_filter(s.after, root) for s in plan.steps[:-1]],
        previous_root=result["previous_root"],
        hint=(
            f"Sent. Read it back with remarkable_read(id='{doc.id}'); it appears on the tablet "
            "after it syncs. A send has no undo: remove it on the tablet if it is unwanted."
        ),
    )


# =============================================================================
# undo
# =============================================================================


def undo(token: str) -> UndoResult:
    scope = write_scope.current()
    index = _index()
    entry = index.get_write(token.strip()) if index is not None else None
    if entry is None:
        raise ToolError(f"No batch with undo token '{token}'.")
    if entry["kind"] != "organize":
        raise ToolError(
            "Only remarkable_organize batches can be undone. A send is not undone, because "
            "that would mean deleting; remove the document on the tablet if it is unwanted."
        )
    if entry["used_at"]:
        raise ToolError(f"That batch was already undone at {entry['used_at']}.")

    record = entry["record"]
    restores, reverted, left = [], [], []
    root = _get_root_path()
    for change in record["changes"]:
        if change["before_entry"] is None:
            left.append(_apply_root_filter(change["after"], root))
            continue
        scope.check(change["after"], "source")
        scope.check(change["before"], "destination")
        restores.append(
            Restore(change["id"], change["before_entry"], change["after_entry"]["hash"])
        )
        reverted.append(
            PlannedChange(
                op=change["op"],
                id=change["id"],
                kind=change["kind"],
                before=_apply_root_filter(change["after"], root),
                after=_apply_root_filter(change["before"], root),
            )
        )
    if not restores:
        raise ToolError("That batch only created folders; undo never deletes, so nothing to do.")

    client, _items = _fresh_collection()
    result = _commit(LibraryWriter(client).restore, restores)
    index.mark_write_undone(token.strip())
    hint = f"Reverted {len(reverted)} change(s) in one commit."
    if left:
        hint += " Folders it created stay (undo never deletes): " + ", ".join(left) + "."
    return UndoResult(
        token=token,
        reverted=reverted,
        left_in_place=left,
        previous_root=result["previous_root"],
        root=result["root"],
        hint=hint,
    )
