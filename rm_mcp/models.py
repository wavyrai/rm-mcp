"""
Shared data models for reMarkable MCP.

Contains the unified Document dataclass and the RemarkableClientProtocol interface.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class RemarkableClientProtocol(Protocol):
    """Protocol defining the interface for reMarkable API clients."""

    def get_meta_items(self, limit: Optional[int] = None, **kwargs) -> list: ...
    def download(self, doc) -> bytes: ...


@dataclass
class Document:
    """Represents a document or folder in the reMarkable system."""

    id: str
    hash: str
    name: str
    doc_type: str  # "DocumentType" or "CollectionType"
    parent: str = ""
    deleted: bool = False
    pinned: bool = False
    last_modified: Optional[datetime] = None
    size: int = 0
    files: List[Dict[str, Any]] = field(default_factory=list)
    synced: bool = True  # False means cloud-archived (not on device)

    @property
    def is_folder(self) -> bool:
        return self.doc_type == "CollectionType"

    @property
    def is_cloud_archived(self) -> bool:
        """True if document is archived to cloud (not on device)."""
        return not self.synced or self.parent == "trash"

    @property
    def visible_name(self) -> str:
        """Return the document's visible name."""
        return self.name

    @property
    def VissibleName(self) -> str:
        """Compatibility with rmapy naming (kept for backward compatibility)."""
        return self.name

    @property
    def ID(self) -> str:
        """Compatibility with rmapy naming."""
        return self.id

    @property
    def Parent(self) -> str:
        """Compatibility with rmapy naming."""
        return self.parent

    @property
    def Type(self) -> str:
        """Compatibility with rmapy naming."""
        return self.doc_type

    @property
    def ModifiedClient(self) -> Optional[datetime]:
        """Compatibility with rmapy naming."""
        return self.last_modified


# Alias for backward compatibility
Folder = Document
