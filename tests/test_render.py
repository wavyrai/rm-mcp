"""Page rendering against the real-format fixtures, with golden images.

Goldens live in tests/fixtures/goldens. After an intended change to the
renderer, regenerate them and look at every image before committing:

    uv run pytest tests/test_render.py --update-goldens
"""

import io
import json
import zipfile
from pathlib import Path

import fitz
import pytest
from PIL import Image, ImageChops, ImageStat

from rm_mcp.extract import get_document_page_count, get_page_hashes
from rm_mcp.extract.render import (
    REMARKABLE_HEIGHT,
    REMARKABLE_WIDTH,
    UNITS_PER_POINT,
    render_page_from_document_zip,
    render_page_from_document_zip_svg,
    render_rm_file_to_png,
    render_rm_file_to_svg,
)

FIXTURES = Path(__file__).parent / "fixtures"
GOLDENS = FIXTURES / "goldens"
ANNOTATED = FIXTURES / "annotated_pdf.rmdoc"
PAPER = "#FBFBFB"
# Mean absolute difference per channel (0-255) a render may drift from its golden.
GOLDEN_TOLERANCE = 1.5
# A4 PDF page fitted into the device frame.
PDF_PAGE_SIZE = (round(595 / 842 * REMARKABLE_HEIGHT), REMARKABLE_HEIGHT)


def _image(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def _dark_pixels(img: Image.Image, box=None) -> int:
    gray = img.convert("L")
    if box:
        gray = gray.crop(box)
    return sum(gray.histogram()[:128])


def _pdf_to_px(x_pt: float, y_pt: float):
    """Pixel of a PDF point on a rendered A4 page."""
    scale = PDF_PAGE_SIZE[1] / 842
    return round(x_pt * scale), round(y_pt * scale)


def _notebook_zip(tmp_path: Path, fixture: str) -> Path:
    zip_path = tmp_path / f"{fixture}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("nb.content", json.dumps({"cPages": {"pages": [{"id": "p1"}]}}))
        zf.write(FIXTURES / fixture, "nb/p1.rm")
    return zip_path


def _epub_zip(tmp_path: Path) -> Path:
    zip_path = tmp_path / "book.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("book.content", json.dumps({"fileType": "epub"}))
        zf.write(FIXTURES / "small.epub", "book.epub")
    return zip_path


def _check_golden(request, name: str, png: bytes) -> None:
    path = GOLDENS / f"{name}.png"
    if request.config.getoption("--update-goldens"):
        GOLDENS.mkdir(exist_ok=True)
        path.write_bytes(png)
        return
    assert path.exists(), f"missing golden {path.name}; run with --update-goldens"
    got = _image(png).convert("RGB")
    want = Image.open(path).convert("RGB")
    assert got.size == want.size
    diff = sum(ImageStat.Stat(ImageChops.difference(got, want)).mean) / 3
    assert diff <= GOLDEN_TOLERANCE, f"{name} drifted from its golden: mean diff {diff:.2f}"


# --- Goldens -----------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["handwriting", "typed_text", "highlight"])
def test_notebook_page_golden(request, fixture):
    _check_golden(request, fixture, render_rm_file_to_png(FIXTURES / f"{fixture}.rm", PAPER))


@pytest.mark.parametrize("page", [1, 2, 3, 4])
def test_annotated_pdf_golden(request, page):
    _check_golden(
        request, f"annotated_pdf_p{page}", render_page_from_document_zip(ANNOTATED, page, PAPER)
    )


def test_epub_golden(request, tmp_path):
    _check_golden(request, "epub_p1", render_page_from_document_zip(_epub_zip(tmp_path), 1, PAPER))


# --- Structure ---------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["handwriting.rm", "typed_text.rm", "highlight.rm"])
def test_notebook_page_is_the_full_device_frame(tmp_path, fixture):
    img = _image(render_page_from_document_zip(_notebook_zip(tmp_path, fixture), 1, PAPER))
    assert img.size == (REMARKABLE_WIDTH, REMARKABLE_HEIGHT)


def test_pdf_page_is_the_full_page_not_the_ink(tmp_path):
    for page in (1, 3, 4):
        assert _image(render_page_from_document_zip(ANNOTATED, page, PAPER)).size == PDF_PAGE_SIZE


def test_strokes_land_where_they_were_drawn():
    img = _image(render_rm_file_to_png(FIXTURES / "handwriting.rm", PAPER)).convert("L")
    # The left wall runs down x = -200 (pixel 502) from y = 600 to 1000.
    assert img.getpixel((502, 800)) < 64
    # The middle of the house, between the walls and above the door, is paper.
    assert img.getpixel((702, 700)) > 240


def test_typed_text_is_drawn():
    img = _image(render_rm_file_to_png(FIXTURES / "typed_text.rm", PAPER))
    assert _dark_pixels(img) > 2000


def test_unannotated_pdf_page_shows_the_pdf_text():
    # Tablet page 3 has no ink: all that is drawn is the PDF's page 2.
    img = _image(render_page_from_document_zip(ANNOTATED, 3, PAPER))
    x0, y0 = _pdf_to_px(68, 84)
    x1, y1 = _pdf_to_px(372, 108)
    assert _dark_pixels(img, (x0, y0, x1, y1)) > 1500
    assert _dark_pixels(img) == _dark_pixels(img, (x0, y0, x1, y1))


def test_unannotated_pdf_without_page_list_still_renders(tmp_path):
    zip_path = tmp_path / "plain.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("doc.content", json.dumps({"fileType": "pdf"}))
        zf.write(FIXTURES / "three_pages.pdf", "doc.pdf")
    assert get_document_page_count(zip_path) == 3
    img = _image(render_page_from_document_zip(zip_path, 3, PAPER))
    assert img.size == PDF_PAGE_SIZE
    assert _dark_pixels(img) > 1500


def test_inserted_page_shifts_the_pdf_pages():
    """Tablet page 2 was inserted; pages 3 and 4 show PDF pages 2 and 3."""
    assert get_document_page_count(ANNOTATED) == 4
    pdf = fitz.open(FIXTURES / "three_pages.pdf")
    zoom = PDF_PAGE_SIZE[1] / 842

    def pdf_page(index):
        pix = pdf[index].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def mean_diff(a, b):
        b = b.resize(a.size)
        return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3

    page3 = _image(render_page_from_document_zip(ANNOTATED, 3, "#FFFFFF")).convert("RGB")
    assert mean_diff(page3, pdf_page(1)) < 0.5
    assert mean_diff(page3, pdf_page(1)) < mean_diff(page3, pdf_page(2))

    # The inserted page is a notebook page with the cross and no PDF text.
    inserted = _image(render_page_from_document_zip(ANNOTATED, 2, PAPER)).convert("L")
    assert inserted.size == (REMARKABLE_WIDTH, REMARKABLE_HEIGHT)
    assert inserted.getpixel((702, 900)) < 64
    assert _dark_pixels(inserted, (0, 0, REMARKABLE_WIDTH, 400)) == 0


def test_pdf_ink_is_mapped_onto_the_pdf_page():
    img = _image(render_page_from_document_zip(ANNOTATED, 1, PAPER)).convert("RGB")
    # The red underline sits 30 units (~9.6 pt) under the text's middle.
    x, y = _pdf_to_px(200, 96 + 30 / UNITS_PER_POINT)
    r, g, b = img.getpixel((x, y))
    assert r > 150 and g < 100 and b < 100
    # The highlighter is translucent yellow over the text line.
    hx, hy = _pdf_to_px(70, 96)
    r, g, b = img.getpixel((hx, hy))
    assert r > 230 and b < 230 and abs(r - g) < 25

    img4 = _image(render_page_from_document_zip(ANNOTATED, 4, PAPER)).convert("RGB")
    # The blue ring passes 110 units left of the centre of "zebras".
    x, y = _pdf_to_px(279 - 110 / UNITS_PER_POINT, 96)
    r, g, b = img4.getpixel((x, y))
    assert b > 150 and r < 120


def test_transparent_background_has_alpha():
    img = _image(render_rm_file_to_png(FIXTURES / "handwriting.rm", "#00000000"))
    assert img.mode == "RGBA"
    assert img.getpixel((5, 5))[3] == 0
    assert img.getpixel((502, 800))[3] > 200


def test_page_out_of_range_is_none():
    assert render_page_from_document_zip(ANNOTATED, 0) is None
    assert render_page_from_document_zip(ANNOTATED, 5) is None


def test_svg_is_the_full_page():
    svg = render_rm_file_to_svg(FIXTURES / "handwriting.rm", PAPER)
    assert svg.startswith("<svg") and f'width="{REMARKABLE_WIDTH}"' in svg
    pdf_svg = render_page_from_document_zip_svg(ANNOTATED, 3, PAPER)
    assert f'height="{REMARKABLE_HEIGHT}"' in pdf_svg
    assert "<path" in pdf_svg


def test_page_hashes_cover_the_pdf_page_underneath():
    hashes = get_page_hashes(ANNOTATED)
    assert len(hashes) == 4 and None not in hashes
    assert len(set(hashes)) == 4
