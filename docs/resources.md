# MCP Resources Reference

Documents in your reMarkable library are automatically registered as MCP resources, allowing AI assistants to access them directly.

## Resource Types

| URI Scheme | Description |
|------------|-------------|
| `remarkable:///` | Your annotations, typed text, and handwriting |
| `remarkableimg:///` | PNG page images (notebooks) |
| `remarkablesvg:///` | SVG page images (notebooks) |

## Text Resources (`remarkable:///`)

Every document is registered as a text resource with its full path.

### URI Format

```
remarkable:///{path}.txt
```

### Examples

```
remarkable:///Meeting%20Notes.txt
remarkable:///Work/Projects/Q4%20Planning.txt
remarkable:///Journals/November.txt
```

### What's Extracted

| Document Type | Content |
|---------------|---------|
| **PDF** | Your annotations, highlights, and typed notes |
| **EPUB** | Your annotations, highlights, and typed notes |
| **Notebook** | Typed text, highlights, and handwritten content (OCR) |

**Note:** Text resources contain only user-created content—not the original PDF/EPUB text. OCR is automatically applied for notebooks with handwritten content.

### Response

Text resources return the extracted content as plain text:

```
Meeting Notes - November 28, 2025

Attendees: Alice, Bob, Charlie

Action Items:
- Review Q4 targets
- Schedule follow-up
...
```

## Image Resources (`remarkableimg:///` and `remarkablesvg:///`)

Notebook pages are available as image resources in both PNG and SVG formats.

### URI Format

```
remarkableimg:///{path}.page-{N}.png
remarkablesvg:///{path}.page-{N}.svg
```

Where `{N}` is the 1-indexed page number.

### Examples

```
remarkableimg:///UI%20Mockup.page-1.png
remarkableimg:///Work/Diagrams/Architecture.page-3.png
remarkablesvg:///Wireframe.page-1.svg
remarkablesvg:///Sketches/Logo.page-2.svg
```

### Response

- **PNG resources** return binary image data with the standard reMarkable paper background color (`#FBFBFB`)
- **SVG resources** return SVG XML content with the same background color

### Use Cases

- **Visual context**: View hand-drawn diagrams, sketches, or UI mockups
- **Design implementation**: Convert wireframes to code
- **SVG editing**: Import vector graphics into design tools for further editing
- **Documentation**: Include notebook drawings in documents

### Notes

- Only available for notebooks (not PDFs or EPUBs)
- For PDFs/EPUBs, the annotation layer would be rendered (not the underlying document)
- Use the `remarkable_image` tool for more control (custom backgrounds, transparent output)

## How Resources Are Registered

On server startup, rm-mcp:

1. Connects to your reMarkable via the Cloud API
2. Fetches the document list
3. Registers each document as an MCP resource

Resources are registered once at startup. If you add new documents, restart the MCP server to pick them up.

## Using Resources

### In Claude Desktop

Resources appear in Claude's context when you mention them or use the "Attach" feature.

### In VS Code

MCP resources can be accessed through the Copilot chat interface. The screenshot below shows resources appearing with the `mcpr` prefix:

![Resources in VS Code](assets/resources-screenshot.png)

### Programmatically

MCP clients can request resources by URI:

```python
# Request a text resource (includes annotations)
content = await client.read_resource("remarkable:///Meeting%20Notes.txt")
```

## Path Encoding

Paths in URIs must be URL-encoded:

| Character | Encoded |
|-----------|---------|
| Space | `%20` |
| `/` | `%2F` (in filename only) |
| `&` | `%26` |

Examples:
- `Meeting Notes` → `Meeting%20Notes`
- `/Work/Q4 Report` → `/Work/Q4%20Report`

## Filtering

Archived documents and trash items are **not** registered as resources. Only documents that are actively synced appear.

### Root Path Filtering

When `REMARKABLE_ROOT_PATH` is configured, only documents within that folder are registered as resources. Paths in URIs are relative to the root:

```json
{
  "env": {
    "REMARKABLE_ROOT_PATH": "/Work"
  }
}
```

With this configuration:
- `/Work/Meeting Notes` → `remarkable:///Meeting%20Notes.txt`
- Documents outside `/Work` are not registered

## Performance Considerations

- Resources are registered at startup (slight delay for large libraries)
- Text extraction happens on-demand when a resource is accessed
- Results are cached per session
