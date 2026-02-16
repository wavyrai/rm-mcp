#!/usr/bin/env python3
"""
Tests for reMarkable MCP Server

Tests the 4 intent-based tools using FastMCP's testing capabilities.
"""

import json
import os
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from rm_mcp.api import register_and_get_token
from rm_mcp.extract import (
    extract_text_from_document_zip,
    extract_text_from_rm_file,
    find_similar_documents,
)
from rm_mcp.paths import get_item_path, get_items_by_id
from rm_mcp.responses import (
    make_error,
    make_response,
)
from rm_mcp.server import mcp

# Helper for patching get_cached_collection to return (mock_client, collection)
# Patch in the tools module where it's imported and used
_PATCH_CACHED = "rm_mcp.tools._helpers.get_cached_collection"

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_document():
    """Create a mock Document object."""
    doc = Mock()
    doc.VissibleName = "Test Document"
    doc.ID = "doc-123"
    doc.Parent = ""
    doc.ModifiedClient = "2024-01-15T10:30:00Z"
    return doc


@pytest.fixture
def mock_folder():
    """Create a mock Folder object."""
    folder = Mock()
    folder.VissibleName = "Test Folder"
    folder.ID = "folder-456"
    folder.Parent = ""
    return folder


@pytest.fixture
def mock_collection(mock_document, mock_folder):
    """Create a mock collection of items."""
    return [mock_document, mock_folder]


@pytest.fixture
def sample_zip_file():
    """Create a sample reMarkable document zip for testing."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        with zipfile.ZipFile(tmp.name, "w") as zf:
            # Add a sample text file
            zf.writestr("sample.txt", "This is sample text content")
            # Add a sample content json
            zf.writestr("metadata.content", '{"text": "Content metadata text"}')
        yield Path(tmp.name)
    Path(tmp.name).unlink(missing_ok=True)


# =============================================================================
# Test MCP Server Initialization
# =============================================================================


class TestMCPServerInitialization:
    """Test MCP server initialization and basic functionality."""

    def test_server_name(self):
        """Test that server has correct name."""
        assert mcp.name == "rm-mcp"

    @pytest.mark.asyncio
    async def test_tools_registered(self):
        """Test that all expected tools are registered."""
        tools = await mcp.list_tools()
        tool_names = [tool.name for tool in tools]

        expected_tools = [
            "remarkable_read",
            "remarkable_browse",
            "remarkable_recent",
            "remarkable_search",
            "remarkable_status",
            "remarkable_image",
        ]

        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Tool {tool_name} not found"

    @pytest.mark.asyncio
    async def test_tools_count(self):
        """Test that we have exactly 6 intent-based tools."""
        tools = await mcp.list_tools()
        assert len(tools) == 6, f"Expected 6 tools, got {len(tools)}"

    @pytest.mark.asyncio
    async def test_tool_schemas(self):
        """Test that tools have proper schemas."""
        tools = await mcp.list_tools()

        for tool in tools:
            assert tool.name, "Tool should have a name"
            assert tool.description, "Tool should have a description"
            assert hasattr(tool, "inputSchema"), "Tool should have inputSchema"

    @pytest.mark.asyncio
    async def test_all_tools_have_xml_docstrings(self):
        """Test that all tools have XML-structured documentation."""
        tools = await mcp.list_tools()

        for tool in tools:
            # Check for XML tags in description
            desc = tool.description
            assert "<usecase>" in desc, f"Tool {tool.name} missing <usecase> tag"


# =============================================================================
# Test Helper Functions
# =============================================================================


class TestHelperFunctions:
    """Test helper functions."""

    def test_make_response(self):
        """Test response creation with hint."""
        data = {"key": "value"}
        result = make_response(data, "This is a hint")
        parsed = json.loads(result)

        assert parsed["key"] == "value"
        assert parsed["_hint"] == "This is a hint"

    def test_make_error(self):
        """Test error creation with suggestions."""
        result = make_error(
            error_type="test_error",
            message="Something went wrong",
            suggestion="Try this instead",
            did_you_mean=["option1", "option2"],
        )
        parsed = json.loads(result)

        assert parsed["_error"]["type"] == "test_error"
        assert parsed["_error"]["message"] == "Something went wrong"
        assert parsed["_error"]["suggestion"] == "Try this instead"
        assert parsed["_error"]["did_you_mean"] == ["option1", "option2"]

    def test_make_error_without_did_you_mean(self):
        """Test error creation without did_you_mean."""
        result = make_error(
            error_type="test_error", message="Error message", suggestion="Suggestion"
        )
        parsed = json.loads(result)

        assert "did_you_mean" not in parsed["_error"]

    def test_find_similar_documents(self):
        """Test fuzzy document matching."""
        docs = [
            Mock(VissibleName="Meeting Notes"),
            Mock(VissibleName="Project Plan"),
            Mock(VissibleName="Notes Daily"),
        ]

        # Exact partial match
        results = find_similar_documents("Notes", docs)
        assert "Meeting Notes" in results or "Notes Daily" in results

        # Fuzzy match
        results = find_similar_documents("Meating", docs, limit=3)
        assert len(results) <= 3

    def test_get_items_by_id(self, mock_collection):
        """Test building ID lookup dict."""
        items_by_id = get_items_by_id(mock_collection)

        assert "doc-123" in items_by_id
        assert "folder-456" in items_by_id

    def test_get_item_path(self, mock_document, mock_collection):
        """Test getting full item path."""
        items_by_id = get_items_by_id(mock_collection)
        path = get_item_path(mock_document, items_by_id)

        assert path == "/Test Document"

    def test_get_item_path_nested(self, mock_folder):
        """Test getting path for nested item."""
        # Create nested structure
        child_doc = Mock()
        child_doc.VissibleName = "Child Doc"
        child_doc.ID = "child-789"
        child_doc.Parent = mock_folder.ID

        items_by_id = {mock_folder.ID: mock_folder, child_doc.ID: child_doc}

        path = get_item_path(child_doc, items_by_id)
        assert path == "/Test Folder/Child Doc"


# =============================================================================
# Test Text Extraction
# =============================================================================


class TestTextExtraction:
    """Test text extraction functions."""

    def test_extract_text_from_document_zip(self, sample_zip_file):
        """Test extracting text from a zip file."""
        result = extract_text_from_document_zip(sample_zip_file)

        assert "typed_text" in result
        assert "highlights" in result
        assert "handwritten_text" in result
        assert "pages" in result

        # Should have extracted text from txt file
        assert any("sample text" in text.lower() for text in result["typed_text"])

    def test_extract_text_from_rm_file_no_rmscene(self):
        """Test graceful fallback when rmscene not available."""
        # Create a dummy file
        with tempfile.NamedTemporaryFile(suffix=".rm", delete=False) as tmp:
            tmp.write(b"dummy data")
            tmp_path = Path(tmp.name)

        try:
            # This should return empty list if rmscene fails
            result = extract_text_from_rm_file(tmp_path)
            assert isinstance(result, list)
        finally:
            tmp_path.unlink(missing_ok=True)


# =============================================================================
# Test remarkable_status Tool
# =============================================================================


class TestRemarkableStatus:
    """Test remarkable_status tool."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_status_authenticated(self, mock_get_cached):
        """Test status when authenticated."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)

        assert data["authenticated"] is True
        assert "transport" in data
        assert "connection" in data
        assert data["status"] == "connected"
        assert "_hint" in data

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_status_not_authenticated(self, mock_get_cached):
        """Test status when not authenticated."""
        mock_get_cached.side_effect = RuntimeError("Not authenticated. Run: uvx rm-mcp --setup")

        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)

        assert data["authenticated"] is False
        assert "error" in data
        assert "_hint" in data
        # Hint should reference --setup
        assert "--setup" in data["_hint"]


# =============================================================================
# Test remarkable_browse Tool
# =============================================================================


class TestRemarkableBrowse:
    """Test remarkable_browse tool."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_browse_root(self, mock_get_cached):
        """Test browsing root folder."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool("remarkable_browse", {"path": "/"})
        data = json.loads(result[0][0].text)

        assert data["mode"] == "browse"
        assert data["path"] == "/"
        assert "_hint" in data

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_browse_error_handling(self, mock_get_cached):
        """Test error handling in browse."""
        mock_get_cached.side_effect = RuntimeError("Connection failed")

        result = await mcp.call_tool("remarkable_browse", {"path": "/"})
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "browse_failed"


# =============================================================================
# Test remarkable_recent Tool
# =============================================================================


class TestRemarkableRecent:
    """Test remarkable_recent tool."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_recent_default_limit(self, mock_get_cached):
        """Test getting recent documents with default limit."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool("remarkable_recent", {})
        data = json.loads(result[0][0].text)

        assert "count" in data
        assert "documents" in data
        assert "_hint" in data

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_recent_custom_limit(self, mock_get_cached):
        """Test getting recent documents with custom limit."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool("remarkable_recent", {"limit": 5})
        data = json.loads(result[0][0].text)

        assert "count" in data
        assert "documents" in data

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_recent_limit_clamped(self, mock_get_cached):
        """Test that limit is clamped to valid range."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        # Test with limit > 50
        result = await mcp.call_tool("remarkable_recent", {"limit": 100})
        # Should not raise an error
        data = json.loads(result[0][0].text)
        assert "count" in data

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_recent_error_handling(self, mock_get_cached):
        """Test error handling in recent."""
        mock_get_cached.side_effect = RuntimeError("Connection failed")

        result = await mcp.call_tool("remarkable_recent", {})
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "recent_failed"


# =============================================================================
# Test remarkable_read Tool
# =============================================================================


class TestRemarkableRead:
    """Test remarkable_read tool."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_read_document_not_found(self, mock_get_cached):
        """Test reading a non-existent document."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool("remarkable_read", {"document": "NonExistent"})
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "document_not_found"
        assert "suggestion" in data["_error"]

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_read_error_handling(self, mock_get_cached):
        """Test error handling in read."""
        mock_get_cached.side_effect = RuntimeError("Connection failed")

        result = await mcp.call_tool("remarkable_read", {"document": "Test"})
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "read_failed"

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_read_provides_suggestions(self, mock_get_cached, mock_document):
        """Test that read provides 'did you mean' suggestions."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [mock_document])

        # Search for something similar but not exact
        result = await mcp.call_tool("remarkable_read", {"document": "Test Doc"})
        data = json.loads(result[0][0].text)

        # Should get a not found error with suggestions
        assert "_error" in data
        assert data["_error"]["type"] == "document_not_found"


# =============================================================================
# Test remarkable_image Tool
# =============================================================================


class TestRemarkableImage:
    """Test remarkable_image tool."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_image_document_not_found(self, mock_get_cached):
        """Test getting image from non-existent document."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool("remarkable_image", {"document": "NonExistent"})
        data = json.loads(result[0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "document_not_found"
        assert "suggestion" in data["_error"]

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_image_error_handling(self, mock_get_cached):
        """Test error handling in image tool."""
        mock_get_cached.side_effect = RuntimeError("Connection failed")

        result = await mcp.call_tool("remarkable_image", {"document": "Test"})
        data = json.loads(result[0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "image_failed"

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_image_provides_suggestions(self, mock_get_cached, mock_document):
        """Test that image tool provides 'did you mean' suggestions."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [mock_document])

        # Search for something similar but not exact
        result = await mcp.call_tool("remarkable_image", {"document": "Test Doc"})
        data = json.loads(result[0].text)

        # Should get a not found error with suggestions
        assert "_error" in data
        assert data["_error"]["type"] == "document_not_found"

    @pytest.mark.asyncio
    async def test_image_compatibility_parameter_in_schema(self):
        """Test that remarkable_image tool has the compatibility parameter in its schema."""
        tools = await mcp.list_tools()
        image_tool = next(t for t in tools if t.name == "remarkable_image")

        # Check that compatibility parameter exists in the input schema
        assert "compatibility" in image_tool.inputSchema.get("properties", {})
        compat_schema = image_tool.inputSchema["properties"]["compatibility"]
        assert compat_schema.get("type") == "boolean"
        assert compat_schema.get("default") is False


# =============================================================================
# Test Registration
# =============================================================================


class TestRegistration:
    """Test registration functionality."""

    @patch("os.close")
    @patch("os.write")
    @patch("os.open", return_value=99)
    @patch("requests.post")
    def test_register_and_get_token(self, mock_post, mock_os_open, mock_os_write, mock_os_close):
        """Test registration process."""
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "test_device_token_12345"
        mock_post.return_value = mock_response

        token = register_and_get_token("test_code")

        # Should return JSON with devicetoken
        import json

        token_data = json.loads(token)
        assert token_data["devicetoken"] == "test_device_token_12345"
        assert "usertoken" in token_data

        # Verify API was called
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "webapp-prod.cloud.remarkable.engineering" in call_args[0][0]

        # Verify token was written to ~/.rmapi
        mock_os_open.assert_called_once()
        written_path = mock_os_open.call_args[0][0]
        assert ".rmapi" in written_path
        mock_os_write.assert_called_once()

    @patch("requests.post")
    def test_register_invalid_code(self, mock_post):
        """Test registration with invalid/expired code."""
        # Mock 400 response (invalid code)
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = ""
        mock_post.return_value = mock_response

        with pytest.raises(RuntimeError, match="Registration failed"):
            register_and_get_token("invalid_code")


# =============================================================================
# Test --setup Command
# =============================================================================


class TestSetupCommand:
    """Test the --setup CLI command."""

    @patch("rm_mcp.api.register_and_get_token")
    @patch("builtins.input", return_value="abc123")
    @patch("rm_mcp.cli.webbrowser.open")
    def test_setup_success(self, mock_browser, mock_input, mock_register, capsys):
        """Test successful --setup flow."""
        mock_register.return_value = '{"devicetoken":"tok","usertoken":""}'

        from rm_mcp.cli import _handle_setup

        _handle_setup()

        mock_browser.assert_called_once_with("https://my.remarkable.com/device/apps/connect")
        mock_register.assert_called_once_with("abc123")

        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "Successfully registered!" in captured.out
        assert "claude mcp add remarkable" in captured.out
        assert "claude_desktop_config.json" in captured.out
        assert "Claude Code" in captured.out
        assert "Claude Desktop" in captured.out

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    @patch("rm_mcp.cli.webbrowser.open")
    def test_setup_cancelled_keyboard_interrupt(self, mock_browser, mock_input):
        """Test that --setup handles KeyboardInterrupt gracefully."""
        from rm_mcp.cli import _handle_setup

        with pytest.raises(SystemExit) as exc_info:
            _handle_setup()
        assert exc_info.value.code == 0

    @patch("builtins.input", side_effect=EOFError)
    @patch("rm_mcp.cli.webbrowser.open")
    def test_setup_cancelled_eof(self, mock_browser, mock_input):
        """Test that --setup handles EOFError gracefully."""
        from rm_mcp.cli import _handle_setup

        with pytest.raises(SystemExit) as exc_info:
            _handle_setup()
        assert exc_info.value.code == 0

    @patch("builtins.input", return_value="")
    @patch("rm_mcp.cli.webbrowser.open")
    def test_setup_empty_code(self, mock_browser, mock_input):
        """Test that --setup rejects empty code."""
        from rm_mcp.cli import _handle_setup

        with pytest.raises(SystemExit) as exc_info:
            _handle_setup()
        assert exc_info.value.code == 1

    def test_setup_arg_parsing(self):
        """Test that --setup is recognized by argparse."""
        import argparse

        # Test that --setup is a valid argument
        parser = argparse.ArgumentParser()
        parser.add_argument("--setup", action="store_true")
        args = parser.parse_args(["--setup"])
        assert args.setup is True


class TestRegisterQuiet:
    """Test --register CODE --quiet flag."""

    @patch("rm_mcp.api.register_and_get_token")
    def test_register_quiet_outputs_raw_token(self, mock_register, capsys):
        """Test that --register CODE --quiet outputs only the raw token JSON."""
        mock_register.return_value = '{"devicetoken":"tok123","usertoken":""}'

        from rm_mcp.cli import main

        with patch("sys.argv", ["rm-mcp", "--register", "abc123", "--quiet"]):
            main()

        captured = capsys.readouterr()
        # Should contain only the raw token, no styling
        assert captured.out.strip() == '{"devicetoken":"tok123","usertoken":""}'
        assert "✓" not in captured.out
        assert "Step" not in captured.out
        assert "Claude" not in captured.out

    @patch("rm_mcp.api.register_and_get_token")
    def test_register_without_quiet_has_styling(self, mock_register, capsys):
        """Test that --register without --quiet still produces styled output."""
        mock_register.return_value = '{"devicetoken":"tok123","usertoken":""}'

        from rm_mcp.cli import main

        with patch("sys.argv", ["rm-mcp", "--register", "abc123"]):
            main()

        captured = capsys.readouterr()
        assert "Successfully registered!" in captured.out
        assert "claude mcp add remarkable" in captured.out

    def test_quiet_arg_parsing(self):
        """Test that --quiet is recognized by argparse."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--register", metavar="CODE")
        parser.add_argument("--quiet", action="store_true")
        args = parser.parse_args(["--register", "code", "--quiet"])
        assert args.quiet is True
        assert args.register == "code"


# =============================================================================
# Test Unauthenticated Mode
# =============================================================================


class TestUnauthenticatedMode:
    """Test that server works gracefully without authentication."""

    @patch("rm_mcp.api.get_rmapi", return_value=None)
    @patch(_PATCH_CACHED, side_effect=RuntimeError("Not authenticated. Run: uvx rm-mcp --setup"))
    @pytest.mark.asyncio
    async def test_tools_return_setup_instructions(self, mock_cached, mock_rmapi):
        """Test that tools return setup instructions when not authenticated."""
        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)

        assert data["authenticated"] is False
        assert "--setup" in data["_hint"]

    def test_get_rmapi_returns_none_when_no_token(self):
        """Test that get_rmapi returns None when no token is configured."""
        import rm_mcp.api as api_mod

        # Save and restore singleton
        old_singleton = api_mod._client_singleton
        old_token = api_mod.REMARKABLE_TOKEN
        try:
            api_mod._client_singleton = None
            api_mod.REMARKABLE_TOKEN = None

            with patch("pathlib.Path.exists", return_value=False):
                result = api_mod.get_rmapi()
                assert result is None
        finally:
            api_mod._client_singleton = old_singleton
            api_mod.REMARKABLE_TOKEN = old_token

    def test_cache_raises_when_no_client(self):
        """Test that get_cached_collection raises RuntimeError when client is None."""
        from rm_mcp.cache import get_cached_collection

        with patch("rm_mcp.api.get_rmapi", return_value=None):
            with pytest.raises(RuntimeError, match="Not authenticated"):
                get_cached_collection()


# =============================================================================
# End-to-End Tests
# =============================================================================


class TestE2E:
    """End-to-end tests for MCP server."""

    def test_server_can_initialize(self):
        """Test that server can be initialized."""
        assert mcp is not None
        assert mcp.name == "rm-mcp"

    @pytest.mark.asyncio
    async def test_server_lists_all_tools(self):
        """Test that server can list all tools (e2e)."""
        tools = await mcp.list_tools()

        assert len(tools) == 6

        # Check each tool has required properties and starts with remarkable_
        for tool in tools:
            assert hasattr(tool, "name")
            assert hasattr(tool, "description")
            assert tool.name.startswith("remarkable_")

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_e2e_call_tool_flow(self, mock_get_cached):
        """Test end-to-end flow of calling a tool."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        # Call status tool
        result = await mcp.call_tool("remarkable_status", {})

        # Verify we get valid JSON back
        data = json.loads(result[0][0].text)
        assert "authenticated" in data
        assert "_hint" in data

    @pytest.mark.asyncio
    async def test_tool_parameters_schema(self):
        """Test that tool parameters have proper schemas."""
        tools = await mcp.list_tools()

        # Check specific tools exist
        browse_tool = next(t for t in tools if t.name == "remarkable_browse")
        assert browse_tool is not None

        read_tool = next(t for t in tools if t.name == "remarkable_read")
        assert read_tool is not None

        recent_tool = next(t for t in tools if t.name == "remarkable_recent")
        assert recent_tool is not None

        status_tool = next(t for t in tools if t.name == "remarkable_status")
        assert status_tool is not None

    @pytest.mark.asyncio
    async def test_all_tools_return_json_with_hint(self):
        """Test that all tools return JSON with _hint field."""
        with patch(_PATCH_CACHED) as mock_get_cached:
            mock_client = Mock()
            mock_get_cached.return_value = (mock_client, [])

            # Test status
            result = await mcp.call_tool("remarkable_status", {})
            data = json.loads(result[0][0].text)
            assert "_hint" in data

            # Test browse
            result = await mcp.call_tool("remarkable_browse", {"path": "/"})
            data = json.loads(result[0][0].text)
            assert "_hint" in data or "_error" in data

            # Test recent
            result = await mcp.call_tool("remarkable_recent", {})
            data = json.loads(result[0][0].text)
            assert "_hint" in data or "_error" in data


# =============================================================================
# Test Response Consistency
# =============================================================================


class TestResponseConsistency:
    """Test that responses follow consistent patterns."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_all_errors_have_required_fields(self, mock_get_cached):
        """Test that all error responses have required fields."""
        mock_get_cached.side_effect = RuntimeError("Test error")

        tools_to_test = [
            ("remarkable_status", {}),
            ("remarkable_browse", {"path": "/"}),
            ("remarkable_recent", {}),
            ("remarkable_read", {"document": "test"}),
        ]

        for tool_name, args in tools_to_test:
            result = await mcp.call_tool(tool_name, args)
            data = json.loads(result[0][0].text)

            # Either success with _hint or error with _error
            has_hint = "_hint" in data
            has_error = "_error" in data

            assert has_hint or has_error, f"Tool {tool_name} response missing _hint or _error"

            if has_error:
                assert "type" in data["_error"], f"Error in {tool_name} missing type"
                assert "message" in data["_error"], f"Error in {tool_name} missing message"
                assert "suggestion" in data["_error"], f"Error in {tool_name} missing suggestion"


# =============================================================================
# Test Capability Checking
# =============================================================================


class TestCapabilityChecking:
    """Test capability checking utilities."""

    def test_get_client_capabilities_without_context(self):
        """Test get_client_capabilities returns None without valid context."""
        from rm_mcp.capabilities import get_client_capabilities

        # Create mock context without session
        mock_ctx = Mock()
        mock_ctx.session = None

        result = get_client_capabilities(mock_ctx)
        assert result is None

    def test_get_client_capabilities_without_client_params(self):
        """Test get_client_capabilities returns None without client_params."""
        from rm_mcp.capabilities import get_client_capabilities

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = None

        result = get_client_capabilities(mock_ctx)
        assert result is None

    def test_get_client_capabilities_with_valid_context(self):
        """Test get_client_capabilities returns capabilities when available."""
        from mcp.types import ClientCapabilities, SamplingCapability

        from rm_mcp.capabilities import get_client_capabilities

        mock_caps = ClientCapabilities(sampling=SamplingCapability())

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        result = get_client_capabilities(mock_ctx)
        assert result is not None
        assert result.sampling is not None

    def test_client_supports_sampling_true(self):
        """Test client_supports_sampling returns True when sampling available."""
        from mcp.types import ClientCapabilities, SamplingCapability

        from rm_mcp.capabilities import client_supports_sampling

        mock_caps = ClientCapabilities(sampling=SamplingCapability())

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        result = client_supports_sampling(mock_ctx)
        assert result is True

    def test_client_supports_sampling_false(self):
        """Test client_supports_sampling returns False when sampling not available."""
        from mcp.types import ClientCapabilities

        from rm_mcp.capabilities import client_supports_sampling

        mock_caps = ClientCapabilities(sampling=None)

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        result = client_supports_sampling(mock_ctx)
        assert result is False

    def test_client_supports_elicitation(self):
        """Test client_supports_elicitation."""
        from mcp.types import ClientCapabilities, ElicitationCapability

        from rm_mcp.capabilities import client_supports_elicitation

        # Test with elicitation enabled
        mock_caps = ClientCapabilities(elicitation=ElicitationCapability())

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_elicitation(mock_ctx) is True

        # Test with elicitation disabled
        mock_caps = ClientCapabilities(elicitation=None)
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_elicitation(mock_ctx) is False

    def test_client_supports_roots(self):
        """Test client_supports_roots."""
        from mcp.types import ClientCapabilities, RootsCapability

        from rm_mcp.capabilities import client_supports_roots

        # Test with roots enabled
        mock_caps = ClientCapabilities(roots=RootsCapability())

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_roots(mock_ctx) is True

        # Test with roots disabled
        mock_caps = ClientCapabilities(roots=None)
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_roots(mock_ctx) is False

    def test_client_supports_experimental(self):
        """Test client_supports_experimental."""
        from mcp.types import ClientCapabilities

        from rm_mcp.capabilities import client_supports_experimental

        # Test with experimental feature present
        mock_caps = ClientCapabilities(experimental={"my_feature": {}})

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_experimental(mock_ctx, "my_feature") is True
        assert client_supports_experimental(mock_ctx, "other_feature") is False

        # Test with no experimental features
        mock_caps = ClientCapabilities(experimental=None)
        mock_ctx.session.client_params.capabilities = mock_caps

        assert client_supports_experimental(mock_ctx, "my_feature") is False

    def test_get_client_info(self):
        """Test get_client_info."""
        from rm_mcp.capabilities import get_client_info

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.clientInfo = Mock()
        mock_ctx.session.client_params.clientInfo.name = "Test Client"
        mock_ctx.session.client_params.clientInfo.version = "1.0.0"
        mock_ctx.session.client_params.protocolVersion = "2024-11-05"

        result = get_client_info(mock_ctx)
        assert result is not None
        assert result["name"] == "Test Client"
        assert result["version"] == "1.0.0"
        assert result["protocol_version"] == "2024-11-05"

    def test_get_client_info_without_client_info(self):
        """Test get_client_info when clientInfo is None."""
        from rm_mcp.capabilities import get_client_info

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.clientInfo = None
        mock_ctx.session.client_params.protocolVersion = "2024-11-05"

        result = get_client_info(mock_ctx)
        assert result is not None
        assert result["name"] is None
        assert result["version"] is None
        assert result["protocol_version"] == "2024-11-05"

    def test_get_protocol_version(self):
        """Test get_protocol_version."""
        from rm_mcp.capabilities import get_protocol_version

        mock_ctx = Mock()
        mock_ctx.session = Mock()
        mock_ctx.session.client_params = Mock()
        mock_ctx.session.client_params.protocolVersion = "2024-11-05"

        result = get_protocol_version(mock_ctx)
        assert result == "2024-11-05"

    def test_get_protocol_version_without_context(self):
        """Test get_protocol_version returns None without valid context."""
        from rm_mcp.capabilities import get_protocol_version

        mock_ctx = Mock()
        mock_ctx.session = None

        result = get_protocol_version(mock_ctx)
        assert result is None

    def test_capability_imports_from_package(self):
        """Test that capability utilities can be imported from main package."""
        from rm_mcp import (
            client_supports_elicitation,
            client_supports_experimental,
            client_supports_roots,
            client_supports_sampling,
            get_client_capabilities,
            get_client_info,
            get_protocol_version,
        )

        # Verify all functions are callable
        assert callable(get_client_capabilities)
        assert callable(client_supports_sampling)
        assert callable(client_supports_elicitation)
        assert callable(client_supports_roots)
        assert callable(client_supports_experimental)
        assert callable(get_client_info)
        assert callable(get_protocol_version)


# =============================================================================
# Test Sampling OCR
# =============================================================================


class TestSamplingOCR:
    """Test sampling-based OCR functionality."""

    def test_get_ocr_backend_default(self):
        """Test default OCR backend is sampling."""
        import os

        from rm_mcp.ocr.sampling import get_ocr_backend

        # Clear any env var
        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        if "REMARKABLE_OCR_BACKEND" in os.environ:
            del os.environ["REMARKABLE_OCR_BACKEND"]

        try:
            result = get_ocr_backend()
            assert result == "sampling"
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup

    def test_get_ocr_backend_sampling(self):
        """Test OCR backend can be set to sampling."""
        import os

        from rm_mcp.ocr.sampling import get_ocr_backend

        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        os.environ["REMARKABLE_OCR_BACKEND"] = "sampling"

        try:
            result = get_ocr_backend()
            assert result == "sampling"
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup
            elif "REMARKABLE_OCR_BACKEND" in os.environ:
                del os.environ["REMARKABLE_OCR_BACKEND"]

    def test_should_use_sampling_ocr_true_when_default(self):
        """Test should_use_sampling_ocr returns True with default config and capable client."""
        import os

        from mcp.types import ClientCapabilities, SamplingCapability

        from rm_mcp.ocr.sampling import should_use_sampling_ocr

        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        if "REMARKABLE_OCR_BACKEND" in os.environ:
            del os.environ["REMARKABLE_OCR_BACKEND"]

        try:
            # Create mock context with sampling capability
            mock_caps = ClientCapabilities(sampling=SamplingCapability())
            mock_ctx = Mock()
            mock_ctx.session = Mock()
            mock_ctx.session.client_params = Mock()
            mock_ctx.session.client_params.capabilities = mock_caps

            # Should return True because default backend is "sampling"
            result = should_use_sampling_ocr(mock_ctx)
            assert result is True
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup

    def test_should_use_sampling_ocr_true_when_configured(self):
        """Test should_use_sampling_ocr returns True when configured and client supports it."""
        import os

        from mcp.types import ClientCapabilities, SamplingCapability

        from rm_mcp.ocr.sampling import should_use_sampling_ocr

        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        os.environ["REMARKABLE_OCR_BACKEND"] = "sampling"

        try:
            # Create mock context with sampling capability
            mock_caps = ClientCapabilities(sampling=SamplingCapability())
            mock_ctx = Mock()
            mock_ctx.session = Mock()
            mock_ctx.session.client_params = Mock()
            mock_ctx.session.client_params.capabilities = mock_caps

            result = should_use_sampling_ocr(mock_ctx)
            assert result is True
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup
            elif "REMARKABLE_OCR_BACKEND" in os.environ:
                del os.environ["REMARKABLE_OCR_BACKEND"]

    def test_should_use_sampling_ocr_false_when_client_doesnt_support(self):
        """Test should_use_sampling_ocr returns False when client doesn't support sampling."""
        import os

        from mcp.types import ClientCapabilities

        from rm_mcp.ocr.sampling import should_use_sampling_ocr

        env_backup = os.environ.get("REMARKABLE_OCR_BACKEND")
        os.environ["REMARKABLE_OCR_BACKEND"] = "sampling"

        try:
            # Create mock context WITHOUT sampling capability
            mock_caps = ClientCapabilities(sampling=None)
            mock_ctx = Mock()
            mock_ctx.session = Mock()
            mock_ctx.session.client_params = Mock()
            mock_ctx.session.client_params.capabilities = mock_caps

            result = should_use_sampling_ocr(mock_ctx)
            assert result is False
        finally:
            if env_backup is not None:
                os.environ["REMARKABLE_OCR_BACKEND"] = env_backup
            elif "REMARKABLE_OCR_BACKEND" in os.environ:
                del os.environ["REMARKABLE_OCR_BACKEND"]

    def test_ocr_system_prompt_structure(self):
        """Test the OCR system prompt is properly structured."""
        from rm_mcp.ocr.sampling import OCR_SYSTEM_PROMPT, OCR_USER_PROMPT

        # Check that system prompt contains key instructions
        assert "OCR" in OCR_SYSTEM_PROMPT
        assert "ONLY" in OCR_SYSTEM_PROMPT
        assert "[NO TEXT DETECTED]" in OCR_SYSTEM_PROMPT
        assert "reMarkable" in OCR_SYSTEM_PROMPT

        # Check user prompt is concise
        assert "text" in OCR_USER_PROMPT.lower()
        assert len(OCR_USER_PROMPT) < 200  # Should be short and focused

    @pytest.mark.asyncio
    async def test_ocr_via_sampling_returns_none_without_session(self):
        """Test ocr_via_sampling returns None when session is not available."""
        from rm_mcp.ocr.sampling import ocr_via_sampling

        mock_ctx = Mock()
        mock_ctx.session = None

        result = await ocr_via_sampling(mock_ctx, b"fake_png_data")
        assert result is None

    def test_sampling_imports_from_module(self):
        """Test that sampling utilities can be imported."""
        from rm_mcp.ocr.sampling import (
            OCR_SYSTEM_PROMPT,
            OCR_USER_PROMPT,
            get_ocr_backend,
            ocr_pages_via_sampling,
            ocr_via_sampling,
            should_use_sampling_ocr,
        )

        # Verify all functions/constants are accessible
        assert callable(ocr_via_sampling)
        assert callable(ocr_pages_via_sampling)
        assert callable(get_ocr_backend)
        assert callable(should_use_sampling_ocr)
        assert isinstance(OCR_SYSTEM_PROMPT, str)
        assert isinstance(OCR_USER_PROMPT, str)


# =============================================================================
# Test Root Path Filtering (api.py)
# =============================================================================


class TestRootPathFiltering:
    """Test the root path utilities in api.py for REMARKABLE_ROOT_PATH support."""

    @patch.dict(os.environ, {"REMARKABLE_ROOT_PATH": ""})
    def test_get_root_path_empty(self):
        """Test _get_root_path returns '/' for empty env var."""
        from rm_mcp.paths import _get_root_path

        assert _get_root_path() == "/"

    @patch.dict(os.environ, {"REMARKABLE_ROOT_PATH": "/"})
    def test_get_root_path_slash(self):
        """Test _get_root_path returns '/' for '/' env var."""
        from rm_mcp.paths import _get_root_path

        assert _get_root_path() == "/"

    @patch.dict(os.environ, {"REMARKABLE_ROOT_PATH": "/Work"})
    def test_get_root_path_with_leading_slash(self):
        """Test _get_root_path normalizes '/Work' correctly."""
        from rm_mcp.paths import _get_root_path

        assert _get_root_path() == "/Work"

    @patch.dict(os.environ, {"REMARKABLE_ROOT_PATH": "Work"})
    def test_get_root_path_without_leading_slash(self):
        """Test _get_root_path adds leading slash when missing."""
        from rm_mcp.paths import _get_root_path

        assert _get_root_path() == "/Work"

    @patch.dict(os.environ, {"REMARKABLE_ROOT_PATH": "/Work/"})
    def test_get_root_path_with_trailing_slash(self):
        """Test _get_root_path strips trailing slash."""
        from rm_mcp.paths import _get_root_path

        assert _get_root_path() == "/Work"

    def test_is_within_root_full_access(self):
        """Test _is_within_root always returns True when root is '/'."""
        from rm_mcp.paths import _is_within_root

        assert _is_within_root("/anything", "/") is True
        assert _is_within_root("/Work/Deep/Path", "/") is True
        assert _is_within_root("/", "/") is True

    def test_is_within_root_inside(self):
        """Test _is_within_root returns True for paths inside the root."""
        from rm_mcp.paths import _is_within_root

        assert _is_within_root("/Work/Project", "/Work") is True
        assert _is_within_root("/Work", "/Work") is True
        assert _is_within_root("/Work/Deep/Nested", "/Work") is True

    def test_is_within_root_outside(self):
        """Test _is_within_root returns False for paths outside the root."""
        from rm_mcp.paths import _is_within_root

        assert _is_within_root("/Personal/Notes", "/Work") is False
        assert _is_within_root("/Workspace", "/Work") is False
        assert _is_within_root("/", "/Work") is False

    def test_is_within_root_case_insensitive(self):
        """Test _is_within_root is case-insensitive."""
        from rm_mcp.paths import _is_within_root

        assert _is_within_root("/work/Project", "/Work") is True
        assert _is_within_root("/WORK/Project", "/Work") is True

    def test_apply_root_filter_no_root(self):
        """Test _apply_root_filter is a no-op when root is '/'."""
        from rm_mcp.paths import _apply_root_filter

        assert _apply_root_filter("/Work/Project", "/") == "/Work/Project"
        assert _apply_root_filter("/Notes", "/") == "/Notes"

    def test_apply_root_filter_strips_prefix(self):
        """Test _apply_root_filter strips the root prefix from paths."""
        from rm_mcp.paths import _apply_root_filter

        assert _apply_root_filter("/Work/Project", "/Work") == "/Project"
        assert _apply_root_filter("/Work/Deep/Path", "/Work") == "/Deep/Path"

    def test_apply_root_filter_root_equals_path(self):
        """Test _apply_root_filter returns '/' when path equals root."""
        from rm_mcp.paths import _apply_root_filter

        assert _apply_root_filter("/Work", "/Work") == "/"

    def test_apply_root_filter_outside_root(self):
        """Test _apply_root_filter returns path unchanged when outside root."""
        from rm_mcp.paths import _apply_root_filter

        assert _apply_root_filter("/Personal/Notes", "/Work") == "/Personal/Notes"

    @patch.dict(os.environ, {"REMARKABLE_ROOT_PATH": "/Work"})
    def test_apply_root_filter_uses_env_when_root_none(self):
        """Test _apply_root_filter reads from env var when root is None."""
        from rm_mcp.paths import _apply_root_filter

        assert _apply_root_filter("/Work/Project") == "/Project"


# =============================================================================
# Test remarkable_recent Tool (additional coverage)
# =============================================================================


class TestRemarkableRecentExtended:
    """Test remarkable_recent tool with document sorting and preview support."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_recent_sorted_by_modification_date(self, mock_get_cached):
        """Test that recent documents are returned sorted by modification date (newest first)."""
        mock_client = Mock()

        doc_old = Mock()
        doc_old.VissibleName = "Old Document"
        doc_old.ID = "doc-old"
        doc_old.Parent = ""
        doc_old.is_folder = False
        doc_old.is_cloud_archived = False
        doc_old.ModifiedClient = "2024-01-01T00:00:00Z"

        doc_new = Mock()
        doc_new.VissibleName = "New Document"
        doc_new.ID = "doc-new"
        doc_new.Parent = ""
        doc_new.is_folder = False
        doc_new.is_cloud_archived = False
        doc_new.ModifiedClient = "2024-06-15T12:00:00Z"

        doc_mid = Mock()
        doc_mid.VissibleName = "Mid Document"
        doc_mid.ID = "doc-mid"
        doc_mid.Parent = ""
        doc_mid.is_folder = False
        doc_mid.is_cloud_archived = False
        doc_mid.ModifiedClient = "2024-03-10T08:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc_old, doc_new, doc_mid])

        result = await mcp.call_tool("remarkable_recent", {"limit": 10})
        data = json.loads(result[0][0].text)

        assert data["count"] == 3
        names = [d["name"] for d in data["documents"]]
        assert names == ["New Document", "Mid Document", "Old Document"]

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_recent_with_include_preview(self, mock_get_cached):
        """Test remarkable_recent with include_preview=True for a non-notebook document."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Report.pdf"
        doc.ID = "doc-pdf"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])

        # Create a minimal zip with a text file for extraction
        import io

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("sample.txt", "Preview text content here")
        zip_bytes = zip_buffer.getvalue()

        mock_client.download.return_value = zip_bytes

        # Also need to mock _get_file_type_cached to return "pdf" so preview is attempted
        with patch("rm_mcp.tools._helpers._get_file_type_cached", return_value="pdf"):
            result = await mcp.call_tool(
                "remarkable_recent", {"limit": 5, "include_preview": True}
            )
            data = json.loads(result[0][0].text)

        assert data["count"] == 1
        # The preview might or might not have content depending on extraction
        assert "documents" in data

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_recent_empty_library(self, mock_get_cached):
        """Test remarkable_recent with no documents in the library."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool("remarkable_recent", {})
        data = json.loads(result[0][0].text)

        assert data["count"] == 0
        assert data["documents"] == []
        assert "_hint" in data

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_recent_excludes_folders(self, mock_get_cached):
        """Test that remarkable_recent excludes folder items."""
        mock_client = Mock()

        folder = Mock()
        folder.VissibleName = "My Folder"
        folder.ID = "folder-1"
        folder.Parent = ""
        folder.is_folder = True
        folder.is_cloud_archived = False
        folder.ModifiedClient = "2024-12-01T00:00:00Z"

        doc = Mock()
        doc.VissibleName = "My Document"
        doc.ID = "doc-1"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [folder, doc])

        result = await mcp.call_tool("remarkable_recent", {})
        data = json.loads(result[0][0].text)

        assert data["count"] == 1
        assert data["documents"][0]["name"] == "My Document"


# =============================================================================
# Test remarkable_image Tool (additional coverage)
# =============================================================================


class TestRemarkableImageExtended:
    """Test remarkable_image tool cache hits and edge cases."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_image_cache_hit(self, mock_get_cached):
        """Test that remarkable_image returns cached image when available."""
        from rm_mcp.tools._helpers import _rendered_image_cache

        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Cached Doc"
        doc.ID = "doc-cached"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])

        # Create a minimal valid zip for the download mock (needs .rm file for page count)
        import io

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("doc-cached.content", '{"pages": ["page-id-1"]}')
            zf.writestr("doc-cached/page-id-1.rm", b"dummy rm data")
        mock_client.download.return_value = zip_buffer.getvalue()

        # Pre-populate the image cache
        cache_key = f"{doc.ID}:1"
        fake_base64 = "aVZCT1J3MEtHZ29BQQ=="  # fake base64 PNG data
        _rendered_image_cache[cache_key] = fake_base64

        try:
            result = await mcp.call_tool(
                "remarkable_image",
                {"document": "Cached Doc", "page": 1, "compatibility": True},
            )
            data = json.loads(result[0].text)

            assert "data_uri" in data
            assert fake_base64 in data["data_uri"]
            assert data["mime_type"] == "image/png"
            assert data["page"] == 1
        finally:
            # Clean up cache
            _rendered_image_cache.pop(cache_key, None)

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_image_document_not_found_with_suggestions(self, mock_get_cached):
        """Test that image tool returns not_found error with suggestions for similar names."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Meeting Notes"
        doc.ID = "doc-meeting"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])

        result = await mcp.call_tool(
            "remarkable_image", {"document": "Meeting Noets"}
        )
        data = json.loads(result[0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "document_not_found"


# =============================================================================
# Test Collection Cache (api.py)
# =============================================================================


class TestCollectionCache:
    """Test the caching logic in api.py for get_cached_collection."""

    def setup_method(self):
        """Save original cache state before each test."""
        import rm_mcp.api as api_mod
        import rm_mcp.cache as cache_mod

        self._orig_collection = cache_mod._cached_collection
        self._orig_hash = cache_mod._cached_root_hash
        self._orig_timestamp = cache_mod._cache_timestamp
        self._orig_client = api_mod._client_singleton

    def teardown_method(self):
        """Restore original cache state after each test."""
        import rm_mcp.api as api_mod
        import rm_mcp.cache as cache_mod

        cache_mod._cached_collection = self._orig_collection
        cache_mod._cached_root_hash = self._orig_hash
        cache_mod._cache_timestamp = self._orig_timestamp
        api_mod._client_singleton = self._orig_client

    @patch("rm_mcp.api.get_rmapi")
    def test_cache_returns_within_ttl(self, mock_get_rmapi):
        """Test that get_cached_collection returns cache when within TTL."""
        import rm_mcp.api as api_mod
        import rm_mcp.cache as cache_mod
        from rm_mcp.cache import get_cached_collection

        mock_client = Mock()
        mock_get_rmapi.return_value = mock_client

        # Populate the cache
        fake_collection = [Mock(), Mock()]
        cache_mod._cached_collection = fake_collection
        cache_mod._cache_timestamp = time.time()  # Just now
        api_mod._client_singleton = mock_client

        client, collection = get_cached_collection()

        assert collection is fake_collection
        assert client is mock_client
        # Should NOT have called get_meta_items since cache is valid
        mock_client.get_meta_items.assert_not_called()

    @patch("rm_mcp.api.get_rmapi")
    def test_cache_checks_root_hash_when_expired(self, mock_get_rmapi):
        """Test that get_cached_collection checks root hash when cache is expired."""
        import rm_mcp.api as api_mod
        import rm_mcp.cache as cache_mod
        from rm_mcp.cache import get_cached_collection

        mock_client = Mock()
        mock_client.get_root_hash.return_value = "same_hash"
        mock_get_rmapi.return_value = mock_client

        # Populate cache with expired timestamp
        fake_collection = [Mock()]
        cache_mod._cached_collection = fake_collection
        cache_mod._cached_root_hash = "same_hash"
        cache_mod._cache_timestamp = 0.0  # Expired long ago
        api_mod._client_singleton = mock_client

        client, collection = get_cached_collection()

        # Root hash unchanged, so should return cached collection
        assert collection is fake_collection
        mock_client.get_root_hash.assert_called_once()
        mock_client.get_meta_items.assert_not_called()

    def test_invalidate_collection_cache(self):
        """Test that invalidate_collection_cache clears all cache state."""
        import rm_mcp.cache as cache_mod
        from rm_mcp.cache import invalidate_collection_cache

        # Set up some cache state
        cache_mod._cached_collection = [Mock()]
        cache_mod._cached_root_hash = "some_hash"
        cache_mod._cache_timestamp = time.time()

        invalidate_collection_cache()

        assert cache_mod._cached_collection is None
        assert cache_mod._cached_root_hash is None
        assert cache_mod._cache_timestamp == 0.0

    @patch("rm_mcp.api.get_rmapi")
    def test_set_cached_collection(self, mock_get_rmapi):
        """Test that set_cached_collection populates the cache."""
        import rm_mcp.api as api_mod
        import rm_mcp.cache as cache_mod
        from rm_mcp.cache import set_cached_collection

        mock_client = Mock()
        # Mock client without get_root_hash support
        del mock_client.get_root_hash

        fake_collection = [Mock(), Mock()]
        set_cached_collection(mock_client, fake_collection)

        assert cache_mod._cached_collection is fake_collection
        assert api_mod._client_singleton is mock_client
        assert cache_mod._cache_timestamp > 0

    @patch("rm_mcp.api.get_rmapi")
    def test_cache_refetches_when_root_hash_changed(self, mock_get_rmapi):
        """Test that cache does a full refetch when root hash has changed."""
        import rm_mcp.api as api_mod
        import rm_mcp.cache as cache_mod
        from rm_mcp.cache import get_cached_collection

        mock_client = Mock()
        mock_client.get_root_hash.return_value = "new_hash"
        new_collection = [Mock(), Mock(), Mock()]
        mock_client.get_meta_items.return_value = new_collection
        mock_get_rmapi.return_value = mock_client

        # Populate cache with old hash and expired timestamp
        cache_mod._cached_collection = [Mock()]
        cache_mod._cached_root_hash = "old_hash"
        cache_mod._cache_timestamp = 0.0  # Expired
        api_mod._client_singleton = mock_client

        client, collection = get_cached_collection()

        assert collection is new_collection
        mock_client.get_root_hash.assert_called_once()
        mock_client.get_meta_items.assert_called_once_with(root_hash="new_hash")


# =============================================================================
# Test OCR Auto-Retry in remarkable_read
# =============================================================================


class TestOCRAutoRetry:
    """Test the auto-retry with OCR logic in remarkable_read.

    When remarkable_read gets empty content from a notebook (total_chars == 0,
    not PDF/EPUB), it should auto-retry with include_ocr=True.
    """

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_auto_retry_with_ocr_on_empty_notebook(self, mock_get_cached):
        """Test that empty notebook content triggers OCR auto-retry."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Handwritten Notes"
        doc.ID = "doc-hw"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-15T10:30:00Z"

        mock_get_cached.return_value = (mock_client, [doc])

        # Create a zip that produces no typed text but has OCR content
        import io

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            # Add a minimal content file
            zf.writestr("doc-hw.content", '{"pages": ["page1"]}')
        zip_bytes = zip_buffer.getvalue()

        mock_client.download.return_value = zip_bytes

        # First call: extract returns empty content (no typed text)
        # Second call (with include_ocr=True): returns OCR content
        empty_content = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }
        ocr_content = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": ["Hello from OCR"],
            "pages": 1,
            "ocr_backend": "sampling",
        }

        with (
            patch("rm_mcp.tools._helpers._get_file_type_cached", return_value="notebook"),
            patch(
                "rm_mcp.tools._helpers.extract_text_from_document_zip",
                side_effect=[empty_content, ocr_content],
            ),
        ):
            result = await mcp.call_tool(
                "remarkable_read", {"document": "Handwritten Notes"}
            )
            data = json.loads(result[0][0].text)

        # Should have auto-enabled OCR
        assert data.get("_ocr_auto_enabled") is True
        assert "OCR auto-enabled" in data.get("_hint", "")


# =============================================================================
# Test _temp_document Context Manager
# =============================================================================


class TestTempDocumentContextManager:
    """Test the _temp_document context manager for temp file lifecycle."""

    def test_creates_and_cleans_up_temp_file(self):
        """Test that _temp_document creates a temp file and cleans it up."""
        from rm_mcp.tools._helpers import _temp_document

        temp_path = None
        with _temp_document(b"test data", suffix=".zip") as path:
            temp_path = path
            assert path.exists()
            assert path.read_bytes() == b"test data"
            assert str(path).endswith(".zip")

        # After exiting context, file should be cleaned up
        assert not temp_path.exists()

    def test_cleanup_on_exception(self):
        """Test that cleanup happens even if an exception occurs inside the with block."""
        from rm_mcp.tools._helpers import _temp_document

        temp_path = None
        with pytest.raises(ValueError, match="test error"):
            with _temp_document(b"important data") as path:
                temp_path = path
                assert path.exists()
                raise ValueError("test error")

        # File should still be cleaned up despite the exception
        assert temp_path is not None
        assert not temp_path.exists()

    def test_yields_path_object(self):
        """Test that _temp_document yields a Path object."""
        from rm_mcp.tools._helpers import _temp_document

        with _temp_document(b"data") as path:
            assert isinstance(path, Path)


# =============================================================================
# Test File Type Caching
# =============================================================================


class TestFileTypeCaching:
    """Test _get_file_type_cached caching behavior in tools.py."""

    def setup_method(self):
        """Clear the file type cache before each test."""
        from rm_mcp.tools._helpers import _file_type_cache

        self._saved_cache = dict(_file_type_cache)
        _file_type_cache.clear()

    def teardown_method(self):
        """Restore the file type cache after each test."""
        from rm_mcp.tools._helpers import _file_type_cache

        _file_type_cache.clear()
        _file_type_cache.update(self._saved_cache)

    def test_caches_result_on_first_call(self):
        """Test that the first call caches the file type result."""
        from rm_mcp.tools._helpers import _file_type_cache, _get_file_type_cached

        mock_client = Mock()
        mock_doc = Mock()
        mock_doc.ID = "doc-cache-test"
        mock_doc.VissibleName = "test.pdf"

        with patch("rm_mcp.tools._helpers.get_file_type", return_value="pdf") as mock_get:
            result = _get_file_type_cached(mock_client, mock_doc)
            assert result == "pdf"
            mock_get.assert_called_once_with(mock_client, mock_doc)

            # Verify it was cached
            assert "doc-cache-test" in _file_type_cache
            assert _file_type_cache["doc-cache-test"] == "pdf"

    def test_returns_cached_on_subsequent_calls(self):
        """Test that repeated calls with the same doc ID return cached result."""
        from rm_mcp.tools._helpers import _get_file_type_cached

        mock_client = Mock()
        mock_doc = Mock()
        mock_doc.ID = "doc-repeat"
        mock_doc.VissibleName = "notes"

        with patch("rm_mcp.tools._helpers.get_file_type", return_value="notebook") as mock_get:
            # First call
            result1 = _get_file_type_cached(mock_client, mock_doc)
            # Second call
            result2 = _get_file_type_cached(mock_client, mock_doc)

            assert result1 == "notebook"
            assert result2 == "notebook"
            # get_file_type should only be called once (second call uses cache)
            mock_get.assert_called_once()

    def test_different_doc_ids_cached_separately(self):
        """Test that different doc IDs are cached separately."""
        from rm_mcp.tools._helpers import _file_type_cache, _get_file_type_cached

        mock_client = Mock()

        doc1 = Mock()
        doc1.ID = "doc-1"
        doc1.VissibleName = "report.pdf"

        doc2 = Mock()
        doc2.ID = "doc-2"
        doc2.VissibleName = "notes"

        with patch("rm_mcp.tools._helpers.get_file_type", side_effect=["pdf", "notebook"]):
            result1 = _get_file_type_cached(mock_client, doc1)
            result2 = _get_file_type_cached(mock_client, doc2)

        assert result1 == "pdf"
        assert result2 == "notebook"
        assert _file_type_cache["doc-1"] == "pdf"
        assert _file_type_cache["doc-2"] == "notebook"


# =============================================================================
# Test remarkable_search Tool
# =============================================================================


class TestRemarkableSearch:
    """Test remarkable_search tool."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_search_finds_matching_documents(self, mock_get_cached):
        """Test that search finds documents matching the query (no download without grep)."""
        mock_client = Mock()

        doc1 = Mock()
        doc1.VissibleName = "Meeting Notes January"
        doc1.ID = "doc-1"
        doc1.Parent = ""
        doc1.is_folder = False
        doc1.is_cloud_archived = False
        doc1.ModifiedClient = "2024-01-15T10:00:00Z"

        doc2 = Mock()
        doc2.VissibleName = "Meeting Notes February"
        doc2.ID = "doc-2"
        doc2.Parent = ""
        doc2.is_folder = False
        doc2.is_cloud_archived = False
        doc2.ModifiedClient = "2024-02-15T10:00:00Z"

        doc3 = Mock()
        doc3.VissibleName = "Project Plan"
        doc3.ID = "doc-3"
        doc3.Parent = ""
        doc3.is_folder = False
        doc3.is_cloud_archived = False
        doc3.ModifiedClient = "2024-03-01T10:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc1, doc2, doc3])

        result = await mcp.call_tool("remarkable_search", {"query": "Meeting"})
        data = json.loads(result[0][0].text)

        assert data["count"] == 2
        names = [d["name"] for d in data["documents"]]
        assert "Meeting Notes January" in names
        assert "Meeting Notes February" in names
        assert "Project Plan" not in names
        # Without grep, no content should be downloaded
        assert "content" not in data["documents"][0]
        mock_client.download.assert_not_called()

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_search_no_results(self, mock_get_cached):
        """Test search returns error when no documents match."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Project Plan"
        doc.ID = "doc-1"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])

        result = await mcp.call_tool(
            "remarkable_search", {"query": "NonExistentDocument"}
        )
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "no_documents_found"

    @pytest.mark.asyncio
    @patch("rm_mcp.tools.read.remarkable_read")
    @patch(_PATCH_CACHED)
    async def test_search_with_grep(self, mock_get_cached, mock_read):
        """Test search with grep parameter returns grep_matches."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Research Paper"
        doc.ID = "doc-rp"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])

        # Mock remarkable_read to return content with grep matches
        mock_read.return_value = json.dumps(
            {
                "content": "The hypothesis was confirmed by the experiment.",
                "total_pages": 1,
                "document": "Research Paper",
                "path": "/Research Paper",
                "grep": "hypothesis",
                "grep_matches": 1,
            }
        )

        result = await mcp.call_tool(
            "remarkable_search", {"query": "Research", "grep": "hypothesis"}
        )
        data = json.loads(result[0][0].text)

        assert data["count"] == 1
        assert data["grep"] == "hypothesis"
        assert data["documents"][0]["grep_matches"] == 1

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_search_limit_clamped(self, mock_get_cached):
        """Test that search limit is clamped to max of 5."""
        mock_client = Mock()

        # Create 7 documents matching the query
        docs = []
        for i in range(7):
            doc = Mock()
            doc.VissibleName = f"Note {i}"
            doc.ID = f"doc-{i}"
            doc.Parent = ""
            doc.is_folder = False
            doc.is_cloud_archived = False
            doc.ModifiedClient = f"2024-01-{10 + i:02d}T00:00:00Z"
            docs.append(doc)

        mock_get_cached.return_value = (mock_client, docs)

        result = await mcp.call_tool(
            "remarkable_search", {"query": "Note", "limit": 10}
        )
        data = json.loads(result[0][0].text)

        # Even though limit=10 was passed, max is 5
        assert data["count"] <= 5


# =============================================================================
# Test remarkable_read Happy Path
# =============================================================================


class TestRemarkableReadHappyPath:
    """Test remarkable_read tool with actual content extraction."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_read_notebook_with_typed_text(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test reading a notebook with typed text content."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Typed Notes"
        doc.ID = "doc-typed"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-05-01T12:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"

        # Mock download to return some bytes (the zip itself is not parsed
        # because we mock extract_text_from_document_zip)
        mock_client.download.return_value = b"fake-zip-data"

        # Mock extraction to return typed text content
        mock_extract.return_value = {
            "typed_text": ["Hello world, this is typed text from the notebook."],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read", {"document": "Typed Notes"}
        )
        data = json.loads(result[0][0].text)

        assert "content" in data
        assert "Hello world" in data["content"]
        assert data["name"] == "Typed Notes"
        assert data["file_type"] == "notebook"
        assert "page" in data
        assert "total_pages" in data
        assert "_hint" in data

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_read_pdf_with_content(
        self, mock_get_cached, mock_extract_zip, mock_file_type
    ):
        """Test reading a PDF document with annotation content."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Report.pdf"
        doc.ID = "doc-pdf"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-04-01T09:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "pdf"

        # Mock the zip extraction for annotations
        mock_client.download.return_value = b"fake-zip-data"
        mock_extract_zip.return_value = {
            "typed_text": ["Annotation on the PDF report."],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read", {"document": "Report.pdf"}
        )
        data = json.loads(result[0][0].text)

        assert data["file_type"] == "pdf"
        assert "Annotation on the PDF report" in data["content"]
        assert data["name"] == "Report.pdf"

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_read_page_out_of_range(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test reading a page that doesn't exist returns page_out_of_range error."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Short Doc"
        doc.ID = "doc-short"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip-data"

        # Return content that has only 1 page worth of text
        mock_extract.return_value = {
            "typed_text": ["A short document with some content."],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read", {"document": "Short Doc", "page": 999}
        )
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "page_out_of_range"


# =============================================================================
# Test remarkable_read Grep Functionality
# =============================================================================


class TestRemarkableReadGrep:
    """Test grep functionality in remarkable_read."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_grep_with_matches(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test grep that finds matching content."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Searchable Doc"
        doc.ID = "doc-search"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T00:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip-data"

        mock_extract.return_value = {
            "typed_text": [
                "The installation process requires sudo privileges. "
                "Run the installation command with elevated permissions."
            ],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "Searchable Doc", "grep": "installation"},
        )
        data = json.loads(result[0][0].text)

        assert "grep_matches" in data
        assert data["grep_matches"] > 0
        assert data["grep"] == "installation"

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_grep_no_matches(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test grep that finds no matching content returns empty."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Some Document"
        doc.ID = "doc-nomatch"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T00:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip-data"

        mock_extract.return_value = {
            "typed_text": ["This document contains some ordinary text about cooking."],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "Some Document", "grep": "quantum_physics"},
        )
        data = json.loads(result[0][0].text)

        # grep with no matches should result in empty content
        assert data.get("content") == "" or data.get("grep_matches", 0) == 0

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_grep_invalid_regex(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test that an invalid regex pattern returns invalid_grep error."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Regex Test Doc"
        doc.ID = "doc-regex"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T00:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip-data"

        mock_extract.return_value = {
            "typed_text": ["Some text content for regex testing."],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "Regex Test Doc", "grep": "[invalid"},
        )
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "invalid_grep"


# =============================================================================
# Test Browse Auto-Redirect to Read
# =============================================================================


class TestBrowseAutoRedirect:
    """Test that remarkable_browse auto-redirects to remarkable_read for documents."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools.read.remarkable_read", new_callable=AsyncMock)
    @patch(_PATCH_CACHED)
    async def test_browse_redirects_to_read_for_document_path(
        self, mock_get_cached, mock_read
    ):
        """Test that browsing to a document path auto-redirects to read."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "My Report"
        doc.ID = "doc-report"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-07-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])

        # Mock remarkable_read to return a valid read response
        mock_read.return_value = json.dumps(
            {
                "name": "My Report",
                "path": "/My Report",
                "content": "Report content here",
                "page": 1,
                "total_pages": 1,
                "total_chars": 19,
                "more": False,
                "_hint": "Page 1/1 (complete).",
            }
        )

        result = await mcp.call_tool(
            "remarkable_browse", {"path": "/My Report"}
        )
        data = json.loads(result[0][0].text)

        assert "_redirected_from" in data
        assert data["_redirected_from"] == "browse:/My Report"
        assert data["name"] == "My Report"
        assert "content" in data


# =============================================================================
# Test DocumentIndex (SQLite FTS5)
# =============================================================================


class TestDocumentIndex:
    """Test the DocumentIndex SQLite FTS5 index."""

    def _make_index(self):
        """Create a fresh in-memory DocumentIndex."""
        from rm_mcp.index import DocumentIndex

        return DocumentIndex(":memory:")

    def test_schema_creation(self):
        """Test that schema is created on initialization."""
        idx = self._make_index()
        conn = idx._get_connection()
        # Check tables exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "documents" in table_names
        assert "pages" in table_names
        assert "pages_fts" in table_names
        idx.close()

    def test_upsert_and_get_document_hash(self):
        """Test upserting a document and retrieving its hash."""
        idx = self._make_index()
        idx.upsert_document(
            doc_id="doc-1",
            doc_hash="hash-abc",
            name="Test Doc",
            path="/Test Doc",
            file_type="notebook",
        )
        assert idx.get_document_hash("doc-1") == "hash-abc"
        assert idx.get_document_hash("nonexistent") is None
        idx.close()

    def test_upsert_document_updates_existing(self):
        """Test that upsert updates existing documents."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="hash-1", name="V1")
        idx.upsert_document(doc_id="doc-1", doc_hash="hash-2", name="V2")
        assert idx.get_document_hash("doc-1") == "hash-2"
        idx.close()

    def test_needs_reindex_new_document(self):
        """Test needs_reindex returns True for unknown documents."""
        idx = self._make_index()
        assert idx.needs_reindex("doc-new", "any-hash") is True
        idx.close()

    def test_needs_reindex_same_hash(self):
        """Test needs_reindex returns False when hash matches."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="hash-1")
        assert idx.needs_reindex("doc-1", "hash-1") is False
        idx.close()

    def test_needs_reindex_different_hash_clears_pages(self):
        """Test needs_reindex returns True and clears pages when hash differs."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="hash-1")
        idx.upsert_page("doc-1", 1, "old content", "typed_text")
        assert idx.needs_reindex("doc-1", "hash-2") is True
        # Pages should have been cleared
        assert idx.get_page_ocr("doc-1", 1) is None
        idx.close()

    def test_upsert_and_get_page_ocr(self):
        """Test upserting a page and retrieving OCR content."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1")
        idx.upsert_page("doc-1", 1, "Hello world from OCR", "ocr", "sampling")
        result = idx.get_page_ocr("doc-1", 1, "sampling")
        assert result == "Hello world from OCR"
        # Non-existent page
        assert idx.get_page_ocr("doc-1", 99) is None
        idx.close()

    def test_store_extraction_result(self):
        """Test store_extraction_result round-trip."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1")
        result = {
            "typed_text": ["Typed content here"],
            "highlights": ["Important highlight"],
            "handwritten_text": ["OCR text from handwriting"],
            "pages": 1,
            "ocr_backend": "sampling",
        }
        idx.store_extraction_result("doc-1", result)

        # Verify typed text was stored
        conn = idx._get_connection()
        row = conn.execute(
            "SELECT content FROM pages WHERE doc_id='doc-1' AND content_type='typed_text'"
        ).fetchone()
        assert row is not None
        assert "Typed content here" in row["content"]

        # Verify highlight was stored
        row = conn.execute(
            "SELECT content FROM pages WHERE doc_id='doc-1' AND content_type='highlight'"
        ).fetchone()
        assert row is not None
        assert "Important highlight" in row["content"]

        # Verify OCR was stored
        row = conn.execute(
            "SELECT content FROM pages WHERE doc_id='doc-1' AND content_type='ocr'"
        ).fetchone()
        assert row is not None
        assert "OCR text from handwriting" in row["content"]
        idx.close()

    def test_fts5_search_basic(self):
        """Test basic FTS5 search."""
        idx = self._make_index()
        idx.upsert_document(
            doc_id="doc-1", doc_hash="h1", name="Notes",
            path="/Notes", file_type="notebook",
        )
        idx.upsert_page("doc-1", 0, "The quantum physics experiment yielded results", "typed_text")

        results = idx.search("quantum")
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc-1"
        assert results[0]["name"] == "Notes"
        assert "quantum" in results[0]["snippet"].lower()
        idx.close()

    def test_fts5_search_phrase(self):
        """Test FTS5 search with a phrase query."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1", name="Meeting", path="/Meeting")
        idx.upsert_page("doc-1", 0, "The project deadline is next Friday", "typed_text")

        results = idx.search('"project deadline"')
        assert len(results) == 1
        idx.close()

    def test_fts5_search_no_results(self):
        """Test FTS5 search with no matching results."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1", name="Notes", path="/Notes")
        idx.upsert_page("doc-1", 0, "Some content about cooking", "typed_text")

        results = idx.search("astrophysics")
        assert len(results) == 0
        idx.close()

    def test_fts5_search_multiple_documents(self):
        """Test FTS5 search across multiple documents."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1", name="Doc A", path="/Doc A")
        idx.upsert_document(doc_id="doc-2", doc_hash="h2", name="Doc B", path="/Doc B")
        idx.upsert_page("doc-1", 0, "Machine learning algorithms", "typed_text")
        idx.upsert_page("doc-2", 0, "Deep learning neural networks", "typed_text")

        results = idx.search("learning")
        assert len(results) == 2
        idx.close()

    def test_fts5_search_deduplicates_by_doc(self):
        """Test FTS5 search deduplicates when multiple pages of same doc match."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1", name="Notes", path="/Notes")
        idx.upsert_page("doc-1", 0, "Python programming basics", "typed_text")
        idx.upsert_page("doc-1", 0, "Python advanced features", "ocr", "sampling")

        results = idx.search("python")
        assert len(results) == 1  # Should be deduplicated
        assert results[0]["doc_id"] == "doc-1"
        idx.close()

    def test_fts5_search_invalid_syntax(self):
        """Test FTS5 search gracefully handles invalid query syntax."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1", name="Notes", path="/Notes")
        idx.upsert_page("doc-1", 0, "Some content", "typed_text")

        # Unmatched quote is invalid FTS5 syntax
        results = idx.search('"unmatched')
        assert results == []
        idx.close()

    def test_upsert_page_updates_fts(self):
        """Test that updating a page also updates the FTS index."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1", name="Notes", path="/Notes")
        idx.upsert_page("doc-1", 0, "original content about cats", "typed_text")

        # Should find "cats"
        assert len(idx.search("cats")) == 1
        # Should NOT find "dogs"
        assert len(idx.search("dogs")) == 0

        # Update the page content
        idx.upsert_page("doc-1", 0, "updated content about dogs", "typed_text")

        # Now should find "dogs" but NOT "cats"
        assert len(idx.search("dogs")) == 1
        assert len(idx.search("cats")) == 0
        idx.close()

    def test_get_stats(self):
        """Test get_stats returns correct counts."""
        idx = self._make_index()
        assert idx.get_stats()["index_documents"] == 0
        assert idx.get_stats()["index_pages"] == 0

        idx.upsert_document(doc_id="doc-1", doc_hash="h1")
        idx.upsert_document(doc_id="doc-2", doc_hash="h2")
        idx.upsert_page("doc-1", 0, "content", "typed_text")

        stats = idx.get_stats()
        assert stats["index_documents"] == 2
        assert stats["index_pages"] == 1
        assert stats["index_path"] == ":memory:"
        idx.close()

    def test_clear(self):
        """Test clear removes all data."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1")
        idx.upsert_page("doc-1", 0, "content", "typed_text")
        idx.clear()
        assert idx.get_stats()["index_documents"] == 0
        assert idx.get_stats()["index_pages"] == 0
        idx.close()

    def test_rebuild(self):
        """Test rebuild does not raise."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1")
        idx.upsert_page("doc-1", 0, "some text", "typed_text")
        idx.rebuild()  # Should not raise
        # Search should still work after rebuild
        results = idx.search("text")
        assert len(results) == 1
        idx.close()

    def test_graceful_degradation(self):
        """Test that get_instance returns None when not initialized."""
        import rm_mcp.index as index_mod

        saved = index_mod._instance
        try:
            index_mod._instance = None
            assert index_mod.get_instance() is None
        finally:
            index_mod._instance = saved


class TestDocumentIndexSingleton:
    """Test the singleton lifecycle of DocumentIndex."""

    def test_initialize_and_close(self):
        """Test initialize creates instance and close removes it."""
        import rm_mcp.index as index_mod

        saved = index_mod._instance
        index_mod._instance = None
        try:
            idx = index_mod.initialize(":memory:")
            assert idx is not None
            assert index_mod.get_instance() is idx
            index_mod.close()
            assert index_mod.get_instance() is None
        finally:
            index_mod._instance = saved

    def test_initialize_returns_existing(self):
        """Test that initialize returns existing instance on second call."""
        import rm_mcp.index as index_mod

        saved = index_mod._instance
        index_mod._instance = None
        try:
            idx1 = index_mod.initialize(":memory:")
            idx2 = index_mod.initialize(":memory:")
            assert idx1 is idx2
            index_mod.close()
        finally:
            index_mod._instance = saved


class TestSearchWithFTS:
    """Test remarkable_search tool with FTS5 integration."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools.read.remarkable_read")
    @patch(_PATCH_CACHED)
    async def test_search_returns_fts_content_matches(self, mock_get_cached, mock_read):
        """Test that search returns FTS content matches alongside name matches."""
        import rm_mcp.index as index_mod

        # Set up a temporary in-memory index
        saved = index_mod._instance
        index_mod._instance = None
        idx = index_mod.initialize(":memory:")
        try:
            # Index some content
            idx.upsert_document(
                doc_id="doc-indexed",
                doc_hash="h1",
                name="Old Journal",
                path="/Old Journal",
                file_type="notebook",
            )
            idx.upsert_page(
                "doc-indexed", 0,
                "The quarterly budget review showed growth",
                "typed_text",
            )

            # Set up collection with a different document (name match)
            mock_client = Mock()
            doc = Mock()
            doc.VissibleName = "Budget Report"
            doc.ID = "doc-budget"
            doc.Parent = ""
            doc.is_folder = False
            doc.is_cloud_archived = False
            doc.ModifiedClient = "2024-06-01T00:00:00Z"
            mock_get_cached.return_value = (mock_client, [doc])

            mock_read.return_value = json.dumps(
                {
                    "content": "Budget details here",
                    "total_pages": 1,
                    "document": "Budget Report",
                    "path": "/Budget Report",
                }
            )

            result = await mcp.call_tool("remarkable_search", {"query": "budget"})
            data = json.loads(result[0][0].text)

            assert data["count"] >= 1
            match_types = [d.get("match_type") for d in data["documents"]]
            # Should have at least the FTS content match
            assert "content" in match_types
            # Should include index_coverage
            assert "index_coverage" in data
            assert "indexed" in data["index_coverage"]
            assert "total" in data["index_coverage"]
        finally:
            index_mod.close()
            index_mod._instance = saved

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_search_fts_only_results(self, mock_get_cached):
        """Test search returns FTS results even when no name matches exist."""
        import rm_mcp.index as index_mod

        saved = index_mod._instance
        index_mod._instance = None
        idx = index_mod.initialize(":memory:")
        try:
            idx.upsert_document(
                doc_id="doc-1",
                doc_hash="h1",
                name="Random Name",
                path="/Random Name",
                file_type="notebook",
            )
            idx.upsert_page(
                "doc-1", 0,
                "Photosynthesis is the process of converting sunlight",
                "typed_text",
            )

            mock_client = Mock()
            mock_get_cached.return_value = (mock_client, [])

            result = await mcp.call_tool("remarkable_search", {"query": "photosynthesis"})
            data = json.loads(result[0][0].text)

            assert data["count"] == 1
            assert data["documents"][0]["match_type"] == "content"
            assert "photosynthesis" in data["documents"][0]["snippet"].lower()
        finally:
            index_mod.close()
            index_mod._instance = saved


class TestStatusWithIndex:
    """Test remarkable_status shows index stats."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_status_includes_index_stats(self, mock_get_cached):
        """Test that status includes index statistics when index is available."""
        import rm_mcp.index as index_mod

        saved = index_mod._instance
        index_mod._instance = None
        idx = index_mod.initialize(":memory:")
        try:
            idx.upsert_document(doc_id="doc-1", doc_hash="h1")
            idx.upsert_page("doc-1", 0, "some content", "typed_text")

            mock_client = Mock()
            mock_get_cached.return_value = (mock_client, [])

            result = await mcp.call_tool("remarkable_status", {})
            data = json.loads(result[0][0].text)

            assert data["authenticated"] is True
            assert "index_documents" in data
            assert data["index_documents"] == 1
            assert "index_pages" in data
            assert data["index_pages"] == 1
            assert "index_path" in data
        finally:
            index_mod.close()
            index_mod._instance = saved


class TestCacheL2Integration:
    """Test L1/L2 cache integration."""

    def test_page_ocr_l2_write_through(self):
        """Test that cache_page_ocr writes to L2 index."""
        import rm_mcp.index as index_mod
        from rm_mcp.cache import cache_page_ocr

        saved = index_mod._instance
        index_mod._instance = None
        idx = index_mod.initialize(":memory:")
        try:
            idx.upsert_document(doc_id="doc-1", doc_hash="h1")
            cache_page_ocr("doc-1", 1, "sampling", "OCR text from page 1")

            # Verify it was written to L2
            stored = idx.get_page_ocr("doc-1", 1, "sampling")
            assert stored == "OCR text from page 1"
        finally:
            index_mod.close()
            index_mod._instance = saved

    def test_page_ocr_l2_read_through(self):
        """Test that get_cached_page_ocr falls back to L2 on L1 miss."""
        import rm_mcp.index as index_mod
        from rm_mcp.cache import _page_ocr_cache, get_cached_page_ocr

        saved = index_mod._instance
        index_mod._instance = None
        idx = index_mod.initialize(":memory:")
        try:
            idx.upsert_document(doc_id="doc-1", doc_hash="h1")
            # Write directly to L2 (bypass L1)
            idx.upsert_page("doc-1", 1, "L2 cached text", "ocr", "sampling")

            # Ensure L1 is empty for this key
            cache_key = ("doc-1", 1, "sampling")
            _page_ocr_cache.pop(cache_key, None)

            # Should find it via L2
            result = get_cached_page_ocr("doc-1", 1, "sampling")
            assert result == "L2 cached text"

            # Should now be promoted to L1
            assert cache_key in _page_ocr_cache
        finally:
            # Clean up L1 cache
            _page_ocr_cache.pop(("doc-1", 1, "sampling"), None)
            index_mod.close()
            index_mod._instance = saved

    def test_ocr_result_l2_write_through(self):
        """Test that cache_ocr_result writes extraction results to L2."""
        import rm_mcp.index as index_mod
        from rm_mcp.cache import cache_ocr_result

        saved = index_mod._instance
        index_mod._instance = None
        idx = index_mod.initialize(":memory:")
        try:
            idx.upsert_document(doc_id="doc-1", doc_hash="h1")
            result = {
                "typed_text": ["Hello from extraction"],
                "highlights": [],
                "handwritten_text": [],
                "pages": 1,
            }
            cache_ocr_result("doc-1", result)

            # Verify typed text was stored in L2
            conn = idx._get_connection()
            row = conn.execute(
                "SELECT content FROM pages WHERE doc_id='doc-1' AND content_type='typed_text'"
            ).fetchone()
            assert row is not None
            assert "Hello from extraction" in row["content"]
        finally:
            index_mod.close()
            index_mod._instance = saved


# =============================================================================
# Test Compact Mode
# =============================================================================


class TestCompactMode:
    """Test compact mode across responses and tools."""

    def test_make_response_compact_omits_hint(self):
        """Test that make_response with compact=True omits _hint."""
        result = make_response({"key": "value"}, "This is a hint", compact=True)
        parsed = json.loads(result)
        assert parsed["key"] == "value"
        assert "_hint" not in parsed

    def test_make_response_non_compact_includes_hint(self):
        """Test that make_response with compact=False includes _hint."""
        result = make_response({"key": "value"}, "This is a hint", compact=False)
        parsed = json.loads(result)
        assert parsed["_hint"] == "This is a hint"

    def test_make_error_compact_omits_suggestion(self):
        """Test that make_error with compact=True omits suggestion and did_you_mean."""
        result = make_error(
            "test_err", "msg", "suggestion", did_you_mean=["a", "b"], compact=True
        )
        parsed = json.loads(result)
        assert parsed["_error"]["type"] == "test_err"
        assert parsed["_error"]["message"] == "msg"
        assert "suggestion" not in parsed["_error"]
        assert "did_you_mean" not in parsed["_error"]

    def test_make_error_non_compact_includes_suggestion(self):
        """Test that make_error with compact=False includes suggestion."""
        result = make_error(
            "test_err", "msg", "suggestion", did_you_mean=["a"], compact=False
        )
        parsed = json.loads(result)
        assert parsed["_error"]["suggestion"] == "suggestion"
        assert parsed["_error"]["did_you_mean"] == ["a"]

    def test_is_compact_param(self):
        """Test is_compact with parameter."""
        from rm_mcp.tools._helpers import is_compact

        assert is_compact(True) is True
        assert is_compact(False) is False

    def test_is_compact_env_var(self):
        """Test is_compact with env var."""
        from rm_mcp.tools._helpers import is_compact

        with patch.dict(os.environ, {"REMARKABLE_COMPACT": "1"}):
            assert is_compact(False) is True
        with patch.dict(os.environ, {"REMARKABLE_COMPACT": "true"}):
            assert is_compact(False) is True
        with patch.dict(os.environ, {"REMARKABLE_COMPACT": "0"}):
            assert is_compact(False) is False

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_tool_with_compact_output(self, mock_get_cached):
        """Test that a tool with compact_output=True omits _hint."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool(
            "remarkable_status", {"compact_output": True}
        )
        data = json.loads(result[0][0].text)

        assert data["authenticated"] is True
        assert "_hint" not in data


# =============================================================================
# Test parse_pages
# =============================================================================


class TestParsePages:
    """Test the parse_pages utility function."""

    def test_all(self):
        from rm_mcp.tools._helpers import parse_pages

        assert parse_pages("all", 5) == [1, 2, 3, 4, 5]

    def test_range(self):
        from rm_mcp.tools._helpers import parse_pages

        assert parse_pages("1-3", 10) == [1, 2, 3]

    def test_individual(self):
        from rm_mcp.tools._helpers import parse_pages

        assert parse_pages("2,4,5", 10) == [2, 4, 5]

    def test_mixed(self):
        from rm_mcp.tools._helpers import parse_pages

        assert parse_pages("1-3,5", 10) == [1, 2, 3, 5]

    def test_out_of_range_clamped(self):
        from rm_mcp.tools._helpers import parse_pages

        result = parse_pages("1-100", 5)
        assert result == [1, 2, 3, 4, 5]

    def test_invalid_input(self):
        from rm_mcp.tools._helpers import parse_pages

        assert parse_pages("abc", 5) == []

    def test_empty_string(self):
        from rm_mcp.tools._helpers import parse_pages

        assert parse_pages("", 5) == []

    def test_below_range(self):
        from rm_mcp.tools._helpers import parse_pages

        result = parse_pages("0", 5)
        assert result == []


# =============================================================================
# Test Multi-Page Read
# =============================================================================


class TestMultiPageRead:
    """Test multi-page read functionality."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_notebook_pages_all(self, mock_get_cached, mock_extract, mock_file_type):
        """Test reading all pages from a notebook."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Multi Page Notes"
        doc.ID = "doc-mp"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip"

        mock_extract.return_value = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": ["Page one content", "Page two content", "Page three content"],
            "pages": 3,
            "ocr_backend": "sampling",
        }

        result = await mcp.call_tool(
            "remarkable_read", {"document": "Multi Page Notes", "pages": "all"}
        )
        data = json.loads(result[0][0].text)

        assert "pages" in data
        assert data["pages"] == [1, 2, 3]
        assert data["total_pages"] == 3
        assert "Page one content" in data["content"]
        assert "Page two content" in data["content"]
        assert "Page three content" in data["content"]

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_notebook_pages_range(self, mock_get_cached, mock_extract, mock_file_type):
        """Test reading a page range from a notebook."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Range Notes"
        doc.ID = "doc-range"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip"

        mock_extract.return_value = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": ["P1", "P2", "P3", "P4", "P5"],
            "pages": 5,
        }

        result = await mcp.call_tool(
            "remarkable_read", {"document": "Range Notes", "pages": "2-4"}
        )
        data = json.loads(result[0][0].text)

        assert data["pages"] == [2, 3, 4]
        assert "P2" in data["content"]
        assert "P3" in data["content"]
        assert "P4" in data["content"]
        assert "P1" not in data["content"]

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_pdf_pages_all(self, mock_get_cached, mock_extract, mock_file_type):
        """Test reading PDF with pages='all' returns full text."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Full PDF"
        doc.ID = "doc-fullpdf"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "pdf"
        mock_client.download.return_value = b"fake-zip"

        mock_extract.return_value = {
            "typed_text": ["Full PDF annotation content that spans many pages."],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read", {"document": "Full PDF", "pages": "all"}
        )
        data = json.loads(result[0][0].text)

        assert data["more"] is False
        assert "Full PDF annotation content" in data["content"]


# =============================================================================
# Test Grep Auto-Redirect
# =============================================================================


class TestGrepAutoRedirect:
    """Test grep auto-redirect functionality in remarkable_read."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_grep_redirects_to_matching_page(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test that grep auto-redirects from page 1 to matching page."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Redirect Test"
        doc.ID = "doc-redir"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip"

        mock_extract.return_value = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": [
                "Nothing interesting here",
                "Also nothing",
                "The keyword is found here",
            ],
            "pages": 3,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "Redirect Test", "page": 1, "grep": "keyword"},
        )
        data = json.loads(result[0][0].text)

        # Should auto-redirect to page 3 (where "keyword" is found)
        assert data["page"] == 3
        assert data["grep_redirected_from"] == 1
        assert data["grep_matches"] > 0
        assert "keyword" in data["content"]

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_grep_no_match_anywhere_still_errors(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test that grep with no match anywhere returns error."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "No Match Doc"
        doc.ID = "doc-nomatch2"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip"

        mock_extract.return_value = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": ["Page one text", "Page two text"],
            "pages": 2,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "No Match Doc", "grep": "nonexistent_term_xyz"},
        )
        data = json.loads(result[0][0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "no_grep_matches"


# =============================================================================
# Test Search Performance (no download without grep)
# =============================================================================


class TestSearchPerformance:
    """Test that search avoids unnecessary downloads."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_search_without_grep_no_download(self, mock_get_cached):
        """Test that search without grep does not call remarkable_read or download."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Test Note"
        doc.ID = "doc-test"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])

        result = await mcp.call_tool("remarkable_search", {"query": "Test"})
        data = json.loads(result[0][0].text)

        assert data["count"] == 1
        assert data["documents"][0]["match_type"] == "name"
        # No content downloaded
        assert "content" not in data["documents"][0]
        mock_client.download.assert_not_called()

    @pytest.mark.asyncio
    @patch("rm_mcp.tools.read.remarkable_read")
    @patch(_PATCH_CACHED)
    async def test_search_with_grep_downloads(self, mock_get_cached, mock_read):
        """Test that search with grep falls back to download when no L2 cache."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Grep Doc"
        doc.ID = "doc-grep"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])

        mock_read.return_value = json.dumps(
            {
                "content": "Found the pattern here",
                "total_pages": 1,
                "name": "Grep Doc",
                "path": "/Grep Doc",
                "grep": "pattern",
                "grep_matches": 1,
            }
        )

        result = await mcp.call_tool(
            "remarkable_search", {"query": "Grep", "grep": "pattern"}
        )
        data = json.loads(result[0][0].text)

        assert data["count"] == 1
        assert data["documents"][0].get("grep_matches", 0) >= 0


# =============================================================================
# Test L2 Preview
# =============================================================================


class TestL2Preview:
    """Test L2 index preview for recent and search."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_recent_preview_from_index(self, mock_get_cached):
        """Test that recent uses L2 index for preview."""
        import rm_mcp.index as index_mod

        saved = index_mod._instance
        index_mod._instance = None
        idx = index_mod.initialize(":memory:")
        try:
            mock_client = Mock()

            doc = Mock()
            doc.VissibleName = "Indexed Doc"
            doc.ID = "doc-indexed"
            doc.Parent = ""
            doc.is_folder = False
            doc.is_cloud_archived = False
            doc.ModifiedClient = "2024-06-01T00:00:00Z"

            mock_get_cached.return_value = (mock_client, [doc])

            # Populate L2 index
            idx.upsert_document(doc_id="doc-indexed", doc_hash="h1")
            idx.upsert_page("doc-indexed", 0, "Preview from L2 cache", "typed_text")

            with patch("rm_mcp.tools._helpers._get_file_type_cached", return_value="notebook"):
                result = await mcp.call_tool(
                    "remarkable_recent", {"limit": 5, "include_preview": True}
                )
                data = json.loads(result[0][0].text)

            assert data["count"] == 1
            assert "preview" in data["documents"][0]
            assert "Preview from L2 cache" in data["documents"][0]["preview"]
            # Should NOT have downloaded from cloud
            mock_client.download.assert_not_called()
        finally:
            index_mod.close()
            index_mod._instance = saved


# =============================================================================
# Test Index New Methods
# =============================================================================


class TestIndexNewMethods:
    """Test get_preview, get_content_snippet, get_indexed_document_count."""

    def _make_index(self):
        from rm_mcp.index import DocumentIndex

        return DocumentIndex(":memory:")

    def test_get_preview_typed_text(self):
        idx = self._make_index()
        idx.upsert_document(doc_id="d1", doc_hash="h1")
        idx.upsert_page("d1", 0, "Hello world preview", "typed_text")
        assert idx.get_preview("d1") == "Hello world preview"
        idx.close()

    def test_get_preview_prefers_typed_over_ocr(self):
        idx = self._make_index()
        idx.upsert_document(doc_id="d1", doc_hash="h1")
        idx.upsert_page("d1", 0, "Typed content", "typed_text")
        idx.upsert_page("d1", 0, "OCR content", "ocr", "sampling")
        assert idx.get_preview("d1") == "Typed content"
        idx.close()

    def test_get_preview_none_when_empty(self):
        idx = self._make_index()
        idx.upsert_document(doc_id="d1", doc_hash="h1")
        assert idx.get_preview("d1") is None
        idx.close()

    def test_get_content_snippet(self):
        idx = self._make_index()
        idx.upsert_document(doc_id="d1", doc_hash="h1")
        idx.upsert_page("d1", 0, "Some long content", "typed_text")
        snippet = idx.get_content_snippet("d1", max_chars=10)
        assert len(snippet) == 10
        idx.close()

    def test_get_content_snippet_none_when_empty(self):
        idx = self._make_index()
        idx.upsert_document(doc_id="d1", doc_hash="h1")
        assert idx.get_content_snippet("d1") is None
        idx.close()

    def test_get_indexed_document_count(self):
        idx = self._make_index()
        assert idx.get_indexed_document_count() == 0
        idx.upsert_document(doc_id="d1", doc_hash="h1")
        idx.upsert_page("d1", 0, "content", "typed_text")
        assert idx.get_indexed_document_count() == 1
        idx.upsert_document(doc_id="d2", doc_hash="h2")
        # d2 has no pages
        assert idx.get_indexed_document_count() == 1
        idx.upsert_page("d2", 0, "more", "typed_text")
        assert idx.get_indexed_document_count() == 2
        idx.close()


# =============================================================================
# Test Auto-OCR Opt-Out
# =============================================================================


class TestAutoOcrOptOut:
    """Test auto_ocr=False prevents OCR auto-retry."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_auto_ocr_false_returns_empty(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test that auto_ocr=False returns empty without OCR retry."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Empty Notebook"
        doc.ID = "doc-empty"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip"

        mock_extract.return_value = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "Empty Notebook", "auto_ocr": False},
        )
        data = json.loads(result[0][0].text)

        # Should return empty content without OCR auto-retry
        assert data.get("content") == ""
        assert data.get("total_chars") == 0
        # Should NOT have _ocr_auto_enabled
        assert "_ocr_auto_enabled" not in data


# =============================================================================
# Test suggest_for_error
# =============================================================================


class TestSuggestForError:
    """Test context-aware error suggestions."""

    def test_authentication_error(self):
        from rm_mcp.tools._helpers import suggest_for_error

        result = suggest_for_error(RuntimeError("Not authenticated. Run: uvx rm-mcp --setup"))
        assert "--setup" in result
        assert "authenticate" in result.lower()

    def test_token_expired_error(self):
        from rm_mcp.tools._helpers import suggest_for_error

        result = suggest_for_error(RuntimeError("Failed to renew user token (HTTP 401)"))
        assert "--setup" in result
        assert "expired" in result.lower()

    def test_re_authenticate_error(self):
        from rm_mcp.tools._helpers import suggest_for_error

        result = suggest_for_error(RuntimeError("Re-authenticate by running: uvx rm-mcp --setup"))
        assert "--setup" in result

    def test_network_error(self):
        from rm_mcp.tools._helpers import suggest_for_error

        result = suggest_for_error(RuntimeError("Network error during request"))
        assert "internet connection" in result.lower()

    def test_timeout_error(self):
        from rm_mcp.tools._helpers import suggest_for_error

        result = suggest_for_error(RuntimeError("Request timeout"))
        assert "internet connection" in result.lower()

    def test_empty_response_error(self):
        from rm_mcp.tools._helpers import suggest_for_error

        result = suggest_for_error(RuntimeError("Empty response from reMarkable API"))
        assert "--setup" in result

    def test_generic_error_fallback(self):
        from rm_mcp.tools._helpers import suggest_for_error

        result = suggest_for_error(RuntimeError("Something unexpected happened"))
        assert "remarkable_status()" in result

    def test_no_device_token(self):
        from rm_mcp.tools._helpers import suggest_for_error

        result = suggest_for_error(RuntimeError("No device token available"))
        assert "--setup" in result


# =============================================================================
# Test Status Config
# =============================================================================


class TestStatusConfig:
    """Test that status tool includes configuration details."""

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_status_includes_config(self, mock_get_cached):
        """Test that authenticated status includes config section."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)

        assert data["authenticated"] is True
        assert "config" in data
        config = data["config"]
        assert "ocr_backend" in config
        assert "root_path" in config
        assert "background_color" in config
        assert "cache_ttl_seconds" in config
        assert "compact_mode" in config

    @pytest.mark.asyncio
    @patch(_PATCH_CACHED)
    async def test_status_config_values(self, mock_get_cached):
        """Test that config values are sensible defaults."""
        mock_client = Mock()
        mock_get_cached.return_value = (mock_client, [])

        result = await mcp.call_tool("remarkable_status", {})
        data = json.loads(result[0][0].text)

        config = data["config"]
        assert config["ocr_backend"] == "sampling"
        assert config["root_path"] == "/"
        assert config["background_color"] == "#FBFBFB"
        assert isinstance(config["cache_ttl_seconds"], int)
        assert isinstance(config["compact_mode"], bool)


# =============================================================================
# Test OCR Unavailable Message
# =============================================================================


class TestOCRUnavailableMessage:
    """Test differentiated OCR failure messages in image tool."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers.should_use_sampling_ocr", return_value=False)
    @patch("rm_mcp.tools._helpers.render_page_from_document_zip")
    @patch("rm_mcp.tools._helpers.get_document_page_count", return_value=3)
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_ocr_unavailable_message(
        self,
        mock_get_cached,
        mock_extract,
        mock_file_type,
        mock_page_count,
        mock_render,
        mock_sampling,
    ):
        """Test that OCR unavailable shows clear message when client doesn't support sampling."""
        mock_client = Mock()
        doc = Mock()
        doc.VissibleName = "Test Notebook"
        doc.ID = "doc-ocr"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip"
        mock_render.return_value = b"\x89PNG fake image data"

        result = await mcp.call_tool(
            "remarkable_image",
            {"document": "Test Notebook", "include_ocr": True, "compatibility": True},
        )
        data = json.loads(result[0].text)

        assert "ocr_message" in data
        assert "unavailable" in data["ocr_message"].lower()
        assert "sampling" in data["ocr_message"].lower()
        assert data["ocr_text"] is None


# =============================================================================
# Test content_type="annotations"
# =============================================================================


class TestAnnotationsMode:
    """Test remarkable_read with content_type='annotations'."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_read_annotations_mode(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test reading annotations returns highlights and handwritten sections."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Annotated Doc"
        doc.ID = "doc-annot"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T10:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        # Use pdf so it goes through the annotation assembly path
        # (notebooks with handwritten_text enter the notebook pagination path)
        mock_file_type.return_value = "pdf"
        mock_client.download.return_value = b"fake-zip-data"

        mock_extract.return_value = {
            "typed_text": ["Some typed annotation text."],
            "highlights": ["Highlighted passage one.", "Highlighted passage two."],
            "handwritten_text": ["Handwritten note content."],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "Annotated Doc", "content_type": "annotations"},
        )
        data = json.loads(result[0][0].text)

        assert "content" in data
        assert data["content_type"] == "annotations"
        assert "Highlighted passage one" in data["content"]
        assert "Handwritten" in data["content"]
        assert "Some typed annotation text" in data["content"]
        # annotations mode should NOT have the "=== Annotations ===" separator
        assert "=== Annotations ===" not in data["content"]

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_read_annotations_empty(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Test reading annotations when there are none returns gracefully."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Empty Annot Doc"
        doc.ID = "doc-empty-annot"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T10:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "pdf"
        mock_client.download.return_value = b"fake-zip-data"

        mock_extract.return_value = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": [],
            "pages": 1,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "Empty Annot Doc", "content_type": "annotations"},
        )
        data = json.loads(result[0][0].text)

        assert data["content"] == ""
        assert data["content_type"] == "annotations"
        assert data["total_chars"] == 0


# =============================================================================
# Test output_format="svg"
# =============================================================================


class TestImageSVG:
    """Test remarkable_image with SVG output format."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers.render_page_from_document_zip_svg")
    @patch("rm_mcp.tools._helpers.get_document_page_count", return_value=3)
    @patch(_PATCH_CACHED)
    async def test_image_svg_compatibility(
        self, mock_get_cached, mock_page_count, mock_render_svg
    ):
        """Test SVG compatibility mode returns svg key and correct mime type."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "SVG Test Doc"
        doc.ID = "doc-svg"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_client.download.return_value = b"fake-zip"
        mock_render_svg.return_value = "<svg><circle cx='50' cy='50' r='40'/></svg>"

        result = await mcp.call_tool(
            "remarkable_image",
            {"document": "SVG Test Doc", "output_format": "svg", "compatibility": True},
        )
        data = json.loads(result[0].text)

        assert "svg" in data
        assert data["mime_type"] == "image/svg+xml"
        assert "image_base64" not in data
        assert data["page"] == 1
        assert data["total_pages"] == 3

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers.render_page_from_document_zip_svg")
    @patch("rm_mcp.tools._helpers.get_document_page_count", return_value=2)
    @patch(_PATCH_CACHED)
    async def test_image_svg_embedded(
        self, mock_get_cached, mock_page_count, mock_render_svg
    ):
        """Test SVG embedded mode returns TextContent + EmbeddedResource."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "SVG Embedded Doc"
        doc.ID = "doc-svg-emb"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_client.download.return_value = b"fake-zip"
        mock_render_svg.return_value = "<svg><rect width='100' height='100'/></svg>"

        result = await mcp.call_tool(
            "remarkable_image",
            {"document": "SVG Embedded Doc", "output_format": "svg", "compatibility": False},
        )

        # Embedded mode returns a list with TextContent and EmbeddedResource
        assert isinstance(result, list)
        assert len(result) == 2
        # First element is TextContent with info
        assert hasattr(result[0], "text")
        assert "SVG" in result[0].text
        # Second element is EmbeddedResource
        assert hasattr(result[1], "resource")
        assert result[1].resource.mimeType == "image/svg+xml"

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers.render_page_from_document_zip_svg")
    @patch("rm_mcp.tools._helpers.get_document_page_count", return_value=1)
    @patch(_PATCH_CACHED)
    async def test_image_svg_render_failed(
        self, mock_get_cached, mock_page_count, mock_render_svg
    ):
        """Test SVG render failure returns render_failed error."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "SVG Fail Doc"
        doc.ID = "doc-svg-fail"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-01-01T00:00:00Z"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_client.download.return_value = b"fake-zip"
        mock_render_svg.return_value = None

        result = await mcp.call_tool(
            "remarkable_image",
            {"document": "SVG Fail Doc", "output_format": "svg"},
        )
        data = json.loads(result[0].text)

        assert "_error" in data
        assert data["_error"]["type"] == "render_failed"


# =============================================================================
# Test _is_cache_valid
# =============================================================================


class TestCacheValidity:
    """Test _is_cache_valid function from cache module."""

    def test_cache_valid_with_fresh_timestamp(self):
        """Fresh timestamp should be valid."""
        from rm_mcp.cache import _is_cache_valid

        entry = {"timestamp": time.time(), "data": "some value"}
        assert _is_cache_valid(entry) is True

    def test_cache_valid_with_expired_timestamp(self):
        """Old timestamp should be invalid."""
        from rm_mcp.cache import EXTRACTION_CACHE_TTL_SECONDS, _is_cache_valid

        entry = {"timestamp": time.time() - EXTRACTION_CACHE_TTL_SECONDS - 1, "data": "old"}
        assert _is_cache_valid(entry) is False

    def test_cache_invalid_without_timestamp(self):
        """Entry without timestamp should be invalid (defensive fix)."""
        from rm_mcp.cache import _is_cache_valid

        entry = {"data": "no timestamp"}
        assert _is_cache_valid(entry) is False


# =============================================================================
# Test notebook page counting (empty notebooks should report real page count)
# =============================================================================


class TestNotebookPageCount:
    """Test that notebooks with no text still report the correct total_pages."""

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_empty_notebook_reports_real_page_count(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """An empty 16-page notebook should report total_pages=16, not 1."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Prototyper Design"
        doc.ID = "doc-proto"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T10:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip-data"

        # Extraction returns no text but reports 16 pages
        mock_extract.return_value = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": [],
            "pages": 16,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "Prototyper Design", "auto_ocr": False},
        )
        data = json.loads(result[0][0].text)

        assert data["total_pages"] == 16
        assert data["total_chars"] == 0
        assert data["page"] == 1

    @pytest.mark.asyncio
    @patch("rm_mcp.tools._helpers._get_file_type_cached")
    @patch("rm_mcp.tools._helpers.extract_text_from_document_zip")
    @patch(_PATCH_CACHED)
    async def test_empty_notebook_page_out_of_range_uses_real_count(
        self, mock_get_cached, mock_extract, mock_file_type
    ):
        """Requesting page 20 of a 16-page empty notebook should say 16 pages."""
        mock_client = Mock()

        doc = Mock()
        doc.VissibleName = "Prototyper Design"
        doc.ID = "doc-proto2"
        doc.Parent = ""
        doc.is_folder = False
        doc.is_cloud_archived = False
        doc.ModifiedClient = "2024-06-01T10:00:00Z"
        doc.Type = "DocumentType"

        mock_get_cached.return_value = (mock_client, [doc])
        mock_file_type.return_value = "notebook"
        mock_client.download.return_value = b"fake-zip-data"

        mock_extract.return_value = {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": [],
            "pages": 16,
        }

        result = await mcp.call_tool(
            "remarkable_read",
            {"document": "Prototyper Design", "page": 20, "auto_ocr": False},
        )
        data = json.loads(result[0][0].text)

        assert data["_error"]["type"] == "page_out_of_range"
        assert "16 page(s)" in data["_error"]["message"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
