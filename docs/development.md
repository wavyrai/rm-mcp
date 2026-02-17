# Development Guide

This guide covers setting up a development environment for contributing to rm-mcp.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- A reMarkable tablet (for testing)

## Setup

```bash
# Clone the repository
git clone https://github.com/wavyrai/rm-mcp.git
cd rm-mcp

# Install dependencies (including dev extras)
uv sync --all-extras

# Verify setup
uv run pytest test_server.py -v
```

## Project Structure

```
rm-mcp/
├── server.py              # Entry point (backwards compatible)
├── rm_mcp/                # Main package
│   ├── __init__.py
│   ├── server.py          # FastMCP server initialization
│   ├── cli.py             # CLI (--setup, --register, server mode)
│   ├── api.py             # reMarkable Cloud API helpers
│   ├── models.py          # Document/Folder dataclasses
│   ├── paths.py           # Path utilities & root path filtering
│   ├── index.py           # SQLite FTS5 full-text search index
│   ├── cache.py           # Multi-layer caching
│   ├── responses.py       # Response formatting
│   ├── resources.py       # MCP resources
│   ├── prompts.py         # MCP prompts
│   ├── capabilities.py    # Client capability detection
│   ├── tools/             # Tool implementations
│   │   ├── read.py        # remarkable_read
│   │   ├── browse.py      # remarkable_browse
│   │   ├── search.py      # remarkable_search
│   │   ├── recent.py      # remarkable_recent
│   │   ├── image.py       # remarkable_image
│   │   └── status.py      # remarkable_status
│   ├── extract/           # Content extraction
│   │   ├── notebook.py    # .rm file parsing
│   │   ├── pdf.py         # PDF text extraction
│   │   ├── epub.py        # EPUB text extraction
│   │   └── render.py      # Page rendering (PNG/SVG)
│   ├── ocr/               # OCR backends
│   │   └── sampling.py    # MCP sampling-based OCR
│   └── clients/
│       └── cloud.py       # reMarkable Cloud API v3/v4 client
├── test_server.py         # Test suite
├── setup.sh               # One-line installer
├── server.json            # MCP Registry definition
├── pyproject.toml         # Project config and dependencies
├── docs/                  # Documentation
└── README.md
```

## Running Tests

```bash
# Run all tests
uv run pytest test_server.py -v

# Run specific test class
uv run pytest test_server.py -v -k "TestClassName"

# Run with coverage
uv run pytest test_server.py -v --cov=rm_mcp
```

Tests use `pytest-asyncio` for async testing. All async tests use the `@pytest.mark.asyncio` decorator.

## Code Quality

Before committing, always run:

```bash
# Lint (required - CI will fail without this)
uv run ruff check .

# Format (required - CI will fail without this)
uv run ruff format --check .

# Fix issues automatically
uv run ruff check . --fix
uv run ruff format .
```

## Git Workflow

**Always work on feature branches and submit PRs. Never push directly to main.**

```bash
# Create a feature branch
git checkout -b feature/my-feature

# After making changes — add specific files, never use git add -A
git add rm_mcp/tools/read.py test_server.py
git commit -m "feat: description of change"
git push origin feature/my-feature
# Then create PR via GitHub
```

**Important:** Never use `git add -A` or `git add .` — this can accidentally commit sensitive files like `.mcpregistry_*` tokens. Always add files by name.

## Adding a New Tool

1. Create a new file in `rm_mcp/tools/` (e.g. `rm_mcp/tools/my_tool.py`)
2. Register it in `rm_mcp/tools/__init__.py`
3. Create unique `ToolAnnotations` with a descriptive title
4. Add tests in `test_server.py`
5. Update the tools table in README.md
6. Update `docs/tools.md` with detailed documentation
7. Run tests: `uv run pytest test_server.py -v`

### Tool Design Principles

- **Intent-based design** — Tools should map to user intents, not API endpoints
- **XML-structured docstrings** — Use `<usecase>`, `<instructions>`, `<parameters>`, `<examples>` tags
- **Response hints** — Always include `_hint` field suggesting next actions
- **Educational errors** — Errors should explain what went wrong and how to fix it
- **Minimal tool count** — Prefer fewer, more capable tools over many simple ones

Example tool structure:

```python
EXAMPLE_ANNOTATIONS = ToolAnnotations(
    title="Descriptive Tool Name",  # Shown in VS Code
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
)

@mcp.tool(annotations=EXAMPLE_ANNOTATIONS)
def remarkable_example(param: str) -> str:
    """
    <usecase>Brief description of when to use this tool.</usecase>
    <instructions>
    Detailed instructions for the AI model on how to use this tool effectively.
    </instructions>
    <parameters>
    - param: Description of the parameter
    </parameters>
    <examples>
    - remarkable_example("value")
    </examples>
    """
```

## Making a Release

Releases are automated via GitHub Actions. The version is derived from the git tag.

1. Ensure all changes are merged to `main`
2. Ensure README.md and docs are current
3. Ensure CI is passing on `main`
4. Update version in `pyproject.toml` and `server.json`
5. Commit, tag, and push:

```bash
git tag v0.X.Y
git push && git push --tags
```

The workflow automatically:
- Creates a GitHub release with generated notes
- Runs tests and linting
- Builds the package with the tag version
- Publishes to PyPI (trusted publishing via OIDC)
- Publishes to MCP Registry (GitHub OIDC authentication)

## Key Dependencies

| Package | Purpose |
|---------|---------|
| `mcp` | Model Context Protocol SDK |
| `requests` | HTTP client for reMarkable Cloud API |
| `rmscene` | Native .rm file parser for text extraction |
| `pymupdf` | PDF text extraction |
| `ebooklib` | EPUB text extraction |
| `cairosvg` | SVG to PNG rendering |
| `Pillow` | Image processing |
| `beautifulsoup4` | HTML/XML parsing for EPUBs |
| `rmc` | reMarkable rendering tools |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `REMARKABLE_TOKEN` | Cloud API authentication token |
| `REMARKABLE_OCR_BACKEND` | OCR backend: `sampling` (default, uses client LLM) |
