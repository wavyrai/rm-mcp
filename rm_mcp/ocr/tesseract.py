"""
Tesseract OCR backend.

Basic quality - designed for printed text, not handwriting.
Requires: pytesseract, rmc, cairosvg (or inkscape)
"""

from pathlib import Path
from typing import List, Optional

from rm_mcp.extract.render import (
    REMARKABLE_HEIGHT,
    REMARKABLE_WIDTH,
    _render_rm_to_ocr_png,
)


def _ocr_tesseract(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Tesseract.
    Basic quality - designed for printed text, not handwriting.

    Requires: pytesseract, rmc, cairosvg (or inkscape)
    """
    try:
        import pytesseract
        from PIL import Image, ImageFilter, ImageOps

        ocr_results = []

        for rm_file in rm_files:
            tmp_png_path = None
            try:
                # Use 1.5x resolution for better OCR (2x is too slow)
                tmp_png_path = _render_rm_to_ocr_png(
                    rm_file,
                    output_width=2106,   # 1.5x reMarkable width
                    output_height=2808,  # 1.5x reMarkable height
                )
                if tmp_png_path is None:
                    continue

                # Preprocess image for better OCR
                img = Image.open(tmp_png_path)
                img = img.convert("L")
                img = ImageOps.autocontrast(img, cutoff=2)
                img = img.filter(ImageFilter.SHARPEN)

                # Run OCR with optimized settings for sparse handwriting
                custom_config = r"--psm 11 --oem 3"
                text = pytesseract.image_to_string(img, config=custom_config)

                if text.strip():
                    ocr_results.append(text.strip())

            except FileNotFoundError:
                return None
            except Exception:
                pass
            finally:
                if tmp_png_path:
                    tmp_png_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # OCR dependencies not installed
        return None
