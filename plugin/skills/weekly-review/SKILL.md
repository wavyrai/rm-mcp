---
name: weekly-review
description: Use when the user asks what they worked on in their reMarkable notes this week (or since a date), for a weekly review, or for open action items from their notes. Lists recently modified documents, reads them, transcribes handwriting, and summarises themes, action items and open questions with page references.
---

# Weekly review of reMarkable notes

You need the `remarkable` MCP server's tools. If `remarkable_status` says the user is not signed in, tell them to run `uvx rm-mcp@latest setup` in a terminal and stop.

1. **What changed.** `remarkable_find(modified_after="<date 7 days ago, YYYY-MM-DD>")`, or the date the user gives. If there are more than about 15 documents, list them and ask which to include.
2. **Read each one.** `remarkable_read(id=<id>)`. For a PDF or book, also check `remarkable_annotations(id=<id>)` for highlights.
3. **Handwriting.** For handwritten pages without a transcript, view them with `remarkable_view(id, pages=...)`, transcribe, and save with `remarkable_save_transcript(id, page, text)` so they are searchable next time. Mark unreadable words `[?]`.
4. **Write the review:**
   - **Themes**: 3 to 6 short paragraphs grouping what the user worked on.
   - **Action items**: a checklist, each item with the document name and page (`- [ ] Email Sam the draft (Standup, p. 3)`).
   - **Open questions**: things the notes leave undecided.
   - **Highlights**: notable passages from annotated PDFs, quoted, with source and page.
5. Offer, without doing it unasked, to save the review as a Markdown file or to send it to the tablet (the send-to-remarkable skill).

This review only reads. Do not rename, move or send anything unless the user asks.
