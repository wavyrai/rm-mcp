"""Per-page content of a reMarkable document archive.

Every page here is a tablet page, in the order the tablet shows them: a page
inserted on the tablet into a PDF counts, and the PDF pages after it shift by
one. For each page this reads what can be known without a model looking at it:

- typed text (the page's root text block),
- the text of the PDF or EPUB page underneath, if any,
- highlights, both the newer in-page glyph ranges and the older per-page
  ``.highlights/<page>.json`` files,
- whether the page carries pen ink (handwriting, drawings, margin notes),
- its content hash (see ``pages.page_content_hashes``), the key that a saved
  transcript of the page is stored under.
"""

import json
import logging
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple

from rm_mcp.extract.pages import PagePlan, page_content_hashes, read_page_plan

logger = logging.getLogger(__name__)

# rmscene Pen values that leave no handwriting: erasers and highlighters.
_NOT_HANDWRITING = {5, 6, 8, 18}


@dataclass(frozen=True)
class Page:
    number: int  # 1-based tablet page
    hash: Optional[str]  # None for a blank notebook page
    source_page: Optional[int]  # 1-based PDF/EPUB page underneath, None if none
    inserted: bool  # a notebook page inserted on the tablet into a PDF/EPUB
    typed_text: str
    source_text: str
    highlights: Tuple[str, ...]
    has_ink: bool

    @property
    def text(self) -> str:
        """Everything readable on the page without a transcript."""
        return "\n\n".join(t for t in (self.typed_text, self.source_text) if t.strip())


@dataclass(frozen=True)
class DocumentPages:
    kind: str  # "notebook", "pdf" or "epub"
    pages: Tuple[Page, ...]


def _scene_parts(data: bytes) -> Tuple[str, List[str], bool]:
    """Typed text, glyph-range highlights and whether there is pen ink."""
    from rmscene import scene_items as si
    from rmscene.text import TextDocument

    from rm_mcp.extract.render import _visible_items, read_scene

    try:
        tree = read_scene(data)
    except Exception:
        logger.warning("Could not parse a page's .rm data (pre-v6 format?)", exc_info=True)
        return "", [], False

    typed = ""
    if tree.root_text is not None:
        try:
            lines = [str(p) for p in TextDocument.from_scene_item(tree.root_text).contents]
            typed = "\n".join(lines).strip()
        except Exception:
            logger.warning("Could not read a page's typed text", exc_info=True)

    highlights: List[str] = []
    has_ink = False
    for item in _visible_items(tree.root):
        if isinstance(item, si.GlyphRange):
            if item.text and item.text.strip():
                highlights.append(item.text.strip())
        elif isinstance(item, si.Line):
            if item.points and int(item.tool) not in _NOT_HANDWRITING:
                has_ink = True
    return typed, highlights, has_ink


def _json_highlights(zf: zipfile.ZipFile) -> Dict[str, List[str]]:
    """Older firmware: ``<doc>.highlights/<page id>.json`` holding the highlighted text."""
    found: Dict[str, List[str]] = {}
    for name in sorted(zf.namelist()):
        path = PurePosixPath(name)
        if path.suffix != ".json" or not path.parent.name.endswith(".highlights"):
            continue
        try:
            data = json.loads(zf.read(name))
        except Exception:
            logger.warning("Unreadable highlights file %s", name, exc_info=True)
            continue
        entries = data.get("highlights", []) if isinstance(data, dict) else []
        texts: List[str] = []
        stack = list(entries)
        while stack:  # the list is nested one level per layer
            entry = stack.pop(0)
            if isinstance(entry, list):
                stack[0:0] = entry
            elif isinstance(entry, dict) and str(entry.get("text") or "").strip():
                texts.append(str(entry["text"]).strip())
        if texts:
            found[path.stem] = texts
    return found


def _source_texts(zf: zipfile.ZipFile, plan: PagePlan) -> List[str]:
    """Text of each page of the PDF/EPUB, laid out the way the renderer lays it out."""
    if not plan.source_member:
        return []
    from rm_mcp.extract.render import open_source_document

    try:
        doc = open_source_document(zf.read(plan.source_member), plan.source_kind)
    except Exception:
        logger.warning("Could not open the document's %s", plan.source_kind, exc_info=True)
        return []
    try:
        return [page.get_text().strip() for page in doc]
    finally:
        doc.close()


def read_document_pages(zip_path) -> DocumentPages:
    """Read every tablet page of a document archive."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        plan = read_page_plan(zf)
        hashes = page_content_hashes(zf, plan)
        sources = _source_texts(zf, plan)
        old_highlights = _json_highlights(zf)
        pages: List[Page] = []
        for number, (ref, page_hash) in enumerate(zip(plan.pages, hashes), start=1):
            typed, highlights, has_ink = "", [], False
            if ref.rm_member:
                typed, highlights, has_ink = _scene_parts(zf.read(ref.rm_member))
            if ref.page_id in old_highlights:
                highlights = highlights + old_highlights[ref.page_id]
            source_text = ""
            if ref.source_page is not None and ref.source_page < len(sources):
                source_text = sources[ref.source_page]
            pages.append(
                Page(
                    number=number,
                    hash=page_hash,
                    source_page=None if ref.source_page is None else ref.source_page + 1,
                    inserted=plan.source_member is not None and ref.source_page is None,
                    typed_text=typed,
                    source_text=source_text,
                    highlights=tuple(highlights),
                    has_ink=has_ink,
                )
            )
    return DocumentPages(kind=plan.source_kind or "notebook", pages=tuple(pages))
