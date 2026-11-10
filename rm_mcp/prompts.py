"""MCP prompts: three workflows that use the tools together."""

from rm_mcp.server import mcp


@mcp.prompt(
    name="transcribe_notebook",
    title="Transcribe a Notebook to Markdown",
    description="Turn a notebook, handwriting included, into Markdown, saving each page's text.",
)
def transcribe_notebook(document: str) -> str:
    return (
        f"Transcribe my reMarkable document '{document}' to Markdown.\n\n"
        f"1. Find it with remarkable_find(query='{document}') and take its id.\n"
        "2. remarkable_read(id, pages='all') gives the typed and PDF text per page, and "
        "marks pages with handwriting.\n"
        "3. For each handwritten page without a transcript, look at it with "
        "remarkable_view(id, pages=...) (up to 4 pages per call), transcribe it exactly, "
        "and save it with remarkable_save_transcript(id, page, text).\n"
        "4. Put the pages together as one Markdown document with a heading per page. "
        "Describe drawings in a short italic line; mark words you cannot read as [?]."
    )


@mcp.prompt(
    name="weekly_review",
    title="Weekly Review of Recent Notes",
    description="Summarise what changed on the tablet this week, with action items.",
)
def weekly_review(since: str = "") -> str:
    when = f"modified_after='{since}'" if since else "modified_after set to 7 days ago"
    return (
        "Review my reMarkable notes from the past week.\n\n"
        f"1. remarkable_find({when}) lists what I worked on.\n"
        "2. Read each with remarkable_read(id). For handwritten pages without a transcript, "
        "look with remarkable_view and save what you read with remarkable_save_transcript.\n"
        "3. Summarise by theme, then list action items and open questions, each with the "
        "document name and page."
    )


@mcp.prompt(
    name="find_topic",
    title="Find Everything About a Topic",
    description="Gather every note, highlight and page about one topic.",
)
def find_topic(topic: str) -> str:
    return (
        f"Find everything on my reMarkable tablet about '{topic}'.\n\n"
        f"1. remarkable_find(query='{topic}') searches names, text, highlights and saved "
        "transcripts; try a synonym or two as well.\n"
        "2. Read the matching pages with remarkable_read(id, pages=...) and check "
        "remarkable_annotations(id) for highlights.\n"
        "3. Handwriting is only searchable once transcribed: for likely notebooks, view the "
        "pages and save transcripts as you go.\n"
        "4. Report what you found, grouped by document, citing pages."
    )
