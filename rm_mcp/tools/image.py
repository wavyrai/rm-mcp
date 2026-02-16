"""remarkable_image tool — render document pages as images."""

import base64
from typing import Optional

from mcp.server.fastmcp import Context
from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    TextResourceContents,
)

from rm_mcp.server import mcp
from rm_mcp.tools import _helpers


@mcp.tool(annotations=_helpers.IMAGE_ANNOTATIONS)
async def remarkable_image(
    document: str,
    page: int = 1,
    background: Optional[str] = None,
    output_format: str = "png",
    compatibility: bool = False,
    include_ocr: bool = False,
    ctx: Optional[Context] = None,
    compact_output: bool = False,
):
    """
    <usecase>Get an image of a specific page from a reMarkable document.</usecase>
    <instructions>
    Renders a notebook or document page as an image (PNG or SVG). This is useful for:
    - Viewing hand-drawn diagrams, sketches, or UI mockups
    - Getting visual context that text extraction might miss
    - Implementing designs based on hand-drawn wireframes
    - SVG format for scalable vector graphics that can be edited

    ## Response Formats

    By default, images are returned inline:
    - PNG: Returned as ImageContent with base64-encoded data
    - SVG: Returned as EmbeddedResource with TextResourceContents containing SVG markup

    If your client doesn't support embedded resources in tool responses, set
    compatibility=True to receive a JSON response with just the resource URI.
    The client can then fetch the resource separately.

    Optionally, enable include_ocr=True to extract text from the image using OCR.
    When REMARKABLE_OCR_BACKEND=sampling is set and the client supports sampling,
    the client's own LLM will be used for OCR (no API keys needed).

    Note: This works best with notebooks and handwritten content. For PDFs/EPUBs,
    the annotations layer is rendered (not the underlying PDF content).
    </instructions>
    <parameters>
    - document: Document name or path (use remarkable_browse to find documents)
    - page: Page number (default: 1, 1-indexed)
    - background: Background color as hex code. Supports RGB (#RRGGBB) or RGBA (#RRGGBBAA).
      Default is "#FBFBFB" (reMarkable paper color), or set REMARKABLE_BACKGROUND_COLOR
      env var to override. Use "#00000000" for transparent.
    - output_format: Output format - "png" (default) or "svg" for vector graphics
    - compatibility: If True, return resource URI in JSON instead of embedded resource.
      Use this if your client doesn't support embedded resources in tool responses.
    - include_ocr: Enable OCR text extraction from the image (default: False).
      When REMARKABLE_OCR_BACKEND=sampling, uses the client's LLM via MCP sampling.
    </parameters>
    <examples>
    - remarkable_image("UI Mockup")  # Get first page as embedded PNG resource
    - remarkable_image("Meeting Notes", page=2)  # Get second page
    - remarkable_image("/Work/Designs/Wireframe", background="#FFFFFF")  # White background
    - remarkable_image("Sketch", background="#00000000")  # Transparent background
    - remarkable_image("Diagram", output_format="svg")  # Get as embedded SVG resource
    - remarkable_image("Notes", compatibility=True)  # Return resource URI for retry
    - remarkable_image("Notes", include_ocr=True)  # Get image with OCR text extraction
    </examples>
    """
    compact = _helpers.is_compact(compact_output)
    try:
        # Resolve background color: use provided value or get from env/default
        if background is None:
            background = _helpers.get_background_color()

        client, collection = _helpers.get_cached_collection()
        items_by_id = _helpers.get_items_by_id(collection)

        root = _helpers._get_root_path()
        # Resolve user-provided path to actual device path
        if document.startswith("/"):
            actual_document = _helpers._resolve_root_path(document)
        else:
            actual_document = document

        # Find the document
        target_doc, doc_path = _helpers._find_document(
            actual_document, collection, items_by_id, root
        )
        if target_doc is None:
            return doc_path  # doc_path contains the error JSON

        # Download the document
        raw_doc = client.download(target_doc)
        with _helpers._temp_document(raw_doc) as tmp_path:
            # Validate format parameter
            format_lower = output_format.lower()
            if format_lower not in ("png", "svg"):
                return _helpers.make_error(
                    error_type="invalid_format",
                    message=f"Invalid format: '{output_format}'. Supported formats: png, svg",
                    suggestion="Use output_format='png' for raster or 'svg' for vectors.",
                    compact=compact,
                )

            # Get total page count
            total_pages = _helpers.get_document_page_count(tmp_path)

            if total_pages == 0:
                return _helpers.make_error(
                    error_type="no_pages",
                    message=f"Document '{target_doc.VissibleName}' has no renderable pages.",
                    suggestion=(
                        "This may be a PDF/EPUB without annotations. "
                        "Use remarkable_read() to extract text content instead."
                    ),
                    compact=compact,
                )

            if page < 1 or page > total_pages:
                return _helpers.make_error(
                    error_type="page_out_of_range",
                    message=f"Page {page} does not exist. Document has {total_pages} page(s).",
                    suggestion=f"Use page=1 to {total_pages} to view different pages.",
                    compact=compact,
                )

            # Build resource URI for this page
            doc_path = _helpers._apply_root_filter(_helpers.get_item_path(target_doc, items_by_id))
            uri_path = doc_path.lstrip("/")

            # Render the page based on format
            if format_lower == "svg":
                svg_content = _helpers.render_page_from_document_zip_svg(
                    tmp_path, page, background_color=background
                )

                if svg_content is None:
                    return _helpers.make_error(
                        error_type="render_failed",
                        message="Failed to render page to SVG.",
                        suggestion="Make sure 'rmc' is installed. Try: uv add rmc",
                        compact=compact,
                    )

                resource_uri = f"remarkablesvg:///{uri_path}.page-{page}.svg"

                if compatibility:
                    # Return SVG content in JSON for clients without embedded resource support
                    hint = (
                        f"Page {page}/{total_pages} as SVG. "
                        f"Use compatibility=False for embedded resource format."
                    )
                    return _helpers.make_response(
                        {
                            "svg": svg_content,
                            "mime_type": "image/svg+xml",
                            "page": page,
                            "total_pages": total_pages,
                            "resource_uri": resource_uri,
                        },
                        hint,
                        compact=compact,
                    )
                else:
                    # Return SVG as embedded TextResourceContents with info hint
                    text_resource = TextResourceContents(
                        uri=resource_uri,
                        mimeType="image/svg+xml",
                        text=svg_content,
                    )
                    embedded = EmbeddedResource(type="resource", resource=text_resource)
                    info = TextContent(
                        type="text",
                        text=f"Page {page}/{total_pages} of '{target_doc.VissibleName}' as SVG. "
                        f"Resource URI: {resource_uri}",
                    )
                    return [info, embedded]
            else:
                # PNG format — check cache first
                cache_key = f"{target_doc.ID}:{page}"
                if cache_key in _helpers._rendered_image_cache and not include_ocr:
                    png_base64 = _helpers._rendered_image_cache[cache_key]
                    resource_uri = f"remarkableimg:///{uri_path}.page-{page}.png"
                    if compatibility:
                        data_uri = f"data:image/png;base64,{png_base64}"
                        hint = (
                            f"Page {page}/{total_pages} as base64-encoded PNG (cached). "
                            f"Use 'data_uri' directly in HTML img src. "
                            f"Use compatibility=False for embedded resource format."
                        )
                        return _helpers.make_response(
                            {
                                "data_uri": data_uri,
                                "mime_type": "image/png",
                                "page": page,
                                "total_pages": total_pages,
                                "resource_uri": resource_uri,
                            },
                            hint,
                            compact=compact,
                        )
                    else:
                        image = ImageContent(
                            type="image", data=png_base64, mimeType="image/png"
                        )
                        info = TextContent(
                            type="text",
                            text=f"Page {page}/{total_pages} of '{target_doc.VissibleName}' "
                            f"as PNG (cached). Resource URI: {resource_uri}",
                        )
                        return [info, image]

                png_data = _helpers.render_page_from_document_zip(
                    tmp_path, page, background_color=background
                )

                if png_data is None:
                    return _helpers.make_error(
                        error_type="render_failed",
                        message="Failed to render page to image.",
                        suggestion=(
                            "Make sure 'rmc' and 'cairosvg' are installed. Try: uv add rmc cairosvg"
                        ),
                        compact=compact,
                    )

                # Handle OCR if requested - extract text from the image
                ocr_text = None
                ocr_backend_used = None
                ocr_message = None
                if include_ocr:
                    # Try sampling-based OCR if configured and available
                    # This sends the image to the client's LLM to extract text
                    if ctx and _helpers.should_use_sampling_ocr(ctx):
                        ocr_text = await _helpers.ocr_via_sampling(ctx, png_data)
                        if ocr_text:
                            ocr_backend_used = "sampling"
                        else:
                            ocr_message = "No text detected in image"
                    else:
                        ocr_message = "OCR unavailable (client does not support sampling)"

                resource_uri = f"remarkableimg:///{uri_path}.page-{page}.png"
                png_base64 = base64.b64encode(png_data).decode("utf-8")

                # Cache the rendered image (evict if cache is too large)
                if len(_helpers._rendered_image_cache) >= 20:
                    _helpers._rendered_image_cache.clear()
                _helpers._rendered_image_cache[cache_key] = png_base64

                # Build OCR info for response if OCR was requested
                ocr_info = {}
                if include_ocr:
                    ocr_info["ocr_text"] = ocr_text
                    ocr_info["ocr_backend"] = ocr_backend_used
                    if ocr_message:
                        ocr_info["ocr_message"] = ocr_message

                if compatibility:
                    # Return base64 PNG in JSON for clients without embedded resource support
                    # Include data URI format for direct use in HTML <img> tags
                    data_uri = f"data:image/png;base64,{png_base64}"
                    hint = (
                        f"Page {page}/{total_pages} as base64-encoded PNG. "
                        f"Use 'data_uri' directly in HTML img src. "
                        f"Use compatibility=False for embedded resource format."
                    )
                    if include_ocr and ocr_text:
                        hint = (
                            f"Page {page}/{total_pages} with OCR text "
                            f"(backend: {ocr_backend_used})."
                        )
                    elif include_ocr:
                        hint = f"Page {page}/{total_pages}. No text detected via OCR."

                    response_data = {
                        "data_uri": data_uri,
                        "mime_type": "image/png",
                        "page": page,
                        "total_pages": total_pages,
                        "resource_uri": resource_uri,
                        **ocr_info,
                    }
                    return _helpers.make_response(response_data, hint, compact=compact)
                else:
                    # Return PNG as ImageContent for direct visibility in clients
                    image = ImageContent(
                        type="image", data=png_base64, mimeType="image/png"
                    )

                    info_text = f"Page {page}/{total_pages} of '{target_doc.VissibleName}' as PNG. "
                    info_text += f"Resource URI: {resource_uri}"
                    if include_ocr and ocr_text:
                        info_text += f"\n\nOCR Text (via {ocr_backend_used}):\n{ocr_text}"
                    elif include_ocr:
                        info_text += "\n\nOCR: No text detected in image."

                    info = TextContent(type="text", text=info_text)
                    return [info, image]

    except Exception as e:
        return _helpers.make_error(
            error_type="image_failed",
            message=str(e),
            suggestion=_helpers.suggest_for_error(e),
            compact=compact,
        )
