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

        # Add index stats if available
        try:
            from rm_mcp.index import get_instance

            index = get_instance()
            if index is not None:
                result.update(index.get_stats())
        except Exception:
            pass

        hint_parts = [f"Connected successfully via {transport}. Found {doc_count} documents."]
        if root != "/":
            hint_parts.append(f"Filtered to root: {root}")
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
            "To authenticate: "
            "1) Go to https://my.remarkable.com/device/browser/connect "
            "2) Get a one-time code "
            "3) Run: uvx rm-mcp --register YOUR_CODE "
            "4) Add REMARKABLE_TOKEN to your MCP config."
        )

        return _helpers.make_response(result, hint, compact=compact)
