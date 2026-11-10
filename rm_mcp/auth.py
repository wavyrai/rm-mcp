"""Where the reMarkable token lives, and which one the server uses.

rm-mcp keeps its token in its own file, in the platform's config directory:

- Linux:   $XDG_CONFIG_HOME/rm-mcp/token.json (default ~/.config/rm-mcp/token.json)
- macOS:   ~/Library/Application Support/rm-mcp/token.json
- Windows: %APPDATA%\\rm-mcp\\token.json

The server reads, in order: that file, then REMARKABLE_TOKEN, then the legacy
~/.rmapi (which belongs to rmapi and is only ever read). Re-running
``rm-mcp setup`` replaces the file, so it always wins over a stale token left
in a client config. The short-lived user token is saved back into the file
after a renewal, so a restart does not need another renewal.

Nothing here prints a token or puts one in an error message.
"""

import base64
import json
import os
import posixpath
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

TOKEN_FILE = "token.json"
ENV_VAR = "REMARKABLE_TOKEN"
# A one-time code the server redeems itself when no token exists (Claude Desktop bundle).
CODE_ENV_VAR = "REMARKABLE_ONE_TIME_CODE"
# A user token closer than this to its expiry is renewed instead of used.
USER_TOKEN_MARGIN_SECONDS = 300


@dataclass(frozen=True)
class TokenSource:
    kind: str  # "file", "env" or "legacy"
    data: str
    path: Optional[Path] = None

    def describe(self) -> str:
        if self.kind == "file":
            return f"token file ({self.path})"
        if self.kind == "env":
            return f"{ENV_VAR} environment variable"
        return f"legacy rmapi file ({self.path}, read-only)"


def config_dir(
    platform: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """The per-user config directory for rm-mcp on this platform."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if platform.startswith("win"):
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "rm-mcp"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "rm-mcp"
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and posixpath.isabs(xdg) else home / ".config"
    return base / "rm-mcp"


def token_path() -> Path:
    return config_dir() / TOKEN_FILE


def legacy_path() -> Path:
    return Path.home() / ".rmapi"


def device_desc(platform: Optional[str] = None) -> str:
    """The deviceDesc the reMarkable Cloud expects for this desktop OS."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return "desktop-windows"
    if platform == "darwin":
        return "desktop-macos"
    return "desktop-linux"


def find_token() -> Optional[TokenSource]:
    """The token to use: our file, then REMARKABLE_TOKEN, then ~/.rmapi."""
    path = token_path()
    data = _read(path)
    if data:
        return TokenSource("file", data, path)
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return TokenSource("env", env)
    legacy = legacy_path()
    data = _read(legacy)
    if data:
        return TokenSource("legacy", data, legacy)
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError, PermissionError, IsADirectoryError):
        return ""


def parse(data: str) -> Dict[str, str]:
    """{devicetoken, usertoken} from a token JSON object or a bare device JWT."""
    data = data.strip()
    if data.startswith("{"):
        try:
            loaded = json.loads(data)
        except json.JSONDecodeError:
            raise ValueError("The token is not valid JSON.")
        if not isinstance(loaded, dict) or not loaded.get("devicetoken"):
            raise ValueError("The token has no 'devicetoken'.")
        return {
            "devicetoken": str(loaded["devicetoken"]),
            "usertoken": str(loaded.get("usertoken") or ""),
        }
    if data.startswith("eyJ"):
        return {"devicetoken": data, "usertoken": ""}
    raise ValueError(
        f"The token matches neither shape ({len(data)} characters): expected a JSON "
        "object with 'devicetoken' or a raw JWT device token."
    )


def jwt_expiry(token: str) -> Optional[float]:
    """The `exp` claim of a JWT, unverified; None when it has none."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def usable_user_token(token: str, now: Optional[float] = None) -> str:
    """The user token if it is still valid for a while, else ''."""
    if not token:
        return ""
    exp = jwt_expiry(token)
    now = time.time() if now is None else now
    if exp is None or exp - USER_TOKEN_MARGIN_SECONDS <= now:
        return ""
    return token


def save_token(token: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """Write the token file atomically, readable only by this user where supported."""
    path = token_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrict(path.parent, 0o700)
    body = json.dumps(token, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".token-", dir=str(path.parent))
    try:
        _restrict(Path(tmp), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _restrict(path, 0o600)
    return path


def _restrict(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def save_user_token(user_token: str, path: Optional[Path] = None) -> None:
    """Store a renewed user token next to the device token in our file."""
    path = token_path() if path is None else path
    current = parse(_read(path))
    if current.get("usertoken") == user_token:
        return
    current["usertoken"] = user_token
    save_token(current, path)


def remove_token() -> Optional[Path]:
    """Delete our token file (never ~/.rmapi). Returns the path removed, if any."""
    path = token_path()
    try:
        path.unlink()
        return path
    except FileNotFoundError:
        return None


def file_signature(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def loose_permissions(path: Path) -> bool:
    """True when the token file is readable by other users (POSIX only)."""
    if os.name != "posix":
        return False
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False
