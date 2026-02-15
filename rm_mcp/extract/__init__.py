"""
Text extraction helpers for reMarkable documents.

Provides PDF, EPUB, notebook (.rm) extraction, rendering, and OCR.
"""

# Re-export cache functions for backward compatibility
from rm_mcp.cache import (  # noqa: F401
    _extraction_cache,
    _is_cache_valid,
    _MAX_EXTRACTION_CACHE_SIZE,
    cache_ocr_result,
    cache_page_ocr,
    clear_extraction_cache,
    get_cached_ocr_result,
    get_cached_page_ocr,
)

# Re-export find_similar_documents for backward compatibility
from rm_mcp.paths import find_similar_documents  # noqa: F401

from rm_mcp.extract.epub import extract_text_from_epub  # noqa: F401
from rm_mcp.extract.notebook import (  # noqa: F401
    _get_ordered_rm_files,
    _safe_extractall,
    extract_text_from_document_zip,
    extract_text_from_rm_file,
    get_document_page_count,
)
from rm_mcp.extract.pdf import extract_text_from_pdf  # noqa: F401
from rm_mcp.extract.render import (  # noqa: F401
    CONTENT_MARGIN,
    REMARKABLE_BACKGROUND_COLOR,
    REMARKABLE_HEIGHT,
    REMARKABLE_WIDTH,
    _DEFAULT_BACKGROUND_COLOR,
    _add_svg_background,
    _get_svg_content_bounds,
    _parse_hex_color,
    _render_rm_to_ocr_png,
    get_background_color,
    render_page_from_document_zip,
    render_page_from_document_zip_svg,
    render_rm_file_to_png,
    render_rm_file_to_svg,
)
from rm_mcp.ocr import (  # noqa: F401
    _ocr_google_vision,
    _ocr_google_vision_rest,
    _ocr_google_vision_sdk,
    _ocr_tesseract,
    extract_handwriting_ocr,
)
