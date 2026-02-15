"""
OCR backend for reMarkable handwriting recognition.

Uses MCP sampling-based OCR (client's LLM extracts text from images).
"""

from rm_mcp.ocr.sampling import (  # noqa: F401
    get_ocr_backend,
    ocr_pages_via_sampling,
    ocr_via_sampling,
    should_use_sampling_ocr,
)
