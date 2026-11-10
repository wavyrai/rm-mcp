"""Entry point for the Claude Desktop bundle: run the rm-mcp server over stdio."""

from rm_mcp.cli import main

if __name__ == "__main__":
    main(["serve"])
