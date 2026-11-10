"""Render reMarkable pages to PNG or SVG with pymupdf.

Pure Python: rmscene reads the page's scene tree and pymupdf draws it, so
rendering needs no system libraries on any platform.

Coordinates. A v6 page stores ink in device pixels (226 dpi) with x = 0 at the
horizontal centre of the page and y = 0 at its top. A notebook page is the
device frame, 1404 x 1872 (or the scene's ``paper_size``). A page over a PDF
or EPUB is the source page at its physical size in the same units (one PDF
point is 226/72 units), centred on x = 0, so the ink lands on the document
where it was written. Every page is drawn on a pymupdf page measured in those
units and then scaled into the device frame for output.
"""

import io
import logging
import math
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# reMarkable 1/2 page frame, in device pixels at 226 dpi.
REMARKABLE_WIDTH = 1404
REMARKABLE_HEIGHT = 1872
UNITS_PER_POINT = 226 / 72

_DEFAULT_BACKGROUND_COLOR = "#FBFBFB"


def get_background_color() -> str:
    """Get the background color, checking env var for override."""
    return os.environ.get("REMARKABLE_BACKGROUND_COLOR", _DEFAULT_BACKGROUND_COLOR)


# Evaluated at import; use get_background_color() to read the env var at runtime.
REMARKABLE_BACKGROUND_COLOR = get_background_color()


def _parse_hex_color(hex_color: str) -> tuple:
    """Parse #RRGGBB or #RRGGBBAA into an (r, g, b, a) tuple (0-255); white otherwise."""
    if not hex_color or not hex_color.startswith("#"):
        return (255, 255, 255, 255)
    hex_str = hex_color[1:]
    try:
        if len(hex_str) == 6:
            r, g, b = (int(hex_str[i : i + 2], 16) for i in (0, 2, 4))
            return (r, g, b, 255)
        if len(hex_str) == 8:
            r, g, b, a = (int(hex_str[i : i + 2], 16) for i in (0, 2, 4, 6))
            return (r, g, b, a)
    except ValueError:
        pass
    return (255, 255, 255, 255)


# --- Pens ---------------------------------------------------------------------

# Ink colours by rmscene PenColor value, as the tablet's own export draws them.
_COLORS = {
    0: (0, 0, 0),  # black
    1: (144, 144, 144),  # gray
    2: (255, 255, 255),  # white
    3: (250, 231, 25),  # yellow
    4: (145, 218, 113),  # green
    5: (192, 127, 210),  # pink
    6: (48, 74, 224),  # blue
    7: (194, 49, 50),  # red
    8: (125, 125, 125),  # gray overlap
    9: (250, 231, 25),  # highlight (real colour is in color_rgba)
    10: (145, 218, 113),  # green 2
    11: (116, 210, 232),  # cyan
    12: (192, 127, 210),  # magenta
    13: (250, 231, 25),  # yellow 2
}


@dataclass(frozen=True)
class _PenStyle:
    width_factor: float
    opacity: float = 1.0
    lighten: float = 0.0  # mix toward white, for graphite tools
    varying: bool = True  # width follows each point (pressure) or one width per stroke


# Keyed by rmscene Pen value. point.width is four times the drawn width.
_PENS = {
    0: _PenStyle(1.0),  # paintbrush 1
    12: _PenStyle(1.0),  # paintbrush 2
    1: _PenStyle(0.8, lighten=0.3),  # pencil 1
    14: _PenStyle(0.8, lighten=0.3),  # pencil 2
    7: _PenStyle(0.7, lighten=0.2, varying=False),  # mechanical pencil 1
    13: _PenStyle(0.7, lighten=0.2, varying=False),  # mechanical pencil 2
    2: _PenStyle(1.0),  # ballpoint 1
    15: _PenStyle(1.0),  # ballpoint 2
    3: _PenStyle(1.0, varying=False),  # marker 1
    16: _PenStyle(1.0, varying=False),  # marker 2
    4: _PenStyle(1.0, varying=False),  # fineliner 1
    17: _PenStyle(1.0, varying=False),  # fineliner 2
    21: _PenStyle(1.0),  # calligraphy
    5: _PenStyle(1.0, opacity=0.35, varying=False),  # highlighter 1
    18: _PenStyle(1.0, opacity=0.35, varying=False),  # highlighter 2
    23: _PenStyle(1.0, opacity=0.2, varying=False),  # shader
}
_ERASERS = {6, 8}
_HIGHLIGHT_OPACITY = 0.35


def _rgb(color: Tuple[int, int, int], lighten: float = 0.0) -> Tuple[float, float, float]:
    return tuple((c + (255 - c) * lighten) / 255 for c in color)


def _line_color(line) -> Tuple[int, int, int]:
    if line.color_rgba is not None:
        return tuple(line.color_rgba[:3])
    return _COLORS.get(int(line.color), (0, 0, 0))


# --- Scene --------------------------------------------------------------------


def read_scene(data: bytes):
    """Parse a v6 .rm blob into an rmscene SceneTree (raises on unreadable data)."""
    from rmscene import read_tree

    return read_tree(io.BytesIO(data))


def _visible_items(group) -> Iterator:
    from rmscene import scene_items as si

    for child in group.children.values():
        if isinstance(child, si.Group):
            if child.visible is not None and child.visible.value is False:
                continue
            yield from _visible_items(child)
        elif child is not None:
            yield child


def _ink_bottom(tree) -> float:
    from rmscene import scene_items as si

    bottom = 0.0
    for item in _visible_items(tree.root):
        if isinstance(item, si.Line) and item.points:
            bottom = max(bottom, max(p.y for p in item.points))
    return bottom


def _draw_line(shape, line, ox: float, oy: float) -> None:
    tool = int(line.tool)
    if tool in _ERASERS or not line.points:
        return
    style = _PENS.get(tool, _PenStyle(1.0))
    color = _rgb(_line_color(line), style.lighten)
    pts = [(ox + p.x, oy + p.y) for p in line.points]
    widths = [max(p.width / 4 * style.width_factor, 0.8) for p in line.points]

    def finish(width: float) -> None:
        shape.finish(
            color=color,
            width=width,
            lineCap=1,
            lineJoin=1,
            closePath=False,
            stroke_opacity=style.opacity,
        )

    if len(pts) == 1:
        shape.draw_line(pts[0], pts[0])
        finish(widths[0])
        return
    if not style.varying:
        shape.draw_polyline(pts)
        finish(sorted(widths)[len(widths) // 2])
        return
    # Pressure-sensitive pens: one polyline per run of (nearly) equal width.
    start = 0
    for i in range(1, len(pts)):
        if i == len(pts) - 1 or round(widths[i] * 2) != round(widths[start] * 2):
            shape.draw_polyline(pts[start : i + 1])
            finish(widths[start])
            start = i


def _draw_glyph(page, glyph, ox: float, oy: float) -> None:
    import fitz

    color = glyph.color_rgba[:3] if glyph.color_rgba is not None else _COLORS[3]
    for r in glyph.rectangles:
        rect = fitz.Rect(ox + r.x, oy + r.y, ox + r.x + r.w, oy + r.y + r.h)
        page.draw_rect(rect, color=None, fill=_rgb(color), fill_opacity=_HIGHLIGHT_OPACITY, width=0)


# Typed text, in device pixels: (font size, line height, bold, prefix, indent).
_TEXT_STYLES = {
    0: (32, 71, False, "", 0),  # basic
    1: (32, 71, False, "", 0),  # plain
    2: (50, 100, True, "", 0),  # heading
    3: (32, 71, True, "", 0),  # bold
    4: (32, 71, False, "• ", 30),  # bullet
    5: (32, 71, False, "• ", 60),  # bullet 2
    6: (32, 71, False, "[ ] ", 0),  # checkbox
    7: (32, 71, False, "[x] ", 0),  # checkbox checked
}


def _wrap(text: str, width: float, fontname: str, size: float) -> List[str]:
    import fitz

    lines, current = [], ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and fitz.get_text_length(candidate, fontname, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return lines


def _draw_text(page, text_item, ox: float, oy: float) -> None:
    from rmscene.text import TextDocument

    doc = TextDocument.from_scene_item(text_item)
    x0 = ox + text_item.pos_x
    y = oy + text_item.pos_y
    width = text_item.width if text_item.width > 0 else REMARKABLE_WIDTH * 2 / 3
    for paragraph in doc.contents:
        style = int(paragraph.style.value) if paragraph.style is not None else 1
        size, height, bold, prefix, indent = _TEXT_STYLES.get(style, _TEXT_STYLES[1])
        text = str(paragraph)
        if not text.strip():
            y += height
            continue
        fontname = "hebo" if bold else "helv"
        for line in _wrap(prefix + text, width - indent, fontname, size):
            # Baseline sits a little below the middle of the line box.
            baseline = y + (height + size) / 2 - size * 0.15
            page.insert_text((x0 + indent, baseline), line, fontname=fontname, fontsize=size)
            y += height


def draw_scene(page, tree, origin: Tuple[float, float]) -> None:
    """Draw a scene's typed text, highlights and strokes onto a pymupdf page.

    ``origin`` is the page position of scene coordinate (0, 0).
    """
    from rmscene import scene_items as si

    ox, oy = origin
    if tree.root_text is not None:
        try:
            _draw_text(page, tree.root_text, ox, oy)
        except Exception:
            logger.warning("Could not lay out the page's typed text", exc_info=True)

    items = list(_visible_items(tree.root))
    for item in items:
        if isinstance(item, si.GlyphRange):
            _draw_glyph(page, item, ox, oy)
    shape = page.new_shape()
    for item in items:
        if isinstance(item, si.Line):
            _draw_line(shape, item, ox, oy)
    shape.commit()


# --- Pages --------------------------------------------------------------------


def open_source_document(data: bytes, kind: str):
    """Open a document's PDF, or its EPUB laid out to the device page, as a PDF."""
    import fitz

    if kind == "pdf":
        return fitz.open(stream=data, filetype="pdf")
    epub = fitz.open(stream=data, filetype="epub")
    try:
        # Lay the EPUB out on the device page, as the tablet paginates it.
        epub.layout(
            width=REMARKABLE_WIDTH / UNITS_PER_POINT,
            height=REMARKABLE_HEIGHT / UNITS_PER_POINT,
            fontsize=11,
        )
        return fitz.open("pdf", epub.convert_to_pdf())
    finally:
        epub.close()


def _paint_background(page, background_color: Optional[str]) -> None:
    if not background_color:
        return
    r, g, b, a = _parse_hex_color(background_color)
    if a == 0:
        return
    page.draw_rect(
        page.rect, color=None, fill=(r / 255, g / 255, b / 255), fill_opacity=a / 255, width=0
    )


def _compose_page(
    tree, source=None, source_page: Optional[int] = None, background_color: Optional[str] = None
):
    """Build a one-page pymupdf document holding the page; return (doc, zoom)."""
    import fitz

    doc = fitz.open()
    if source is not None:
        rect = source[source_page].rect
        width, height = rect.width * UNITS_PER_POINT, rect.height * UNITS_PER_POINT
        zoom = min(REMARKABLE_WIDTH / width, REMARKABLE_HEIGHT / height)
    else:
        width, height = REMARKABLE_WIDTH, REMARKABLE_HEIGHT
        info = getattr(tree, "scene_info", None) if tree is not None else None
        if info is not None and info.paper_size and all(info.paper_size):
            width, height = map(float, info.paper_size)
        if tree is not None:
            # A notebook page grows downward when ink goes past the frame.
            height = max(height, math.ceil(_ink_bottom(tree) + 50))
        zoom = 1.0
    page = doc.new_page(width=width, height=height)
    _paint_background(page, background_color)
    if source is not None:
        page.show_pdf_page(page.rect, source, source_page)
    if tree is not None:
        draw_scene(page, tree, (width / 2, 0.0))
    return doc, zoom


def _to_png(doc, zoom: float, background_color: Optional[str]) -> bytes:
    import fitz

    opaque = bool(background_color) and _parse_hex_color(background_color)[3] == 255
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=not opaque)
    return pix.tobytes("png")


def _to_svg(doc, zoom: float) -> str:
    import fitz

    return doc[0].get_svg_image(matrix=fitz.Matrix(zoom, zoom), text_as_path=True)


def _render(tree, source, source_page, background_color, fmt: str):
    doc, zoom = _compose_page(tree, source, source_page, background_color)
    try:
        return _to_png(doc, zoom, background_color) if fmt == "png" else _to_svg(doc, zoom)
    finally:
        doc.close()


def _render_rm_file(rm_file_path: Path, background_color: Optional[str], fmt: str):
    try:
        tree = read_scene(Path(rm_file_path).read_bytes())
        return _render(tree, None, None, background_color, fmt)
    except Exception:
        logger.exception("Failed to render %s to %s", rm_file_path, fmt.upper())
        return None


def render_rm_file_to_png(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[bytes]:
    """Render a .rm page as a full notebook page to PNG (None background = transparent)."""
    return _render_rm_file(rm_file_path, background_color, "png")


def render_rm_file_to_svg(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[str]:
    """Render a .rm page as a full notebook page to SVG (None background = transparent)."""
    return _render_rm_file(rm_file_path, background_color, "svg")


def _render_document_page(zip_path: Path, page: int, background_color: Optional[str], fmt: str):
    from rm_mcp.extract.pages import read_page_plan

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            plan = read_page_plan(zf)
            if page < 1 or page > len(plan.pages):
                return None
            ref = plan.pages[page - 1]
            tree = read_scene(zf.read(ref.rm_member)) if ref.rm_member else None
            source_bytes = (
                zf.read(plan.source_member)
                if plan.source_member and ref.source_page is not None
                else None
            )
        source = open_source_document(source_bytes, plan.source_kind) if source_bytes else None
        try:
            source_page = ref.source_page
            if source is not None and source_page >= source.page_count:
                logger.warning(
                    "Page %d points at source page %d of %d; rendering it without the source",
                    page,
                    source_page + 1,
                    source.page_count,
                )
                source.close()
                source, source_page = None, None
            return _render(tree, source, source_page, background_color, fmt)
        finally:
            if source is not None:
                source.close()
    except Exception:
        logger.exception("Failed to render page %d of %s to %s", page, zip_path, fmt.upper())
        return None


def render_page_from_document_zip(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[bytes]:
    """Render one page (1-indexed) of a document archive to PNG.

    Notebook pages fill the device frame; PDF and EPUB pages show the source
    page with the ink on top. Returns None if the page does not exist or the
    render fails.
    """
    return _render_document_page(zip_path, page, background_color, "png")


def render_page_from_document_zip_svg(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[str]:
    """Render one page (1-indexed) of a document archive to SVG; see the PNG variant."""
    return _render_document_page(zip_path, page, background_color, "svg")
