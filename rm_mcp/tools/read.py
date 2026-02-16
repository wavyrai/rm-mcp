"""remarkable_read tool — read and extract text from documents."""

import re
from typing import Literal, Optional

from mcp.server.fastmcp import Context

from rm_mcp.server import mcp
from rm_mcp.tools import _helpers


@mcp.tool(annotations=_helpers.READ_ANNOTATIONS)
async def remarkable_read(
    document: str,
    content_type: Literal["text", "annotations"] = "text",
    page: int = 1,
    pages: Optional[str] = None,
    grep: Optional[str] = None,
    include_ocr: bool = False,
    auto_ocr: bool = True,
    ctx: Optional[Context] = None,
    compact_output: bool = False,
) -> str:
    """
    <usecase>Read and extract text content from a reMarkable document.</usecase>
    <instructions>
    Extracts content from a document with pagination to preserve context window.

    Content types:
    - "text" (default): Full extracted text (annotations, highlights, OCR)
    - "annotations": Only annotations, highlights, and handwritten notes

    Reading modes:
    - Single page: page=N (default page=1), paginated
    - Multi-page: pages="all", pages="1-3", pages="2,4,5" — returns multiple pages concatenated
    - grep auto-redirects to the first matching page when no match on current page

    When REMARKABLE_OCR_BACKEND=sampling is set and the client supports sampling,
    OCR will use the client's LLM for handwriting recognition (no API keys needed).
    </instructions>
    <parameters>
    - document: Document name or path (use remarkable_browse to find documents)
    - content_type: "text" (full), "annotations" (notes only)
    - page: Page number for single-page mode (default: 1)
    - pages: Multi-page spec: "all", "1-3", "2,4,5" (overrides page param for notebooks)
    - grep: Regex pattern to filter content (auto-redirects to matching page)
    - include_ocr: Enable handwriting OCR for annotations (default: False)
    - auto_ocr: Auto-retry with OCR on empty notebooks (default: True, set False to skip)
    </parameters>
    <examples>
    - remarkable_read("Meeting Notes")  # Get first page of text
    - remarkable_read("Notes", pages="all")  # Get all pages in one call
    - remarkable_read("Notes", pages="1-3")  # Get pages 1 through 3
    - remarkable_read("Report", page=2)  # Get second page
    - remarkable_read("Manual", grep="installation")  # Auto-finds matching page
    </examples>
    """
    compact = _helpers.is_compact(compact_output)
    try:
        client, collection = _helpers.get_cached_collection()
        items_by_id = _helpers.get_items_by_id(collection)

        # Validate parameters
        page = max(1, page)
        # Internal page size for PDF/EPUB character-based pagination
        page_size = _helpers.DEFAULT_PAGE_SIZE

        root = _helpers._get_root_path()
        # Resolve user-provided path to actual device path
        actual_document = _helpers._resolve_root_path(document) if document.startswith("/") else document

        # Find the document
        target_doc, doc_path = _helpers._find_document(actual_document, collection, items_by_id, root)
        if target_doc is None:
            return doc_path  # doc_path contains the error JSON

        file_type = _helpers._get_file_type_cached(client, target_doc)

        # Collect content based on content_type
        text_parts = []

        # Get annotations/typed text
        notebook_pages = []  # List of page content for notebook pagination
        ocr_backend_used = None  # Track which OCR backend was used
        content = None  # Will hold extraction result
        total_notebook_pages = 0  # Track total pages for sampling mode

        if content_type in ("text", "annotations"):
            # For notebooks (no PDF/EPUB), use page-based pagination
            is_notebook = file_type not in ("pdf", "epub")

            # Determine if we should use sampling OCR
            use_sampling = is_notebook and include_ocr and ctx and _helpers.should_use_sampling_ocr(ctx)

            # For sampling OCR: use per-page caching and only OCR requested page
            if use_sampling:
                # Check per-page cache first
                cached_text = _helpers.get_cached_page_ocr(target_doc.ID, page, "sampling")
                if cached_text is not None:
                    # We have cached OCR for this page
                    # Still need to get total page count
                    raw_doc = client.download(target_doc)
                    with _helpers._temp_document(raw_doc) as tmp_path:
                        total_notebook_pages = _helpers.get_document_page_count(tmp_path)

                    # Build notebook_pages list with just the cached page
                    notebook_pages = [""] * total_notebook_pages
                    notebook_pages[page - 1] = cached_text
                    ocr_backend_used = "sampling"
                else:
                    # No cache - render and OCR just the requested page
                    raw_doc = client.download(target_doc)
                    with _helpers._temp_document(raw_doc) as tmp_path:
                        total_notebook_pages = _helpers.get_document_page_count(tmp_path)

                        if page > total_notebook_pages:
                            return _helpers.make_error(
                                error_type="page_out_of_range",
                                message=f"Page {page} does not exist. "
                                f"Document has {total_notebook_pages} notebook page(s).",
                                suggestion=f"Use page=1 to {total_notebook_pages} "
                                "to read different pages.",
                                compact=compact,
                            )

                        # Render just the requested page
                        png_data = _helpers.render_page_from_document_zip(tmp_path, page)
                        if png_data:
                            # OCR the single page
                            ocr_text = await _helpers.ocr_via_sampling(ctx, png_data)
                            if ocr_text:
                                # Cache the result
                                _helpers.cache_page_ocr(target_doc.ID, page, "sampling", ocr_text)
                                # Build notebook_pages list
                                notebook_pages = [""] * total_notebook_pages
                                notebook_pages[page - 1] = ocr_text
                                ocr_backend_used = "sampling"

            # If not using sampling OCR, perform standard extraction
            if not notebook_pages and is_notebook:
                raw_doc = client.download(target_doc)
                with _helpers._temp_document(raw_doc) as tmp_path:
                    content = _helpers.extract_text_from_document_zip(
                        tmp_path, include_ocr=include_ocr, doc_id=target_doc.ID
                    )
                    if content.get("handwritten_text"):
                        notebook_pages = content["handwritten_text"]
                        ocr_backend_used = content.get("ocr_backend")

            # For non-notebooks or when no OCR pages, build annotation sections
            if not (is_notebook and notebook_pages):
                if content is None:
                    # Need to extract if we haven't already
                    raw_doc = client.download(target_doc)
                    with _helpers._temp_document(raw_doc) as tmp_path:
                        content = _helpers.extract_text_from_document_zip(
                            tmp_path, include_ocr=include_ocr, doc_id=target_doc.ID
                        )

                # Add annotations section
                annotation_parts = []
                if content.get("typed_text"):
                    annotation_parts.extend(content["typed_text"])
                if content.get("highlights"):
                    annotation_parts.append("\n--- Highlights ---")
                    annotation_parts.extend(content["highlights"])
                if content.get("handwritten_text"):
                    annotation_parts.append("\n--- Handwritten (OCR) ---")
                    annotation_parts.extend(content["handwritten_text"])

                if annotation_parts:
                    if text_parts and content_type == "text":
                        text_parts.append("\n\n=== Annotations ===\n")
                    text_parts.extend(annotation_parts)

        # For notebooks with OCR: use page-based pagination
        if notebook_pages:
            total_pages = len(notebook_pages)

            # ---- Multi-page read ----
            if pages is not None:
                requested = _helpers.parse_pages(pages, total_pages)
                if not requested:
                    return _helpers.make_error(
                        error_type="invalid_pages",
                        message=f"No valid pages in '{pages}'. Document has {total_pages} page(s).",
                        suggestion=f"Use pages='all' or pages='1-{total_pages}'.",
                        compact=compact,
                    )

                parts = []
                returned_pages = []
                total_len = 0
                truncated = False
                for p in requested:
                    pg_content = notebook_pages[p - 1]
                    separator = f"--- Page {p} ---\n"
                    chunk = separator + pg_content
                    if total_len + len(chunk) > _helpers.MAX_OUTPUT_CHARS:
                        truncated = True
                        remaining = _helpers.MAX_OUTPUT_CHARS - total_len
                        if remaining > len(separator):
                            parts.append(separator + pg_content[: remaining - len(separator)])
                            returned_pages.append(p)
                        break
                    parts.append(chunk)
                    returned_pages.append(p)
                    total_len += len(chunk)

                combined = "\n\n".join(parts)

                # Apply grep across combined content
                grep_matches = 0
                if grep:
                    try:
                        pattern = re.compile(grep, re.IGNORECASE | re.MULTILINE)
                        matches = []
                        for match in pattern.finditer(combined):
                            start = max(0, match.start() - 100)
                            end = min(len(combined), match.end() + 100)
                            context = combined[start:end]
                            if start > 0:
                                context = "..." + context
                            if end < len(combined):
                                context = context + "..."
                            matches.append(context)
                            grep_matches += 1
                        if matches:
                            combined = "\n\n---\n\n".join(matches)
                    except re.error as e:
                        return _helpers.make_error(
                            error_type="invalid_grep",
                            message=f"Invalid regex pattern: {e}",
                            suggestion="Use a valid regex pattern or simple text string.",
                            compact=compact,
                        )

                result = {
                    "name": target_doc.VissibleName,
                    "path": _helpers._apply_root_filter(doc_path),
                    "file_type": "notebook",
                    "content_type": content_type,
                    "content": combined,
                    "pages": returned_pages,
                    "total_pages": total_pages,
                    "page_type": "notebook",
                    "total_chars": len(combined),
                    "truncated": truncated,
                    "modified": (
                        target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
                    ),
                }
                if include_ocr and ocr_backend_used:
                    result["ocr_backend"] = ocr_backend_used
                if grep:
                    result["grep"] = grep
                    result["grep_matches"] = grep_matches

                hint = f"Returned {len(returned_pages)}/{total_pages} pages."
                if truncated:
                    hint += " Output truncated at character limit."
                return _helpers.make_response(result, hint, compact=compact)

            # ---- Single page read ----
            if page > total_pages:
                return _helpers.make_error(
                    error_type="page_out_of_range",
                    message=f"Page {page} does not exist. "
                    f"Document has {total_pages} notebook page(s).",
                    suggestion=f"Use page=1 to {total_pages} to read different pages.",
                    compact=compact,
                )

            page_content = notebook_pages[page - 1]
            has_more = page < total_pages
            grep_redirected_from = None

            # Apply grep filter if specified
            grep_matches = 0
            if grep:
                try:
                    pattern = re.compile(grep, re.IGNORECASE | re.MULTILINE)
                    if not pattern.search(page_content):
                        # No match on this page — auto-redirect to first matching page
                        matching_pages = []
                        for i, pg in enumerate(notebook_pages, 1):
                            if pattern.search(pg):
                                matching_pages.append(i)
                        if matching_pages:
                            # Auto-redirect: switch to first matching page
                            grep_redirected_from = page
                            page = matching_pages[0]
                            page_content = notebook_pages[page - 1]
                            has_more = page < total_pages
                        else:
                            return _helpers.make_error(
                                error_type="no_grep_matches",
                                message=f"No matches for '{grep}' in document.",
                                suggestion="Try a different search term.",
                                compact=compact,
                            )
                    grep_matches = len(pattern.findall(page_content))
                except re.error as e:
                    return _helpers.make_error(
                        error_type="invalid_grep",
                        message=f"Invalid regex pattern: {e}",
                        suggestion="Use a valid regex pattern or simple text string.",
                        compact=compact,
                    )

            result = {
                "name": target_doc.VissibleName,
                "path": _helpers._apply_root_filter(doc_path),
                "file_type": "notebook",
                "content_type": content_type,
                "content": page_content,
                "page": page,
                "total_pages": total_pages,
                "page_type": "notebook",
                "total_chars": len(page_content),
                "more": has_more,
                "modified": (
                    target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
                ),
            }

            # Add OCR backend info if OCR was used
            if include_ocr and ocr_backend_used:
                result["ocr_backend"] = ocr_backend_used

            if grep:
                result["grep"] = grep
                result["grep_matches"] = grep_matches
            if grep_redirected_from is not None:
                result["grep_redirected_from"] = grep_redirected_from

            hint_parts = [f"Notebook page {page}/{total_pages}."]
            if grep_redirected_from is not None:
                hint_parts.insert(0, f"Auto-redirected from page {grep_redirected_from}.")
            if has_more:
                doc_name = target_doc.VissibleName
                hint_parts.append(f"Next: remarkable_read('{doc_name}', page={page + 1}).")
            else:
                hint_parts.append("(last page)")
            if grep_matches:
                hint_parts.insert(0, f"Found {grep_matches} match(es) for '{grep}'.")

            return _helpers.make_response(result, " ".join(hint_parts), compact=compact)

        # Combine all content
        full_text = "\n\n".join(text_parts) if text_parts else ""
        total_chars = len(full_text)

        # ---- Multi-page for PDFs/EPUBs ----
        if pages is not None and total_chars > 0:
            # pages="all" returns full text; page ranges don't apply to PDFs
            content = full_text
            if len(content) > _helpers.MAX_OUTPUT_CHARS:
                content = content[: _helpers.MAX_OUTPUT_CHARS]
                truncated = True
            else:
                truncated = False

            # Apply grep across combined content
            grep_matches = 0
            if grep:
                try:
                    pattern = re.compile(grep, re.IGNORECASE | re.MULTILINE)
                    matches = []
                    for match in pattern.finditer(content):
                        start = max(0, match.start() - 100)
                        end = min(len(content), match.end() + 100)
                        ctx_text = content[start:end]
                        if start > 0:
                            ctx_text = "..." + ctx_text
                        if end < len(content):
                            ctx_text = ctx_text + "..."
                        matches.append(ctx_text)
                        grep_matches += 1
                    if matches:
                        content = "\n\n---\n\n".join(matches)
                except re.error as e:
                    return _helpers.make_error(
                        error_type="invalid_grep",
                        message=f"Invalid regex pattern: {e}",
                        suggestion="Use a valid regex pattern or simple text string.",
                        compact=compact,
                    )

            result = {
                "name": target_doc.VissibleName,
                "path": _helpers._apply_root_filter(doc_path),
                "file_type": file_type or "notebook",
                "content_type": content_type,
                "content": content,
                "total_chars": len(content),
                "truncated": truncated,
                "more": False,
                "modified": (
                    target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
                ),
            }
            if grep:
                result["grep"] = grep
                result["grep_matches"] = grep_matches

            hint = f"Full document content ({len(content)} chars)."
            if truncated:
                hint += " Output truncated at character limit."
            return _helpers.make_response(result, hint, compact=compact)

        # Apply grep filter if specified
        grep_matches = 0
        if grep and full_text:
            try:
                pattern = re.compile(grep, re.IGNORECASE | re.MULTILINE)
                # Find all matches and include context
                matches = []
                for match in pattern.finditer(full_text):
                    start = max(0, match.start() - 100)
                    end = min(len(full_text), match.end() + 100)
                    context = full_text[start:end]
                    # Add ellipsis if truncated
                    if start > 0:
                        context = "..." + context
                    if end < len(full_text):
                        context = context + "..."
                    matches.append(context)
                    grep_matches += 1

                if matches:
                    full_text = "\n\n---\n\n".join(matches)
                    total_chars = len(full_text)
                else:
                    full_text = ""
                    total_chars = 0
            except re.error as e:
                return _helpers.make_error(
                    error_type="invalid_grep",
                    message=f"Invalid regex pattern: {e}",
                    suggestion="Use a valid regex pattern or simple text string.",
                    compact=compact,
                )

        # Apply pagination
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size

        # Handle empty content case - auto-retry with OCR if not already enabled
        if total_chars == 0 and not include_ocr and file_type not in ("pdf", "epub") and auto_ocr:
            # Auto-retry with OCR for notebooks
            import json

            ocr_result = await remarkable_read(
                document=document,
                content_type=content_type,
                page=page,
                grep=grep,
                include_ocr=True,  # Enable OCR automatically
                ctx=ctx,
            )
            result_data = json.loads(ocr_result)
            if "_error" not in result_data:
                result_data["_ocr_auto_enabled"] = True
                result_data["_hint"] = (
                    "OCR auto-enabled (notebook had no typed text). " + result_data.get("_hint", "")
                )
            return json.dumps(result_data, indent=2)

        if total_chars == 0:
            if page > 1:
                return _helpers.make_error(
                    error_type="page_out_of_range",
                    message=f"Page {page} does not exist. Document has 1 page(s).",
                    suggestion="Use page=1 to start from the beginning.",
                    compact=compact,
                )
            # Return empty result for page 1
            result = {
                "name": target_doc.VissibleName,
                "path": _helpers._apply_root_filter(doc_path),
                "file_type": file_type or "notebook",
                "content_type": content_type,
                "content": "",
                "page": 1,
                "total_pages": 1,
                "total_chars": 0,
                "more": False,
                "modified": (
                    target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
                ),
            }
            hint = (
                f"Document '{target_doc.VissibleName}' has no extractable text content. "
                "This may be a handwritten notebook - try include_ocr=True for OCR extraction."
            )
            return _helpers.make_response(result, hint, compact=compact)

        if start_idx >= total_chars:
            # Page out of range
            total_pages = max(1, (total_chars + page_size - 1) // page_size)
            return _helpers.make_error(
                error_type="page_out_of_range",
                message=f"Page {page} does not exist. Document has {total_pages} page(s).",
                suggestion="Use page=1 to start from the beginning.",
                compact=compact,
            )

        page_content = full_text[start_idx:end_idx]
        has_more = end_idx < total_chars
        total_pages = max(1, (total_chars + page_size - 1) // page_size)

        result = {
            "name": target_doc.VissibleName,
            "path": _helpers._apply_root_filter(doc_path),
            "file_type": file_type or "notebook",
            "content_type": content_type,
            "content": page_content,
            "page": page,
            "total_pages": total_pages,
            "total_chars": total_chars,
            "more": has_more,
            "modified": (
                target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
            ),
        }

        if has_more:
            result["next_page"] = page + 1

        if grep:
            result["grep"] = grep
            result["grep_matches"] = grep_matches

        # Build contextual hint
        hint_parts = []

        if grep:
            if grep_matches > 0:
                hint_parts.append(f"Found {grep_matches} match(es) for '{grep}'.")
            else:
                hint_parts.append(f"No matches for '{grep}' on this page.")
                if has_more:
                    hint_parts.append("Try searching other pages.")

        if has_more:
            hint_parts.append(
                f"Page {page}/{total_pages}. Next: remarkable_read('{document}', page={page + 1})"
            )
        else:
            hint_parts.append(f"Page {page}/{total_pages} (complete).")

        return _helpers.make_response(result, " ".join(hint_parts), compact=compact)

    except Exception as e:
        return _helpers.make_error(
            error_type="read_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
            compact=compact,
        )
