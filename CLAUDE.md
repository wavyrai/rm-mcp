# CLAUDE.md

## Project overview

rm-mcp is an MCP (Model Context Protocol) server that gives AI assistants read-only access to a reMarkable tablet's library via the reMarkable Cloud API. It supports notebooks, PDFs, EPUBs, handwriting OCR (via MCP sampling), and full-text search.

- **Package name:** `rm-mcp` (PyPI), `io.github.wavyrai/rm-mcp` (MCP Registry)
- **Python:** >=3.10, built with Hatchling
- **Package manager:** uv (lockfile: `uv.lock`)
- **Entry point:** `rm_mcp/cli.py` → `rm-mcp` CLI command

## Repository layout

```
rm_mcp/                   # Main package
  server.py               # FastMCP server subclass & initialization
  cli.py                  # CLI (--setup, --register, or start server)
  api.py                  # Singleton reMarkable Cloud client
  models.py               # Document/Folder dataclasses
  paths.py                # Path utilities & root path filtering
  index.py                # SQLite FTS5 full-text search index
  cache.py                # Multi-layer caching (memory → SQLite → Cloud)
  resources.py            # MCP resource registration
  prompts.py              # MCP prompt templates
  capabilities.py         # Client capability detection
  tools/                  # Tool implementations (one file per tool)
    read.py, browse.py, search.py, recent.py, image.py, status.py
  extract/                # Content extraction
    notebook.py, pdf.py, epub.py, render.py
  ocr/                    # OCR backends
    sampling.py           # Uses client LLM via MCP sampling (only backend)
  clients/
    cloud.py              # reMarkable Cloud API v3/v4 client
test_server.py            # Test suite (pytest + pytest-asyncio)
setup.sh                  # One-line installer script
server.json               # MCP Registry server definition
server.py                 # Root-level backwards-compatible entry point
docs/                     # Documentation (tools, resources, capabilities, dev guide)
.github/workflows/        # CI/CD (ci.yml, publish.yml, conformance.yml)
```

## Common commands

```bash
uv sync --all-extras            # Install all dependencies
uv run pytest test_server.py -v # Run tests
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

Steps: install deps (Homebrew, Cairo, uv, Claude Code) → register reMarkable tablet → configure Claude Code → configure Claude Desktop (macOS only).

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
| `REMARKABLE_OCR_BACKEND` | No | OCR backend, default `sampling` (only supported value) |
| `REMARKABLE_ROOT_PATH` | No | Restrict to a folder (e.g. `/Work`) |
| `REMARKABLE_BACKGROUND_COLOR` | No | Image background, default `#FBFBFB` |
| `REMARKABLE_CACHE_TTL` | No | Collection cache TTL in seconds, default `60` |
| `REMARKABLE_INDEX_PATH` | No | FTS index location, default `~/.cache/rm-mcp/index.db` |
| `REMARKABLE_COMPACT` | No | Set `1` or `true` to omit hints globally |
| `REMARKABLE_MAX_OUTPUT_CHARS` | No | Max tool response size, default `50000` |
| `REMARKABLE_PAGE_SIZE` | No | PDF/EPUB page size in chars, default `8000` |
| `REMARKABLE_PARALLEL_WORKERS` | No | Parallel metadata fetch workers, default `5` |

## Code style

- Line length: 100 (ruff)
- Ruff rules: E, F, W, I
- Async: all tool handlers are async, pytest uses `asyncio_mode = "auto"`
- All tools are read-only and idempotent

## Gotchas

- **Never use `git add -A`** — it can pick up sensitive files like `.mcpregistry_*` tokens. Always add specific files by name.
- **Claude Desktop needs full path to uvx** — `"command": "uvx"` fails because Desktop has a limited PATH. Use the absolute path (e.g. `/Users/you/.local/bin/uvx`).
- **uvx caching** — Always use `uvx rm-mcp@latest` (not `uvx --refresh rm-mcp`). The `--refresh` flag only refreshes the cache but does NOT upgrade if `rm-mcp` was installed as a persistent tool via `uv tool install`. The `@latest` suffix forces uvx to use the latest PyPI version in an ephemeral environment, bypassing persistent installs entirely.
- **PyPI trusted publishing** — configured via OIDC in the `pypi` GitHub environment. No API tokens needed. The workflow file is `publish.yml` and environment name is `pypi`.
