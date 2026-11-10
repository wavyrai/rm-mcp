"""Extraction against real-format fixtures (see tests/fixtures/make_fixtures.py)."""

import json
import zipfile
from pathlib import Path

from rm_mcp.extract.document import read_document_pages
from rm_mcp.extract.notebook import extract_text_from_rm_file

FIXTURES = Path(__file__).parent / "fixtures"
TYPED_LINES = ["Meeting notes", "Ship the fixture set", "Fix typed text extraction"]


def _zip(tmp_path: Path, content: dict, members: dict) -> Path:
    doc_id = "11111111-2222-3333-4444-555555555555"
    zip_path = tmp_path / "doc.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{doc_id}.content", json.dumps(content))
        for name, fixture in members.items():
            zf.write(FIXTURES / fixture, name.format(doc=doc_id))
    return zip_path


def _notebook_zip(tmp_path: Path, pages: list) -> Path:
    """Wrap fixture pages in a document zip the way the cloud serves a notebook."""
    page_ids = [f"page-{n}" for n in range(len(pages))]
    content = {"cPages": {"pages": [{"id": pid} for pid in page_ids]}}
    return _zip(tmp_path, content, {f"{{doc}}/{pid}.rm": f for pid, f in zip(page_ids, pages)})


def test_typed_text_comes_from_root_text():
    text = "\n".join(extract_text_from_rm_file(FIXTURES / "typed_text.rm"))
    assert text.splitlines() == TYPED_LINES


def test_strokes_and_highlights_are_not_typed_text():
    assert extract_text_from_rm_file(FIXTURES / "handwriting.rm") == []
    assert extract_text_from_rm_file(FIXTURES / "highlight.rm") == []


def test_notebook_pages_report_typed_text_and_ink(tmp_path):
    doc = read_document_pages(_notebook_zip(tmp_path, ["typed_text.rm", "handwriting.rm"]))
    assert doc.kind == "notebook" and len(doc.pages) == 2
    assert doc.pages[0].typed_text.splitlines() == TYPED_LINES
    assert not doc.pages[0].has_ink
    assert doc.pages[1].has_ink and doc.pages[1].text == ""


def test_glyph_highlight_is_a_highlight_not_ink(tmp_path):
    [page] = read_document_pages(_notebook_zip(tmp_path, ["highlight.rm"])).pages
    assert page.highlights == ("quick brown fox",) and not page.has_ink


def test_pdf_fixture_has_three_text_pages(tmp_path):
    doc = read_document_pages(_zip(tmp_path, {}, {"{doc}.pdf": "three_pages.pdf"}))
    assert doc.kind == "pdf" and len(doc.pages) == 3
    assert "zebras" in doc.pages[2].source_text
    assert [p.source_page for p in doc.pages] == [1, 2, 3]


def test_annotated_pdf_counts_the_inserted_page():
    doc = read_document_pages(FIXTURES / "annotated_pdf.rmdoc")
    assert [p.source_page for p in doc.pages] == [1, None, 2, 3]
    assert [p.inserted for p in doc.pages] == [False, True, False, False]
    assert [p.has_ink for p in doc.pages] == [True, True, False, True]
    assert "zebras" in doc.pages[3].text


def test_epub_fixture_has_chapters(tmp_path):
    doc = read_document_pages(_zip(tmp_path, {}, {"{doc}.epub": "small.epub"}))
    text = "\n".join(p.text for p in doc.pages)
    assert doc.kind == "epub"
    assert "lighthouses" in text and "tidal pools" in text


def test_newer_firmware_page_reads_quietly(caplog):
    """SceneInfo (block 13) with unparsed fields and an unknown block type.

    rmscene 0.8 reads past both and logs at WARNING/INFO; those messages go to
    rm-mcp's logger at DEBUG, and the strokes and typed text still read.
    """
    import logging

    from rm_mcp.extract.render import read_scene, render_rm_file_to_png

    caplog.set_level(logging.DEBUG)
    text = extract_text_from_rm_file(FIXTURES / "future_format.rm")
    tree = read_scene((FIXTURES / "future_format.rm").read_bytes())
    png = render_rm_file_to_png(FIXTURES / "future_format.rm")

    assert "\n".join(text).splitlines() == TYPED_LINES
    lines = [i for i in tree.walk() if type(i).__name__ == "Line"]
    assert len(lines) == 2 and png
    loud = [r for r in caplog.records if r.name.startswith("rmscene") and r.levelno > 10]
    assert loud == []
    quiet = [r for r in caplog.records if r.name == "rm_mcp.extract.rmscene"]
    messages = " ".join(r.getMessage() for r in quiet)
    assert "block_type=13" in messages and "Unknown block type 126" in messages
    assert all(r.levelno == logging.DEBUG for r in quiet)
