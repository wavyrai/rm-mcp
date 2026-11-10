"""Structured output of the read tools (published as each tool's outputSchema)."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Doc(BaseModel):
    id: str = Field(description="Stable document id (UUID); pass it to the other tools.")
    name: str
    path: str = Field(description="Path from the library root (or REMARKABLE_ROOT_PATH).")
    type: str = Field(description="notebook, pdf, epub or folder.")
    modified: Optional[str] = Field(None, description="Last modified on the tablet, ISO 8601.")
    page_count: Optional[int] = Field(None, description="Tablet pages, once the document is read.")


class Hit(BaseModel):
    page: int = Field(description="Tablet page number (inserted pages count).")
    snippet: str
    source: Literal["text", "transcript"] = Field(
        description="text: typed text, PDF/EPUB text or highlights; transcript: a saved transcript."
    )


class FoundDoc(Doc):
    hits: List[Hit]
    matched_on: List[str] = Field(description="Any of name, text, transcript.")


class Coverage(BaseModel):
    indexed: int = Field(description="Documents in scope whose current version is searchable.")
    total: int


class FindResult(BaseModel):
    query: Optional[str]
    results: List[FoundDoc]
    total: int = Field(description="Matching documents before the limit.")
    index_coverage: Coverage
    hint: str


class ListItem(Doc):
    parent_id: Optional[str]
    depth: int = Field(description="1 for the folder's own items, 2 for one level down, ...")
    item_count: Optional[int] = Field(None, description="For folders: items directly inside.")


class FolderRef(BaseModel):
    id: Optional[str] = Field(description="None for the top of the library.")
    path: str


class ListResult(BaseModel):
    folder: FolderRef
    items: List[ListItem]
    total: int
    next_cursor: Optional[str] = Field(description="Pass as cursor for the next page; None at end.")
    hint: str


class PageText(BaseModel):
    page: int = Field(description="Tablet page number.")
    text: str = Field(description="Typed text plus the PDF/EPUB text of the page.")
    handwriting: bool = Field(description="The page has pen ink (handwriting or drawing).")
    transcript: Optional[str] = Field(
        description="Saved transcript of the handwriting; None if none was saved."
    )
    source_page: Optional[int] = Field(description="PDF/EPUB page underneath, if any.")
    inserted: bool = Field(description="A notebook page inserted into a PDF/EPUB on the tablet.")
    truncated: bool = False


class ReadResult(Doc):
    pages: List[PageText]
    cut_pages: List[int] = Field(description="Requested pages left out by the output cap.")
    hint: str
    deprecated: Optional[str] = None


class Highlight(BaseModel):
    page: int
    text: str


class InkPage(BaseModel):
    page: int
    source_page: Optional[int]
    inserted: bool
    transcript_saved: bool


class AnnotationsResult(Doc):
    highlights: List[Highlight]
    ink_pages: List[InkPage] = Field(description="Pages with handwriting or drawings.")
    hint: str


class SavedTranscript(Doc):
    page: int
    saved: bool
    chars: int
    note: str


class IndexStats(BaseModel):
    path: str
    documents: int
    pages: int
    size_bytes: int


class StatusConfig(BaseModel):
    root_path: str
    background_color: str
    cache_ttl_seconds: int
    max_output_chars: int
    read_only: bool
    writes: str = Field("", description="Where the write tools may write.")


class StatusResult(BaseModel):
    authenticated: bool
    connection: str = Field(description="Where the token came from.")
    document_count: Optional[int] = None
    index: Optional[IndexStats] = None
    index_coverage: Optional[Coverage] = None
    config: StatusConfig
    error: Optional[str] = None
    hint: str


# --- write tools ---


class Change(BaseModel):
    """One change in a remarkable_organize batch."""

    op: Literal["rename", "move", "create_folder"]
    item: Optional[str] = Field(
        None, description="rename/move: the document or folder (id, path or unique name)."
    )
    name: Optional[str] = Field(None, description="rename: the new name (not a path).")
    to: Optional[str] = Field(
        None, description="move: destination folder (id, path or unique name; '/' = top)."
    )
    path: Optional[str] = Field(
        None,
        description="create_folder: '/A/New' (parent must exist) or 'New' "
        "(under the default write folder, created if missing).",
    )


class PlannedChange(BaseModel):
    op: str
    id: str = Field(description="The item's id (for a new folder, the id it will get).")
    kind: str = Field(description="folder or document.")
    before: Optional[str] = Field(description="Path before; None for something new.")
    after: str = Field(description="Path after.")


class OrganizeResult(BaseModel):
    dry_run: bool
    applied: bool
    changes: List[PlannedChange]
    undo_token: Optional[str] = Field(None, description="Pass to remarkable_undo to revert.")
    previous_root: Optional[str] = Field(
        None, description="Root hash the batch replaced (a complete snapshot of the library)."
    )
    hint: str


class SendResult(BaseModel):
    id: str
    name: str
    path: str
    type: str = Field(description="pdf or epub.")
    page_count: int
    size_bytes: int
    source: str = Field(description="file, url or markdown.")
    created_folders: List[str]
    previous_root: str
    hint: str


class UndoResult(BaseModel):
    token: str
    reverted: List[PlannedChange] = Field(
        description="Each item, from where it is to where it was."
    )
    left_in_place: List[str] = Field(
        description="Folders the batch created; undo never deletes, so they stay."
    )
    previous_root: str
    root: str
    hint: str
