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

### ☁️ Cloud Mode

Uses the reMarkable Cloud API. Requires a reMarkable Connect subscription.

#### 1. Get a One-Time Code

Go to [my.remarkable.com/device/browser/connect](https://my.remarkable.com/device/browser/connect) and generate a code.

#### 2. Convert to Token

```bash
uvx rm-mcp --register YOUR_CODE
```

#### 3. Install

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install-0098FF?style=for-the-badge&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%22%2C%22password%22%3Atrue%7D%2C%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22rm-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%2C%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D)
[![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install-24bfa5?style=for-the-badge&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=remarkable&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22token%22%2C%22description%22%3A%22reMarkable%20API%20token%22%2C%22password%22%3Atrue%7D%2C%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22google_vision_api_key%22%2C%22description%22%3A%22Google%20Vision%20API%20Key%20(for%20handwriting%20OCR)%22%2C%22password%22%3Atrue%7D%5D&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22rm-mcp%22%5D%2C%22env%22%3A%7B%22REMARKABLE_TOKEN%22%3A%22%24%7Binput%3Atoken%7D%22%2C%22GOOGLE_VISION_API_KEY%22%3A%22%24%7Binput%3Agoogle_vision_api_key%7D%22%7D%7D&quality=insiders)

Or configure manually in `.vscode/mcp.json`:

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "remarkable-token",
      "description": "reMarkable API Token",
      "password": true
    },
    {
      "type": "promptString",
      "id": "google-vision-key",
      "description": "Google Vision API Key",
      "password": true
    }
  ],
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["rm-mcp"],
      "env": {
        "REMARKABLE_TOKEN": "${input:remarkable-token}",
        "GOOGLE_VISION_API_KEY": "${input:google-vision-key}"
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
| `remarkable_browse` | Navigate folders or search by document name |
| `remarkable_search` | Search content across multiple documents |
| `remarkable_recent` | Get recently modified documents |
| `remarkable_status` | Check connection status |
| `remarkable_image` | Get PNG/SVG images of pages (supports OCR via sampling) |

All tools are **read-only** and return structured JSON with hints for next actions.

📖 **[Full Tools Documentation](docs/tools.md)**

### Smart Features

- **Auto-redirect** — Browsing a document path returns its content automatically
- **Auto-OCR** — Notebooks with no typed text automatically enable OCR
- **Batch search** — Search across multiple documents in one call
- **Vision support** — Get page images for visual context (diagrams, mockups, sketches)
- **Sampling OCR** — Use client's AI for OCR on images (no API key needed)

### Example Usage

```python
# Read a document
remarkable_read("Meeting Notes")

# Search for keywords
remarkable_read("Project Plan", grep="deadline")

# Enable OCR for handwritten notes
remarkable_read("Journal", include_ocr=True)

# Browse your library
remarkable_browse("/Work/Projects")

# Search across documents
remarkable_search("meeting", grep="action items")

# Get recent documents
remarkable_recent(limit=10)

# Get a page image (for visual content like UI mockups or diagrams)
remarkable_image("UI Mockup", page=1)

# Get SVG for editing in design tools
remarkable_image("Wireframe", output_format="svg")

# Get image with OCR text extraction (uses sampling if configured)
remarkable_image("Handwritten Notes", include_ocr=True)

# Transparent background for compositing
remarkable_image("Logo Sketch", background="#00000000")

# Compatibility mode: return resource URI instead of embedded resource
remarkable_image("Diagram", compatibility=True)
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

For handwritten content, rm-mcp offers several OCR backends. Choose based on your setup and requirements:

| Backend | Setup | Quality | Offline | Best For |
|---------|-------|---------|---------|----------|
| **Sampling** | No API key | Depends on client model | ✅ | Users with capable AI clients |
| **Google Vision** | API key | Excellent | ❌ | Best handwriting accuracy |
| **Tesseract** | System install | Poor for handwriting | ✅ | Printed text, offline fallback |

### Quick Setup

Set `REMARKABLE_OCR_BACKEND` in your MCP config:

```json
{
  "env": {
    "REMARKABLE_OCR_BACKEND": "sampling"
  }
}
```

**Options:** `sampling`, `google`, `tesseract`, `auto`

<details>
<summary>📖 Sampling OCR (No API Key)</summary>

Uses your MCP client's AI model for OCR. Works with clients that support MCP sampling (VS Code + Copilot, Claude Desktop, etc.).

**Pros:**
- No additional API keys needed
- Quality depends on your client's model (GPT-4, Claude, etc.)
- Private — handwriting stays local to your client

**Cons:**
- Only available with sampling-capable clients
- Falls back to Google Vision (if API key configured) or Tesseract if sampling unavailable

</details>

<details>
<summary>📖 Google Cloud Vision</summary>

Provides consistently excellent handwriting recognition.

**Setup:**
1. Enable [Cloud Vision API](https://console.cloud.google.com/apis/library/vision.googleapis.com)
2. Create an [API key](https://console.cloud.google.com/apis/credentials)
3. Add to config: `"GOOGLE_VISION_API_KEY": "your-key"`

**Cost:** 1,000 free requests/month, then ~$1.50 per 1,000.

📖 **[Full Google Vision Setup Guide](docs/google-vision-setup.md)**

</details>

<details>
<summary>📖 Tesseract (Fallback)</summary>

Open-source OCR designed for printed text. Poor results with handwriting, but useful as an offline fallback.

```bash
# Install Tesseract
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt install tesseract-ocr

# Windows
choco install tesseract
```

</details>

### Default Behavior (`auto`)

When `REMARKABLE_OCR_BACKEND=auto` (default):
1. Google Vision (if `GOOGLE_VISION_API_KEY` is set)
2. Tesseract (fallback)

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
        "REMARKABLE_ROOT_PATH": "/Work",
        "GOOGLE_VISION_API_KEY": "your-api-key"
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
| [Google Vision Setup](docs/google-vision-setup.md) | Set up handwriting OCR |
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
