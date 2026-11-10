"""Page layout of a reMarkable document archive.

A document's pages are listed in its ``.content`` file. For a PDF or EPUB each
tablet page points at a page of the source document, and pages inserted on the
tablet point at nothing:

- format 2: ``cPages.pages[i].redir.value`` is the 0-based source page; an
  inserted page has no ``redir``.
- format 1: ``pages`` lists the page ids and ``redirectionPageMap[i]`` the
  source page, with ``-1`` for an inserted page. Without the map the pages
  follow the source one to one.

The plan is read straight from the zip, without extracting it.
"""

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import List, Optional


@dataclass(frozen=True)
class PageRef:
    """One tablet page: its ink (if any) and the source page under it (if any)."""

    page_id: Optional[str]
    rm_member: Optional[str]
    source_page: Optional[int]


@dataclass(frozen=True)
class PagePlan:
    pages: List[PageRef]
    source_member: Optional[str]
    source_kind: Optional[str]  # "pdf" or "epub"


def _source_member(names: List[str]):
    for kind in ("pdf", "epub"):
        found = sorted(n for n in names if n.lower().endswith(f".{kind}"))
        if found:
            return found[0], kind
    return None, None


def _redir_value(entry) -> Optional[int]:
    redir = entry.get("redir") if isinstance(entry, dict) else None
    value = redir.get("value") if isinstance(redir, dict) else None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _read_content(zf: zipfile.ZipFile, names: List[str]) -> dict:
    for name in sorted(names):
        if name.endswith(".content") and "/" not in name:
            try:
                data = json.loads(zf.read(name))
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def source_page_count(data: bytes, kind: str) -> int:
    from rm_mcp.extract.render import open_source_document

    doc = open_source_document(data, kind)
    try:
        return doc.page_count
    finally:
        doc.close()


def read_page_plan(zf: zipfile.ZipFile) -> PagePlan:
    """The document's pages in tablet order, each mapped to its ink and source page."""
    names = zf.namelist()
    rm_by_id = {PurePosixPath(n).stem: n for n in sorted(names) if n.endswith(".rm")}
    source_member, source_kind = _source_member(names)
    content = _read_content(zf, names)

    ids: List[Optional[str]] = []
    redirs: List[Optional[int]] = []
    cpages = content.get("cPages")
    if isinstance(cpages, dict) and isinstance(cpages.get("pages"), list):
        entries = [e for e in cpages["pages"] if isinstance(e, dict)]
        ids = [e.get("id") for e in entries]
        redirs = [_redir_value(e) for e in entries]
    elif isinstance(content.get("pages"), list):
        ids = list(content["pages"])
        page_map = content.get("redirectionPageMap")
        if isinstance(page_map, list):
            redirs = [v if isinstance(v, int) and v >= 0 else None for v in page_map]
            redirs += [None] * (len(ids) - len(redirs))
        elif source_member:
            redirs = list(range(len(ids)))
        else:
            redirs = [None] * len(ids)

    if source_member is None:
        redirs = [None] * len(ids)

    pages = [PageRef(pid, rm_by_id.get(pid), r) for pid, r in zip(ids, redirs)]
    listed = set(ids)
    pages += [PageRef(pid, n, None) for pid, n in rm_by_id.items() if pid not in listed]

    if not pages and source_member:
        # No page list and no ink: the pages are the source document's own.
        count = source_page_count(zf.read(source_member), source_kind)
        pages = [PageRef(None, None, i) for i in range(count)]

    return PagePlan(pages, source_member, source_kind)


def page_content_hashes(zf: zipfile.ZipFile, plan: PagePlan) -> List[Optional[str]]:
    """Hash of everything each page renders from (None for a blank notebook page).

    A notebook page is its .rm blob, so the hash is that blob's sha256. A page
    over a source document also depends on the source and which page of it
    sits underneath, so those are folded in.
    """
    source_hash = (
        hashlib.sha256(zf.read(plan.source_member)).hexdigest() if plan.source_member else None
    )
    hashes: List[Optional[str]] = []
    for ref in plan.pages:
        ink = hashlib.sha256(zf.read(ref.rm_member)).hexdigest() if ref.rm_member else None
        if ref.source_page is None or source_hash is None:
            hashes.append(ink)
        else:
            key = f"{ink or ''}:{source_hash}:{ref.source_page}"
            hashes.append(hashlib.sha256(key.encode()).hexdigest())
    return hashes
