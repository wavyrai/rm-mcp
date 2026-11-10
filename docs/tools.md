# MCP Tools Reference

## Overview

| Tool | Purpose | Writes |
|------|---------|--------|
| [`remarkable_find`](#remarkable_find) | Search names, text, highlights and transcripts; no query lists recent documents | no |
| [`remarkable_list`](#remarkable_list) | The folder tree with ids, types, page counts and dates | no |
| [`remarkable_read`](#remarkable_read) | Text per tablet page | no |
| [`remarkable_view`](#remarkable_view) | Page images with the ink (PNG) | no |
| [`remarkable_annotations`](#remarkable_annotations) | Highlights and pages with handwriting | no |
| [`remarkable_save_transcript`](#remarkable_save_transcript) | Keep a transcript of a handwritten page | local index only |
| [`remarkable_status`](#remarkable_status) | Connection, index and settings | no |
| [`remarkable_send`](#remarkable_send) | Put a PDF, EPUB, URL or Markdown on the tablet | adds a document |
| [`remarkable_organize`](#remarkable_organize) | Rename, move, create folders as one batch (dry run first) | library structure |
| [`remarkable_undo`](#remarkable_undo) | Revert an organize batch | library structure |

Every tool except `remarkable_view` returns structured content and publishes an
`outputSchema`. Failures are MCP tool errors whose message names the cause and
the next step (for example "No document matches 'Quarterly Repot'. Did you mean
'Quarterly Report'? Search with remarkable_find(...)").

## Documents are addressed by id

Every document and folder has a stable id (a UUID) that survives renames and
moves. Every result carries it; pass it to the next tool. A path
(`/Work/Standup`) or a unique name (`Standup`) is also accepted and resolved.
When several documents share a name, the error lists each one's path and id.

## Pages are tablet pages

Page numbers are the ones the tablet shows. A page inserted on the tablet into
a PDF counts, and the PDF pages after it shift by one; `source_page` says which
PDF/EPUB page sits underneath (null for an inserted page).

## Handwriting and transcripts

Handwriting has no text until someone reads it. `remarkable_read` marks a
handwritten page `"handwriting": true, "transcript": null`. The agent looks at
it with `remarkable_view`, then stores what it read with
`remarkable_save_transcript`. From then on `remarkable_find` and
`remarkable_read` include the transcript. It is stored under the page's content
hash, so editing that page on the tablet drops it, and editing another page
keeps it.

rm-mcp does not run OCR itself. Earlier versions asked the client's model
through MCP sampling; that is deprecated and Claude clients do not support it,
so it was removed. Text saved by that OCR is still read as a transcript.

## Root path filtering

With `REMARKABLE_ROOT_PATH=/Work`, every tool sees only `/Work`, paths are
relative to it (`/Work/Project` appears as `/Project`), and ids outside it are
reported as not found.

---

## remarkable_find

Search the library, or list recent documents.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | none | Words to look for. Omit to list the most recently modified documents. |
| `folder` | string | none | Only documents under this folder (path or id). |
| `type` | `notebook` \| `pdf` \| `epub` | none | Only this kind of document. |
| `modified_after` | string | none | ISO date or datetime, e.g. `2026-11-01`. |
| `limit` | int | 20 | Maximum results (max 100). |

The query matches document names, typed text, PDF/EPUB text, highlights and
saved transcripts. Page text is indexed in the background at startup and
whenever a document is read; `index_coverage` says how much of the scope is
searchable.

```json
{
  "query": "zebras",
  "results": [
    {
      "id": "a0a0a0a0-0000-4000-8000-000000000001",
      "name": "Annotated paper",
      "path": "/Annotated paper",
      "type": "pdf",
      "modified": "2025-06-15T15:10:00+00:00",
      "page_count": 4,
      "hits": [{"page": 4, "snippet": "Page three: the last page mentions zebras.", "source": "text"}],
      "matched_on": ["text"]
    }
  ],
  "total": 1,
  "index_coverage": {"indexed": 5, "total": 5},
  "hint": "1 document(s) match 'zebras'. Read one with remarkable_read(id='a0a0…', pages='4')."
}
```

`source` is `text` (typed, PDF/EPUB text or highlights) or `transcript`.
`matched_on` lists `name`, `text` and/or `transcript`.

## remarkable_list

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `folder` | string | `/` | Folder path or id. |
| `depth` | int | 1 | 1 lists the folder; 2 also lists inside its subfolders; up to 10. |
| `sort` | `name` \| `modified` | `name` | Within each folder; folders come first. |
| `limit` | int | 100 | Items per call (max 500). |
| `cursor` | string | none | `next_cursor` from the previous call. |

Each item has `id`, `name`, `path`, `type` (`folder`, `notebook`, `pdf`,
`epub`), `modified`, `page_count` (known once read or indexed), `parent_id`,
`depth`, and for folders `item_count`.

## remarkable_read

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | string | required | Document id; a path or name also works. |
| `pages` | string | `1-5` | `3`, `1-5`, `2,4`, `7-` or `all`. |

```json
{
  "id": "n000…0001", "name": "Standup", "path": "/Work/Standup", "type": "notebook",
  "page_count": 2,
  "pages": [
    {"page": 1, "text": "Meeting notes\nShip the fixture set", "handwriting": false,
     "transcript": null, "source_page": null, "inserted": false, "truncated": false},
    {"page": 2, "text": "", "handwriting": true, "transcript": null,
     "source_page": null, "inserted": false, "truncated": false}
  ],
  "cut_pages": [],
  "hint": "Pages 1-2 of 2. Page(s) 2 have handwriting with no saved transcript: look at them with remarkable_view(...) ..."
}
```

`text` is the page's typed text plus the PDF/EPUB text under it. Output is
capped by `REMARKABLE_MAX_OUTPUT_CHARS` (default 50000): the page that crosses
the cap is truncated (`truncated: true`), the pages after it are listed in
`cut_pages`, and the hint says how to read them.

The 0.7 parameters `document`, `page` and `grep` still work for one release and
add a `deprecated` note to the result. `content_type="annotations"` now returns
an error pointing to `remarkable_annotations`.

## remarkable_view

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | string | required | Document id; a path or name also works. |
| `pages` | string | `1` | `1`, `2-3`, `1,4`. |
| `size` | `normal` \| `large` | `normal` | `normal` is 60% of the device resolution; `large` is full size (1404x1872 for a notebook page). |

Returns one text block, then one PNG `ImageContent` per page. Each image is the
whole page: the notebook page, or the PDF/EPUB page with the ink on top. The
background is opaque paper colour (`REMARKABLE_BACKGROUND_COLOR`, default
`#FBFBFB`), so ink stays visible in dark-mode clients.

At most 4 pages and about 1 MB of image data are returned per call, because
images count against the client's result budget (Claude Code: 25k tokens). A
page that does not fit is first rendered smaller; pages that still do not fit
are named in the text block with the call that fetches them. The text block
also tells the agent it can transcribe the page and save it with
`remarkable_save_transcript`.

## remarkable_annotations

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | string | required | Document id; a path or name also works. |
| `pages` | string | all | Limit to these pages. |

Returns `highlights` (`{page, text}`) from both formats: in-page highlights
(glyph ranges in the `.rm` file, current firmware) and the older per-page
`.highlights/<page>.json` files. `ink_pages` lists the pages with handwriting or
drawings: `{page, source_page, inserted, transcript_saved}`. Highlighter strokes
and erasers do not count as handwriting.

## remarkable_save_transcript

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Document id; a path or name also works. |
| `page` | int | Tablet page number. |
| `text` | string | What the page says, plain text or Markdown. |

Annotated `readOnlyHint: false`, `destructiveHint: false`. It writes only to
the local index (`REMARKABLE_INDEX_PATH`); nothing on the tablet or in the
cloud changes. Saving again replaces the transcript. A blank page (no ink and
nothing underneath) cannot take one.

## remarkable_status

No parameters. Returns `authenticated`, `connection` (where the token came
from), `document_count`, `index` (path, documents, pages, size),
`index_coverage`, `config` (root path, background colour, cache TTL, output
cap, read-only, where writes may go) and, when not connected, `error` and a `hint` with the fix. It
never fails.

---

## Writing: scope, guard and what never happens

No tool deletes anything, and every write is one root commit: the library
changes completely or not at all.

**Write scope**, enforced in code before anything is uploaded:

- `REMARKABLE_READ_ONLY=1` refuses every write (send, organize, including its
  dry run, and undo).
- `REMARKABLE_WRITE_ROOT=/Some/Folder` confines writes to that folder (a full
  library path). Every source and destination of every change must be inside
  it, or the whole batch is refused. It must lie inside `REMARKABLE_ROOT_PATH`.
- Unset, the write root is `REMARKABLE_ROOT_PATH`. With neither set, writes may
  go anywhere, and a location without a leading `/` (or none, for
  `remarkable_send`) means the folder `/rm-mcp`, created when needed.

`remarkable_status` shows the scope under `config.writes`.

**Root guard.** Before the root update, the old and new root index are parsed
and compared: only the new document/folder ids may appear, and only the
touched ids may change hash. Anything else aborts with nothing committed. The
commit names the generation it read, so a concurrent change from the tablet is
retried from a fresh read (three attempts) instead of being overwritten, and a
change whose document moved on since it was planned is refused.

## remarkable_send

`remarkable_send(source, folder?, title?)`. `source` is one of:

- a local file path: `.pdf`, `.epub` (checked by content), or `.md`/`.markdown`
  (typeset like Markdown text);
- an `http(s)` URL to a PDF or EPUB: at most 100 MB, the content type must be a
  PDF/EPUB (or generic binary) type and the bytes must be one; hosts that
  resolve to private, loopback or link-local addresses are refused, also after
  a redirect;
- Markdown text, typeset into a PDF the shape of the tablet's screen
  (157 x 210 mm) with headings, lists, code blocks, tables and live links. Raw
  HTML in the text is shown as text.

`folder` is a path (`/A/B` from the top, or `B` under the default write folder);
missing folders inside the write scope are created in the same commit. `title`
defaults to the file name or the first heading. The upload is the blob set
rmapi uses: `<id>.pdf`/`<id>.epub`, `<id>.content` (fileType, pageCount),
`<id>.metadata`, and the document index. Returns the new `id`, `path`, `type`,
`page_count`, `size_bytes`, `created_folders` and `previous_root`. There is no
undo for a send (it would mean deleting); remove the document on the tablet.

## remarkable_organize

`remarkable_organize(changes, dry_run=true)`. Each change is one of:

- `{"op": "rename", "item": <id|path|name>, "name": "New name"}`
- `{"op": "move", "item": <id|path|name>, "to": <folder id|path|name, "/" = top>}`
- `{"op": "create_folder", "path": "/A/New"}` (the parent must exist) or
  `"New"` (under the default write folder, created if missing)

Changes apply in order, so a batch can create a folder and move things into
it. An item may be changed once per batch. With `dry_run=true` (the default)
the result is the exact plan, `changes: [{op, id, kind, before, after}]`, and
nothing is written. With `dry_run=false` the same plan is applied in one
commit and the result carries `undo_token` and `previous_root` (the root hash
replaced, a complete snapshot of the library). Rename and move never touch
`lastModified`.

## remarkable_undo

`remarkable_undo(token)` reverts an applied organize batch: each renamed or
moved item is pointed back at its earlier version, in one commit, but only if
none of them changed since; otherwise nothing changes. Folders the batch
created stay (undo never deletes) and are listed in `left_in_place`. A token
works once. Tokens live in the local index database.

---

## Deprecated aliases

Kept for one release. Their titles end in "(deprecated)" and each result says
which tool replaces it.

| Old tool | Use instead | Notes |
|----------|-------------|-------|
| `remarkable_search(query, ...)` | `remarkable_find(query)` | `grep` and `include_ocr` are ignored. |
| `remarkable_recent(limit, ...)` | `remarkable_find()` | `include_preview` is ignored. |
| `remarkable_browse(path)` | `remarkable_list(folder)` | A document path returns its first page, as before. |
| `remarkable_image(document, page, ...)` | `remarkable_view(id, pages)` | PNG only; `output_format`, `background`, `compatibility` and `include_ocr` are ignored. |
| `remarkable_rename(item, new_name)` | `remarkable_organize` (`op: rename`) | Applied at once; returns an `undo_token`. |
| `remarkable_move(item, destination)` | `remarkable_organize` (`op: move`) | Applied at once; returns an `undo_token`. |
| `remarkable_create_folder(path)` | `remarkable_organize` (`op: create_folder`) | A path without a leading `/` now goes under the default write folder. |
