# Changelog

## Unreleased

- Removed the daily canary workflow (`canary.yml`, `scripts/canary.py`).

## 2.0.0

A rebuild of rm-mcp: ten id-addressed tools, writes that never delete, a token that setup can actually replace, and installs for Claude Code and Claude Desktop.

### Tools

- Ten tools: `remarkable_find`, `remarkable_list`, `remarkable_read`, `remarkable_view`, `remarkable_annotations`, `remarkable_save_transcript`, `remarkable_status`, `remarkable_send`, `remarkable_organize` and `remarkable_undo`. Every document is addressed by a stable id; paths and names still work. Every tool except `remarkable_view` returns structured output with an `outputSchema`.
- Page numbers are tablet pages: a page inserted into a PDF counts.
- Handwriting: Claude views the page and saves a transcript with `remarkable_save_transcript`. The transcript is tied to the page's content hash and becomes searchable. MCP sampling OCR is gone (clients don't support it); transcripts it produced are still read.
- `remarkable_send` puts a PDF, EPUB, URL or Markdown (typeset to a tablet-sized PDF) on the tablet.
- `remarkable_organize` renames, moves and creates folders as one batch, as a dry run by default, and returns an undo token when applied. `remarkable_undo` reverts a batch if nothing it touched changed since.
- Resource templates `remarkable://doc/{id}` and `remarkable://doc/{id}/page/{n}.png`, and three prompts (`transcribe_notebook`, `weekly_review`, `find_topic`).

### Safety

- Nothing deletes: no HTTP DELETE, no dropped root entry, no `deleted` flag. A test scans for it.
- `REMARKABLE_READ_ONLY=1` refuses every write; `REMARKABLE_WRITE_ROOT` (default `REMARKABLE_ROOT_PATH`) bounds every write path. One violation refuses the whole batch before any upload.
- Every write is one root commit based on the generation it read, behind a root guard that refuses any change outside the batch.
- An organize batch is refused, with nothing uploaded, when its undo log can't be recorded.
- Renames and moves keep each document's modified time.

### Setup and auth

- rm-mcp keeps its token in its own file: `~/.config/rm-mcp/token.json` (Linux), `~/Library/Application Support/rm-mcp/token.json` (macOS), `%APPDATA%\rm-mcp\token.json` (Windows), mode 0600. It reads that file first, then `REMARKABLE_TOKEN`, then `~/.rmapi` (read only; rm-mcp no longer writes rmapi's file). Re-running setup now replaces the token in use, also in a running server (#6).
- `rm-mcp setup` registers with the real OS name, saves the token, and offers to add rm-mcp to Claude Code (`claude mcp add -s user`, no token in the command) and Claude Desktop (edited only when its JSON parses, written atomically, `.bak` kept, full path to `uvx`). Commands are shown with the right quoting for each OS (#1).
- `rm-mcp doctor` checks the token, the connection, `uvx` and client configs and prints fixes. `rm-mcp logout` removes rm-mcp's token file.
- The renewed user token is saved and reused for its lifetime, so a restart doesn't need a renewal.
- `setup.sh` only installs uv if needed and runs `uvx rm-mcp@latest setup`.

### Distribution

- Claude Code plugin with three skills (transcribe a notebook to Markdown, send to reMarkable, weekly review): `/plugin marketplace add wavyrai/rm-mcp`, then `/plugin install remarkable@rm-mcp`.
- Claude Desktop bundle (`.mcpb`), attached to each release. It takes a one-time code in its settings and redeems it on first start.

### Under the hood

- MCP Python SDK 2.x.
- Incremental sync: only new or changed documents are fetched; page counts come from `.content` without a download.
- Content-addressed download cache (`~/.cache/rm-mcp/blobs`, sha256-verified, size-capped).
- Pages render with rmscene and PyMuPDF; Cairo and other system libraries are no longer needed.
- Dependencies dropped: `ebooklib`, `beautifulsoup4`, `rmc`, `cairosvg`; `Pillow` is now test-only.
- CI runs on Linux, macOS and Windows with Python 3.10 to 3.13, and a daily read-only canary probes the real cloud.

### Thanks

To JasonC-AerisMapping for tracking down the `rm-filename` header change (#7) and the Cairo failure on Windows (#8), to tuzumkuru for diagnosing the SDK 2.0 import error (#9), and to everyone who reported setup problems (#1, #6).

### Migrating from 0.7

1. Run `uvx rm-mcp@latest setup` once. It saves a token in rm-mcp's own file and can rewrite your Claude Code and Claude Desktop entries.
2. Remove these from your client configs:
   - `REMARKABLE_TOKEN`: no longer needed, and ignored while the token file exists.
   - `REMARKABLE_OCR_BACKEND`: removed; handwriting is read by Claude through `remarkable_view` and saved with `remarkable_save_transcript`.
   - `REMARKABLE_PAGE_SIZE`: removed; `remarkable_read` takes `pages` (such as `"1-5"` or `"all"`) and `REMARKABLE_MAX_OUTPUT_CHARS` caps the text per call.
3. Update prompts or scripts that use the old tool names. They still answer in 2.0 as deprecated aliases and will be removed in the next release:

   | 0.7 tool | 2.0 tool |
   |----------|----------|
   | `remarkable_search` | `remarkable_find(query)` |
   | `remarkable_recent` | `remarkable_find()` |
   | `remarkable_browse` | `remarkable_list(folder)` |
   | `remarkable_image` | `remarkable_view(id, pages)` |
   | `remarkable_rename` | `remarkable_organize` with `op: rename` |
   | `remarkable_move` | `remarkable_organize` with `op: move` |
   | `remarkable_create_folder` | `remarkable_organize` with `op: create_folder` |

   `remarkable_read`'s old `document`, `page`, `grep` and `content_type` parameters also still work for one release; use `id` and `pages`.
4. `rm-mcp --setup` and `rm-mcp --register CODE` still work. `--register` now saves the token to the file and prints nothing secret; `--register CODE --quiet` no longer prints the token.
