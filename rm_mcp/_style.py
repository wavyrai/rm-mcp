"""Minimal CLI styling helpers with TTY-aware ANSI formatting."""

import sys

# Detect TTY — skip all formatting if output is piped
_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

# ANSI codes
BOLD = "\033[1m" if _USE_COLOR else ""
DIM = "\033[2m" if _USE_COLOR else ""
GREEN = "\033[32m" if _USE_COLOR else ""
YELLOW = "\033[33m" if _USE_COLOR else ""
CYAN = "\033[36m" if _USE_COLOR else ""
RED = "\033[31m" if _USE_COLOR else ""
RESET = "\033[0m" if _USE_COLOR else ""


def header(version: str) -> str:
    """Return the branded header line."""
    return f"{BOLD}rm-mcp{RESET} {DIM}v{version}{RESET} — reMarkable MCP Server"


def step(n: int, text: str) -> str:
    """Format a step label like '  Step 1 → text'."""
    return f"  {BOLD}Step {n}{RESET} {DIM}→{RESET} {text}"


def success(text: str) -> str:
    return f"  {GREEN}✓{RESET} {text}"


def error(text: str) -> str:
    return f"  {RED}✗{RESET} {text}"


def box(title: str, lines: list[str]) -> str:
    """Render content in a bordered box with title."""
    width = max(len(line) for line in lines) + 4
    width = max(width, len(title) + 6)
    top = f"  ┌─ {title} " + "─" * (width - len(title) - 5) + "┐"
    bot = "  └" + "─" * (width - 2) + "┘"
    body = "\n".join(f"  │ {line.ljust(width - 4)} │" for line in lines)
    return f"{top}\n{body}\n{bot}"
