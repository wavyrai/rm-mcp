"""
Where the writing tools may write.

The scope is enforced here, in code, for every source and destination of every
change, before anything is uploaded. Settings:

- ``REMARKABLE_READ_ONLY=1`` refuses every write (send, organize, undo).
- ``REMARKABLE_WRITE_ROOT=/Some/Folder`` (a full library path) confines all
  writes to that folder and what is inside it. It must lie inside
  ``REMARKABLE_ROOT_PATH`` when that is set, or every write is refused.
- Unset, the write root is ``REMARKABLE_ROOT_PATH``. When that is unset too,
  writes may go anywhere, and a location given without a leading ``/`` (or no
  location at all, for ``remarkable_send``) lands in ``/rm-mcp``.

Paths here are full library paths (from the top of the account), compared
case-insensitively the way the tablet's own folders are.
"""

from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError

from rm_mcp.config import env_bool
from rm_mcp.paths import _get_root_path, _is_within_root

DEFAULT_FOLDER = "/rm-mcp"


class WriteRefused(ToolError):
    """A write is outside what the configuration allows. Nothing was uploaded."""

    def __init__(self, message: str, kind: str = "outside_write_root"):
        super().__init__(message)
        self.kind = kind


def normalize(path: str) -> str:
    path = "/" + path.strip().strip("/")
    return path if path != "/" else "/"


@dataclass(frozen=True)
class WriteScope:
    root: str  # full path writes are confined to ("/" = anywhere)
    configured: bool  # REMARKABLE_WRITE_ROOT was set
    default_folder: str  # full path for locations given without a leading "/"

    def allows(self, full_path: str) -> bool:
        return _is_within_root(full_path, self.root)

    def check(self, full_path: str, what: str) -> None:
        if not self.allows(full_path):
            raise WriteRefused(
                f"Refused: {what} '{full_path}' is outside the write root '{self.root}'. "
                "Nothing was changed. Every source and destination must be inside "
                "REMARKABLE_WRITE_ROOT (or REMARKABLE_ROOT_PATH when that is unset)."
            )


def current() -> WriteScope:
    """The write scope from the environment, read per call. Raises when writes are off."""
    if env_bool("REMARKABLE_READ_ONLY"):
        raise WriteRefused(
            "This server is read-only (REMARKABLE_READ_ONLY is set). Nothing was changed.",
            kind="writes_disabled",
        )
    import os

    read_root = _get_root_path()
    raw = os.environ.get("REMARKABLE_WRITE_ROOT", "").strip()
    if raw:
        root = normalize(raw)
        if not _is_within_root(root, read_root):
            raise WriteRefused(
                f"REMARKABLE_WRITE_ROOT '{root}' is outside REMARKABLE_ROOT_PATH '{read_root}'; "
                "every write is refused until it is inside it.",
                kind="writes_disabled",
            )
        return WriteScope(root=root, configured=True, default_folder=root)
    if read_root != "/":
        return WriteScope(root=read_root, configured=False, default_folder=read_root)
    return WriteScope(root="/", configured=False, default_folder=DEFAULT_FOLDER)


def describe() -> str:
    """One line for remarkable_status: where writes may go."""
    try:
        scope = current()
    except WriteRefused as e:
        return str(e)
    if scope.root == "/":
        return f"anywhere; locations without a leading '/' go to {scope.default_folder}"
    return f"inside {scope.root}"
