---
name: send-to-remarkable
description: Use when the user wants to put something on their reMarkable tablet - a local PDF or EPUB, a URL to one, a Markdown file, or text Claude writes (a summary, a reading list, notes for a meeting). Sends it with remarkable_send to the folder they name.
---

# Send something to the reMarkable

You need the `remarkable` MCP server's tools. If `remarkable_status` says the user is not signed in, tell them to run `uvx rm-mcp@latest setup` in a terminal and stop. If it reports read-only mode, say so and stop: writes are switched off on purpose.

1. **Work out the source.**
   - A file path ending in `.pdf`, `.epub` or `.md`: pass the path.
   - An `http(s)` URL to a PDF or EPUB: pass the URL (public hosts only, up to 100 MB).
   - Anything else to read on the tablet (a summary you write, notes, a checklist): write it as Markdown with a `# Title` first line and pass the Markdown text. It is typeset into a PDF sized for the tablet.
2. **Pick the folder.** Use the folder the user names (`/Reading`, `Inbox`). A path without a leading `/` goes under the default write folder that `remarkable_status` shows. Missing folders are created. If the user named no folder, use the default and say where it went.
3. **Confirm when it isn't obvious.** A send cannot be undone (undo would mean deleting, which rm-mcp never does). If the user didn't clearly ask to send this exact thing, show the title, source and folder and ask first.
4. **Send.** `remarkable_send(source=<source>, folder=<folder>, title=<optional title>)`.
5. **Report** the name, folder and page count from the result, and that it appears on the tablet after it syncs.
