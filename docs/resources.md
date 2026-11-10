# MCP Resources Reference

rm-mcp exposes two resource templates. Nothing is registered per document at
startup: each read looks the id up in the current library, so renames, moves
and edits show immediately, and `resources/list` stays empty.

| URI template | MIME type | Content |
|--------------|-----------|---------|
| `remarkable://doc/{id}` | `text/plain` | Every tablet page's text, with saved transcripts |
| `remarkable://doc/{id}/page/{n}.png` | `image/png` | Tablet page `n` at full size |

`{id}` is the document id that `remarkable_find` and `remarkable_list` return.
A path or a unique name also resolves. An unknown id is a "resource not found"
error whose message says how to find the document.

## `remarkable://doc/{id}`

```
# Standup
id: 5f0c6f0e-…
path: /Work/Standup

--- Page 1 ---
Meeting notes
Ship the fixture set

--- Page 2 ---
[handwriting, no transcript: see remarkable_view]
```

Each page carries its typed text and the PDF/EPUB text under it. A page with a
saved transcript adds it after `[transcript]`; a handwritten page without one
says so. The text is not capped, unlike `remarkable_read`.

## `remarkable://doc/{id}/page/{n}.png`

The whole tablet page as a PNG on the paper colour (`REMARKABLE_BACKGROUND_COLOR`,
default `#FBFBFB`): the notebook page, or the PDF/EPUB page with the ink on top.
Page numbers are tablet pages, so a page inserted into a PDF counts.
Completion suggests page numbers once the document's page count is known.

## Root path

With `REMARKABLE_ROOT_PATH` set, ids outside that folder are not found, the
same as in the tools.
