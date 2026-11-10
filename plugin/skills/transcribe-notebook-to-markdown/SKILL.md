---
name: transcribe-notebook-to-markdown
description: Use when the user wants a reMarkable notebook (handwriting included) turned into a Markdown file, for example a note for Obsidian. Finds the notebook, reads typed text, views and transcribes handwritten pages, saves each transcript back to the search index, and writes one .md file with front matter.
---

# Transcribe a reMarkable notebook to Markdown

You need the `remarkable` MCP server's tools. If `remarkable_status` says the user is not signed in, tell them to run `uvx rm-mcp@latest setup` in a terminal and stop.

1. **Find it.** `remarkable_find(query="<what the user called it>")`. If several match, show the names, paths and modified dates and ask which one. Keep the `id`.
2. **Read what is already text.** `remarkable_read(id=<id>, pages="all")`. It returns typed text, PDF text and saved transcripts per page, and marks pages that carry handwriting without a transcript. For a long notebook, read in ranges (`pages="1-20"`) and follow the returned cursor.
3. **Transcribe handwriting.** For each page marked as handwritten with no transcript, call `remarkable_view(id=<id>, pages="<n>")` (up to 4 pages per call; use `size="large"` for small writing). Transcribe exactly what is written:
   - keep the writer's words, line breaks between thoughts, lists and headings;
   - write `[?]` for a word you can't read, and never guess silently;
   - describe a drawing or diagram in one italic line, such as `*Sketch: three boxes joined by arrows, labelled A, B, C.*`
4. **Save each transcript** with `remarkable_save_transcript(id=<id>, page=<n>, text=<transcript>)` before moving on. This makes the page searchable with `remarkable_find` and means the next run skips it. It writes only the local index, never the tablet.
5. **Write the Markdown file** where the user asked (default: the current directory, named after the notebook, such as `Meeting notes 2026-11.md`). Make it Obsidian-friendly:

   ```markdown
   ---
   title: <notebook name>
   source: remarkable
   remarkable_id: <id>
   remarkable_path: <path>
   modified: <modified date from find/read>
   tags: [remarkable]
   ---

   # <notebook name>

   ## Page 1

   <text>
   ```

   - one `## Page N` heading per tablet page (page numbers are tablet pages);
   - turn `#tag` words the writer used into Obsidian tags as written;
   - keep `[?]` markers so the user can fix them;
   - if the file exists, ask before overwriting it.
6. **Report** the file path, the page count, how many pages you transcribed now, and how many `[?]` markers are left.

Never send, rename or move anything on the tablet as part of this task.
