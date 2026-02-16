"""
MCP Resources for reMarkable tablet access.

Provides:
- remarkable:///{path}.txt - extracted text from any document
- remarkableimg:///{path}.page-{page}.png - page image for notebooks (PNG)
- remarkablesvg:///{path}.page-{page}.svg - page image for notebooks (SVG vector)

Resources are loaded in background batches via the cloud API.
Respects REMARKABLE_ROOT_PATH environment variable for folder filtering.
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional, Set

from mcp.types import Completion, ResourceTemplateReference

from rm_mcp.paths import _apply_root_filter, _get_root_path, _is_within_root
from rm_mcp.server import mcp

logger = logging.getLogger(__name__)


# Background loader state
_registered_docs: Set[str] = set()  # Track document IDs for text resources
_registered_img: Set[str] = set()  # Track document IDs for image resources
_registered_uris: Set[str] = set()  # Track URIs for collision detection
_img_uri_to_doc: dict[str, tuple] = {}  # Map image URI template -> (client, doc) for page count


def _make_doc_resource(client, document):
    """Create a resource function for a document.

    Returns only user-supplied content: typed text, annotations, highlights.
    Does NOT include original PDF/EPUB text.

    Note: OCR is not available for resources (sampling OCR requires async Context).
    Use the remarkable_read tool with include_ocr=True for OCR.
    """
    from rm_mcp.extract import extract_text_from_document_zip

    def doc_resource() -> str:
        try:
            text_parts = []

            # Download notebook data for annotations/typed text/handwritten
            raw = client.download(document)
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)
            try:
                # First try without OCR (faster) - use doc_id to leverage cache
                content = extract_text_from_document_zip(
                    tmp_path, include_ocr=False, doc_id=document.ID
                )

                if content["typed_text"]:
                    text_parts.extend(content["typed_text"])
                if content["highlights"]:
                    if text_parts:
                        text_parts.append("\n--- Highlights ---")
                    text_parts.extend(content["highlights"])

                return "\n\n".join(text_parts) if text_parts else "(No user content)"
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as e:
            return f"Error: {e}"

    return doc_resource


def _make_image_resource(client, document):
    """Create a resource function for page images from a notebook.

    Returns a function that takes a page number and returns PNG bytes.
    Uses the standard reMarkable background color for resources (configurable via env).
    """
    from rm_mcp.extract import get_background_color, render_page_from_document_zip

    def image_resource(page: str) -> bytes:
        try:
            page_num = int(page)
            if page_num < 1:
                raise ValueError("Page number must be >= 1")
        except ValueError as e:
            raise ValueError(f"Invalid page number: {page}") from e

        raw_doc = client.download(document)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_doc)
            tmp_path = Path(tmp.name)

        try:
            # Use reMarkable standard background color for resources
            png_data = render_page_from_document_zip(
                tmp_path, page_num, background_color=get_background_color()
            )
            if png_data is None:
                raise RuntimeError(
                    f"Failed to render page {page_num}. "
                    "Make sure 'rmc' and 'cairosvg' are installed."
                )
            return png_data
        finally:
            tmp_path.unlink(missing_ok=True)

    return image_resource


def _make_svg_resource(client, document):
    """Create a resource function for SVG page images from a notebook.

    Returns a function that takes a page number and returns SVG content.
    Uses the standard reMarkable background color for resources (configurable via env).
    """
    from rm_mcp.extract import (
        get_background_color,
        render_page_from_document_zip_svg,
    )

    def svg_resource(page: str) -> str:
        try:
            page_num = int(page)
            if page_num < 1:
                raise ValueError("Page number must be >= 1")
        except ValueError as e:
            raise ValueError(f"Invalid page number: {page}") from e

        raw_doc = client.download(document)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_doc)
            tmp_path = Path(tmp.name)

        try:
            # Use reMarkable standard background color for resources
            svg_content = render_page_from_document_zip_svg(
                tmp_path, page_num, background_color=get_background_color()
            )
            if svg_content is None:
                raise RuntimeError(
                    f"Failed to render page {page_num} to SVG. Make sure 'rmc' is installed."
                )
            return svg_content
        finally:
            tmp_path.unlink(missing_ok=True)

    return svg_resource


def _register_document(
    client, doc, items_by_id=None, root: str = "/"
) -> bool:
    """Register a single document as resources.

    Registers:
    - Text resource for all documents
    - Image template resource for notebooks (not PDF/EPUB)

    Args:
        client: The reMarkable API client
        doc: Document metadata object
        items_by_id: Dict mapping IDs to items for path resolution
        root: Root path filter (documents outside root are skipped)
    """
    global _registered_docs, _registered_img, _registered_uris

    doc_id = doc.ID

    # Skip if already registered (by ID)
    if doc_id in _registered_docs:
        return False

    # Skip cloud-archived documents (not available on device)
    if hasattr(doc, "is_cloud_archived") and doc.is_cloud_archived:
        return False

    # Get the full path
    doc_name = doc.VissibleName
    if items_by_id:
        from rm_mcp.paths import get_item_path

        full_path = get_item_path(doc, items_by_id)
    else:
        full_path = f"/{doc_name}"

    # Filter by root path
    if not _is_within_root(full_path, root):
        return False

    # Apply root filter for display paths (e.g., /Work/Project -> /Project)
    display_path = _apply_root_filter(full_path, root)

    # Use the filtered path for URIs
    uri_path = display_path.lstrip("/")

    # Register text resource (use /// for empty netloc)
    base_uri = f"remarkable:///{uri_path}.txt"
    counter = 1
    final_uri = base_uri
    display_name = f"{display_path}.txt"
    while final_uri in _registered_uris:
        final_uri = f"remarkable:///{uri_path}_{counter}.txt"
        display_name = f"{display_path} ({counter}).txt"
        counter += 1

    desc = f"Content from '{display_path}'"
    if doc.ModifiedClient:
        desc += f" (modified: {doc.ModifiedClient})"

    mcp.resource(final_uri, name=display_name, description=desc, mime_type="text/plain")(
        _make_doc_resource(client, doc)
    )

    _registered_docs.add(doc_id)
    _registered_uris.add(final_uri)

    # Get file type for this document
    name_lower = doc.VissibleName.lower()
    if name_lower.endswith(".pdf"):
        file_type = "pdf"
    elif name_lower.endswith(".epub"):
        file_type = "epub"
    else:
        file_type = "notebook"

    # Register image template resources for notebooks only (not PDF/EPUB)
    if file_type == "notebook":
        # PNG resource template with {page} parameter
        img_uri = f"remarkableimg:///{uri_path}.page-{{page}}.png"
        img_counter = 1
        final_img_uri = img_uri
        img_display = f"{display_path} (page image)"
        while final_img_uri in _registered_uris:
            final_img_uri = f"remarkableimg:///{uri_path}_{img_counter}.page-{{page}}.png"
            img_display = f"{display_path} ({img_counter}) (page image)"
            img_counter += 1

        img_desc = f"PNG image of page from notebook '{display_path}'"
        if doc.ModifiedClient:
            img_desc += f" (modified: {doc.ModifiedClient})"

        mcp.resource(
            final_img_uri,
            name=img_display,
            description=img_desc,
            mime_type="image/png",
        )(_make_image_resource(client, doc))

        _registered_img.add(doc_id)
        _registered_uris.add(final_img_uri)

        # Store mapping for completion handler to look up page counts
        _img_uri_to_doc[final_img_uri] = (client, doc)

        # SVG resource template with {page} parameter
        svg_uri = f"remarkablesvg:///{uri_path}.page-{{page}}.svg"
        svg_counter = 1
        final_svg_uri = svg_uri
        svg_display = f"{display_path} (SVG)"
        while final_svg_uri in _registered_uris:
            final_svg_uri = f"remarkablesvg:///{uri_path}_{svg_counter}.page-{{page}}.svg"
            svg_display = f"{display_path} ({svg_counter}) (SVG)"
            svg_counter += 1

        svg_desc = f"SVG vector image of page from notebook '{display_path}'"
        if doc.ModifiedClient:
            svg_desc += f" (modified: {doc.ModifiedClient})"

        mcp.resource(
            final_svg_uri,
            name=svg_display,
            description=svg_desc,
            mime_type="image/svg+xml",
        )(_make_svg_resource(client, doc))

        _registered_uris.add(final_svg_uri)

        # Store mapping for SVG completions too
        _img_uri_to_doc[final_svg_uri] = (client, doc)

    return True


async def _load_documents_background(shutdown_event: asyncio.Event):
    """
    Background task to load and register documents in batches.

    Respects REMARKABLE_ROOT_PATH environment variable.
    """
    try:
        from rm_mcp.api import get_rmapi
        from rm_mcp.cache import set_cached_collection
        from rm_mcp.paths import get_items_by_id

        client = get_rmapi()
        loop = asyncio.get_event_loop()

        batch_size = 10
        offset = 0
        consecutive_errors = 0
        max_consecutive_errors = 3
        items_by_id = {}  # Build incrementally

        root = _get_root_path()
        if root != "/":
            logger.info(f"Root path filter: {root}")

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("Background document loader cancelled by shutdown")
                break

            # Fetch next batch - run sync code in executor to not block
            try:
                items = await loop.run_in_executor(
                    None, lambda: client.get_meta_items(limit=offset + batch_size)
                )
                # Update items_by_id with all items for path resolution
                items_by_id = get_items_by_id(items)
                # Populate the shared cache so tools can use it
                set_cached_collection(client, items)
                consecutive_errors = 0  # Reset on success
            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"Error fetching documents (attempt {consecutive_errors}): {e}")
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        f"Background loader stopping after {max_consecutive_errors} "
                        "consecutive errors"
                    )
                    break
                # Wait before retry
                await asyncio.sleep(2**consecutive_errors)
                continue

            # Get documents from this batch, skipping folders and already-registered docs.
            # We track by ID rather than using a raw offset, because the folder/document
            # ratio can vary across calls, causing offset-based slicing to drift and skip docs.
            documents = [item for item in items if not item.is_folder]
            batch_docs = [doc for doc in documents if doc.ID not in _registered_docs][:batch_size]

            if not batch_docs:
                # No more documents
                logger.info(
                    f"Background loader complete: {len(_registered_docs)} documents registered"
                    + (f" (filtered to {root})" if root != "/" else "")
                )
                break

            # Register this batch
            registered_count = 0
            for doc in batch_docs:
                if shutdown_event.is_set():
                    break
                try:
                    if _register_document(client, doc, items_by_id, root=root):
                        registered_count += 1

                    # Index document metadata in SQLite (L2 cache)
                    try:
                        from rm_mcp.index import get_instance

                        index = get_instance()
                        if index is not None:
                            doc_path = _get_root_path()
                            if items_by_id:
                                from rm_mcp.paths import get_item_path

                                doc_path = get_item_path(doc, items_by_id)

                            # Determine file type from name
                            name_lower = doc.VissibleName.lower()
                            if name_lower.endswith(".pdf"):
                                file_type = "pdf"
                            elif name_lower.endswith(".epub"):
                                file_type = "epub"
                            else:
                                file_type = "notebook"

                            doc_hash = getattr(doc, "Version", None) or getattr(
                                doc, "ModifiedClient", None
                            )
                            # Check for stale content BEFORE upserting
                            # (upsert overwrites the hash, making comparison impossible)
                            if doc_hash and index.needs_reindex(
                                doc.ID, str(doc_hash)
                            ):
                                logger.debug(
                                    f"Document '{doc.VissibleName}' needs re-indexing"
                                )
                            index.upsert_document(
                                doc_id=doc.ID,
                                doc_hash=str(doc_hash) if doc_hash else None,
                                name=doc.VissibleName,
                                path=doc_path,
                                file_type=file_type,
                                modified_at=getattr(doc, "ModifiedClient", None),
                            )
                    except Exception:
                        pass  # Index failure is non-fatal

                except Exception as e:
                    logger.debug(f"Failed to register document '{doc.VissibleName}': {e}")

            if registered_count > 0:
                logger.debug(
                    f"Registered batch of {registered_count} documents "
                    f"(total: {len(_registered_docs)})"
                )

            offset += batch_size

            # Yield control - allow other async tasks to run
            # Small delay to be gentle on the API
            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        logger.info("Background document loader cancelled")
        raise
    except Exception as e:
        logger.warning(f"Background document loader error: {e}")


def start_background_loader() -> Optional[asyncio.Task]:
    """Start the background document loader task. Returns the task."""
    shutdown_event = asyncio.Event()

    try:
        task = asyncio.create_task(_load_documents_background(shutdown_event))
        # Store the event on the task so we can access it later
        task.shutdown_event = shutdown_event  # type: ignore[attr-defined]
        logger.info("Started background document loader")
        return task
    except Exception as e:
        logger.warning(f"Could not start background loader: {e}")
        return None


async def stop_background_loader(task: Optional[asyncio.Task]):
    """Stop the background document loader task."""
    if task is None:
        return

    # Signal shutdown via event
    if hasattr(task, "shutdown_event"):
        task.shutdown_event.set()

    # Cancel and wait
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info("Stopped background document loader")


# Completion handler for resource templates
@mcp.completion()
async def handle_completion(ref, argument, context):
    """Provide completions for resource template parameters.

    Currently handles:
    - remarkableimg:// page parameter: looks up actual page count for the document
    - remarkablesvg:// page parameter: looks up actual page count for the document
    """
    if isinstance(ref, ResourceTemplateReference):
        uri = ref.uri if hasattr(ref, "uri") else str(ref)

        # Handle page completions for image resources (PNG and SVG)
        is_img = uri.startswith("remarkableimg://") and argument.name == "page"
        is_svg = uri.startswith("remarkablesvg://") and argument.name == "page"

        if is_img or is_svg:
            # Extract any partial value the user has typed
            partial = argument.value or ""

            # Try to find the matching URI template and get actual page count
            page_count = 1  # Default to 1 if we can't determine
            for template_uri, (client, doc) in _img_uri_to_doc.items():
                # Check if the request URI matches this template (ignoring the {page} part)
                # Template: remarkableimg:///Drawing/Frogalina.page-{page}.png
                # Request:  remarkableimg:///Drawing/Frogalina.page-{page}.png
                if template_uri == uri:
                    try:
                        # Download and count pages
                        from rm_mcp.extract import get_document_page_count

                        raw_doc = client.download(doc)
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                            tmp.write(raw_doc)
                            tmp_path = Path(tmp.name)
                        try:
                            page_count = get_document_page_count(tmp_path)
                        finally:
                            tmp_path.unlink(missing_ok=True)
                    except Exception as e:
                        logger.debug(f"Failed to get page count for completion: {e}")
                    break

            # Suggest page numbers up to the actual count
            suggestions = [str(i) for i in range(1, page_count + 1)]
            if partial:
                suggestions = [s for s in suggestions if s.startswith(partial)]

            return Completion(values=suggestions[:10], hasMore=len(suggestions) > 10)

    return None
