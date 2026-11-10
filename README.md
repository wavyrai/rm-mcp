<p align="center"><img src="https://raw.githubusercontent.com/wavyrai/rm-mcp/main/plugin/.claude-plugin/icon.png" alt="rm-mcp icon: a pen writing a line on an e-paper tablet" width="128" height="128"></p>

# rm-mcp: your reMarkable in Claude

rm-mcp is an MCP server that lets Claude (and other MCP clients) find, read and look at everything on your reMarkable tablet: notebooks, PDFs, EPUBs, highlights and handwriting. It can also send documents to the tablet and tidy your folders. It never deletes anything.

<!-- mcp-name: io.github.wavyrai/rm-mcp -->

- **Find anything**: search names, typed text, PDF and EPUB text, highlights and the transcripts of your handwriting.
- **Read handwriting**: Claude looks at the page and writes down what it says. The transcript is saved, so the page becomes searchable.
- **Send to the tablet**: a PDF, an EPUB, a link to one, or Markdown that is typeset into a clean PDF.
- **Organise safely**: rename, move and create folders in one batch, as a dry run first, with undo.

It works through the reMarkable Cloud, so your tablet needs cloud sync turned on (see the [FAQ](#faq)).

---

## Install

Pick the app you use. Every path ends with the same one-time sign-in to your reMarkable account.

### Claude Code (plugin)

In Claude Code:

```
/plugin marketplace add wavyrai/rm-mcp
/plugin install remarkable@rm-mcp
```

Or in one step on Claude Code 2.1.275 or later:

```
/plugin install remarkable --marketplace wavyrai/rm-mcp
```

Then connect your reMarkable once, in a terminal:

```bash
uvx rm-mcp@latest setup --no-clients
```

The plugin adds the server plus three skills: `/remarkable:transcribe-notebook-to-markdown` (a notebook to an Obsidian-ready `.md` file), `/remarkable:send-to-remarkable` and `/remarkable:weekly-review`.

### Claude Desktop (bundle)

Download `rm-mcp-2.0.0.mcpb` from the [latest release](https://github.com/wavyrai/rm-mcp/releases/latest) and open it; Claude Desktop shows an install dialog. Get a one-time code at [my.remarkable.com/device/apps/connect](https://my.remarkable.com/device/apps/connect) and paste it into the **One-time code** field. rm-mcp swaps it for a token on first start and keeps that in its own file. If you already ran `rm-mcp setup` on this computer, leave the field empty.

### Any client, with uv

```bash
uvx rm-mcp@latest setup
```

This opens the code page, asks for the code, saves the token, and then asks whether to add rm-mcp to Claude Code and to Claude Desktop. It changes a Desktop config only if it can read it, and keeps the old one as `claude_desktop_config.json.bak`.

No uv yet? `curl -fsSL https://thijsverreck.com/setup.sh | sh` installs [uv](https://docs.astral.sh/uv/) and runs the same command. On Windows, install uv with `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`, then run `uvx rm-mcp@latest setup`.

To add rm-mcp to another MCP client by hand, run `uvx rm-mcp@latest` over stdio (for clients with a short PATH, such as Claude Desktop, use the full path to `uvx`). No token belongs in the client config.

### Check and sign out

```bash
uvx rm-mcp@latest doctor   # token, connection, uvx, client configs, with fixes
uvx rm-mcp@latest logout   # removes rm-mcp's token file
```

The token lives in `~/.config/rm-mcp/token.json` (Linux), `~/Library/Application Support/rm-mcp/token.json` (macOS) or `%APPDATA%\rm-mcp\token.json` (Windows), readable only by you.

---

## Tools

| Tool | What it does |
|------|--------------|
| `remarkable_find` | Search names, text, highlights and transcripts; with no query, lists recent documents |
| `remarkable_list` | The folder tree with ids, types, page counts and dates |
| `remarkable_read` | Text per tablet page; marks handwritten pages that have no transcript yet |
| `remarkable_view` | Pages as images, up to 4 per call |
| `remarkable_annotations` | Highlights, and the pages that carry handwriting |
| `remarkable_save_transcript` | Keep Claude's transcript of a handwritten page (local index only) |
| `remarkable_status` | Connection, library size, index and settings |
| `remarkable_send` | Put a PDF, EPUB, URL or Markdown on the tablet |
| `remarkable_organize` | Rename, move and create folders as one batch; dry run by default |
| `remarkable_undo` | Revert an organize batch |

Every result carries a stable document **id**; a path or a name works wherever an id does. Page numbers are tablet pages, so a page you inserted into a PDF counts.

📖 [Tools reference](docs/tools.md) · [Resources](docs/resources.md)

The server also offers three prompts (`transcribe_notebook`, `weekly_review`, `find_topic`) and two resource templates (`remarkable://doc/{id}` and `remarkable://doc/{id}/page/{n}.png`).

## What rm-mcp promises

- **It never deletes.** No code path removes a document, sends an HTTP DELETE or marks anything deleted; a test checks this. Undo leaves folders it created, and a send cannot be undone (remove it on the tablet if you don't want it).
- **Writes stay where you allow them.** `REMARKABLE_READ_ONLY=1` refuses every write. `REMARKABLE_WRITE_ROOT` (default: `REMARKABLE_ROOT_PATH`) is the folder every write must stay inside; one path outside it refuses the whole batch before anything is uploaded.
- **Organising is a dry run first.** `remarkable_organize` returns the exact plan and changes nothing until it is called again with `dry_run=false`.
- **Every organize can be undone.** An applied batch returns an undo token. If rm-mcp can't record the undo (for example, the local index is unavailable), it refuses the batch instead of applying it.
- **One batch, one commit.** Every write is a single update of the library, guarded so that nothing outside the batch can change, and based on the version it read, so a change from the tablet in the meantime is never overwritten.
- **Renames and moves keep your "last opened" order.** They don't touch a document's modified time.

## Handwriting

rm-mcp doesn't run OCR. Claude already reads images: `remarkable_read` marks handwritten pages, `remarkable_view` shows them, and `remarkable_save_transcript` stores what Claude read. A transcript is tied to the page's content, so it stays until you edit that page on the tablet.

---

## Settings

Set these as environment variables in the client config. Most people need none of them.

| Variable | Default | Description |
|----------|---------|-------------|
| `REMARKABLE_ROOT_PATH` | `/` | Show only this folder, such as `/Work` |
| `REMARKABLE_READ_ONLY` | off | `1` refuses every write (send, organize, undo) |
| `REMARKABLE_WRITE_ROOT` | `REMARKABLE_ROOT_PATH` | Folder (full path) every write must stay inside |
| `REMARKABLE_BACKGROUND_COLOR` | `#FBFBFB` | Page image background, `#RRGGBB` or `#RRGGBBAA` |
| `REMARKABLE_MAX_OUTPUT_CHARS` | `50000` | Page text per `remarkable_read` call |
| `REMARKABLE_CACHE_TTL` | `60` | Seconds before the library is checked for changes |
| `REMARKABLE_INDEX_CONTENT` | `1` | `0` turns off background indexing of page text |
| `REMARKABLE_INDEX_PATH` | `~/.cache/rm-mcp/index.db` | Search index (and undo log) location |
| `REMARKABLE_INDEX_REBUILD` | off | `1` clears the search index at start (transcripts are kept) |
| `REMARKABLE_BLOB_CACHE_MB` | `500` | Download cache size; `0` turns it off |
| `REMARKABLE_BLOB_CACHE_DIR` | `~/.cache/rm-mcp/blobs` | Download cache location |
| `REMARKABLE_PARALLEL_WORKERS` | `5` | Parallel metadata fetches |
| `REMARKABLE_COMPACT` | off | `1` omits hints from the deprecated organising tools |
| `REMARKABLE_TOKEN` | | Only used when rm-mcp's token file is absent |

---

## FAQ

**Do I need reMarkable Connect?**
Yes, in practice. rm-mcp reads your library from the reMarkable Cloud, and only what your tablet syncs to the cloud is visible to it. Without a [Connect](https://remarkable.com/store/connect) subscription reMarkable limits cloud sync, so documents can be missing.

**I re-ran setup but rm-mcp still uses the old account or an expired token.**
Update to 2.0. Older versions let a `REMARKABLE_TOKEN` in the client config win over a new login. 2.0 reads its own token file first, and a running server picks up a new login without a restart. `uvx rm-mcp@latest doctor` shows which token is in use.

**Claude Desktop says the server failed to start.**
Run `uvx rm-mcp@latest doctor`. The usual cause is a `"command": "uvx"` without a full path; `rm-mcp setup` writes the full path.

**Does it change my tablet?**
Only when you ask it to send or organise something, and never by deleting. Set `REMARKABLE_READ_ONLY=1` to rule writes out.

**Where does my data go?**
Between your computer and the reMarkable Cloud only. The search index, transcripts and download cache stay in `~/.cache/rm-mcp`.

---

## Upgrading from 0.x

See [CHANGELOG.md](CHANGELOG.md#migrating-from-07). In short: run `uvx rm-mcp@latest setup` once, and remove `REMARKABLE_TOKEN`, `REMARKABLE_OCR_BACKEND` and `REMARKABLE_PAGE_SIZE` from your client configs. The 0.7 tool names still answer for one release.

## Development

```bash
git clone https://github.com/wavyrai/rm-mcp.git
cd rm-mcp
uv sync --all-extras
uv run pytest
```

📖 [Development guide](docs/development.md)

## License

MIT. Built with [rmscene](https://github.com/ricklupton/rmscene) and [PyMuPDF](https://pymupdf.readthedocs.io/), with thanks to [ddvk/rmapi](https://github.com/ddvk/rmapi).
