"""
Content extraction for reMarkable documents: page plans, per-page text and rendering.
"""

# rmscene's "newer format" notices go to DEBUG (see rmscene_logs).
from rm_mcp.extract.rmscene_logs import quiet_rmscene

quiet_rmscene()

from rm_mcp.extract.document import DocumentPages, Page, read_document_pages  # noqa: E402, F401
from rm_mcp.extract.notebook import (  # noqa: E402, F401
    extract_text_from_rm_file,
    get_document_page_count,
    get_page_hashes,
)
from rm_mcp.extract.render import (  # noqa: E402, F401
    REMARKABLE_HEIGHT,
    REMARKABLE_WIDTH,
    get_background_color,
    render_page_from_document_zip,
    render_page_from_document_zip_svg,
    render_rm_file_to_png,
    render_rm_file_to_svg,
)
