"""remarkable_search tool — search across multiple documents."""

import json
from typing import Optional

from rm_mcp.server import mcp
from rm_mcp.tools import _helpers


@mcp.tool(annotations=_helpers.SEARCH_ANNOTATIONS)
async def remarkable_search(
    query: str,
    grep: Optional[str] = None,
    limit: int = 5,
    include_ocr: bool = False,
) -> str:
    """
    <usecase>Search across multiple documents and return matching content.</usecase>
    <instructions>
    Searches document names for the query, then optionally searches content with grep.
    Returns summaries from multiple documents in a single call.

    This is efficient for finding information across your library without
    making many individual tool calls.

    Limits:
    - Max 5 documents per search (to keep response size manageable)
    - Returns first page (~8000 chars) of each matching document
    - Use grep to filter to relevant sections
    </instructions>
    <parameters>
    - query: Search term for document names
    - grep: Optional pattern to search within document content
    - limit: Max documents to return (default: 5, max: 5)
    - include_ocr: Enable OCR for handwritten content (default: False)
    </parameters>
    <examples>
    - remarkable_search("meeting")  # Find docs with "meeting" in name
    - remarkable_search("journal", grep="project")  # Find "project" in journals
    - remarkable_search("notes", include_ocr=True)  # Search with OCR enabled
    </examples>
    """
    try:
        # Enforce limits
        limit = min(max(1, limit), 5)

        # Use cached collection directly — no JSON round-trip through browse/read
        client, collection = _helpers.get_cached_collection()
        items_by_id = _helpers.get_items_by_id(collection)
        root = _helpers._get_root_path()

        # Find documents matching query by name
        query_lower = query.lower()
        matching_docs = []
        for item in collection:
            if item.is_folder:
                continue
            if _helpers._is_cloud_archived(item):
                continue
            item_path = _helpers.get_item_path(item, items_by_id)
            if not _helpers._is_within_root(item_path, root):
                continue
            if query_lower in item.VissibleName.lower():
                matching_docs.append((item, item_path))

        if not matching_docs:
            return _helpers.make_error(
                error_type="no_documents_found",
                message=f"No documents found matching '{query}'.",
                suggestion="Try a different search term or use remarkable_browse('/') to list all.",
            )

        # Limit results
        matching_docs = matching_docs[:limit]

        # Read each document
        from rm_mcp.tools import read as _read_mod

        search_results = []
        for doc, doc_full_path in matching_docs:
            display_path = _helpers._apply_root_filter(doc_full_path)
            doc_result = {
                "name": doc.VissibleName,
                "path": display_path,
                "modified": (doc.ModifiedClient if hasattr(doc, "ModifiedClient") else None),
            }

            try:
                read_result = await _read_mod.remarkable_read(
                    document=display_path,
                    page=1,
                    grep=grep,
                    include_ocr=include_ocr,
                )
                read_data = json.loads(read_result)

                if "_error" not in read_data:
                    doc_result["content"] = read_data.get("content", "")[:2000]
                    doc_result["total_pages"] = read_data.get("total_pages", 1)
                    if grep:
                        doc_result["grep_matches"] = read_data.get("grep_matches", 0)
                    if len(read_data.get("content", "")) > 2000:
                        doc_result["truncated"] = True
                else:
                    doc_result["error"] = read_data["_error"]["message"]
            except Exception as e:
                doc_result["error"] = str(e)

            search_results.append(doc_result)

        result = {
            "query": query,
            "grep": grep,
            "count": len(search_results),
            "documents": search_results,
        }

        # Build hint
        docs_with_content = [d for d in search_results if "content" in d]
        if grep:
            matches = sum(d.get("grep_matches", 0) for d in docs_with_content)
            hint = f"Found {len(docs_with_content)} document(s) with {matches} grep match(es)."
        else:
            hint = f"Found {len(docs_with_content)} document(s) matching '{query}'."

        if docs_with_content:
            hint += f" To read more: remarkable_read('{docs_with_content[0]['path']}')."

        return _helpers.make_response(result, hint)

    except Exception as e:
        return _helpers.make_error(
            error_type="search_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )
