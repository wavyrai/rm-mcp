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
    annotated_pdf.rmdoc document archive: three_pages.pdf, a .content whose
                        page 2 was inserted on the tablet, and ink on pages
                        1, 2 and 4 (page 3 is the PDF's page 2, unannotated)
"""

import io
import json
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


def _stroke(
    points,
    item_id: CrdtId,
    left_id: CrdtId,
    tool=si.Pen.FINELINER_2,
    color=si.PenColor.BLACK,
    width: int = 24,
    color_rgba=None,
) -> SceneLineItemBlock:
    line = si.Line(
        color=color,
        tool=tool,
        points=[
            si.Point(x=float(x), y=float(y), speed=0, direction=0, width=width, pressure=200)
            for x, y in points
        ],
        thickness_scale=2.0,
        starting_length=0.0,
        color_rgba=color_rgba,
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


# PDF points to .rm units: the page sits at 226 dpi with x = 0 at its centre.
_UNITS = 226 / 72
_PDF_WIDTH = 595


def _pdf_xy(x_pt: float, y_pt: float):
    return (x_pt * _UNITS - _PDF_WIDTH * _UNITS / 2, y_pt * _UNITS)


def _ellipse(cx, cy, rx, ry, steps: int = 36):
    import math

    return [
        (cx + rx * math.cos(2 * math.pi * i / steps), cy + ry * math.sin(2 * math.pi * i / steps))
        for i in range(steps + 1)
    ]


def _ink_page(strokes) -> bytes:
    """A v6 page holding the given (points, style) strokes."""
    blocks = _without_root_text(_base_blocks())
    left = CrdtId(0, 0)
    for n, (pts, style) in enumerate(strokes):
        item_id = CrdtId(1, 300 + n)
        blocks.append(_stroke(pts, item_id, left, **style))
        left = item_id
    buf = io.BytesIO()
    write_blocks(buf, blocks, options={"version": "3.1"})
    return buf.getvalue()


ANNOTATED_DOC = "a0a0a0a0-0000-4000-8000-000000000001"
# Tablet pages: (page id, source PDF page or None when inserted on the tablet).
ANNOTATED_PAGES = [("pg-one", 0), ("pg-inserted", None), ("pg-two", 1), ("pg-three", 2)]


def make_annotated_pdf() -> None:
    # Page 1: the text line spans x 72-367 and y 87-104 in PDF points.
    left, right = _pdf_xy(72, 0)[0], _pdf_xy(367, 0)[0]
    y_text = _pdf_xy(0, 96)[1]
    page_one = _ink_page(
        [
            (
                _segment((left, y_text), (right, y_text), 20),
                dict(
                    tool=si.Pen.HIGHLIGHTER_2,
                    color=si.PenColor.HIGHLIGHT,
                    width=120,
                    color_rgba=(255, 237, 117, 255),
                ),
            ),
            (
                _segment((left, y_text + 30), (right, y_text + 30), 20),
                dict(tool=si.Pen.BALLPOINT_2, color=si.PenColor.RED, width=16),
            ),
        ]
    )
    # Inserted page: a large cross on an otherwise blank notebook page.
    inserted = _ink_page(
        [
            (_segment((-300, 600), (300, 1200), 12), {}),
            (_segment((300, 600), (-300, 1200), 12), {}),
        ]
    )
    # PDF page 3: a blue ring around "zebras" at the end of the line.
    cx, cy = _pdf_xy(279, 96)
    page_three = _ink_page(
        [(_ellipse(cx, cy, 110, 50), dict(tool=si.Pen.FINELINER_2, color=si.PenColor.BLUE))]
    )
    ink = {"pg-one": page_one, "pg-inserted": inserted, "pg-three": page_three}

    pages = []
    for n, (pid, redir) in enumerate(ANNOTATED_PAGES):
        entry = {"id": pid, "idx": {"timestamp": "1:2", "value": f"b{n}"}}
        if redir is not None:
            entry["redir"] = {"timestamp": "1:2", "value": redir}
        pages.append(entry)
    content = {
        "cPages": {"original": {"timestamp": "1:2", "value": 3}, "pages": pages},
        "fileType": "pdf",
        "formatVersion": 2,
        "orientation": "portrait",
        "originalPageCount": 3,
        "pageCount": len(pages),
    }
    with zipfile.ZipFile(HERE / "annotated_pdf.rmdoc", "w") as zf:
        _zip_add(zf, f"{ANNOTATED_DOC}.content", json.dumps(content, indent=2))
        zf.writestr(
            zipfile.ZipInfo(f"{ANNOTATED_DOC}.pdf", date_time=_FIXED_DATE),
            (HERE / "three_pages.pdf").read_bytes(),
        )
        for pid, data in ink.items():
            info = zipfile.ZipInfo(f"{ANNOTATED_DOC}/{pid}.rm", date_time=_FIXED_DATE)
            zf.writestr(info, data)


def main() -> None:
    make_typed_text()
    make_handwriting()
    make_highlight()
    make_pdf()
    make_epub()
    make_annotated_pdf()
    for path in sorted(HERE.iterdir()):
        if path.suffix in {".rm", ".pdf", ".epub", ".rmdoc"}:
            print(f"{path.name:20} {path.stat().st_size:6} bytes")


if __name__ == "__main__":
    main()
