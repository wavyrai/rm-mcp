# reMarkable MCP Server

Unlock the full potential of your reMarkable tablet as a **second brain** for AI assistants. This MCP server lets Claude, VS Code Copilot, and other AI tools read, search, and traverse your entire reMarkable library — including handwritten notes via OCR.

<!-- mcp-name: io.github.wavyrai/rm-mcp -->

## Why rm-mcp?

Your reMarkable tablet is a powerful tool for thinking, note-taking, and research. But that knowledge stays trapped on the device. This MCP server changes that:

- **Full library access** — Browse folders, search documents, read any file
- **Typed text extraction** — Native support for Type Folio and typed annotations
- **Handwriting OCR** — Convert handwritten notes to searchable text
- **PDF & EPUB support** — Extract text from documents, plus your annotations
- **Smart search** — Find content across your entire library
- **Second brain integration** — Use with Obsidian, note-taking apps, or any AI workflow

Whether you're researching, writing, or developing ideas, rm-mcp lets you leverage everything on your reMarkable through AI.

---

## Quick Install

Uses the reMarkable Cloud API. Requires a reMarkable Connect subscription.

### One-command setup (recommended)

```bash
uvx rm-mcp --setup
```

This opens your browser, prompts for the one-time code, and prints the ready-to-paste config for Claude Code and Claude Desktop.

### Manual setup

#### 1. Get a One-Time Code

Go to [my.remarkable.com/device/browser/connect](https://my.remarkable.com/device/apps/connect) and generate a code.

#### 2. Convert to Token

```bash
uvx rm-mcp --register YOUR_CODE
```

#### 3. Add to your MCP client

**Claude Code:**

```bash
claude mcp add remarkable \
  -e REMARKABLE_TOKEN='<paste token from step 2>' \
  -e REMARKABLE_OCR_BACKEND=sampling \
  -- uvx --refresh rm-mcp
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "remarkable": {
      "command": "uvx",
      "args": ["--refresh", "rm-mcp"],
      "env": {
        "REMARKABLE_TOKEN": "<paste token from step 2>"
      }
    }
  }
}
```

---

<!-- Screenshots section - uncomment when screenshots are added
## Screenshots

### MCP Resources

Documents appear as resources that AI assistants can access directly:

![Resources in VS Code](docs/assets/resources-screenshot.png)

### Tool Calls in Action

AI assistants use the tools to read documents, search content, and more:

![Tool calls in VS Code](docs/assets/tool-calls-screenshot.png)
-->

---

## Tools

| Tool | Description |
|------|-------------|
| `remarkable_read` | Read and extract text from documents (with pagination and search) |
| `remarkable_browse` | Navigate folders in your library |
| `remarkable_search` | Search content across multiple documents |
| `remarkable_recent` | Get recently modified documents |
| `remarkable_status` | Check connection status |
| `remarkable_image` | Get PNG/SVG images of pages (supports OCR via sampling) |

All tools are **read-only** and return structured JSON with hints for next actions.

📖 **[Full Tools Documentation](docs/tools.md)**

### Smart Features

- **Multi-page read** — Read all pages at once with `pages="all"`, or a range like `pages="1-3"`
- **Grep auto-redirect** — `grep` automatically finds and jumps to the matching page
- **Auto-redirect** — Browsing a document path returns its content automatically
- **Auto-OCR** — Notebooks with no typed text automatically enable OCR (opt out with `auto_ocr=False`)
- **Full-text search** — Reading a document indexes it for fast future searches
- **Compact mode** — Use `compact_output=True` to reduce token usage in responses
- **Batch search** — Search across multiple documents in one call
- **Vision support** — Get page images for visual context (diagrams, mockups, sketches)
- **Sampling OCR** — Use client's AI for OCR on images (no API key needed)

### Example Usage

```python
# Read a document
remarkable_read("Meeting Notes")

# Read all pages at once
remarkable_read("Meeting Notes", pages="all")

# Read a range of pages
remarkable_read("Research Paper", pages="1-3")

# Search for keywords (auto-redirects to matching page)
remarkable_read("Project Plan", grep="deadline")

# Enable OCR for handwritten notes
remarkable_read("Journal", include_ocr=True)

# Browse your library
remarkable_browse("/Work/Projects")

# Search across documents
remarkable_search("meeting", grep="action items")

# Get recent documents with previews
remarkable_recent(limit=5, include_preview=True)

# Get a page image
remarkable_image("UI Mockup", page=1)

# Get image with OCR text extraction
remarkable_image("Handwritten Notes", include_ocr=True)
```

---

## Resources

Documents are automatically registered as MCP resources:

| URI Scheme | Description |
|------------|-------------|
| `remarkable:///{path}.txt` | Extracted text content |
| `remarkableimg:///{path}.page-{N}.png` | PNG image of page N (notebooks only) |
| `remarkablesvg:///{path}.page-{N}.svg` | SVG vector image of page N (notebooks only) |

📖 **[Full Resources Documentation](docs/resources.md)**

---

## OCR for Handwriting

rm-mcp uses **sampling OCR** — your MCP client's AI model extracts text from handwritten notes. No additional API keys or services needed.

### How It Works

When you use `include_ocr=True`, rm-mcp sends page images to your client's LLM (Claude, GPT-4, etc.) via MCP sampling. The model reads the handwriting and returns the text.

### Usage

```python
# OCR on a page image
remarkable_image("Handwritten Notes", include_ocr=True)

# OCR when reading a notebook
remarkable_read("Journal", include_ocr=True)
```

### Requirements

- Your MCP client must support the **sampling** capability (VS Code + Copilot, Claude Desktop, etc.)
- `REMARKABLE_OCR_BACKEND=sampling` (this is the default)

---

## Advanced Configuration

### Root Path Filtering

Limit the MCP server to a specific folder on your reMarkable. All operations will be scoped to this folder:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["rm-mcp"],
      "env": {
        "REMARKABLE_TOKEN": "your-token",
        "REMARKABLE_ROOT_PATH": "/Work"
      }
    }
  }
}
```

With this configuration:
- `remarkable_browse("/")` shows contents of `/Work`
- `remarkable_browse("/Projects")` shows `/Work/Projects`
- Documents outside `/Work` are not accessible

Useful for:
- Focusing on work documents during office hours
- Separating personal and professional notes
- Limiting scope for specific AI workflows

### Custom Background Color

Set the default background color for image rendering:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["rm-mcp"],
      "env": {
        "REMARKABLE_TOKEN": "your-token",
        "REMARKABLE_BACKGROUND_COLOR": "#FFFFFF"
      }
    }
  }
}
```

Supported formats:
- `#RRGGBB` — RGB hex (e.g., `#FFFFFF` for white)
- `#RRGGBBAA` — RGBA hex (e.g., `#00000000` for transparent)

Default is `#FBFBFB` (reMarkable paper color). This affects both the `remarkable_image` tool and image resources.

### All Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REMARKABLE_TOKEN` | *(required)* | Auth token from `uvx rm-mcp --setup` |
| `REMARKABLE_ROOT_PATH` | `/` | Limit access to a specific folder |
| `REMARKABLE_OCR_BACKEND` | `sampling` | OCR backend (`sampling`) |
| `REMARKABLE_BACKGROUND_COLOR` | `#FBFBFB` | Background color for rendered images (`#RRGGBB` or `#RRGGBBAA`) |
| `REMARKABLE_CACHE_TTL` | `60` | Collection cache TTL in seconds |
| `REMARKABLE_COMPACT` | *(off)* | Set to `1` or `true` to omit hints from responses globally |
| `REMARKABLE_MAX_OUTPUT_CHARS` | `50000` | Maximum characters in tool responses |
| `REMARKABLE_PAGE_SIZE` | `8000` | PDF/EPUB page size in characters |
| `REMARKABLE_PARALLEL_WORKERS` | `5` | Parallel workers for metadata fetching |
| `REMARKABLE_INDEX_PATH` | `~/.cache/rm-mcp/index.db` | SQLite full-text search index location |
| `REMARKABLE_INDEX_REBUILD` | *(off)* | Set to `1` to force index rebuild on startup |

Most users only need `REMARKABLE_TOKEN`. The rest are for advanced tuning.

---

## Use Cases

### Research & Writing

Use rm-mcp while working in an Obsidian vault or similar to transfer knowledge from your handwritten notes into structured documents. AI can read your research notes and help develop your ideas.

### Daily Review

Ask your AI assistant to summarize your recent notes, find action items, or identify patterns across your journal entries.

### Document Search

Find that half-remembered note by searching across your entire library — including handwritten content.

### Knowledge Management

Treat your reMarkable as a second brain that AI can access. Combined with tools like Obsidian, you can build a powerful personal knowledge system.

---

## Documentation

| Guide | Description |
|-------|-------------|
| [Tools Reference](docs/tools.md) | Detailed tool documentation |
| [Resources Reference](docs/resources.md) | MCP resources documentation |
| [Capability Negotiation](docs/capabilities.md) | MCP protocol capabilities |
| [Development](docs/development.md) | Contributing and development setup |
| [Future Plans](docs/future-plans.md) | Roadmap and planned features |

---

## Development

```bash
git clone https://github.com/wavyrai/rm-mcp.git
cd rm-mcp
uv sync --all-extras
uv run pytest test_server.py -v
```

📖 **[Development Guide](docs/development.md)**

---

## License

MIT

---

Built with [rmscene](https://github.com/ricklupton/rmscene), [PyMuPDF](https://pymupdf.readthedocs.io/), and inspiration from [ddvk/rmapi](https://github.com/ddvk/rmapi).
