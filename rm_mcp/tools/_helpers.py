"""
Shared helpers, caches, and re-exports for MCP tool modules.

Tool modules access commonly-patched names through this module
(e.g., ``_helpers.get_cached_collection()``) so that a single
``unittest.mock.patch`` target works for all tools.
"""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

from mcp.types import ToolAnnotations

# --- Re-exports (commonly patched in tests) ---
# Tool modules access these via ``_helpers.X()`` so tests can patch once
# at ``rm_mcp.tools._helpers.X``.

from rm_mcp.api import (  # noqa: F401
    REMARKABLE_TOKEN,
    get_file_type,
)
from rm_mcp.paths import (  # noqa: F401
    _apply_root_filter,
    _find_document,
    _get_root_path,
    _is_within_root,
    get_item_path,
    get_items_by_id,
    get_items_by_parent,
)
from rm_mcp.cache import get_cached_collection  # noqa: F401
from rm_mcp.extract import (  # noqa: F401
    cache_page_ocr,
    extract_text_from_document_zip,
    extract_text_from_epub,
    extract_text_from_pdf,
    get_background_color,
    get_cached_ocr_result,
    get_cached_page_ocr,
    get_document_page_count,
    render_page_from_document_zip,
    render_page_from_document_zip_svg,
)
from rm_mcp.responses import make_error, make_response  # noqa: F401
from rm_mcp.ocr.sampling import (  # noqa: F401
    get_ocr_backend,
    ocr_via_sampling,
    should_use_sampling_ocr,
)


# --- Helper functions ---


@contextmanager
def _temp_document(data: bytes, suffix: str = ".zip"):
    """Context manager for writing data to a temp file with guaranteed cleanup."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(data)
        yield tmp_path
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


# --- Caches ---

_file_type_cache: Dict[str, str] = {}

_rendered_image_cache: Dict[str, str] = {}  # key: f"{doc_id}:{page}" -> base64 PNG


def _get_file_type_cached(client, doc) -> str:
    """Get file type with caching to avoid repeated lookups."""
    doc_id = doc.ID
    if doc_id in _file_type_cache:
        return _file_type_cache[doc_id]
    file_type = get_file_type(client, doc)
    _file_type_cache[doc_id] = file_type
    return file_type


def _resolve_root_path(path: str) -> str:
    """Resolve a user-provided path to the actual path on device.

    If root is '/Work', then '/Project' becomes '/Work/Project'.
    """
    root = _get_root_path()
    if root == "/":
        return path
    if path == "/":
        return root
    # Prepend root to the path
    return root + path


def _is_cloud_archived(item) -> bool:
    """Check if an item is cloud-archived (not available on device).

    Items in the trash (parent == "trash") are not downloadable.
    """
    if hasattr(item, "is_cloud_archived"):
        return item.is_cloud_archived
    parent = item.Parent if hasattr(item, "Parent") else getattr(item, "parent", "")
    return parent == "trash"


def _ocr_png_tesseract(png_path: Path) -> Optional[str]:
    """
    OCR a PNG file using Tesseract.

    Args:
        png_path: Path to the PNG file

    Returns:
        Extracted text, or None if OCR failed
    """
    try:
        import pytesseract
        from PIL import Image as PILImage
        from PIL import ImageFilter, ImageOps

        img = PILImage.open(png_path)

        # Convert to grayscale
        img = img.convert("L")

        # Increase contrast
        img = ImageOps.autocontrast(img, cutoff=2)

        # Slight sharpening
        img = img.filter(ImageFilter.SHARPEN)

        # Run OCR with settings optimized for sparse handwriting
        custom_config = r"--psm 11 --oem 3"
        text = pytesseract.image_to_string(img, config=custom_config)

        return text.strip() if text.strip() else None

    except ImportError:
        return None
    except Exception:
        return None


def _ocr_png_google_vision(png_path: Path) -> Optional[str]:
    """
    OCR a PNG file using Google Cloud Vision API.

    Args:
        png_path: Path to the PNG file

    Returns:
        Extracted text, or None if OCR failed
    """
    import base64

    import requests

    api_key = os.environ.get("GOOGLE_VISION_API_KEY")
    if not api_key:
        return None

    try:
        with open(png_path, "rb") as f:
            image_content = base64.b64encode(f.read()).decode("utf-8")

        url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
        payload = {
            "requests": [
                {
                    "image": {"content": image_content},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        }

        response = requests.post(url, json=payload, timeout=60)
        if response.status_code == 200:
            data = response.json()
            if "responses" in data and data["responses"]:
                resp = data["responses"][0]
                if "fullTextAnnotation" in resp:
                    text = resp["fullTextAnnotation"]["text"]
                    return text.strip() if text.strip() else None

    except Exception:
        # Silently fail - OCR is best-effort and caller will handle None
        pass

    return None


# --- Tool annotations ---

# Base annotations for read-only operations
_BASE_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,  # Private cloud account, not open world
}

# Unique annotations for each tool with descriptive titles
READ_ANNOTATIONS = ToolAnnotations(
    title="Read reMarkable Document",
    **_BASE_ANNOTATIONS,
)

BROWSE_ANNOTATIONS = ToolAnnotations(
    title="Browse reMarkable Library",
    **_BASE_ANNOTATIONS,
)

SEARCH_ANNOTATIONS = ToolAnnotations(
    title="Search reMarkable Documents",
    **_BASE_ANNOTATIONS,
)

RECENT_ANNOTATIONS = ToolAnnotations(
    title="Get Recent reMarkable Documents",
    **_BASE_ANNOTATIONS,
)

STATUS_ANNOTATIONS = ToolAnnotations(
    title="Check reMarkable Connection",
    **_BASE_ANNOTATIONS,
)

IMAGE_ANNOTATIONS = ToolAnnotations(
    title="Get reMarkable Page Image",
    **_BASE_ANNOTATIONS,
)

# Default page size for pagination (characters) - used for PDFs/EPUBs
DEFAULT_PAGE_SIZE = int(os.environ.get("REMARKABLE_PAGE_SIZE", "8000"))
