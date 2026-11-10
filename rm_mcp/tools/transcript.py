"""remarkable_save_transcript — keep an agent's transcript of a handwritten page."""

from mcp.types import ToolAnnotations

from rm_mcp import reader
from rm_mcp.server import mcp
from rm_mcp.tools import _helpers
from rm_mcp.tools.schemas import SavedTranscript

SAVE_TRANSCRIPT_ANNOTATIONS = ToolAnnotations(
    title="Save Page Transcript (local)",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


@mcp.tool(title=SAVE_TRANSCRIPT_ANNOTATIONS.title, annotations=SAVE_TRANSCRIPT_ANNOTATIONS)
async def remarkable_save_transcript(id: str, page: int, text: str) -> SavedTranscript:
    """Store your transcript of a handwritten page so find and read cover it.

    Writes only to the local rm-mcp index, never to the tablet or the cloud.
    The transcript is tied to the page's exact content: if the page is edited
    on the tablet, the transcript no longer applies and is dropped. Saving
    again replaces it.

    Args:
        id: Document id; a path or name also works.
        page: Tablet page number.
        text: What the page says, as plain text or Markdown.
    """
    return SavedTranscript(**await _helpers.run_blocking(reader.save_transcript, id, page, text))
