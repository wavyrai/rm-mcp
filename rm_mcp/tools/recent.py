"""remarkable_recent tool — get recently modified documents."""

from rm_mcp.server import mcp
from rm_mcp.tools import _helpers


@mcp.tool(annotations=_helpers.RECENT_ANNOTATIONS)
def remarkable_recent(
    limit: int = 10, include_preview: bool = False, compact_output: bool = False
) -> str:
    """
    <usecase>Get your most recently modified documents.</usecase>
    <instructions>
    Returns documents sorted by modification date (newest first).
    Optionally includes a text preview of each document's content.
    Previews are served from the local index when available (no cloud download).

    Use this to quickly find what you were working on recently.

    Note: If REMARKABLE_ROOT_PATH is configured, only documents within that
    folder are included.
    </instructions>
    <parameters>
    - limit: Maximum documents to return (default: 10, max: 50 without preview, 10 with preview)
    - include_preview: Include first ~200 chars of text content (default: False)
    </parameters>
    <examples>
    - remarkable_recent()  # Last 10 documents
    - remarkable_recent(limit=5, include_preview=True)  # With content preview
    </examples>
    """
    compact = _helpers.is_compact(compact_output)
    try:
        client, collection = _helpers.get_cached_collection()
        items_by_id = _helpers.get_items_by_id(collection)

        # Clamp limit - lower max when previews enabled (expensive operation)
        max_limit = 10 if include_preview else 50
        limit = min(max(1, limit), max_limit)

        root = _helpers._get_root_path()

        # Get documents sorted by modified date (excluding archived, filtered by root)
        documents = []
        for item in collection:
            if item.is_folder or _helpers._is_cloud_archived(item):
                continue
            item_path = _helpers.get_item_path(item, items_by_id)
            if not _helpers._is_within_root(item_path, root):
                continue
            documents.append(item)

        documents.sort(
            key=lambda x: (
                x.ModifiedClient if hasattr(x, "ModifiedClient") and x.ModifiedClient else ""
            ),
            reverse=True,
        )

        results = []
        for doc in documents[:limit]:
            doc_path = _helpers.get_item_path(doc, items_by_id)
            file_type = _helpers._get_file_type_cached(client, doc)
            doc_info = {
                "name": doc.VissibleName,
                "path": _helpers._apply_root_filter(doc_path),
                "file_type": file_type,
                "modified": (doc.ModifiedClient if hasattr(doc, "ModifiedClient") else None),
            }

            if include_preview:
                # Try L2 index first (no cloud download needed)
                l2_preview = None
                try:
                    from rm_mcp.index import get_instance

                    index = get_instance()
                    if index is not None:
                        l2_preview = index.get_preview(doc.ID, max_chars=200)
                except Exception:
                    pass

                if l2_preview:
                    if len(l2_preview) == 200:
                        doc_info["preview"] = l2_preview + "..."
                    else:
                        doc_info["preview"] = l2_preview
                elif file_type == "notebook":
                    # Notebooks need OCR for preview, skip for performance
                    doc_info["preview_skipped"] = "notebook (use remarkable_read with include_ocr)"
                else:
                    # PDFs and EPUBs have extractable text - fall back to cloud download
                    try:
                        raw_doc = client.download(doc)
                        with _helpers._temp_document(raw_doc) as tmp_path:
                            content = _helpers.extract_text_from_document_zip(
                                tmp_path, include_ocr=False, doc_id=doc.ID
                            )
                            preview_text = "\n".join(content["typed_text"])[:200]
                            if preview_text:
                                if len(preview_text) == 200:
                                    doc_info["preview"] = preview_text + "..."
                                else:
                                    doc_info["preview"] = preview_text
                    except Exception:
                        pass

            results.append(doc_info)

        result = {"count": len(results), "documents": results}

        if results:
            next_limit = min(limit * 2, 50)
            hint = (
                f"Showing {len(results)} recent documents. "
                f"To read one: remarkable_read('{results[0]['name']}'). "
                f"To see more: remarkable_recent(limit={next_limit})."
            )
        else:
            hint = "No documents found. Use remarkable_browse('/') to check your library."

        return _helpers.make_response(result, hint, compact=compact)

    except Exception as e:
        return _helpers.make_error(
            error_type="recent_failed",
            message=str(e),
            suggestion=_helpers.suggest_for_error(e),
            compact=compact,
        )
