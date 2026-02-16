#!/usr/bin/env python3
"""
reMarkable MCP Server

An MCP server that provides access to reMarkable tablet data through the reMarkable Cloud API.
Uses rmscene for native text extraction.

Usage:
    # Interactive setup (recommended)
    python server.py --setup

    # As MCP server (default)
    python server.py

This is a backwards-compatible entry point. The actual CLI is in rm_mcp/cli.py.
"""

from rm_mcp.cli import main

if __name__ == "__main__":
    main()
