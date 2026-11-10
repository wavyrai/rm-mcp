"""Generate the small real-format fixtures in this folder.

Run from the repo root:

    uv run python tests/fixtures/make_fixtures.py

Every output is deterministic (fixed UUIDs, no timestamps), so regenerating
them leaves git clean unless a library changed its output format.

Outputs:
    typed_text.rm       v6 page whose text lives in the root text block
    handwriting.rm      v6 page with fineliner strokes drawing a house
    highlight.rm        v6 page with a highlighter glyph range over PDF text
    three_pages.pdf     3-page PDF with real (extractable) text
    small.epub          2-chapter EPUB
"""

import io
import uuid
import zipfile
from pathlib import Path

import fitz  # pymupdf
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import (
    RootTextBlock,
    SceneGlyphItemBlock,
    SceneLineItemBlock,
    simple_text_document,
    write_blocks,
)
from rmscene.tagged_block_common import CrdtId

HERE = Path(__file__).resolve().parent
AUTHOR = uuid.UUID("5f0c6f0e-8b8a-4d1e-9b52-2b6a3c7d9e01")
LAYER = CrdtId(0, 11)

TYPED_TEXT = "Meeting notes\nShip the fixture set\nFix typed text extraction"
PDF_PAGES = [
    "Page one: the quick brown fox jumps over the lazy dog.",
    "Page two: reMarkable fixtures carry real extractable text.",
    "Page three: the last page mentions zebras.",
]
EPUB_CHAPTERS = [
    ("Chapter One", "The first chapter talks about lighthouses."),
    ("Chapter Two", "The second chapter talks about tidal pools."),
]


def _base_blocks(text: str = ""):
    """Header, layer and (optionally empty) root text from rmscene's template."""
    return list(simple_text_document(text, author_uuid=AUTHOR))


def _without_root_text(blocks):
    return [b for b in blocks if not isinstance(b, RootTextBlock)]


def _write_rm(path: Path, blocks) -> None:
    buf = io.BytesIO()
    write_blocks(buf, blocks, options={"version": "3.1"})
    path.write_bytes(buf.getvalue())


def _stroke(points, item_id: CrdtId, left_id: CrdtId) -> SceneLineItemBlock:
    line = si.Line(
        color=si.PenColor.BLACK,
        tool=si.Pen.FINELINER_2,
        points=[
            si.Point(x=float(x), y=float(y), speed=0, direction=0, width=24, pressure=200)
            for x, y in points
        ],
        thickness_scale=2.0,
        starting_length=0.0,
    )
    return SceneLineItemBlock(
        parent_id=LAYER,
        item=CrdtSequenceItem(
            item_id=item_id,
            left_id=left_id,
            right_id=CrdtId(0, 0),
            deleted_length=0,
            value=line,
        ),
    )


def _segment(a, b, steps: int = 8):
    (x0, y0), (x1, y1) = a, b
    return [(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps) for i in range(steps + 1)]


def make_typed_text() -> None:
    _write_rm(HERE / "typed_text.rm", _base_blocks(TYPED_TEXT))


def make_handwriting() -> None:
    # A house: square walls, a roof, a door. One stroke per pen line.
    walls = _segment((-200, 600), (200, 600)) + _segment((200, 600), (200, 1000))[1:]
    walls += _segment((200, 1000), (-200, 1000))[1:] + _segment((-200, 1000), (-200, 600))[1:]
    roof = _segment((-240, 620), (0, 380)) + _segment((0, 380), (240, 620))[1:]
    door = _segment((-50, 1000), (-50, 820)) + _segment((-50, 820), (50, 820))[1:]
    door += _segment((50, 820), (50, 1000))[1:]

    blocks = _without_root_text(_base_blocks())
    left = CrdtId(0, 0)
    for n, pts in enumerate([walls, roof, door]):
        item_id = CrdtId(1, 100 + n)
        blocks.append(_stroke(pts, item_id, left))
        left = item_id
    _write_rm(HERE / "handwriting.rm", blocks)


def make_highlight() -> None:
    text = "quick brown fox"
    glyph = si.GlyphRange(
        start=None,
        length=len(text),
        text=text,
        color=si.PenColor.HIGHLIGHT,
        rectangles=[si.Rectangle(x=-300.0, y=200.0, w=220.0, h=36.0)],
    )
    blocks = _without_root_text(_base_blocks())
    blocks.append(
        SceneGlyphItemBlock(
            parent_id=LAYER,
            item=CrdtSequenceItem(
                item_id=CrdtId(1, 200),
                left_id=CrdtId(0, 0),
                right_id=CrdtId(0, 0),
                deleted_length=0,
                value=glyph,
            ),
        )
    )
    _write_rm(HERE / "highlight.rm", blocks)


def make_pdf() -> None:
    doc = fitz.open()
    for text in PDF_PAGES:
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), text, fontsize=12)
    doc.set_metadata({"title": "Fixture PDF", "producer": "", "creator": ""})
    doc.save(HERE / "three_pages.pdf", garbage=4, deflate=True, no_new_id=True)
    doc.close()


_FIXED_DATE = (2026, 1, 1, 0, 0, 0)


def _zip_add(zf: zipfile.ZipFile, name: str, data: str, compress: bool = True) -> None:
    info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zf.writestr(info, data)


def make_epub() -> None:
    """A minimal EPUB 3, written by hand so the bytes are reproducible."""
    container = (
        '<?xml version="1.0"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    items, spine, nav_links = [], [], []
    chapters = {}
    for n, (title, body) in enumerate(EPUB_CHAPTERS, 1):
        name = f"chap{n}.xhtml"
        items.append(f'<item id="c{n}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{n}"/>')
        nav_links.append(f'<li><a href="{name}">{title}</a></li>')
        chapters[name] = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>{title}</title></head><body><h1>{title}</h1><p>{body}</p></body></html>"
        )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
        + "".join(nav_links)
        + "</ol></nav></body></html>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="id">urn:uuid:7d1f2c3a-0b4e-4c5d-8e9f-a0b1c2d3e4f5</dc:identifier>'
        "<dc:title>Fixture EPUB</dc:title><dc:language>en</dc:language>"
        '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>'
        '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>' + "".join(items) + "</manifest>"
        "<spine>" + "".join(spine) + "</spine></package>"
    )
    with zipfile.ZipFile(HERE / "small.epub", "w") as zf:
        _zip_add(zf, "mimetype", "application/epub+zip", compress=False)
        _zip_add(zf, "META-INF/container.xml", container)
        _zip_add(zf, "OEBPS/content.opf", opf)
        _zip_add(zf, "OEBPS/nav.xhtml", nav)
        for name, html in chapters.items():
            _zip_add(zf, f"OEBPS/{name}", html)


def main() -> None:
    make_typed_text()
    make_handwriting()
    make_highlight()
    make_pdf()
    make_epub()
    for path in sorted(HERE.iterdir()):
        if path.suffix in {".rm", ".pdf", ".epub"}:
            print(f"{path.name:20} {path.stat().st_size:6} bytes")


if __name__ == "__main__":
    main()
