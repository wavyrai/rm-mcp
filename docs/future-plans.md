# Future Plans & Ideas

This document outlines potential future features for rm-mcp. These are ideas under consideration, not commitments.

> **Track progress:** See open [enhancement issues](https://github.com/wavyrai/rm-mcp/issues?q=is%3Aissue+is%3Aopen+label%3Aenhancement) on GitHub.

### Write Support ([#24](https://github.com/wavyrai/rm-mcp/issues/24))

Currently, rm-mcp is read-only. Future versions may add:

- **Create documents** — Create new notebooks or upload PDFs
- **Sync from Obsidian** — Push markdown notes to reMarkable as PDFs
- **Template support** — Apply templates when creating notebooks
- **Folder management** — Create, rename, move folders

Write support requires careful consideration of:
- Sync conflicts with reMarkable's own sync
- Data safety and backup
- API stability

### Additional OCR Providers ([#25](https://github.com/wavyrai/rm-mcp/issues/25))

rm-mcp runs no OCR: the assistant reads page images from `remarkable_view` and saves transcripts with `remarkable_save_transcript`. MCP sampling OCR was removed in v2 (sampling is deprecated and unsupported by Claude clients).

| Provider | Status | Notes |
|----------|--------|-------|
| Agent transcripts | ✅ Implemented | The assistant transcribes from page images; stored per page content hash |
| **Microsoft Azure** | 🔮 Possible | Competitive handwriting OCR |
| **Local LLaVA** | 🔮 Possible | Fully offline, privacy-focused |

### Enhanced Search ([#26](https://github.com/wavyrai/rm-mcp/issues/26))

- ~~**Full-text indexing** — Index all documents for instant search~~ ✅ Implemented (SQLite FTS5)
- ~~**Cross-document search** — Search annotations across your entire library~~ ✅ Implemented
- **Semantic search** — Find documents by meaning, not just keywords

### Obsidian Integration

Deep integration with Obsidian vaults:

- **Bi-directional sync** — Notes flow between reMarkable and Obsidian
- **Link resolution** — reMarkable documents as Obsidian attachments
- **Daily notes** — Sync reMarkable journals to Obsidian daily notes

### Export Features ([#27](https://github.com/wavyrai/rm-mcp/issues/27))

- **PDF export** — Export notebooks as PDFs
- **Markdown export** — Convert notebooks to markdown
- **Batch export** — Export entire folders

## Community Requests

Have an idea? Open an issue on GitHub with the `enhancement` label.

Popular requests we're tracking:

1. **Handwriting-to-text conversion** — Beyond OCR, actual handwriting recognition
2. **Tag support** — Organize documents with tags
3. **Favorites** — Quick access to frequently-used documents
4. **Version history** — Access previous versions of documents

## Technical Improvements

### Performance ([#28](https://github.com/wavyrai/rm-mcp/issues/28))

- **Parallel resource registration** — Faster startup for large libraries
- ~~**Incremental sync** — Only fetch changed documents~~ ✅ Implemented (root hash change detection)
- ~~**Persistent cache** — Cache OCR results across sessions~~ ✅ Implemented (SQLite index)

### Reliability ([#29](https://github.com/wavyrai/rm-mcp/issues/29))

- ~~**Retry logic** — Handle transient API failures~~ ✅ Implemented (connection pooling & retry in cloud client)
- **Health checks** — Proactive connection monitoring

### Developer Experience

- **TypeScript types** — Full type definitions for MCP clients
- **Example integrations** — Sample code for common use cases
- **Plugin system** — Extensible architecture for custom features

## Contributing

Interested in implementing any of these features? We welcome contributions!

1. Check existing issues for the feature
2. Open a discussion if it's a major change
3. Fork, implement, and submit a PR

See [Development Guide](development.md) for setup instructions.

## Non-Goals

Some things we're explicitly **not** planning:

- **reMarkable firmware modifications** — We work with the official software
- **Bypassing DRM** — We respect content protection
- **Subscription circumvention** — Cloud API requires Connect subscription
- **Real-time sync** — We're a query tool, not a sync service
