"""
reMarkable notebook (.rm) extraction.

Handles .rm file parsing, page ordering, zip extraction,
and document text extraction.
"""

import hashlib
import json
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _safe_extractall(zf: zipfile.ZipFile, target_dir: Path) -> None:
    """Extract zip contents with Zip Slip protection."""
    resolved_target = target_dir.resolve()
    for member in zf.namelist():
        member_path = (target_dir / member).resolve()
        # A prefix string comparison would also accept a sibling directory whose
        # name merely starts with the target's, so compare path components.
        if member_path != resolved_target and resolved_target not in member_path.parents:
            raise ValueError(f"Zip member '{member}' would extract outside target directory")
    zf.extractall(target_dir)


def extract_text_from_rm_file(rm_file_path: Path) -> List[str]:
    """
    Extract typed text from a .rm file using rmscene.

    This extracts text that was typed via Type Folio or on-screen keyboard.
    Does NOT require OCR - text is stored natively in v6 .rm files.
    """
    try:
        from rmscene import read_tree
        from rmscene.text import TextDocument
    except ImportError:
        return []  # rmscene not available

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


def _get_ordered_rm_files(tmpdir_path: Path) -> List[Optional[Path]]:
    """Extract and order .rm files from an extracted document directory.

    Reads the .content file to determine page order and returns entries for
    ALL pages (including blank ones without .rm files as None).
    Falls back to filesystem order if no page order found.

    Args:
        tmpdir_path: Path to the extracted document directory

    Returns:
        List of .rm file paths (or None for blank pages) in correct page order
    """
    # Get page order from .content file
    page_order = []
    for content_file in tmpdir_path.glob("*.content"):
        try:
            data = json.loads(content_file.read_text())
            # New format: cPages.pages array
            if "cPages" in data and "pages" in data["cPages"]:
                page_order = [p["id"] for p in data["cPages"]["pages"]]
            # Fallback: pages array directly
            elif "pages" in data and isinstance(data["pages"], list):
                page_order = data["pages"]
        except Exception:
            # Ignore errors reading/parsing .content file; fallback to default page order
            pass
        break

    rm_files = list(tmpdir_path.glob("**/*.rm"))

    # Sort rm_files by page order if available
    if page_order:
        rm_by_id = {}
        for rm_file in rm_files:
            page_id = rm_file.stem
            rm_by_id[page_id] = rm_file

        ordered: List[Optional[Path]] = []
        for page_id in page_order:
            # None for blank pages (no .rm file)
            ordered.append(rm_by_id.get(page_id))
        # Add any remaining files not in page order
        seen = set(page_order)
        for rm_file in rm_files:
            if rm_file.stem not in seen:
                ordered.append(rm_file)
        return ordered

    return rm_files


def archive_content_hash(zip_path: Path) -> str:
    """Content hash of a document archive: its member names and their bytes.

    The archive's own bytes are not stable (zip entries carry write times), but
    its members are the document's blobs, so hashing (name, sha256) pairs gives
    the same key for the same document version however it was packed.
    """
    digest = hashlib.sha256()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in sorted(zf.namelist()):
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(zf.read(name)).digest())
    return digest.hexdigest()


def get_page_hashes(zip_path: Path) -> List[Optional[str]]:
    """SHA-256 of each page's .rm blob, in page order (None for blank pages).

    A page's .rm bytes are everything its render and its OCR depend on, so the
    hash is the cache key for both: editing one page changes only its own key.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extractall(zf, tmpdir_path)
        return [
            hashlib.sha256(f.read_bytes()).hexdigest() if f is not None else None
            for f in _get_ordered_rm_files(tmpdir_path)
        ]


def get_document_page_count(zip_path: Path) -> int:
    """
    Get the number of pages in a reMarkable document zip.

    Uses the .content metadata (cPages.pages or pages array) which lists all
    pages including blank ones. Falls back to counting .rm files if no metadata.

    Args:
        zip_path: Path to the document zip file

    Returns:
        Number of pages (0 if unable to determine)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extractall(zf, tmpdir_path)

        # Read page count from .content metadata (includes blank pages)
        for content_file in tmpdir_path.glob("*.content"):
            try:
                data = json.loads(content_file.read_text())
                if "cPages" in data and "pages" in data["cPages"]:
                    return len(data["cPages"]["pages"])
                if "pages" in data and isinstance(data["pages"], list):
                    return len(data["pages"])
            except Exception:
                pass
            break

        # Fallback: count .rm files (misses blank pages)
        return len(list(tmpdir_path.glob("**/*.rm")))


def _extract_source_pages(tmpdir_path: Path) -> List[str]:
    """Extract the original PDF or EPUB bundled in a document zip, per page.

    A reMarkable document zip carries the source file alongside the annotation
    layer, so a PDF's own text is available without a second download. PDF
    entries are aligned with the document's real page numbers (blank pages stay
    as empty strings); EPUB entries are the spine's content documents.
    """
    from rm_mcp.extract.epub import extract_epub_sections
    from rm_mcp.extract.pdf import extract_pdf_pages

    for pdf_file in sorted(tmpdir_path.glob("**/*.pdf")):
        pages = extract_pdf_pages(pdf_file)
        if any(p.strip() for p in pages):
            return pages

    for epub_file in sorted(tmpdir_path.glob("**/*.epub")):
        sections = extract_epub_sections(epub_file)
        if sections:
            return sections

    return []


def extract_text_from_document_zip(
    zip_path: Path,
    include_ocr: bool = False,
    doc_id: Optional[str] = None,
    include_source: bool = True,
) -> Dict[str, Any]:
    """
    Extract all text content from a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file
        include_ocr: Whether to run OCR on handwritten content
        doc_id: Optional document ID for caching OCR results
        include_source: Whether to extract the underlying PDF/EPUB text

    Returns:
        {
            "typed_text": [...],      # From rmscene parsing (list of strings)
            "highlights": [...],       # From PDF annotations
            "handwritten_text": [...], # From OCR (if enabled) - one per page, in order
            "source_text": str,        # Text of the underlying PDF/EPUB, if any
            "source_pages": [...],     # Same text, one entry per real page/section
            "pages": int,
            "page_ids": [...],         # Page UUIDs in order
            "ocr_backend": str,        # Which OCR backend was used (if any)
        }
    """
    from rm_mcp.cache import cache_ocr_result, get_cached_ocr_result

    # Cached results are keyed by the archive's content hash, so an edited
    # document misses instead of returning its old text. A cached result that
    # includes OCR (or source text) can satisfy a request without it, but not
    # the other way around.
    source_hash = archive_content_hash(zip_path) if doc_id else None
    if doc_id:
        cached = get_cached_ocr_result(
            doc_id, source_hash, include_ocr=include_ocr, include_source=include_source
        )
        if cached is not None:
            return cached

    result: Dict[str, Any] = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": None,
        "source_text": "",
        "source_pages": [],
        "pages": 0,
        "page_ids": [],
        "ocr_backend": None,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extractall(zf, tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)
        result["page_ids"] = [f.stem if f else None for f in rm_files]
        result["pages"] = len(rm_files)

        # Extract typed text from .rm files using rmscene (skip blank pages)
        for rm_file in rm_files:
            if rm_file is not None:
                text_lines = extract_text_from_rm_file(rm_file)
                result["typed_text"].extend(text_lines)

        # Extract text from .txt and .md files
        for txt_file in tmpdir_path.glob("**/*.txt"):
            try:
                content = txt_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        for md_file in tmpdir_path.glob("**/*.md"):
            try:
                content = md_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        # Extract from .content files (metadata with text)
        for content_file in tmpdir_path.glob("**/*.content"):
            try:
                data = json.loads(content_file.read_text())
                if "text" in data:
                    result["typed_text"].append(data["text"])
            except Exception:
                # Malformed JSON or read error - skip this file
                pass

        # Extract PDF highlights
        for json_file in tmpdir_path.glob("**/*.json"):
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict) and "highlights" in data:
                    for h in data.get("highlights", []):
                        if "text" in h and h["text"]:
                            result["highlights"].append(h["text"])
            except Exception:
                # Malformed JSON - skip this file
                pass

        # Extract the underlying PDF/EPUB text (the document's own content,
        # as opposed to the annotation layer above).
        if include_source:
            try:
                source_pages = _extract_source_pages(tmpdir_path)
                result["source_pages"] = source_pages
                result["source_text"] = "\n\n".join(
                    f"--- Page {n} ---\n{text}"
                    for n, text in enumerate(source_pages, 1)
                    if text.strip()
                )
            except Exception:
                logger.warning("Failed to extract source document text", exc_info=True)

        # OCR for handwritten content is handled at the tool level via sampling.
        # This function no longer performs OCR directly.

    # Cache result if doc_id provided. Goes through cache_ocr_result so the
    # write-through to the persistent index happens here too.
    if doc_id:
        cache_ocr_result(
            doc_id, source_hash, result, include_ocr=include_ocr, include_source=include_source
        )

    return result
