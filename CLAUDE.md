# CLAUDE.md

## Project overview

rm-mcp is an MCP (Model Context Protocol) server that gives AI assistants read-only access to a reMarkable tablet's library via the reMarkable Cloud API. It supports notebooks, PDFs, EPUBs, full-text search, and handwriting via transcripts the agent saves after viewing a page.

- **Package name:** `rm-mcp` (PyPI), `io.github.wavyrai/rm-mcp` (MCP Registry)
- **Python:** >=3.10, built with Hatchling
- **Package manager:** uv (lockfile: `uv.lock`)
- **Entry point:** `rm_mcp/cli.py` → `rm-mcp` CLI command

## Repository layout

```
rm_mcp/                   # Main package
  server.py               # MCPServer subclass & initialization
  cli.py                  # CLI (--setup, --register, or start server)
  api.py                  # Singleton reMarkable Cloud client
  config.py               # Tolerant env var parsing (env_int/env_bool)
  models.py               # Document/Folder dataclasses
  paths.py                # Path utilities & root path filtering
  index.py                # SQLite: per-page FTS5 search, sync state, page transcripts
  cache.py                # Multi-layer caching (memory → SQLite → Cloud)
  blobstore.py            # Content-addressed on-disk blob cache (sha256, LRU, size cap)
  library.py              # Stable-ID view: id → path, name, kind, hash, modified, pages
  reader.py               # Read side: resolve id/path/name, pages, find/list/read/view/annotations
  loader.py               # Startup: load the library, index page text in the background
  resources.py            # Resource templates remarkable://doc/{id} and .../page/{n}.png
  prompts.py              # MCP prompt templates (transcribe, weekly review, find topic)
  tools/                  # Tool registration (one file per tool)
    find.py, listing.py, read.py, view.py, annotations.py, status.py
    transcript.py         # remarkable_save_transcript (writes the local index only)
    schemas.py            # Pydantic output models → outputSchema
    deprecated.py         # 0.7 names (search, browse, recent, image) as thin aliases
    organize.py           # rename / move / create folder (the only library writes)
  clients/
    organize.py           # Merkle-tree mutation + root commit
  extract/                # Content extraction
    pages.py              # .content page plan: tablet page → .rm + source PDF/EPUB page
    document.py           # Per tablet page: typed text, source text, highlights, ink, hash
    notebook.py           # Typed text of one .rm page, page hashes, page count
    render.py             # rmscene + pymupdf page renderer (PNG/SVG, no system libs)
  clients/
    cloud.py              # reMarkable Cloud API v3/v4 client
tests/test_tools.py       # The v2 tools end to end on the fake cloud (fixtures, real client)
tests/test_server.py      # Registration, CLI setup, root-path helpers, cache, index
tests/test_integration.py # Integration tests — fake library, real index/ZIP/PDF
tests/test_fixtures.py    # Extraction against tests/fixtures (make_fixtures.py)
tests/test_sync.py        # Incremental sync, blob store, hash-keyed caches (fake cloud)
tests/test_render.py      # Renderer goldens (tests/fixtures/goldens, --update-goldens)
setup.sh                  # One-line installer script
server.json               # MCP Registry server definition
server.py                 # Root-level backwards-compatible entry point
docs/                     # Documentation (tools, resources, dev guide)
.github/workflows/        # CI/CD (ci.yml, publish.yml, conformance.yml)
```

## Common commands

```bash
uv sync --all-extras            # Install all dependencies
uv run pytest -v                # Run all tests (unit + integration)
uv run pytest tests/test_integration.py -v  # Integration tests only
uv run ruff check .             # Lint
uv run ruff format --check .    # Check formatting
uv run ruff format .            # Auto-format
uv run ruff check . --fix       # Auto-fix lint issues (e.g. import sorting)
uv run rm-mcp                   # Start the MCP server locally
uv build                        # Build wheel + sdist into dist/
```

## Version management

The version lives in `pyproject.toml` (single source of truth). The publish workflow extracts it from the git tag (`v0.4.7` → `0.4.7`) and patches both `pyproject.toml` and `server.json` before building. When bumping manually, update `pyproject.toml` and `server.json`.

## Release process

Pushing a git tag triggers the full pipeline:

```bash
# 1. Update version in pyproject.toml and server.json
# 2. Commit and tag
git tag v0.X.Y
git push && git push --tags
```

The `publish.yml` workflow then runs sequentially:
1. **create-release** — GitHub Release with auto-generated notes
2. **test** — pytest + ruff on Python 3.12
3. **build** — patches version from tag, runs `uv build`, uploads artifacts
4. **publish-pypi** — trusted publishing via OIDC (no API token needed)
5. **publish-mcp-registry** — patches `server.json` version, authenticates via GitHub OIDC, runs `mcp-publisher publish`

To publish manually (without CI):
```bash
mcp-publisher login github
mcp-publisher publish
```

## CI

`ci.yml` runs on every push/PR to main:
- **lint:** `ruff format --check` + `ruff check`
- **test:** pytest across Python 3.10, 3.11, 3.12

`conformance.yml` runs MCP protocol conformance tests.

## setup.sh installer

The one-line installer lives in two places and must be kept in sync:
- `setup.sh` in this repo
- `public/setup.sh` in the [thijsverreck.com](https://github.com/wavyrai/thijsverreck.com) repo (served at `https://thijsverreck.com/setup.sh`)

Usage: `curl -fsSL https://thijsverreck.com/setup.sh | sh`

Steps: install deps (Homebrew, uv, Claude Code) → register reMarkable tablet → configure Claude Code → configure Claude Desktop (macOS only).

When modifying `setup.sh`, commit and push to **both** repos.

Key details:
- `claude mcp add` uses `|| true` so the script continues even if the server is already configured
- Claude Desktop config uses the **full path to uvx** (resolved via `command -v uvx`) because Claude Desktop has a limited PATH and can't find `~/.local/bin/uvx`
- All `uvx` commands use `rm-mcp@latest` (not `--refresh rm-mcp`) to ensure the latest PyPI version is always used, even if a persistent tool install exists

## MCP Registry

The server is published to the official MCP Registry as `io.github.wavyrai/rm-mcp`. Ownership is verified via `<!-- mcp-name: io.github.wavyrai/rm-mcp -->` in README.md (checked against the PyPI package description). Do not remove this comment.

`server.json` defines the registry entry. The publish workflow updates its version automatically from the git tag — no need to edit it manually for releases.

The `.mcpregistry_*` token files are gitignored — never commit them.

## Key environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `REMARKABLE_TOKEN` | Yes | Cloud API auth token |
| `REMARKABLE_ROOT_PATH` | No | Restrict to a folder (e.g. `/Work`) |
| `REMARKABLE_BACKGROUND_COLOR` | No | Image background, default opaque `#FBFBFB` (read per call) |
| `REMARKABLE_CACHE_TTL` | No | Collection cache TTL in seconds, default `60` |
| `REMARKABLE_INDEX_CONTENT` | No | Set `0` to disable background page-text indexing |
| `REMARKABLE_INDEX_PATH` | No | FTS index location, default `~/.cache/rm-mcp/index.db` |
| `REMARKABLE_COMPACT` | No | Set `1` or `true` to omit hints from the organising tools |
| `REMARKABLE_MAX_OUTPUT_CHARS` | No | Page text per `remarkable_read` call, default `50000` |
| `REMARKABLE_PARALLEL_WORKERS` | No | Parallel metadata fetch workers, default `5` |
| `REMARKABLE_BLOB_CACHE_MB` | No | Blob store size cap in MB, default `500`; `0` disables it |
| `REMARKABLE_BLOB_CACHE_DIR` | No | Blob store location, default `~/.cache/rm-mcp/blobs` |

## Code style

- Line length: 100 (ruff)
- Ruff rules: E, F, W, I
- Async: all tool handlers are async, pytest uses `asyncio_mode = "auto"`
- Reading tools are read-only and idempotent; `tools/organize.py` writes the library and `remarkable_save_transcript` writes only the local index

## Architecture rules

These are invariants, not preferences — each one exists because breaking it caused a real bug.

- **Tools are `async def`, and blocking work goes through `_helpers.run_blocking()`.** In mcp SDK 2.x a plain `def` tool does not block the loop (MCPServer runs it on a worker thread), so this is for consistency, not correctness: every tool has one shape and which work runs off the loop is explicit. What does block the loop is synchronous I/O *inside* an `async def` tool; that is what `run_blocking()` is for.
- **Never bypass the root filter.** Anything that returns document names, paths, or content must check `_is_within_root` against the live collection. The persistent index outlives config changes and deletions, so index hits are only trusted when the document is still visible in the current library.
- **Don't swallow cache failures at `debug`.** A cache that fails silently looks identical to a slow one. L2 write failures log at `warning`.
- **Every derived-content cache key is the content hash of what it was derived from.** Never key by document ID or page number alone; that serves stale text after an edit. The scheme:
  - Blobs (`blobstore.py`): the blob's sha256, the same hash the cloud addresses it by. Bytes are verified on every read and write, and a blob whose bytes don't match is never served.
  - Page transcripts (`cache.get_page_transcript`, `index.page_ocr`): `(page_hash, backend)`, backend `transcript` (or legacy `sampling`), where `page_hash` is the sha256 of the page's `.rm` blob (`extract.get_page_hashes`); for a page over a PDF/EPUB it also folds in the source's sha256 and the source page index, since the render shows both. Editing one page drops only that page's transcript. No hash, no transcript. `index.clear()` keeps `page_ocr`: transcripts are agent work that cannot be rebuilt.
  - Per-page content (`reader.document_pages`): the document version hash. Downloads are keyed by document id and version hash.
  - Searchable pages (`pages`/FTS, one row per tablet page: `page` text and `ocr` transcripts) are not a cache. They are tied to `documents.doc_hash`, cleared when sync sees a new version (`mark_document_version`), and `remarkable_find` trusts a row only when the indexed hash equals the document's current hash.
  Shared in-memory caches are written from worker threads, so each one has a lock.
- **Sync is incremental and keeps what it can't refresh.** The client diffs the root index against the last known state (in memory and in `sync_documents` in the index DB) and fetches the blob index and metadata only for new or changed documents. If a document fails to fetch, it keeps its previous version (`Document.stale`), the sync is marked incomplete, and the next call retries it even when the root hash is unchanged. A fetch error never drops a known document (a new one that fails appears on the retry), and `download()` raises if any blob fails.
- **Config comes from `config.env_int`/`env_bool`.** Bare `int(os.environ[...])` at import time turns a typo in a client config into an unexplained startup crash.
- **`index.upsert_page`/`replace_document_pages` must ensure the parent row exists** — `pages` has a foreign key onto `documents`.
- **Page numbers are tablet pages.** Use `extract/pages.py`'s plan (via `extract/document.py`) for anything per page; an inserted page shifts the PDF pages after it.
- **Read failures raise `ToolError` with the cause and the next step.** No silent empty results or fallbacks.

## Writing to the library

`clients/organize.py` is the only code that mutates anything. The rules it relies on, all verified against the live API:

- **Every hash is `sha256` of the blob's exact bytes** — for file blobs, document indexes and the root index alike. `serialize_index` must therefore reproduce the server's formatting byte-for-byte; it is verified against real indexes in the tests.
- **Index header line**: `0:<owner>:<count>:<total_size>`, where owner is `.` for the root and the document UUID for a document index. Root entries use type `0`, `subfiles` = number of files, `size` = their total.
- **Uploads need both headers**: `rm-filename` (recognised extension) and `x-goog-hash: crc32c=<base64>`. Missing the checksum gives 400.
- **The root PUT is the only step that changes anything.** Upload everything first; a failure before the commit leaves the library untouched.
- **Always pass the generation you read.** It is the optimistic-concurrency token — without it a concurrent tablet edit is silently overwritten.
- **Never add a delete.** Nothing in this codebase removes documents; keep it that way unless explicitly asked.
- **Rename and move must not touch `lastModified`.** It means "when did I last work in this"; bumping it on a bulk reorganisation flattens the recently-used ordering for everything touched. Sync does not need it — the tablet sees the change because the document hash changed. Only `create_folder` sets a fresh timestamp.
- **File type comes from the blob list, not the name.** Most documents carry no extension in their visible name; a `.pdf`/`.epub` blob in `doc.files` is authoritative.

## Gotchas

- **Never use `git add -A`** — it can pick up sensitive files like `.mcpregistry_*` tokens. Always add specific files by name.
- **Claude Desktop needs full path to uvx** — `"command": "uvx"` fails because Desktop has a limited PATH. Use the absolute path (e.g. `/Users/you/.local/bin/uvx`).
- **uvx caching** — Always use `uvx rm-mcp@latest` (not `uvx --refresh rm-mcp`). The `--refresh` flag only refreshes the cache but does NOT upgrade if `rm-mcp` was installed as a persistent tool via `uv tool install`. The `@latest` suffix forces uvx to use the latest PyPI version in an ephemeral environment, bypassing persistent installs entirely.
- **PyPI trusted publishing** — configured via OIDC in the `pypi` GitHub environment. No API tokens needed. The workflow file is `publish.yml` and environment name is `pypi`.
