"""
OCR backend dispatch.

Selects the appropriate OCR backend based on environment configuration.
"""

import os
from pathlib import Path
from typing import List, Optional

from rm_mcp.ocr.google_vision import _ocr_google_vision
from rm_mcp.ocr.tesseract import _ocr_tesseract


def extract_handwriting_ocr(rm_files: List[Path]) -> tuple[Optional[List[str]], Optional[str]]:
    """
    Extract handwritten text using OCR.

    Supports multiple backends (set REMARKABLE_OCR_BACKEND env var):
    - "sampling": Uses client's LLM via MCP sampling (requires async context, tools only)
    - "google": Google Cloud Vision - best for handwriting
    - "tesseract": pytesseract - basic OCR, requires rmc + cairosvg
    - "auto" (default): Google if API key provided, else Tesseract

    Note: "sampling" backend requires async context and is only available via tools,
    not via MCP resources. When sampling is configured but this sync function is called
    (e.g., from resources), it falls back to the auto-detection logic.

    Returns:
        Tuple of (ocr_results, backend_used) where backend_used is "google" or "tesseract"
    """
    backend = os.environ.get("REMARKABLE_OCR_BACKEND", "auto").lower()

    # Sampling backend requires async context - can't be used from sync functions
    # Fall back to auto-detection for resources and other sync callers
    if backend == "sampling":
        backend = "auto"

    # Auto-detect best available backend
    if backend == "auto":
        # Check for Google Vision API key first (simplest auth method)
        if os.environ.get("GOOGLE_VISION_API_KEY"):
            backend = "google"
        else:
            backend = "tesseract"

    if backend == "google":
        result = _ocr_google_vision(rm_files)
        return (result, "google")
    else:
        result = _ocr_tesseract(rm_files)
        return (result, "tesseract")
