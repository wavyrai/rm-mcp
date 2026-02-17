"""remarkable_search tool — search across multiple documents."""

import json
import logging
from typing import Optional

from rm_mcp.server import mcp
from rm_mcp.tools import _helpers

logger = logging.getLogger(__name__)


@mcp.tool(annotations=_helpers.SEARCH_ANNOTATIONS)
async def remarkable_search(
    query: str,
    grep: Optional[str] = None,
    limit: int = 5,
    include_ocr: bool = False,
    compact_output: bool = False,
) -> str:
    """
    <usecase>Search across multiple documents and return matching content.</usecase>
    <instructions>
    Searches document names and previously-indexed content (FTS5) for the query.

    Without grep: returns metadata only (name, path, file_type, modified) — no cloud downloads.
    With grep: searches document content for the pattern, using cached index when available.

    Reading a document via remarkable_read indexes its content for future full-text search.
    The response includes index_coverage showing how many documents are searchable.

    Limits:
    - Max 5 documents per search
    - Use grep to search within document content
    </instructions>
    <parameters>
    - query: Search term for document names and previously-indexed content
    - grep: Optional regex pattern to search within document content
    - limit: Max documents to return (default: 5, max: 5)
    - include_ocr: Enable OCR for handwritten content (default: False)
    </parameters>
    <examples>
    - remarkable_search("meeting")  # Find docs with "meeting" in name or content
    - remarkable_search("journal", grep="project")  # Find "project" in journals
    - remarkable_search("notes", include_ocr=True)  # Search with OCR enabled
    </examples>
    """
    compact = _helpers.is_compact(compact_output)
    try:
        # Enforce limits
        limit = min(max(1, limit), 5)
        warnings = []

        # Use cached collection directly — no JSON round-trip through browse/read
        client, collection = _helpers.get_cached_collection()
        items_by_id = _helpers.get_items_by_id(collection)
        root = _helpers._get_root_path()

        # ---- Phase 1: FTS5 content search (previously-indexed content) ----
        fts_results = []
        fts_doc_ids = set()
        try:
            from rm_mcp.index import get_instance

            index = get_instance()
            if index is not None:
                fts_hits = index.search(query, limit=limit)
                for hit in fts_hits:
                    fts_doc_ids.add(hit["doc_id"])
                    fts_results.append(
                        {
                            "name": hit["name"],
                            "path": hit["path"],
                            "file_type": hit.get("file_type"),
                            "modified": hit.get("modified_at"),
                            "snippet": hit.get("snippet", ""),
                            "match_type": "content",
                        }
                    )
        except Exception as e:
            logger.debug("FTS search failed", exc_info=True)
            warnings.append(f"Full-text search unavailable: {e}")

        # ---- Phase 2: Name search (existing behavior) ----
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
                # Skip if already found via FTS
                if item.ID not in fts_doc_ids:
                    matching_docs.append((item, item_path))

        # ---- Phase 3: No results at all ----
        if not fts_results and not matching_docs:
            return _helpers.make_error(
                error_type="no_documents_found",
                message=f"No documents found matching '{query}'.",
                suggestion="Try a different search term or use remarkable_browse('/') to list all.",
                compact=compact,
            )

        # Limit name-search results to fill remaining slots
        remaining = limit - len(fts_results)
        matching_docs = matching_docs[: max(0, remaining)]

        # Get index reference for L2 lookups
        index = None
        try:
            from rm_mcp.index import get_instance

            index = get_instance()
        except Exception:
            pass

        search_results = list(fts_results)  # Start with FTS results

        import re as _re

        for doc, doc_full_path in matching_docs:
            display_path = _helpers._apply_root_filter(doc_full_path)
            file_type = _helpers._get_file_type_cached(client, doc)
            doc_result = {
                "name": doc.VissibleName,
                "path": display_path,
                "file_type": file_type,
                "modified": (doc.ModifiedClient if hasattr(doc, "ModifiedClient") else None),
                "match_type": "name",
            }

            if grep:
                # With grep: try L2 index first, fall back to cloud download
                l2_content = None
                if index is not None:
                    l2_content = index.get_content_snippet(
                        doc.ID, max_chars=_helpers.MAX_OUTPUT_CHARS
                    )

                if l2_content:
                    # Grep against cached content locally
                    try:
                        pattern = _re.compile(grep, _re.IGNORECASE | _re.MULTILINE)
                        matches = []
                        for match in pattern.finditer(l2_content):
                            start = max(0, match.start() - 100)
                            end = min(len(l2_content), match.end() + 100)
                            context = l2_content[start:end]
                            if start > 0:
                                context = "..." + context
                            if end < len(l2_content):
                                context = context + "..."
                            matches.append(context)
                        doc_result["grep_matches"] = len(matches)
                        if matches:
                            doc_result["content"] = "\n\n---\n\n".join(matches)[:2000]
                            if len(doc_result["content"]) == 2000:
                                doc_result["truncated"] = True
                    except _re.error as e:
                        doc_result["grep_matches"] = 0
                        doc_result["grep_error"] = f"Invalid regex '{grep}': {e}"
                else:
                    # L2 miss — fall back to cloud download via remarkable_read
                    try:
                        from rm_mcp.tools import read as _read_mod

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
                            doc_result["grep_matches"] = read_data.get("grep_matches", 0)
                            if len(read_data.get("content", "")) > 2000:
                                doc_result["truncated"] = True
                        else:
                            doc_result["error"] = read_data["_error"]["message"]
                    except Exception as e:
                        doc_result["error"] = str(e)
            # Without grep: metadata only — no cloud download

            search_results.append(doc_result)

        # ---- Index coverage ----
        index_coverage = None
        if index is not None:
            try:
                indexed_count = index.get_indexed_document_count()
                total_docs = sum(
                    1
                    for item in collection
                    if not item.is_folder
                    and not _helpers._is_cloud_archived(item)
                    and _helpers._is_within_root(_helpers.get_item_path(item, items_by_id), root)
                )
                index_coverage = {"indexed": indexed_count, "total": total_docs}
            except Exception:
                pass

        result = {
            "query": query,
            "grep": grep,
            "count": len(search_results),
            "documents": search_results,
        }
        if warnings:
            result["_warnings"] = warnings
        if index_coverage is not None:
            result["index_coverage"] = index_coverage

        # Build hint
        docs_with_content = [d for d in search_results if "content" in d or "snippet" in d]
        content_matches = [d for d in search_results if d.get("match_type") == "content"]
        name_matches = [d for d in search_results if d.get("match_type") == "name"]

        if grep:
            matches = sum(d.get("grep_matches", 0) for d in docs_with_content)
            hint = f"Found {len(docs_with_content)} document(s) with {matches} grep match(es)."
        elif content_matches and name_matches:
            hint = (
                f"Found {len(content_matches)} content match(es) and "
                f"{len(name_matches)} name match(es) for '{query}'."
            )
        elif content_matches:
            hint = f"Found {len(content_matches)} document(s) with matching content for '{query}'."
        else:
            hint = f"Found {len(name_matches)} document(s) matching '{query}' by name."

        first_doc = search_results[0] if search_results else None
        if first_doc and "path" in first_doc:
            hint += f" To read more: remarkable_read('{first_doc['path']}')."

        if (
            index_coverage
            and not docs_with_content
            and index_coverage["indexed"] < index_coverage["total"]
        ):
            indexed = index_coverage["indexed"]
            total = index_coverage["total"]
            hint += f" Content search covers {indexed}/{total} documents."

        return _helpers.make_response(result, hint, compact=compact)

    except Exception as e:
        return _helpers.make_error(
            error_type="search_failed",
            message=str(e),
            suggestion=_helpers.suggest_for_error(e),
            compact=compact,
        )
