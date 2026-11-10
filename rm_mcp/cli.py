"""
The rm-mcp command.

    rm-mcp                 run the MCP server (stdio)
    rm-mcp setup           connect your reMarkable, then offer to configure clients
    rm-mcp doctor          check the token, connection, uvx and client configs
    rm-mcp logout          remove rm-mcp's token file

`--setup` and `--register CODE` still work as aliases.
"""

import argparse
import os
import sys
import webbrowser
from importlib.metadata import version as pkg_version
from typing import Callable, List, Optional

from rm_mcp import auth, client_config
from rm_mcp._style import box, error, header, step, success

REMARKABLE_CONNECT_URL = "https://my.remarkable.com/device/apps/connect"

try:
    _VERSION = pkg_version("rm-mcp")
except Exception:
    _VERSION = "dev"


def _ask(question: str, default: bool = True, input_fn: Callable[[str], str] = input) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input_fn("           " + question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


# =============================================================================
# setup
# =============================================================================


def _register(code: str) -> None:
    from rm_mcp.api import register_and_get_token

    print("           Registering...")
    try:
        path = register_and_get_token(code)
    except Exception as e:
        print(error(f"Registration failed: {e}"), file=sys.stderr)
        sys.exit(1)
    print(success(f"Connected as {auth.device_desc()}. Token saved to {path}"))


def _handle_setup(
    code: Optional[str] = None,
    open_browser: bool = True,
    configure: Optional[bool] = None,
    input_fn: Callable[[str], str] = input,
) -> None:
    """Interactive setup: get a code, register, save the token, offer client config."""
    print(header(_VERSION))
    print()
    if code is None:
        print(step(1, f"Get a one-time code at {REMARKABLE_CONNECT_URL}"))
        print("           If the browser doesn't open, visit the URL yourself.")
        print()
        if open_browser:
            try:
                webbrowser.open(REMARKABLE_CONNECT_URL)
            except Exception:
                pass
        try:
            code = input_fn(step(2, "Enter the one-time code: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled.")
            sys.exit(0)
        if not code:
            print("No code entered. Setup cancelled.", file=sys.stderr)
            sys.exit(1)
        print()
    _register(code)
    print()
    print(step(3, "Add rm-mcp to your apps"))
    _configure_clients(configure, input_fn)
    print()
    print("  Check everything with: uvx rm-mcp@latest doctor")


def _configure_clients(configure: Optional[bool], input_fn: Callable[[str], str]) -> None:
    def want(question: str) -> bool:
        return configure if configure is not None else _ask(question, True, input_fn)

    # Claude Code
    code_cmd = client_config.display_command(client_config.claude_code_add_argv())
    state = client_config.claude_code_status()
    if state == "missing":
        print("           Claude Code not found. To add rm-mcp to it later, run:")
        print(f"             {code_cmd}")
    elif state == "configured":
        print(success("Claude Code already has a 'remarkable' server; it uses the new token."))
    elif want("Add rm-mcp to Claude Code?"):
        result = client_config.add_to_claude_code()
        if result.returncode == 0:
            print(success("Added to Claude Code (user scope)."))
        else:
            print(error("Claude Code refused the change:"))
            print("           " + (result.stderr or result.stdout).strip()[:400])
            print(f"           Run it yourself: {code_cmd}")
    else:
        print(f"           Skipped. Later: {code_cmd}")

    # Claude Desktop
    path = client_config.desktop_config_path()
    uvx = client_config.uvx_path()
    if uvx is None:
        print(error("uvx not found, so Claude Desktop can't be configured. Install uv:"))
        print("           https://docs.astral.sh/uv/getting-started/installation/")
        return
    if not path.parent.exists():
        print("           Claude Desktop not found. To add it later, put this in")
        print(f"           {path}:")
        print(box("Claude Desktop", client_config.snippet(uvx).splitlines()))
        return
    if not want(f"Add rm-mcp to Claude Desktop ({path})?"):
        print("           Skipped. To add it yourself, merge this into that file:")
        print(box("Claude Desktop", client_config.snippet(uvx).splitlines()))
        return
    result = client_config.configure_desktop(path, uvx)
    if result.status == "invalid":
        print(error(f"Left {path} untouched: {result.reason}."))
        print('           Fix the file, then add this under "mcpServers":')
        print(box("Claude Desktop", client_config.snippet(uvx).splitlines()))
    elif result.status == "unchanged":
        print(success("Claude Desktop is already set up."))
    else:
        saved = f" (previous version: {result.backup})" if result.backup else ""
        print(success(f"Claude Desktop configured{saved}. Restart Claude Desktop."))


# =============================================================================
# doctor
# =============================================================================


def _handle_doctor(online: bool = True) -> int:
    """Print what is set up and how to fix what is not. Returns the exit code."""
    print(header(_VERSION))
    print()
    problems = 0

    def ok(text: str) -> None:
        print(success(text))

    def bad(text: str, fix: str) -> None:
        nonlocal problems
        problems += 1
        print(error(text))
        print(f"      Fix: {fix}")

    # Token
    source = auth.find_token()
    if source is None:
        bad("No reMarkable token.", "uvx rm-mcp@latest setup")
    else:
        try:
            auth.parse(source.data)
            ok(f"Token: {source.describe()}")
        except ValueError as e:
            bad(f"Token in {source.describe()} is unusable: {e}", "uvx rm-mcp@latest setup")
            source = None
    if source is not None and source.kind == "file" and auth.loose_permissions(source.path):
        bad(f"{source.path} is readable by other users.", f"chmod 600 '{source.path}'")
    if source is not None and source.kind == "legacy":
        print("      Note: using rmapi's ~/.rmapi. Run `uvx rm-mcp@latest setup` to give")
        print("      rm-mcp its own token.")
    if source is not None and source.kind == "file" and os.environ.get(auth.ENV_VAR):
        print(f"      Note: {auth.ENV_VAR} is set but ignored; the token file wins.")

    # Connection
    if source is not None and online:
        try:
            from rm_mcp.clients.cloud import load_client_from_token

            client = load_client_from_token(source.data)
            client.get_root_hash()
            ok("Connected to the reMarkable Cloud.")
        except Exception as e:
            bad(f"Could not reach your library: {e}", "uvx rm-mcp@latest setup")

    # uvx
    uvx = client_config.uvx_path()
    if uvx:
        ok(f"uvx: {uvx}")
    else:
        bad("uvx not found.", "install uv: https://docs.astral.sh/uv/getting-started/installation/")

    # Clients
    state = client_config.claude_code_status()
    if state == "configured":
        ok("Claude Code has a 'remarkable' server.")
    elif state == "absent":
        bad(
            "Claude Code has no 'remarkable' server.",
            client_config.display_command(client_config.claude_code_add_argv()),
        )
    elif state == "missing":
        print("  -  Claude Code not installed (skipped).")
    path = client_config.desktop_config_path()
    if path.exists():
        findings = client_config.desktop_findings(path)
        for finding in findings:
            bad(finding, "uvx rm-mcp@latest setup")
        if not findings:
            try:
                has = client_config.SERVER_NAME in client_config.read_desktop_config(path).get(
                    "mcpServers", {}
                )
            except ValueError:
                has = False
            if has:
                ok(f"Claude Desktop: {path}")
            else:
                print(f"  -  Claude Desktop has no 'remarkable' server ({path}).")
    else:
        print("  -  Claude Desktop config not found (skipped).")

    print()
    print(success("All good.") if not problems else f"  {problems} problem(s) found.")
    return 1 if problems else 0


# =============================================================================
# logout
# =============================================================================


def _handle_logout() -> None:
    removed = auth.remove_token()
    if removed:
        print(success(f"Removed {removed}."))
    else:
        print(f"  No token file at {auth.token_path()}.")
    if os.environ.get(auth.ENV_VAR) or auth.legacy_path().exists():
        print(
            f"  Note: {auth.ENV_VAR} or ~/.rmapi still provides a token; rm-mcp leaves those alone."
        )


# =============================================================================
# main
# =============================================================================


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rm-mcp",
        description="reMarkable MCP server. With no command it runs the server over stdio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  uvx rm-mcp@latest setup\n  uvx rm-mcp@latest doctor\n",
    )
    parser.add_argument("--setup", action="store_true", help="Same as `rm-mcp setup`")
    parser.add_argument("--register", metavar="CODE", help="Register with a one-time code")
    parser.add_argument("--quiet", action="store_true", help="With --register: print nothing")
    sub = parser.add_subparsers(dest="command")
    p_setup = sub.add_parser("setup", help="Connect your reMarkable and configure clients")
    p_setup.add_argument("--code", help="One-time code (skips the prompt)")
    p_setup.add_argument("--no-browser", action="store_true", help="Don't open the code page")
    choice = p_setup.add_mutually_exclusive_group()
    choice.add_argument("--yes", action="store_true", help="Configure every client found")
    choice.add_argument("--no-clients", action="store_true", help="Don't configure clients")
    p_doctor = sub.add_parser("doctor", help="Check the setup and print fixes")
    p_doctor.add_argument("--offline", action="store_true", help="Skip the connection check")
    sub.add_parser("logout", help="Remove rm-mcp's token file")
    sub.add_parser("serve", help="Run the MCP server over stdio (the default)")
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    args = _parser().parse_args(argv)

    if args.command == "setup":
        configure = True if args.yes else False if args.no_clients else None
        _handle_setup(args.code, not args.no_browser, configure)
    elif args.setup:
        _handle_setup()
    elif args.command == "doctor":
        sys.exit(_handle_doctor(online=not args.offline))
    elif args.command == "logout":
        _handle_logout()
    elif args.register:
        if args.quiet:
            from rm_mcp.api import register_and_get_token

            register_and_get_token(args.register)
            return
        _register(args.register)
    else:
        from rm_mcp.server import run

        run()


if __name__ == "__main__":
    main()
