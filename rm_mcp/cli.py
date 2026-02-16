#!/usr/bin/env python3
"""
CLI entry point for reMarkable MCP Server.

Usage:
    # Interactive setup (recommended)
    rm-mcp --setup

    # As MCP server (uses cloud API)
    rm-mcp

    # Convert one-time code to token (run once)
    rm-mcp --register <one-time-code>
"""

import argparse
import json
import sys
import webbrowser
from importlib.metadata import version as pkg_version

from rm_mcp._style import box, error, header, step, success

REMARKABLE_CONNECT_URL = "https://my.remarkable.com/device/browser/connect"

try:
    _VERSION = pkg_version("rm-mcp")
except Exception:
    _VERSION = "dev"


def _print_config_instructions(token: str) -> None:
    """Print ready-to-paste config for Claude Code and Claude Desktop."""
    print()
    print(step(3, "Add to your MCP client:"))
    print()

    # Claude Code box
    code_lines = [
        "claude mcp add remarkable \\",
        f"  -e REMARKABLE_TOKEN='{token}' \\",
        "  -e REMARKABLE_OCR_BACKEND=sampling \\",
        "  -- uvx rm-mcp",
    ]
    print(box("Claude Code", code_lines))

    print()

    # Claude Desktop box
    desktop_config = json.dumps(
        {
            "mcpServers": {
                "remarkable": {
                    "command": "uvx",
                    "args": ["rm-mcp"],
                    "env": {
                        "REMARKABLE_TOKEN": token,
                    },
                }
            }
        },
        indent=2,
    )
    desktop_lines = ["Add to claude_desktop_config.json:", ""] + desktop_config.splitlines()
    print(box("Claude Desktop", desktop_lines))


def _handle_setup() -> None:
    """Interactive setup: open browser, prompt for code, register, print config."""
    print(header(_VERSION))
    print()
    print(step(1, f"Opening {REMARKABLE_CONNECT_URL}..."))
    print("           If the browser doesn't open, visit the URL manually.")
    print()

    try:
        webbrowser.open(REMARKABLE_CONNECT_URL)
    except Exception:
        pass  # Browser open is best-effort

    try:
        code = input(step(2, "Enter the one-time code: ")).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        sys.exit(0)

    if not code:
        print("No code entered. Setup cancelled.", file=sys.stderr)
        sys.exit(1)

    from rm_mcp.api import register_and_get_token

    try:
        print()
        print("           Registering...")
        token = register_and_get_token(code)
        print(success("Successfully registered!"))
        _print_config_instructions(token)
    except Exception as e:
        print(error(f"Registration failed: {e}"), file=sys.stderr)
        sys.exit(1)


def main():
    """Main entry point - handle CLI args or run MCP server."""
    parser = argparse.ArgumentParser(
        description="reMarkable MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive setup (recommended)
  uvx rm-mcp --setup

  # Register and get token (run once)
  uvx rm-mcp --register abcd1234

  # Run as MCP server (cloud API)
  uvx rm-mcp

  # Run with token from environment
  REMARKABLE_TOKEN="your-token" uvx rm-mcp
""",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Interactive setup: open browser, enter code, get config",
    )
    parser.add_argument(
        "--register",
        metavar="CODE",
        help="Register with reMarkable using a one-time code and print the token",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="With --register: output only the raw token JSON (for scripting)",
    )

    args = parser.parse_args()

    if args.setup:
        _handle_setup()
    elif args.register:
        # Registration mode - convert one-time code to token
        from rm_mcp.api import register_and_get_token

        try:
            if args.quiet:
                token = register_and_get_token(args.register)
                print(token)
                return

            print(header(_VERSION))
            print()
            print("           Registering...")
            token = register_and_get_token(args.register)
            print(success("Successfully registered!"))
            print()
            print("  Add to your MCP client:")
            print()

            # Claude Code box
            code_lines = [
                "claude mcp add remarkable \\",
                f"  -e REMARKABLE_TOKEN='{token}' \\",
                "  -e REMARKABLE_OCR_BACKEND=sampling \\",
                "  -- uvx rm-mcp",
            ]
            print(box("Claude Code", code_lines))

            print()

            # Claude Desktop box
            desktop_config = json.dumps(
                {
                    "mcpServers": {
                        "remarkable": {
                            "command": "uvx",
                            "args": ["rm-mcp"],
                            "env": {
                                "REMARKABLE_TOKEN": token,
                            },
                        }
                    }
                },
                indent=2,
            )
            desktop_lines = [
                "Add to claude_desktop_config.json:",
                "",
            ] + desktop_config.splitlines()
            print(box("Claude Desktop", desktop_lines))
        except Exception as e:
            print(error(f"Registration failed: {e}"), file=sys.stderr)
            sys.exit(1)
    else:
        # MCP server mode - only now import the full server
        from rm_mcp.server import run

        run()


if __name__ == "__main__":
    main()
