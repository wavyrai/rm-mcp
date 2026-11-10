"""
Planning writes: turn requested changes into an exact, checked plan.

A plan is computed against one snapshot of the library, applying the changes
in order to a copy of the folder tree, so a batch can create a folder and move
things into it. Every source and destination path is checked against the write
scope (`write_scope`) here, before anything is uploaded; a single violation
refuses the whole batch. The plan names each change's path before and after,
which is what `remarkable_organize(dry_run=True)` returns.

Every change is pinned to the version of the document it was planned against
(its root entry hash); the writer refuses to commit if that version is gone.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver.exceptions import ToolError

from rm_mcp.clients.organize import MetadataEdit, NewDocument, new_document, new_folder
from rm_mcp.paths import _apply_root_filter, _get_root_path, _is_within_root, _resolve_root_path
from rm_mcp.write_scope import WriteScope, normalize


class PlanError(ToolError):
    """A change cannot be planned. Nothing was changed."""

    def __init__(self, message: str, kind: str, did_you_mean: Optional[List[str]] = None):
        super().__init__(message)
        self.kind = kind
        self.did_you_mean = did_you_mean or []


@dataclass
class Node:
    id: str
    name: str
    parent: str
    is_folder: bool
    hash: str = ""
    new: bool = False


@dataclass
class Step:
    op: str  # rename, move, create_folder, send
    id: str
    kind: str  # folder or document
    before: Optional[str]  # full path; None for something new
    after: str  # full path
    fields: Dict[str, Any] = field(default_factory=dict)
    expected_hash: str = ""
    new_doc: Optional[NewDocument] = None


@dataclass
class Plan:
    steps: List[Step]
    read_root: str

    def edits(self) -> List[MetadataEdit]:
        return [
            MetadataEdit(s.id, s.fields, s.expected_hash) for s in self.steps if s.new_doc is None
        ]

    def new_docs(self) -> List[NewDocument]:
        return [s.new_doc for s in self.steps if s.new_doc is not None]

    def show(self, path: Optional[str]) -> Optional[str]:
        return None if path is None else _apply_root_filter(path, self.read_root)

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "op": s.op,
                "id": s.id,
                "kind": s.kind,
                "before": self.show(s.before),
                "after": self.show(s.after),
            }
            for s in self.steps
        ]


class Tree:
    """A mutable copy of the folder tree, as the batch so far leaves it."""

    def __init__(self, items, scope: WriteScope):
        self.scope = scope
        self.read_root = _get_root_path()
        self.nodes: Dict[str, Node] = {
            item.ID: Node(
                id=item.ID,
                name=item.VissibleName,
                parent=getattr(item, "Parent", "") or "",
                is_folder=bool(item.is_folder),
                hash=getattr(item, "hash", "") or "",
            )
            for item in items
        }
        self.touched: set = set()
        self.steps: List[Step] = []

    # --- paths ---------------------------------------------------------

    def path(self, node_id: str) -> str:
        if node_id == "":
            return "/"
        parts, seen, current = [], set(), node_id
        while current and current in self.nodes and current not in seen:
            seen.add(current)
            node = self.nodes[current]
            parts.insert(0, node.name)
            current = node.parent
        return "/" + "/".join(parts)

    def in_trash(self, node_id: str) -> bool:
        seen = set()
        while node_id and node_id not in seen:
            if node_id == "trash":
                return True
            seen.add(node_id)
            node = self.nodes.get(node_id)
            node_id = node.parent if node else ""
        return False

    def visible(self, node: Node) -> bool:
        path = self.path(node.id)
        return (
            not self.in_trash(node.id)
            and _is_within_root(path, self.read_root)
            and not (node.is_folder and path.lower() == self.read_root.lower())
        )

    def by_path(self, full: str, folders_only: bool = False) -> List[Node]:
        full = normalize(full).lower()
        return [
            n
            for n in self.nodes.values()
            if (n.is_folder or not folders_only)
            and not self.in_trash(n.id)
            and self.path(n.id).lower() == full
        ]

    def top_id(self) -> str:
        """Parent id meaning "the top of what the caller sees"."""
        if self.read_root == "/":
            return ""
        found = self.by_path(self.read_root, folders_only=True)
        if len(found) != 1:
            raise PlanError(f"The root folder '{self.read_root}' was not found.", kind="not_found")
        return found[0].id

    def show(self, full: str) -> str:
        return _apply_root_filter(full, self.read_root)

    # --- resolving references -----------------------------------------

    def _one(self, matches: List[Node], ref: str, what: str) -> Node:
        if not matches:
            raise PlanError(
                f"{what} not found: '{ref}'. Use remarkable_list() to see what is there.",
                kind="not_found",
            )
        if len(matches) > 1:
            paths = sorted(f"{self.show(self.path(n.id))} ({n.id})" for n in matches)
            raise PlanError(
                f"{len(matches)} items match '{ref}'. Use an id or a full path: "
                + ", ".join(paths),
                kind="ambiguous_document",
                did_you_mean=paths,
            )
        return matches[0]

    def item(self, ref: str) -> Node:
        """An existing document or folder by id, path or unique name."""
        ref = (ref or "").strip()
        node = self.nodes.get(ref)
        if node is not None and self.visible(node):
            return node
        if ref.startswith("/"):
            matches = [n for n in self.by_path(_resolve_root_path(ref)) if self.visible(n)]
        else:
            matches = [
                n for n in self.nodes.values() if n.name.lower() == ref.lower() and self.visible(n)
            ]
        return self._one(matches, ref, "Item")

    def folder(self, ref: str) -> str:
        """A destination folder id by id, path or unique name; "/" is the top."""
        ref = (ref or "").strip()
        if ref in ("", "/"):
            return self.top_id()
        node = self.nodes.get(ref)
        if node is not None and node.is_folder and self.visible(node):
            return node.id
        if ref.startswith("/"):
            matches = [
                n
                for n in self.by_path(_resolve_root_path(ref), folders_only=True)
                if self.visible(n)
            ]
        else:
            matches = [
                n
                for n in self.nodes.values()
                if n.is_folder and n.name.lower() == ref.lower() and self.visible(n)
            ]
        return self._one(matches, ref, "Folder").id

    def full_location(self, ref: Optional[str]) -> str:
        """A folder location as a full path: absolute paths are under the read root,
        relative ones under the default write folder."""
        ref = (ref or "").strip()
        if not ref:
            return self.scope.default_folder
        if ref.startswith("/"):
            return normalize(_resolve_root_path(normalize(ref)))
        base = self.scope.default_folder.rstrip("/")
        return normalize(f"{base}/{ref}")

    def ensure_folder(self, full: str, create: bool) -> str:
        """The id of the folder at `full`, creating missing levels inside the scope."""
        full = normalize(full)
        if full == "/":
            return ""
        if not _is_within_root(full, self.read_root):
            raise PlanError(f"'{full}' is outside the library root.", kind="not_found")
        parent_id = ""
        walked = ""
        for name in full.strip("/").split("/"):
            walked = f"{walked}/{name}"
            found = [
                n
                for n in self.nodes.values()
                if n.is_folder
                and n.parent == parent_id
                and n.name.lower() == name.lower()
                and not self.in_trash(n.id)
            ]
            if len(found) > 1:
                self._one(found, self.show(walked), "Folder")
            if found:
                parent_id = found[0].id
                continue
            if not create:
                raise PlanError(
                    f"Folder not found: '{self.show(walked)}'. Create it first "
                    "(remarkable_organize with op create_folder).",
                    kind="not_found",
                )
            parent_id = self._add_folder(name, parent_id).id
        return parent_id

    # --- recording changes --------------------------------------------

    def _touch(self, node_id: str) -> None:
        if node_id in self.touched:
            raise PlanError(
                f"'{self.show(self.path(node_id))}' is changed twice in this batch; "
                "combine the changes into one or use two batches.",
                kind="duplicate_change",
            )
        self.touched.add(node_id)

    def _add_folder(self, name: str, parent_id: str) -> Node:
        folder = new_folder(name, parent_id)
        node = Node(folder.doc_id, name, parent_id, True, new=True)
        self.nodes[node.id] = node
        after = self.path(node.id)
        self.scope.check(after, "new folder")
        self.touched.add(node.id)
        self.steps.append(Step("create_folder", node.id, "folder", None, after, new_doc=folder))
        return node

    def rename(self, ref: str, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise PlanError("The new name is empty.", kind="invalid_name")
        if "/" in name:
            raise PlanError(
                f"'{name}' contains a '/', which is part of a path, not a name. "
                "Use op move to change where something lives.",
                kind="invalid_name",
            )
        node = self.item(ref)
        before = self.path(node.id)
        self.scope.check(before, "source")
        self._touch(node.id)
        node.name = name
        after = self.path(node.id)
        self.scope.check(after, "destination")
        self.steps.append(
            Step(
                "rename",
                node.id,
                "folder" if node.is_folder else "document",
                before,
                after,
                {"visibleName": name},
                node.hash,
            )
        )

    def move(self, ref: str, to: str) -> None:
        node = self.item(ref)
        dest = self.folder(to)
        before = self.path(node.id)
        self.scope.check(before, "source")
        if dest == node.id:
            raise PlanError("A folder cannot be moved into itself.", kind="invalid_move")
        if node.parent == dest:
            raise PlanError(f"'{node.name}' is already in that folder.", kind="already_there")
        ancestor, seen = dest, set()
        while ancestor and ancestor not in seen:
            seen.add(ancestor)
            if ancestor == node.id:
                raise PlanError(
                    f"'{node.name}' cannot be moved into its own subfolder.", kind="invalid_move"
                )
            parent = self.nodes.get(ancestor)
            ancestor = parent.parent if parent else ""
        if node.new:
            raise PlanError(
                "Create the folder where it should be instead of moving it in the same batch.",
                kind="duplicate_change",
            )
        self._touch(node.id)
        node.parent = dest
        after = self.path(node.id)
        self.scope.check(after, "destination")
        self.steps.append(
            Step(
                "move",
                node.id,
                "folder" if node.is_folder else "document",
                before,
                after,
                {"parent": dest},
                node.hash,
            )
        )

    def create_folder(self, path: str) -> None:
        cleaned = (path or "").strip().rstrip("/")
        if not cleaned.strip("/"):
            raise PlanError("No folder name given.", kind="invalid_name")
        full = self.full_location(cleaned)
        parent_path, _, name = full.rpartition("/")
        relative = not cleaned.startswith("/")
        parent_id = self.ensure_folder(parent_path or "/", create=relative)
        for n in self.nodes.values():
            if (
                n.is_folder
                and n.parent == parent_id
                and n.name.lower() == name.lower()
                and not self.in_trash(n.id)
            ):
                raise PlanError(
                    f"A folder called '{n.name}' is already there.", kind="already_exists"
                )
        self._add_folder(name, parent_id)


def plan_organize(items, changes: List[Dict[str, Any]], scope: WriteScope) -> Plan:
    """Plan a batch of rename/move/create_folder changes. Raises PlanError/WriteRefused."""
    if not changes:
        raise PlanError("No changes given.", kind="invalid_change")
    tree = Tree(items, scope)
    for number, change in enumerate(changes, 1):
        op = change.get("op")
        try:
            if op == "rename":
                tree.rename(_need(change, "item"), _need(change, "name"))
            elif op == "move":
                tree.move(_need(change, "item"), _need(change, "to"))
            elif op == "create_folder":
                tree.create_folder(_need(change, "path"))
            else:
                raise PlanError(
                    f"Unknown op '{op}'; use rename, move or create_folder.",
                    kind="invalid_change",
                )
        except ToolError as e:
            if len(changes) > 1:
                e.args = (f"Change {number} ({op}): {e}",)
            raise
    return Plan(tree.steps, tree.read_root)


def _need(change: Dict[str, Any], key: str) -> str:
    value = change.get(key)
    if not isinstance(value, str):
        raise PlanError(f"'{change.get('op')}' needs '{key}'.", kind="invalid_change")
    return value


def plan_send(
    items,
    scope: WriteScope,
    folder: Optional[str],
    title: str,
    data: bytes,
    kind: str,
    page_count: int,
) -> Plan:
    """Plan a new document in `folder`, creating missing folders inside the scope."""
    title = title.strip()
    if not title or "/" in title:
        raise PlanError(f"'{title}' is not a usable title (empty or contains '/').", "invalid_name")
    tree = Tree(items, scope)
    full = tree.full_location(folder)
    scope.check(normalize(f"{full.rstrip('/')}/{title}"), "destination")
    parent_id = tree.ensure_folder(full, create=True)
    doc = new_document(title, parent_id, data, kind, page_count)
    tree.nodes[doc.doc_id] = Node(doc.doc_id, title, parent_id, False, new=True)
    after = tree.path(doc.doc_id)
    scope.check(after, "destination")
    tree.steps.append(Step("send", doc.doc_id, "document", None, after, new_doc=doc))
    return Plan(tree.steps, tree.read_root)
