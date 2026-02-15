"""PDF text extraction."""

from pathlib import Path


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text from a PDF file using PyMuPDF.

    Returns the full text content of the PDF.
    """
    try:
        import fitz  # PyMuPDF

        text_parts = []
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, 1):
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(f"--- Page {page_num} ---\n{page_text.strip()}")

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        return ""
