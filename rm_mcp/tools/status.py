"""remarkable_status tool — check connection and authentication."""

from rm_mcp.server import mcp
from rm_mcp.tools import _helpers


@mcp.tool(annotations=_helpers.STATUS_ANNOTATIONS)
def remarkable_status(compact_output: bool = False) -> str:
    """
    <usecase>Check connection status and authentication with reMarkable Cloud.</usecase>
    <instructions>
    Returns authentication status and diagnostic information.
    Use this to verify your connection or troubleshoot issues.
    Includes index statistics when available.
    </instructions>
    <examples>
    - remarkable_status()
    - remarkable_status(compact_output=True)  # Omit hints
    </examples>
    """
    compact = _helpers.is_compact(compact_output)
    transport = "cloud"
    connection_info = "environment variable" if _helpers.REMARKABLE_TOKEN else "file (~/.rmapi)"

    try:
        client, collection = _helpers.get_cached_collection()
        items_by_id = _helpers.get_items_by_id(collection)

        root = _helpers._get_root_path()

        # Count documents (not folders, filtered by root)
        doc_count = 0
        for item in collection:
            if item.is_folder:
                continue
            item_path = _helpers.get_item_path(item, items_by_id)
            if _helpers._is_within_root(item_path, root):
                doc_count += 1

        result = {
            "authenticated": True,
            "transport": transport,
            "connection": connection_info,
            "status": "connected",
            "document_count": doc_count,
        }

        # Add root path info if configured
        if root != "/":
            result["root_path"] = root

        # Add configuration details
        from rm_mcp.cache import _CACHE_TTL_SECONDS

        result["config"] = {
            "ocr_backend": _helpers.get_ocr_backend(),
            "root_path": root,
            "background_color": _helpers.get_background_color(),
            "cache_ttl_seconds": _CACHE_TTL_SECONDS,
            "compact_mode": _helpers.is_compact(),
        }

        # Add index stats if available
        try:
            from rm_mcp.index import get_instance

            index = get_instance()
            if index is not None:
                stats = index.get_stats()
                result.update(stats)
                # Add coverage percentage
                indexed = stats.get("index_documents", 0)
                if doc_count > 0:
                    pct = int(indexed / doc_count * 100)
                    result["index_coverage"] = f"{indexed}/{doc_count} documents indexed ({pct}%)"
        except Exception:
            pass

        hint_parts = [f"Connected successfully via {transport}. Found {doc_count} documents."]
        if root != "/":
            hint_parts.append(f"Filtered to root: {root}")
        if "index_coverage" in result:
            hint_parts.append(f"Index coverage: {result['index_coverage']}.")
        hint_parts.append(
            "Use remarkable_browse() to see your files, "
            "or remarkable_recent() for recent documents."
        )

        return _helpers.make_response(result, " ".join(hint_parts), compact=compact)

    except Exception as e:
        error_msg = str(e)

        result = {
            "authenticated": False,
            "transport": transport,
            "connection": connection_info,
            "error": error_msg,
        }

        hint = (
            "To authenticate: run 'uvx rm-mcp --setup' and follow the instructions."
        )

        return _helpers.make_response(result, hint, compact=compact)
