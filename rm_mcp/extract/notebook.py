"""
reMarkable notebook (.rm) helpers: typed text of one page, page hashes, page count.

Per-page content of a whole document lives in ``extract.document``.
"""

import logging
import zipfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def extract_text_from_rm_file(rm_file_path: Path) -> List[str]:
    """
    Typed text of a v6 .rm page (Type Folio or on-screen keyboard), one entry per paragraph.

    Text is stored natively in the page, so no OCR is involved.
    """
    from rmscene import read_tree
    from rmscene.text import TextDocument

    try:
        with open(rm_file_path, "rb") as f:
            tree = read_tree(f)
        # Typed text lives in the page's root text block, not among the
        # layer's children (those hold strokes and highlight glyph ranges).
        if tree.root_text is None:
            return []
        # TextDocument resolves the CRDT ordering, deletions and inline
        # formatting codes into one entry per paragraph.
        lines = [str(p) for p in TextDocument.from_scene_item(tree.root_text).contents]
        while lines and not lines[-1].strip():
            lines.pop()
        return lines if any(line.strip() for line in lines) else []
    except Exception:
        # Older (pre-v6) files and unknown blocks fail to parse; that page
        # simply has no typed text.
        logger.debug("No typed text extracted from %s", rm_file_path, exc_info=True)
        return []


def get_page_hashes(zip_path: Path) -> List[Optional[str]]:
    """Content hash of each page, in page order (None for a blank notebook page).

    A notebook page's hash is the sha256 of its .rm blob. A page over a PDF or
    EPUB also folds in the source's hash and its page index, since the render
    (and so a transcript) shows both. Editing one page changes only its own key.
    """
    from rm_mcp.extract.pages import page_content_hashes, read_page_plan

    with zipfile.ZipFile(zip_path, "r") as zf:
        return page_content_hashes(zf, read_page_plan(zf))


def get_document_page_count(zip_path: Path) -> int:
    """
    Number of tablet pages in a document archive.

    The .content page list counts blank and inserted pages. Without one, the
    .rm files count, and failing those the source PDF/EPUB's own pages.
    """
    from rm_mcp.extract.pages import read_page_plan

    with zipfile.ZipFile(zip_path, "r") as zf:
        return len(read_page_plan(zf).pages)
