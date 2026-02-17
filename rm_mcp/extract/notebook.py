"""
reMarkable notebook (.rm) extraction.

Handles .rm file parsing, page ordering, zip extraction,
and document text extraction.
"""

import json
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from rm_mcp.cache import (
    _MAX_EXTRACTION_CACHE_SIZE,
    _extraction_cache,
    _is_cache_valid,
)


def _safe_extractall(zf: zipfile.ZipFile, target_dir: Path) -> None:
    """Extract zip contents with Zip Slip protection."""
    resolved_target = str(target_dir.resolve())
    for member in zf.namelist():
        member_path = (target_dir / member).resolve()
        if not str(member_path).startswith(resolved_target):
            raise ValueError(f"Zip member '{member}' would extract outside target directory")
    zf.extractall(target_dir)


def extract_text_from_rm_file(rm_file_path: Path) -> List[str]:
    """
    Extract typed text from a .rm file using rmscene.

    This extracts text that was typed via Type Folio or on-screen keyboard.
    Does NOT require OCR - text is stored natively in v6 .rm files.
    """
    try:
        from rmscene import read_blocks
        from rmscene.scene_items import Text
        from rmscene.scene_tree import SceneTree

        with open(rm_file_path, "rb") as f:
            tree = SceneTree()
            for block in read_blocks(f):
                tree.add_block(block)

        text_lines = []

        # Extract text from the scene tree
        for item in tree.root.children.values():
            if hasattr(item, "value") and isinstance(item.value, Text):
                text_obj = item.value
                if hasattr(text_obj, "items"):
                    for text_item in text_obj.items:
                        if hasattr(text_item, "value") and text_item.value:
                            text_lines.append(str(text_item.value))

        return text_lines

    except ImportError:
        return []  # rmscene not available
    except Exception:
        # Log but don't fail - file might be older format
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


def extract_text_from_document_zip(
    zip_path: Path, include_ocr: bool = False, doc_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Extract all text content from a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file
        include_ocr: Whether to run OCR on handwritten content
        doc_id: Optional document ID for caching OCR results

    Returns:
        {
            "typed_text": [...],      # From rmscene parsing (list of strings)
            "highlights": [...],       # From PDF annotations
            "handwritten_text": [...], # From OCR (if enabled) - one per page, in order
            "pages": int,
            "page_ids": [...],         # Page UUIDs in order
            "ocr_backend": str,        # Which OCR backend was used (if any)
        }
    """
    # Check cache if doc_id provided
    if doc_id and doc_id in _extraction_cache:
        cached = _extraction_cache[doc_id]
        # Return cached result if OCR requirement is satisfied and cache is valid
        # (cached with OCR can satisfy no-OCR request, but not vice versa)
        if (cached["include_ocr"] or not include_ocr) and _is_cache_valid(cached):
            return cached["result"]

    result: Dict[str, Any] = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": None,
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

        # OCR for handwritten content is handled at the tool level via sampling.
        # This function no longer performs OCR directly.

    # Cache result if doc_id provided
    if doc_id:
        _extraction_cache[doc_id] = {
            "result": result,
            "include_ocr": include_ocr,
            "timestamp": time.time(),
        }
        if len(_extraction_cache) > _MAX_EXTRACTION_CACHE_SIZE:
            # Evict oldest entries (by insertion order, dicts are ordered in Python 3.7+)
            excess = len(_extraction_cache) - _MAX_EXTRACTION_CACHE_SIZE
            keys_to_remove = list(_extraction_cache.keys())[:excess]
            for key in keys_to_remove:
                del _extraction_cache[key]

    return result
