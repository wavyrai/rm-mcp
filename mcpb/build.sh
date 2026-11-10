#!/bin/sh
# Pack the Claude Desktop bundle: mcpb/rm-mcp-<version>.mcpb
# Needs Node (for npx). Validates the manifest first; signing is a separate step.
set -eu
cd "$(dirname "$0")"
VERSION=$(python3 -c "import json; print(json.load(open('manifest.json'))['version'])")
PYPROJECT_VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' ../pyproject.toml | head -1)
if [ "$VERSION" != "$PYPROJECT_VERSION" ]; then
  echo "manifest.json version $VERSION != pyproject.toml version $PYPROJECT_VERSION" >&2
  exit 1
fi
npx -y @anthropic-ai/mcpb validate manifest.json
npx -y @anthropic-ai/mcpb pack . "rm-mcp-$VERSION.mcpb"
echo "Built mcpb/rm-mcp-$VERSION.mcpb"
