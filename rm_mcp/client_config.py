"""Configuring MCP clients (Claude Code, Claude Desktop) to run rm-mcp.

No token goes into a client config any more: the server reads rm-mcp's own
token file (rm_mcp.auth). Claude Desktop's config is edited only when it
parses, is written atomically, and the original is kept as a .bak copy.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

SERVER_NAME = "remarkable"
PACKAGE = "rm-mcp@latest"
# Env vars that 2.0 no longer reads; dropped when an old entry is rewritten.
STALE_ENV = ("REMARKABLE_TOKEN", "REMARKABLE_OCR_BACKEND", "REMARKABLE_PAGE_SIZE")


def uvx_path(which=shutil.which) -> Optional[str]:
    """Absolute path to uvx (Claude Desktop's PATH does not include ~/.local/bin)."""
    found = which("uvx")
    if found:
        return found
    for candidate in _uvx_candidates():
        if candidate.is_file():
            return str(candidate)
    return None


def _uvx_candidates() -> List[Path]:
    home = Path.home()
    if sys.platform.startswith("win"):
        return [home / ".local" / "bin" / "uvx.exe", home / ".cargo" / "bin" / "uvx.exe"]
    return [
        home / ".local" / "bin" / "uvx",
        home / ".cargo" / "bin" / "uvx",
        Path("/opt/homebrew/bin/uvx"),
        Path("/usr/local/bin/uvx"),
    ]


def display_command(argv: List[str], platform: Optional[str] = None) -> str:
    """A command line the user can paste into their shell on this platform."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


# =============================================================================
# Claude Code
# =============================================================================


def claude_code_add_argv() -> List[str]:
    return ["claude", "mcp", "add", "-s", "user", SERVER_NAME, "--", "uvx", PACKAGE]


def claude_code_status(run=subprocess.run, which=shutil.which) -> str:
    """'missing' (no claude CLI), 'configured', 'absent' or 'unknown'."""
    claude = which("claude")
    if not claude:
        return "missing"
    try:
        result = run(
            [claude, "mcp", "get", SERVER_NAME],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "configured" if result.returncode == 0 else "absent"


def add_to_claude_code(run=subprocess.run, which=shutil.which) -> subprocess.CompletedProcess:
    claude = which("claude")
    argv = claude_code_add_argv()
    argv[0] = claude or argv[0]
    return run(argv, capture_output=True, text=True, timeout=60)


# =============================================================================
# Claude Desktop
# =============================================================================


def desktop_config_path(
    platform: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if platform.startswith("win"):
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else home / ".config"
    return base / "Claude" / "claude_desktop_config.json"


def desktop_entry(uvx: str, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The server entry, keeping env vars of an existing entry that 2.0 still reads."""
    entry: Dict[str, Any] = {"command": uvx, "args": [PACKAGE]}
    env = (existing or {}).get("env")
    if isinstance(env, dict):
        kept = {k: v for k, v in env.items() if k not in STALE_ENV}
        if kept:
            entry["env"] = kept
    return entry


def snippet(uvx: str) -> str:
    return json.dumps({"mcpServers": {SERVER_NAME: desktop_entry(uvx)}}, indent=2)


@dataclass
class DesktopResult:
    status: str  # "written", "unchanged", "invalid"
    path: Path
    backup: Optional[Path] = None
    reason: str = ""


def read_desktop_config(path: Path) -> Dict[str, Any]:
    """The parsed config ({} when missing). Raises ValueError when it can't be edited safely."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    try:
        config = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"it is not valid JSON (line {e.lineno}, column {e.colno})")
    if not isinstance(config, dict):
        raise ValueError("its top level is not a JSON object")
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("its 'mcpServers' is not a JSON object")
    return config


def configure_desktop(path: Path, uvx: str) -> DesktopResult:
    """Add or update the rm-mcp entry. Never writes a file it could not parse."""
    try:
        config = read_desktop_config(path)
    except ValueError as e:
        return DesktopResult("invalid", path, reason=str(e))
    servers = config.setdefault("mcpServers", {})
    entry = desktop_entry(uvx, servers.get(SERVER_NAME))
    if servers.get(SERVER_NAME) == entry:
        return DesktopResult("unchanged", path)
    servers[SERVER_NAME] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    _atomic_write(path, json.dumps(config, indent=2) + "\n")
    return DesktopResult("written", path, backup=backup)


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if path.exists() and os.name == "posix":
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def desktop_findings(path: Path) -> List[str]:
    """Problems with the Desktop entry, for `rm-mcp doctor` (empty when fine or absent)."""
    try:
        config = read_desktop_config(path)
    except ValueError as e:
        return [f"{path} can't be read: {e}. Fix the JSON, then run `rm-mcp setup` again."]
    entry = config.get("mcpServers", {}).get(SERVER_NAME)
    if entry is None:
        return []
    findings = []
    if not isinstance(entry, dict):
        return [f"The '{SERVER_NAME}' entry in {path} is not an object."]
    command = str(entry.get("command", ""))
    if command and not os.path.isabs(command):
        findings.append(
            f"Claude Desktop runs '{command}' without a full path and may not find it. "
            "Run `rm-mcp setup` to rewrite the entry with the full path to uvx."
        )
    elif command and not Path(command).exists():
        findings.append(f"Claude Desktop's command {command} does not exist.")
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    stale = [k for k in env if k in STALE_ENV]
    if stale:
        findings.append(
            f"The Desktop entry still sets {', '.join(stale)}; 2.0 ignores a "
            "REMARKABLE_TOKEN there when the token file exists, and the others are gone. "
            "Run `rm-mcp setup` to clean the entry."
        )
    return findings
