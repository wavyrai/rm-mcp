"""remarkable_browse tool — browse library and search by name."""

from typing import Optional

from rm_mcp.server import mcp
from rm_mcp.tools import _helpers


@mcp.tool(annotations=_helpers.BROWSE_ANNOTATIONS)
async def remarkable_browse(path: str = "/", query: Optional[str] = None) -> str:
    """
    <usecase>Browse your reMarkable library or search for documents.</usecase>
    <instructions>
    Two modes:
    1. Browse mode (default): List contents of a folder
       - Use path="/" for root folder
       - Use path="/FolderName" to navigate into folders
    2. Search mode: Find documents by name
       - Set query="search term" to search across all documents

    Results include document names, types, and modification dates.

    Note: If REMARKABLE_ROOT_PATH is configured, only documents within that
    folder are accessible. Paths are relative to the root path.
    </instructions>
    <parameters>
    - path: Folder path to browse (default: "/" for root)
    - query: Search term to find documents by name (optional, triggers search mode)
    </parameters>
    <examples>
    - remarkable_browse()  # List root folder
    - remarkable_browse("/Work")  # List Work folder
    - remarkable_browse(query="meeting")  # Search for "meeting"
    </examples>
    """
    try:
        client, collection = _helpers.get_cached_collection()
        items_by_id = _helpers.get_items_by_id(collection)
        items_by_parent = _helpers.get_items_by_parent(collection)

        root = _helpers._get_root_path()
        # Resolve user path to actual device path
        actual_path = _helpers._resolve_root_path(path)

        # Search mode
        if query:
            query_lower = query.lower()
            matches = []

            for item in collection:
                # Skip cloud-archived items
                if _helpers._is_cloud_archived(item):
                    continue
                item_path = _helpers.get_item_path(item, items_by_id)
                # Filter by root path
                if not _helpers._is_within_root(item_path, root):
                    continue
                if query_lower in item.VissibleName.lower():
                    matches.append(
                        {
                            "name": item.VissibleName,
                            "path": _helpers._apply_root_filter(item_path),
                            "type": "folder" if item.is_folder else "document",
                            "modified": (
                                item.ModifiedClient if hasattr(item, "ModifiedClient") else None
                            ),
                        }
                    )

            matches.sort(key=lambda x: x["name"])

            result = {"mode": "search", "query": query, "count": len(matches), "results": matches}

            if matches:
                first_doc = next((m for m in matches if m["type"] == "document"), None)
                if first_doc:
                    hint = (
                        f"Found {len(matches)} results. "
                        f"To read a document: remarkable_read('{first_doc['name']}')."
                    )
                else:
                    hint = (
                        f"Found {len(matches)} folders. "
                        f"To browse one: remarkable_browse('{matches[0]['path']}')."
                    )
            else:
                hint = (
                    f"No results for '{query}'. "
                    "Try remarkable_browse('/') to see all files, "
                    "or use a different search term."
                )

            return _helpers.make_response(result, hint)

        # Browse mode - use actual_path (with root applied)
        if actual_path == "/" or actual_path == "":
            target_parent = ""
        else:
            # Navigate to the folder (case-insensitive)
            path_parts = [p for p in actual_path.strip("/").split("/") if p]
            current_parent = ""

            for i, part in enumerate(path_parts):
                part_lower = part.lower()
                found = False
                found_document = None

                for item in items_by_parent.get(current_parent, []):
                    if item.VissibleName.lower() == part_lower:
                        if item.is_folder:
                            current_parent = item.ID
                            found = True
                            break
                        else:
                            # Found a document with this name
                            found_document = item

                if not found:
                    # Check if it's a document (only valid as the last path part)
                    if found_document and i == len(path_parts) - 1:
                        # Auto-redirect: return first page of the document
                        doc_path = _helpers.get_item_path(found_document, items_by_id)
                        # Check if within root before redirecting
                        if not _helpers._is_within_root(doc_path, root):
                            return _helpers.make_error(
                                error_type="access_denied",
                                message=(
                                    f"Document '{found_document.VissibleName}' "
                                    "is outside the configured root path."
                                ),
                                suggestion="Check REMARKABLE_ROOT_PATH configuration.",
                            )
                        # Call remarkable_read internally and add redirect note
                        from rm_mcp.tools import read as _read_mod

                        read_result = await _read_mod.remarkable_read(
                            _helpers._apply_root_filter(doc_path), page=1
                        )
                        import json

                        result_data = json.loads(read_result)
                        if "_error" not in result_data:
                            result_data["_redirected_from"] = f"browse:{path}"
                            result_data["_hint"] = (
                                f"Auto-redirected from browse to read. "
                                f"{result_data.get('_hint', '')}"
                            )
                        return json.dumps(result_data, indent=2)

                    # Folder not found - suggest alternatives
                    available_folders = [
                        item.VissibleName
                        for item in items_by_parent.get(current_parent, [])
                        if item.is_folder
                    ]
                    available_docs = [
                        item.VissibleName
                        for item in items_by_parent.get(current_parent, [])
                        if not item.is_folder
                    ]
                    suggestion = "Use remarkable_browse('/') to see root folder contents."
                    if available_docs:
                        # Check if user might be looking for a document
                        for doc_name in available_docs:
                            if doc_name.lower() == part_lower:
                                suggestion = (
                                    f"'{doc_name}' is a document. "
                                    f"Use remarkable_read('{doc_name}') to read it."
                                )
                                break
                    return _helpers.make_error(
                        error_type="folder_not_found",
                        message=f"Folder not found: '{part}'",
                        suggestion=suggestion,
                        did_you_mean=(available_folders[:5] if available_folders else None),
                    )

            target_parent = current_parent

        items = items_by_parent.get(target_parent, [])

        folders = []
        documents = []

        for item in sorted(items, key=lambda x: x.VissibleName.lower()):
            # Skip cloud-archived items
            if _helpers._is_cloud_archived(item):
                continue
            if item.is_folder:
                folders.append({"name": item.VissibleName, "id": item.ID})
            else:
                documents.append(
                    {
                        "name": item.VissibleName,
                        "id": item.ID,
                        "modified": (
                            item.ModifiedClient if hasattr(item, "ModifiedClient") else None
                        ),
                    }
                )

        result = {"mode": "browse", "path": path, "folders": folders, "documents": documents}

        # Build helpful hint
        hint_parts = [f"Found {len(folders)} folder(s) and {len(documents)} document(s)."]

        if documents:
            hint_parts.append(f"To read a document: remarkable_read('{documents[0]['name']}').")
        if folders:
            folder_path = f"{path.rstrip('/')}/{folders[0]['name']}"
            hint_parts.append(f"To enter a folder: remarkable_browse('{folder_path}').")
        if not folders and not documents:
            hint_parts.append("This folder is empty.")

        return _helpers.make_response(result, " ".join(hint_parts))

    except Exception as e:
        return _helpers.make_error(
            error_type="browse_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )
