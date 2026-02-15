"""
OCR backends for reMarkable handwriting recognition.

Provides Google Vision, Tesseract, and MCP sampling-based OCR.
"""

from rm_mcp.ocr.dispatch import extract_handwriting_ocr  # noqa: F401
from rm_mcp.ocr.google_vision import (  # noqa: F401
    _ocr_google_vision,
    _ocr_google_vision_rest,
    _ocr_google_vision_sdk,
)
from rm_mcp.ocr.sampling import (  # noqa: F401
    get_ocr_backend,
    ocr_pages_via_sampling,
    ocr_via_sampling,
    should_use_sampling_ocr,
)
from rm_mcp.ocr.tesseract import _ocr_tesseract  # noqa: F401
