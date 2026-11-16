"""reMarkable Methods templates (documents of type ``TemplateType``).

A template has no pages: its archive holds a ``<id>.template`` JSON layout
(boxes, lines and text labels with expressions for their geometry) and an SVG
preview in ``iconData``. To read it like any other document it is shown as one
page: its labels as text, its preview as the image.
"""

import base64
import json
import logging
import zipfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def template_member(zf: zipfile.ZipFile) -> Optional[str]:
    """Name of the archive's .template file, or None if it is not a template."""
    for name in zf.namelist():
        if name.endswith(".template") and "/" not in name:
            return name
    return None


def _load(data: bytes) -> Dict[str, Any]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _texts(node: Any, found: List[str]) -> None:
    if isinstance(node, dict):
        text = node.get("text")
        if node.get("type") == "text" and isinstance(text, str) and text.strip():
            found.append(text.strip())
        for value in node.values():
            _texts(value, found)
    elif isinstance(node, list):
        for value in node:
            _texts(value, found)


def template_text(data: bytes) -> str:
    """The template's name, categories, labels and the text printed on it."""
    t = _load(data)
    lines: List[str] = []
    if isinstance(t.get("name"), str):
        lines.append(t["name"])
    for key, label in (("categories", "Categories"), ("labels", "Labels")):
        values = [v for v in t.get(key) or [] if isinstance(v, str)]
        if values:
            lines.append(f"{label}: {', '.join(values)}")
    printed: List[str] = []
    _texts(t.get("items"), printed)
    # Labels repeat (one per row or column); keep the first of each.
    unique = list(dict.fromkeys(printed))
    if unique:
        lines.append("Text on the page: " + " · ".join(unique))
    return "\n".join(lines)


def template_icon_svg(data: bytes) -> Optional[bytes]:
    """The template's SVG preview, or None if it has none."""
    icon = _load(data).get("iconData")
    if not isinstance(icon, str) or not icon:
        return None
    try:
        return base64.b64decode(icon, validate=True)
    except (ValueError, TypeError):
        return None


def render_template_icon(data: bytes, fmt: str = "png", scale: float = 1.0):
    """Render the preview to PNG bytes (or return the SVG text); None if impossible.

    The preview is a 150x200 icon; it is drawn at device height (1872 px at
    scale 1) so it reads like a page.
    """
    svg = template_icon_svg(data)
    if svg is None:
        return None
    if fmt == "svg":
        return svg.decode("utf-8", errors="replace")
    try:
        import fitz

        doc = fitz.open(stream=svg, filetype="svg")
        try:
            page = doc[0]
            zoom = 1872 * scale / max(page.rect.height, 1)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pixmap.tobytes("png")
        finally:
            doc.close()
    except Exception:
        logger.warning("Could not render a template preview", exc_info=True)
        return None
