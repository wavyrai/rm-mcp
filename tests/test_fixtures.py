"""Extraction against real-format fixtures (see tests/fixtures/make_fixtures.py)."""

import json
import zipfile
from pathlib import Path

from rm_mcp.extract.epub import extract_epub_sections
from rm_mcp.extract.notebook import extract_text_from_document_zip, extract_text_from_rm_file
from rm_mcp.extract.pdf import extract_pdf_pages

FIXTURES = Path(__file__).parent / "fixtures"
TYPED_LINES = ["Meeting notes", "Ship the fixture set", "Fix typed text extraction"]


def _notebook_zip(tmp_path: Path, pages: list) -> Path:
    """Wrap fixture pages in a document zip the way the cloud serves a notebook."""
    doc_id = "11111111-2222-3333-4444-555555555555"
    page_ids = [f"page-{n}" for n in range(len(pages))]
    content = {"cPages": {"pages": [{"id": pid} for pid in page_ids]}}
    zip_path = tmp_path / "notebook.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{doc_id}.content", json.dumps(content))
        for pid, fixture in zip(page_ids, pages):
            zf.write(FIXTURES / fixture, f"{doc_id}/{pid}.rm")
    return zip_path


def test_typed_text_comes_from_root_text():
    text = "\n".join(extract_text_from_rm_file(FIXTURES / "typed_text.rm"))
    assert text.splitlines() == TYPED_LINES


def test_strokes_and_highlights_are_not_typed_text():
    assert extract_text_from_rm_file(FIXTURES / "handwriting.rm") == []
    assert extract_text_from_rm_file(FIXTURES / "highlight.rm") == []


def test_notebook_zip_reports_typed_text(tmp_path):
    zip_path = _notebook_zip(tmp_path, ["typed_text.rm", "handwriting.rm"])
    result = extract_text_from_document_zip(zip_path)
    assert result["pages"] == 2
    assert "\n".join(result["typed_text"]).splitlines() == TYPED_LINES


def test_pdf_fixture_has_three_text_pages():
    pages = extract_pdf_pages(FIXTURES / "three_pages.pdf")
    assert len(pages) == 3
    assert "zebras" in pages[2]


def test_epub_fixture_has_chapters():
    text = "\n".join(extract_epub_sections(FIXTURES / "small.epub"))
    assert "lighthouses" in text and "tidal pools" in text
