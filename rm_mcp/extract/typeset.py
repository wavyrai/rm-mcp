"""Typeset Markdown into a PDF sized for the tablet.

markdown-it-py turns the Markdown into HTML (raw HTML in the source is shown
as text, not interpreted), and pymupdf's Story lays it out over as many pages
as it needs, with live links. The page is the reMarkable 2 screen's shape
(157 x 210 mm), so text reads at a comfortable size without zooming.
"""

import io
from typing import Optional

import fitz  # pymupdf
from markdown_it import MarkdownIt

# 1404 x 1872 px at 226 dpi = 157.8 x 210.4 mm.
PAGE_WIDTH = 447.3
PAGE_HEIGHT = 596.4
MARGIN = 36

CSS = """
* { font-family: sans-serif; }
body { font-size: 11pt; line-height: 1.45; }
h1 { font-size: 19pt; margin: 0 0 8pt 0; }
h2 { font-size: 15pt; margin: 12pt 0 6pt 0; }
h3 { font-size: 12.5pt; margin: 10pt 0 4pt 0; }
h4, h5, h6 { font-size: 11pt; margin: 8pt 0 4pt 0; }
p { margin: 0 0 7pt 0; }
ul, ol { margin: 0 0 7pt 0; padding-left: 16pt; }
li { margin: 0 0 2pt 0; }
pre { font-family: monospace; font-size: 8.5pt; background-color: #EEEEEE;
      padding: 5pt; margin: 0 0 7pt 0; }
code { font-family: monospace; font-size: 9.5pt; }
blockquote { margin: 0 0 7pt 10pt; padding-left: 6pt; color: #444444; }
a { color: #1A4FA0; text-decoration: underline; }
table { border-collapse: collapse; margin: 0 0 7pt 0; }
th, td { border: 1px solid #999999; padding: 2pt 4pt; font-size: 9.5pt; }
hr { margin: 6pt 0; }
"""


def markdown_to_html(text: str) -> str:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    md.enable("strikethrough")
    return md.render(text)


def markdown_to_pdf(text: str, title: Optional[str] = None) -> bytes:
    """Typeset Markdown text into PDF bytes."""
    html = markdown_to_html(text)
    if not html.strip():
        raise ValueError("The Markdown text is empty.")
    story = fitz.Story(html=html, user_css=CSS)
    page = fitz.Rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT)
    content = page + (MARGIN, MARGIN, -MARGIN, -MARGIN)

    def rectfn(rect_num, filled):
        return page, content, None

    doc = story.write_with_links(rectfn)
    try:
        if title:
            doc.set_metadata({"title": title, "creator": "rm-mcp"})
        out = io.BytesIO()
        doc.save(out, garbage=3, deflate=True)
        return out.getvalue()
    finally:
        doc.close()
