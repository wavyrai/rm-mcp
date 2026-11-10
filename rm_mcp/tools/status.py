"""remarkable_status — connection, library size, index and settings."""

from rm_mcp import reader
from rm_mcp.config import env_bool
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers
from rm_mcp.tools.schemas import Coverage, IndexStats, StatusConfig, StatusResult


def _config() -> StatusConfig:
    from rm_mcp.cache import _CACHE_TTL_SECONDS
    from rm_mcp.extract.render import get_background_color

    return StatusConfig(
        root_path=_helpers._get_root_path(),
        background_color=get_background_color(),
        cache_ttl_seconds=_CACHE_TTL_SECONDS,
        max_output_chars=reader.max_output_chars(),
        read_only=env_bool("REMARKABLE_READ_ONLY"),
    )


def status() -> StatusResult:
    from rm_mcp.api import REMARKABLE_TOKEN
    from rm_mcp.index import get_instance

    connection = "REMARKABLE_TOKEN" if REMARKABLE_TOKEN else "~/.rmapi"
    try:
        snap = reader.snapshot()
    except Exception as e:
        return StatusResult(
            authenticated=False,
            connection=connection,
            config=_config(),
            error=str(e),
            hint=_helpers.suggest_for_error(e),
        )

    docs = snap.documents()
    result = StatusResult(
        authenticated=True,
        connection=connection,
        document_count=len(docs),
        config=_config(),
        hint=f"Connected; {len(docs)} documents in scope. Start with remarkable_find().",
    )
    index = get_instance()
    if index is not None:
        stats = index.get_stats()
        versions = index.indexed_versions()
        result.index = IndexStats(
            path=stats["index_path"],
            documents=stats["index_documents"],
            pages=stats["index_pages"],
            size_bytes=stats["index_size"],
        )
        result.index_coverage = Coverage(
            indexed=sum(1 for e in docs if e.hash and versions.get(e.id) == e.hash),
            total=len(docs),
        )
    return result


@mcp.tool(
    title="Check reMarkable Connection",
    annotations=_helpers.read_only("Check reMarkable Connection"),
)
async def remarkable_status() -> StatusResult:
    """Check the reMarkable Cloud connection, library size, search index and settings.

    Never fails: when not connected it returns authenticated=false with the
    error and how to fix it.
    """
    return await _helpers.run_blocking(status)
