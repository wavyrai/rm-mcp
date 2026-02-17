#!/bin/sh
# rm-mcp — One-line installer for reMarkable MCP Server
# Usage: curl -fsSL https://thijsverreck.com/setup.sh | sh
#
# Wrapped in main() so the shell reads the entire script before executing.
# Without this, piped subprocesses (brew, curl) can consume stdin and cause
# the shell to lose its place in the script.

main() {
  set -e

  CONNECT_URL="https://my.remarkable.com/device/apps/connect"

  # --- Helpers ---
  bold=""  dim=""  green=""  red=""  reset=""
  if [ -t 1 ]; then
    bold="\033[1m"  dim="\033[2m"  green="\033[32m"  red="\033[31m"  reset="\033[0m"
  fi

  info()    { printf "  %b\n" "$1"; }
  ok()      { printf "  ${green}✓${reset} %s\n" "$1"; }
  fail()    { printf "  ${red}✗${reset} %s\n" "$1" >&2; exit 1; }

  IS_MAC=false
  [ "$(uname -s)" = "Darwin" ] && IS_MAC=true

  printf "\n  ${bold}rm-mcp${reset} — Quick Setup\n"
  printf "  ────────────────────\n\n"

  # --- Step 1: Check / install dependencies ---
  info "${bold}Step 1${reset} ${dim}→${reset} Checking dependencies..."

  # Homebrew (macOS only — needed for installing uv and claude)
  if $IS_MAC; then
    if command -v brew >/dev/null 2>&1; then
      ok "Homebrew found"
    else
      info "  Installing Homebrew..."
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)" || true
      command -v brew >/dev/null 2>&1 || fail "Failed to install Homebrew. Install manually: https://brew.sh"
      ok "Homebrew installed"
    fi
  fi

  # Cairo (needed for PNG image rendering)
  if $IS_MAC && command -v brew >/dev/null 2>&1; then
    if brew list cairo >/dev/null 2>&1; then
      ok "cairo found"
    else
      info "  Installing cairo via Homebrew..."
      brew install cairo
      ok "cairo installed"
    fi
  fi

  # uv
  if command -v uv >/dev/null 2>&1; then
    ok "uv found"
  elif $IS_MAC && command -v brew >/dev/null 2>&1; then
    info "  Installing uv via Homebrew..."
    brew install uv
    ok "uv installed"
  else
    info "  Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck source=/dev/null
    . "$HOME/.local/bin/env" 2>/dev/null || true
    command -v uv >/dev/null 2>&1 || fail "Failed to install uv. Install manually: https://docs.astral.sh/uv/"
    ok "uv installed"
  fi

  # Claude Code
  if command -v claude >/dev/null 2>&1; then
    ok "Claude Code found"
  else
    info "  Installing Claude Code..."
    curl -fsSL https://claude.ai/install.sh | sh
    # shellcheck source=/dev/null
    . "$HOME/.local/bin/env" 2>/dev/null || true
    command -v claude >/dev/null 2>&1 || fail "Failed to install Claude Code. Install manually: https://code.claude.com/docs/en/setup"
    ok "Claude Code installed"
  fi
  printf "\n"

  # --- Step 2: Register reMarkable tablet ---
  info "${bold}Step 2${reset} ${dim}→${reset} Register your reMarkable tablet"

  # Open browser (best-effort)
  if command -v open >/dev/null 2>&1; then
    open "$CONNECT_URL" 2>/dev/null || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$CONNECT_URL" 2>/dev/null || true
  else
    info "  Open this URL in your browser:"
    info "  ${CONNECT_URL}"
  fi

  printf "  Enter the one-time code: "
  read -r CODE < /dev/tty
  [ -z "$CODE" ] && fail "No code entered. Setup cancelled."

  info "  Registering..."
  TOKEN=$(uvx --refresh rm-mcp --register "$CODE" --quiet) || fail "Registration failed. Is the code correct?"
  ok "Successfully registered!"
  printf "\n"

  # --- Step 3: Configure Claude Code ---
  info "${bold}Step 3${reset} ${dim}→${reset} Configuring Claude Code..."
  claude mcp add remarkable \
    -e REMARKABLE_TOKEN="$TOKEN" \
    -e REMARKABLE_OCR_BACKEND=sampling \
    -- uvx --refresh rm-mcp || true
  ok "MCP server added to Claude Code!"
  printf "\n"

  # --- Step 4: Configure Claude Desktop (macOS only) ---
  if $IS_MAC; then
    info "${bold}Step 4${reset} ${dim}→${reset} Configuring Claude Desktop..."
    DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
    mkdir -p "$(dirname "$DESKTOP_CONFIG")"
    UVX_PATH=$(command -v uvx)
    python3 -c "
import json, os, sys

path = sys.argv[1]
token = sys.argv[2]
uvx_path = sys.argv[3]

# Read existing config or start fresh
try:
    with open(path) as f:
        config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    config = {}

# Ensure mcpServers key exists
config.setdefault('mcpServers', {})

# Set the remarkable server entry (use full path so Claude Desktop can find uvx)
config['mcpServers']['remarkable'] = {
    'command': uvx_path,
    'args': ['--refresh', 'rm-mcp'],
    'env': {
        'REMARKABLE_TOKEN': token,
        'REMARKABLE_OCR_BACKEND': 'sampling'
    }
}

with open(path, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
" "$DESKTOP_CONFIG" "$TOKEN" "$UVX_PATH"
    ok "MCP server added to Claude Desktop!"
    info "${dim}Restart Claude Desktop to pick up the new config.${reset}"
  fi

  printf "\n  ${green}You're all set!${reset} Start a new Claude Code session to use your reMarkable.\n\n"
}

main
