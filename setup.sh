#!/bin/sh
# rm-mcp installer: installs uv if it is missing, then runs `rm-mcp setup`.
# Usage: curl -fsSL https://thijsverreck.com/setup.sh | sh
#
# `rm-mcp setup` connects your reMarkable, saves the token in rm-mcp's own
# file, and asks before it adds rm-mcp to Claude Code or Claude Desktop.
# This script changes nothing else.
#
# Wrapped in main() so the shell reads the whole script before running it.

main() {
  set -eu

  if [ -t 1 ]; then
    green=$(printf '\033[32m') red=$(printf '\033[31m') reset=$(printf '\033[0m')
  else
    green="" red="" reset=""
  fi
  ok()   { printf '  %s✓%s %s\n' "$green" "$reset" "$1"; }
  fail() { printf '  %s✗%s %s\n' "$red" "$reset" "$1" >&2; exit 1; }

  if command -v uvx >/dev/null 2>&1; then
    ok "uv found"
  else
    printf '  Installing uv (https://docs.astral.sh/uv)...\n'
    command -v curl >/dev/null 2>&1 || fail "curl is needed to install uv."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
    command -v uvx >/dev/null 2>&1 \
      || fail "uv did not install. Install it yourself: https://docs.astral.sh/uv/getting-started/installation/"
    ok "uv installed"
  fi

  # Setup prompts for a code and for each client, so give it the terminal
  # even when this script itself arrives on stdin through a pipe.
  if (: < /dev/tty) 2>/dev/null; then
    uvx rm-mcp@latest setup < /dev/tty
  else
    uvx rm-mcp@latest setup
  fi
}

main "$@"
