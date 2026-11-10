"""
The read side of the library, behind the tools and resources.

Documents are addressed by their stable id (UUID); a path or a name is accepted
and resolved, and every result carries the id. Everything here is synchronous
and may hit the network, so tools call it through ``run_blocking``.

Failures raise ``ToolError`` with the cause and the next step, never a silent
empty result.
"""

import difflib
import io
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from mcp.server.mcpserver.exceptions import ToolError

from rm_mcp import cache as _cache
from rm_mcp.config import env_int
from rm_mcp.extract.document import DocumentPages, Page, read_document_pages
from rm_mcp.extract.render import get_background_color, render_page_from_document_zip
from rm_mcp.library import Library, LibraryEntry, library_for
from rm_mcp.paths import _apply_root_filter, _get_root_path, _is_within_root, _resolve_root_path

logger = logging.getLogger(__name__)

DOCUMENT_KINDS = ("notebook", "pdf", "epub")
MAX_VIEW_PAGES = 4
# Images count against the client's result budget (Claude Code: 25k tokens).
VIEW_BUDGET_BYTES = 1_000_000
VIEW_SCALES = {"normal": 0.6, "large": 1.0}


def max_output_chars() -> int:
    return env_int("REMARKABLE_MAX_OUTPUT_CHARS", 50000, minimum=1000)


def _index():
    from rm_mcp.index import get_instance

    return get_instance()


# =============================================================================
# The library as the caller may see it
# =============================================================================


@dataclass
class Snapshot:
    client: Any
    library: Library
    items: Dict[str, Any]
    root: str

    def in_trash(self, entry: LibraryEntry) -> bool:
        seen = set()
        while entry is not None and entry.id not in seen:
            if entry.parent == "trash":
                return True
            seen.add(entry.id)
            entry = self.library.get(entry.parent)
        return False

    def visible(self, entry: Optional[LibraryEntry]) -> bool:
        return (
            entry is not None
            and not self.in_trash(entry)
            and _is_within_root(entry.path, self.root)
            and not (entry.is_folder and entry.path.lower() == self.root.lower())
        )

    def entries(self) -> Iterator[LibraryEntry]:
        return (e for e in self.library if self.visible(e))

    def documents(self) -> List[LibraryEntry]:
        return [e for e in self.entries() if not e.is_folder]

    def path(self, entry: LibraryEntry) -> str:
        return _apply_root_filter(entry.path, self.root)


def snapshot() -> Snapshot:
    client, collection = _cache.get_cached_collection()
    return Snapshot(
        client=client,
        library=library_for(collection),
        items={item.ID: item for item in collection},
        root=_get_root_path(),
    )


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _page_count(entry: LibraryEntry) -> Optional[int]:
    index = _index()
    if index is None:
        return entry.page_count
    try:
        return index.get_page_count(entry.id) or entry.page_count
    except Exception:
        logger.warning("Could not read the page count of %s from the index", entry.id)
        return entry.page_count


def describe(snap: Snapshot, entry: LibraryEntry) -> Dict[str, Any]:
    return {
        "id": entry.id,
        "name": entry.name,
        "path": snap.path(entry),
        "type": entry.kind,
        "modified": _iso(entry.modified),
        "page_count": None if entry.is_folder else _page_count(entry),
    }


# =============================================================================
# Resolving a reference (id, path or name)
# =============================================================================


def _not_found(snap: Snapshot, ref: str, what: str, candidates: List[LibraryEntry]) -> ToolError:
    names = [e.name for e in candidates]
    similar = difflib.get_close_matches(ref.strip("/").split("/")[-1], names, n=5, cutoff=0.5)
    message = f"No {what} matches '{ref}'."
    if similar:
        message += " Did you mean: " + ", ".join(f"'{n}'" for n in similar) + "?"
    if what == "document":
        message += (
            f" Search with remarkable_find(query='{ref}') or browse with remarkable_list(), "
            "then pass the id."
        )
    else:
        message += " See the folders with remarkable_list(), then pass the folder's id or path."
    return ToolError(message)


def _match(snap: Snapshot, ref: str, candidates: List[LibraryEntry], what: str) -> LibraryEntry:
    wanted = (_resolve_root_path(ref) if ref.startswith("/") else ref).lower().strip("/")
    by_path = [e for e in candidates if e.path.lower().strip("/") == wanted]
    by_name = [e for e in candidates if e.name.lower() == ref.lower().strip("/")]
    matches = by_path or by_name
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listed = "; ".join(f"{snap.path(e)} (id {e.id})" for e in matches[:10])
        raise ToolError(
            f"{len(matches)} {what}s are named '{ref}': {listed}. Pass the id of the one you mean."
        )
    raise _not_found(snap, ref, what, candidates)


def resolve_document(snap: Snapshot, ref: str) -> LibraryEntry:
    ref = (ref or "").strip()
    if not ref:
        raise ToolError("No document given. Pass an id from remarkable_find or remarkable_list.")
    entry = snap.library.get(ref)
    if entry is not None and snap.visible(entry):
        if entry.is_folder:
            raise ToolError(
                f"'{snap.path(entry)}' is a folder, not a document. "
                f"List it with remarkable_list(folder='{entry.id}')."
            )
        return entry
    return _match(snap, ref, snap.documents(), "document")


def resolve_folder(snap: Snapshot, ref: Optional[str]) -> Optional[LibraryEntry]:
    """The folder a reference names; None means the top of what the caller can see."""
    ref = (ref or "").strip()
    if ref in ("", "/"):
        return None
    entry = snap.library.get(ref)
    if entry is not None and snap.visible(entry):
        if not entry.is_folder:
            raise ToolError(
                f"'{snap.path(entry)}' is a document, not a folder. "
                f"Read it with remarkable_read(id='{entry.id}')."
            )
        return entry
    return _match(snap, ref, [e for e in snap.entries() if e.is_folder], "folder")


def _top_parent_id(snap: Snapshot) -> str:
    if snap.root == "/":
        return ""
    for entry in snap.library:
        if entry.is_folder and entry.path.lower() == snap.root.lower():
            return entry.id
    raise ToolError(
        f"REMARKABLE_ROOT_PATH is '{snap.root}', but no such folder exists. "
        "Fix the setting or create the folder on the tablet."
    )


# =============================================================================
# Downloads and pages
# =============================================================================

_download_cache: "OrderedDict[str, bytes]" = OrderedDict()
_MAX_DOWNLOAD_CACHE_BYTES = 128 * 1024 * 1024
_download_lock = threading.Lock()

_pages_cache: "OrderedDict[str, DocumentPages]" = OrderedDict()
_MAX_PAGES_CACHE = 64
_pages_lock = threading.Lock()


def clear_caches() -> None:
    with _download_lock:
        _download_cache.clear()
    with _pages_lock:
        _pages_cache.clear()


def download(snap: Snapshot, entry: LibraryEntry) -> bytes:
    """The document's archive, reusing a recent download of the same version."""
    key = f"{entry.id}:{entry.hash}"
    with _download_lock:
        cached = _download_cache.get(key)
        if cached is not None:
            _download_cache.move_to_end(key)
            return cached
    try:
        data = snap.client.download(snap.items[entry.id])
    except Exception as e:
        raise ToolError(
            f"Could not download '{snap.path(entry)}' (id {entry.id}): {e}. "
            "Check the connection with remarkable_status() and try again."
        ) from e
    with _download_lock:
        _download_cache[key] = data
        total = sum(len(v) for v in _download_cache.values())
        while total > _MAX_DOWNLOAD_CACHE_BYTES and len(_download_cache) > 1:
            _, evicted = _download_cache.popitem(last=False)
            total -= len(evicted)
    return data


def document_pages(snap: Snapshot, entry: LibraryEntry) -> DocumentPages:
    """Every tablet page of the document's current version (cached by version hash)."""
    with _pages_lock:
        cached = _pages_cache.get(entry.hash) if entry.hash else None
    if cached is None:
        data = download(snap, entry)
        try:
            cached = read_document_pages(io.BytesIO(data))
        except Exception as e:
            raise ToolError(
                f"Could not read the pages of '{snap.path(entry)}' (id {entry.id}): {e}."
            ) from e
        if entry.hash:
            with _pages_lock:
                _pages_cache[entry.hash] = cached
                while len(_pages_cache) > _MAX_PAGES_CACHE:
                    _pages_cache.popitem(last=False)
    index_document(entry, cached)
    return cached


def transcript(entry: LibraryEntry, page: Page) -> Optional[str]:
    return _cache.get_page_transcript(entry.id, page.number, page.hash)


def index_document(entry: LibraryEntry, doc: DocumentPages) -> None:
    """Make this version's pages (and their saved transcripts) searchable."""
    index = _index()
    if index is None:
        return
    try:
        if entry.hash and index.get_document_hash(entry.id) == entry.hash:
            if index.has_indexed_content(entry.id):
                return
        rows: List[Tuple[int, str, str]] = []
        for page in doc.pages:
            body = "\n\n".join([page.text, *page.highlights]).strip()
            if body:
                rows.append((page.number, "page", body))
            saved = transcript(entry, page)
            if saved:
                rows.append((page.number, "ocr", saved))
        if not rows:
            # Nothing searchable (a scan, blank pages): mark it so startup
            # indexing does not download it again.
            rows.append((0, "no_text", ""))
        index.replace_document_pages(
            entry.id,
            entry.hash or None,
            rows,
            name=entry.name,
            path=entry.path,
            file_type=entry.kind,
            modified_at=_iso(entry.modified),
            page_count=len(doc.pages),
        )
    except Exception:
        logger.warning("Could not index the pages of %s", entry.id, exc_info=True)


def needs_indexing(entry: LibraryEntry) -> bool:
    index = _index()
    if index is None:
        return False
    return not (
        entry.hash
        and index.get_document_hash(entry.id) == entry.hash
        and index.has_indexed_content(entry.id)
    )


# =============================================================================
# Page selection
# =============================================================================

_PART = re.compile(r"^(\d+)(?:\s*-\s*(\d*))?$")


def parse_pages(spec: Optional[str], total: int, name: str) -> List[int]:
    """'3', '1-5', '2,4', '7-' or 'all' → page numbers within 1..total."""
    if total == 0:
        raise ToolError(f"'{name}' has no pages.")
    text = (spec or "").strip().lower()
    if text in ("all", "*"):
        return list(range(1, total + 1))
    wanted = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        match = _PART.match(part)
        if not match:
            raise ToolError(
                f"Can't read pages='{spec}'. Use a page number ('3'), a range ('1-5'), "
                "a list ('2,4') or 'all'."
            )
        start = int(match.group(1))
        end = start if match.group(2) is None else int(match.group(2) or total)
        wanted.update(range(start, end + 1))
    pages = sorted(p for p in wanted if 1 <= p <= total)
    if not pages:
        raise ToolError(
            f"pages='{spec}' is outside '{name}', which has {total} pages "
            f"(1-{total}). Pass pages='1-{total}' or 'all'."
        )
    return pages


def _ranges(numbers: List[int]) -> str:
    parts, start, prev = [], None, None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = n
    if start is not None:
        parts.append(f"{start}-{prev}" if prev != start else str(start))
    return ",".join(parts)


# =============================================================================
# Tools
# =============================================================================


def _parse_date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"modified_after='{value}' is not an ISO date. "
            "Use e.g. '2026-11-01' or '2026-11-01T09:00:00Z'."
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _modified_key(entry: LibraryEntry) -> datetime:
    if isinstance(entry.modified, datetime):
        m = entry.modified
        return m if m.tzinfo else m.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def find(
    query: Optional[str] = None,
    folder: Optional[str] = None,
    kind: Optional[str] = None,
    modified_after: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    snap = snapshot()
    if kind is not None and kind not in DOCUMENT_KINDS:
        raise ToolError(f"type='{kind}' is not one of {', '.join(DOCUMENT_KINDS)}.")
    limit = min(max(1, limit), 100)
    docs = snap.documents()
    if folder:
        scope = resolve_folder(snap, folder)
        if scope is not None:
            prefix = scope.path.lower() + "/"
            docs = [e for e in docs if e.path.lower().startswith(prefix)]
    if kind:
        docs = [e for e in docs if e.kind == kind]
    if modified_after:
        after = _parse_date(modified_after)
        docs = [e for e in docs if _modified_key(e) > after]
    docs.sort(key=_modified_key, reverse=True)

    index = _index()
    indexed = index.indexed_versions() if index is not None else {}
    coverage = {
        "indexed": sum(1 for e in docs if e.hash and indexed.get(e.id) == e.hash),
        "total": len(docs),
    }

    query = (query or "").strip()
    if not query:
        results = [dict(describe(snap, e), hits=[], matched_on=[]) for e in docs]
        return {"query": None, "results": results[:limit], "total": len(results), **_cov(coverage)}

    by_id = {e.id: e for e in docs}
    position_of = {e.id: i for i, e in enumerate(docs)}
    words = query.lower().split()
    found: Dict[str, Dict[str, Any]] = {}
    order: Dict[str, Tuple[int, float, int]] = {}
    for position, entry in enumerate(docs):
        if all(w in entry.name.lower() for w in words):
            found[entry.id] = {"hits": [], "matched_on": {"name"}}
            order[entry.id] = (0, 0.0, position)

    rows = index.search(query, limit=500) if index is not None else []
    for row in rows:
        entry = by_id.get(row["doc_id"])
        # The index outlives edits, deletions and root changes: trust a row
        # only for a visible document whose indexed version is the current one.
        if entry is None or not entry.hash or indexed.get(entry.id) != entry.hash:
            continue
        if row["content_type"] not in ("page", "ocr"):
            continue
        hit = found.setdefault(entry.id, {"hits": [], "matched_on": set()})
        source = "transcript" if row["content_type"] == "ocr" else "text"
        hit["matched_on"].add(source)
        if len(hit["hits"]) < 5:
            hit["hits"].append(
                {"page": row["page_number"], "snippet": row["snippet"], "source": source}
            )
        if entry.id not in order:
            order[entry.id] = (1, row["rank"], position_of[entry.id])

    ranked = sorted(found, key=lambda doc_id: order[doc_id])
    results = [
        dict(
            describe(snap, by_id[doc_id]),
            hits=found[doc_id]["hits"],
            matched_on=sorted(found[doc_id]["matched_on"]),
        )
        for doc_id in ranked
    ]
    return {"query": query, "results": results[:limit], "total": len(results), **_cov(coverage)}


def _cov(coverage: Dict[str, int]) -> Dict[str, Any]:
    return {"index_coverage": coverage}


def list_folder(
    folder: Optional[str] = "/",
    depth: int = 1,
    sort: str = "name",
    limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    snap = snapshot()
    if sort not in ("name", "modified"):
        raise ToolError(f"sort='{sort}' is not 'name' or 'modified'.")
    if not 1 <= depth <= 10:
        raise ToolError(f"depth={depth} is out of range; use 1 (this folder) up to 10.")
    limit = min(max(1, limit), 500)
    offset = 0
    if cursor:
        if not cursor.isdigit():
            raise ToolError(
                f"cursor='{cursor}' is not one this tool returned. Pass next_cursor from "
                "the previous remarkable_list call, or omit it to start over."
            )
        offset = int(cursor)

    scope = resolve_folder(snap, folder)
    top = scope.id if scope is not None else _top_parent_id(snap)
    children: Dict[str, List[LibraryEntry]] = {}
    for entry in snap.entries():
        parent = entry.parent if entry.parent in snap.library else ""
        children.setdefault(parent, []).append(entry)

    def key(e: LibraryEntry):
        if sort == "modified":
            return (not e.is_folder, -_modified_key(e).timestamp(), e.name.lower())
        return (not e.is_folder, e.name.lower())

    items: List[Dict[str, Any]] = []

    def walk(parent: str, level: int) -> None:
        for entry in sorted(children.get(parent, []), key=key):
            item = dict(describe(snap, entry), parent_id=parent or None, depth=level)
            if entry.is_folder:
                item["item_count"] = len(children.get(entry.id, []))
            items.append(item)
            if entry.is_folder and level < depth:
                walk(entry.id, level + 1)

    walk(top, 1)
    page = items[offset : offset + limit]
    more = offset + limit < len(items)
    return {
        "folder": {
            "id": scope.id if scope is not None else None,
            "path": snap.path(scope) if scope is not None else "/",
        },
        "items": page,
        "total": len(items),
        "next_cursor": str(offset + limit) if more else None,
    }


def read(ref: str, pages: Optional[str] = "1-5", grep: Optional[str] = None) -> Dict[str, Any]:
    snap = snapshot()
    entry = resolve_document(snap, ref)
    doc = document_pages(snap, entry)
    total = len(doc.pages)
    numbers = parse_pages(pages if pages is not None else "1-5", total, entry.name)
    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
        except re.error as e:
            raise ToolError(f"grep='{grep}' is not a valid regular expression: {e}.") from None
        numbers = [
            n
            for n in numbers
            if pattern.search(doc.pages[n - 1].text)
            or pattern.search(transcript(entry, doc.pages[n - 1]) or "")
        ]

    cap = max_output_chars()
    used = 0
    out: List[Dict[str, Any]] = []
    cut: List[int] = []
    truncated_page = None
    for n in numbers:
        if truncated_page is not None or cut:
            cut.append(n)
            continue
        page = doc.pages[n - 1]
        text = page.text
        saved = transcript(entry, page)
        truncated = used + len(text) + len(saved or "") > cap
        if truncated:
            room = cap - used
            if room <= 0:
                cut.append(n)
                continue
            text = text[:room]
            if saved is not None:
                saved = saved[: room - len(text)]
            truncated_page = n
        used += len(text) + len(saved or "")
        out.append(
            {
                "page": n,
                "text": text,
                "handwriting": page.has_ink,
                "transcript": saved,
                "source_page": page.source_page,
                "inserted": page.inserted,
                "truncated": truncated,
            }
        )

    hint = [f"Pages {_ranges([p['page'] for p in out]) or 'none'} of {total}."]
    if grep and not numbers:
        hint = [f"No page matches grep='{grep}'."]
    unread = [p["page"] for p in out if p["handwriting"] and p["transcript"] is None]
    if unread:
        hint.append(
            f"Page(s) {_ranges(unread)} have handwriting with no saved transcript: look at them "
            f"with remarkable_view(id='{entry.id}', pages='{_ranges(unread)}') and save what you "
            "read with remarkable_save_transcript."
        )
    if truncated_page is not None or cut:
        hint.append(
            f"Output is capped at {cap} characters (REMARKABLE_MAX_OUTPUT_CHARS)"
            + (f": page {truncated_page} was truncated" if truncated_page else "")
            + (
                f"; pages {_ranges(cut)} were cut. Read them with "
                f"remarkable_read(id='{entry.id}', pages='{_ranges(cut)}')."
                if cut
                else "."
            )
        )
    elif numbers and numbers[-1] < total:
        hint.append(f"More: remarkable_read(id='{entry.id}', pages='{numbers[-1] + 1}-{total}').")
    return dict(
        describe(snap, entry), page_count=total, pages=out, cut_pages=cut, hint=" ".join(hint)
    )


def view(ref: str, pages: str = "1", size: str = "normal") -> Tuple[Dict[str, Any], List[bytes]]:
    """Render pages to opaque PNGs within the per-call budget; returns (info, pngs)."""
    if size not in VIEW_SCALES:
        raise ToolError(f"size='{size}' is not 'normal' or 'large'.")
    snap = snapshot()
    entry = resolve_document(snap, ref)
    doc = document_pages(snap, entry)
    numbers = parse_pages(pages, len(doc.pages), entry.name)
    data = download(snap, entry)
    background = get_background_color()

    shown: List[int] = []
    images: List[bytes] = []
    left_out = numbers[MAX_VIEW_PAGES:]
    used = 0
    for position, n in enumerate(numbers[:MAX_VIEW_PAGES]):
        png = None
        for factor in (1.0, 0.7, 0.5):
            png = render_page_from_document_zip(
                io.BytesIO(data), n, background, scale=VIEW_SCALES[size] * factor
            )
            if png is None:
                raise ToolError(
                    f"Page {n} of '{snap.path(entry)}' could not be rendered (see the server "
                    f"log). Its text is available with remarkable_read(id='{entry.id}', "
                    f"pages='{n}')."
                )
            if used + _b64_len(png) <= VIEW_BUDGET_BYTES:
                break
            png = None
        if png is None:
            left_out = numbers[position:]
            break
        used += _b64_len(png)
        shown.append(n)
        images.append(png)

    info = dict(
        describe(snap, entry),
        page_count=len(doc.pages),
        shown=shown,
        left_out=left_out,
        size=size,
    )
    return info, images


def _b64_len(data: bytes) -> int:
    return (len(data) + 2) // 3 * 4


def annotations(ref: str, pages: Optional[str] = None) -> Dict[str, Any]:
    snap = snapshot()
    entry = resolve_document(snap, ref)
    doc = document_pages(snap, entry)
    numbers = parse_pages(pages or "all", len(doc.pages), entry.name)
    highlights, ink = [], []
    for n in numbers:
        page = doc.pages[n - 1]
        highlights += [{"page": n, "text": text} for text in page.highlights]
        if page.has_ink:
            ink.append(
                {
                    "page": n,
                    "source_page": page.source_page,
                    "inserted": page.inserted,
                    "transcript_saved": transcript(entry, page) is not None,
                }
            )
    hint = f"{len(highlights)} highlight(s); {len(ink)} page(s) with handwriting."
    pending = [p["page"] for p in ink if not p["transcript_saved"]]
    if pending:
        hint += (
            f" To read the handwriting on pages {_ranges(pending)}: "
            f"remarkable_view(id='{entry.id}', pages='{_ranges(pending[:MAX_VIEW_PAGES])}')."
        )
    return dict(
        describe(snap, entry),
        page_count=len(doc.pages),
        highlights=highlights,
        ink_pages=ink,
        hint=hint,
    )


def save_transcript(ref: str, page: int, text: str) -> Dict[str, Any]:
    snap = snapshot()
    entry = resolve_document(snap, ref)
    doc = document_pages(snap, entry)
    [number] = parse_pages(str(page), len(doc.pages), entry.name)
    target = doc.pages[number - 1]
    if not target.hash:
        raise ToolError(
            f"Page {number} of '{snap.path(entry)}' is blank (no ink and nothing underneath), "
            "so there is nothing to transcribe."
        )
    text = (text or "").strip()
    if not text:
        raise ToolError("The transcript is empty. Pass the text you read on the page.")
    try:
        _cache.cache_page_ocr(entry.id, number, _cache.TRANSCRIPT_BACKEND, text, target.hash)
    except Exception as e:
        raise ToolError(
            f"Could not store the transcript: {e}. Check REMARKABLE_INDEX_PATH is writable."
        ) from e
    return dict(
        describe(snap, entry),
        page_count=len(doc.pages),
        page=number,
        saved=True,
        chars=len(text),
        note=(
            "Stored in the local rm-mcp index only; nothing on the tablet or in the cloud "
            "changed. remarkable_find and remarkable_read now include it, until the page "
            "is edited."
        ),
    )


def document_text(ref: str) -> str:
    """Every page's text in one string, for the remarkable://doc/{id} resource."""
    snap = snapshot()
    entry = resolve_document(snap, ref)
    doc = document_pages(snap, entry)
    parts = [f"# {entry.name}\nid: {entry.id}\npath: {snap.path(entry)}"]
    for page in doc.pages:
        body = [page.text] if page.text else []
        saved = transcript(entry, page)
        if saved:
            body.append(f"[transcript]\n{saved}")
        elif page.has_ink:
            body.append("[handwriting, no transcript: see remarkable_view]")
        parts.append(f"--- Page {page.number} ---\n" + "\n\n".join(body))
    return "\n\n".join(parts)


def page_png(ref: str, number: int) -> bytes:
    snap = snapshot()
    entry = resolve_document(snap, ref)
    doc = document_pages(snap, entry)
    [n] = parse_pages(str(number), len(doc.pages), entry.name)
    png = render_page_from_document_zip(io.BytesIO(download(snap, entry)), n)
    if png is None:
        raise ToolError(f"Page {n} of '{snap.path(entry)}' could not be rendered.")
    return png
