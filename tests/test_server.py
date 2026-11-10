#!/usr/bin/env python3
"""
Tests for reMarkable MCP Server

Server registration, CLI setup, root-path helpers, collection cache and index.
"""

import json
import os
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from rm_mcp.api import register_and_get_token
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
            "remarkable_find",
            "remarkable_list",
            "remarkable_read",
            "remarkable_view",
            "remarkable_annotations",
            "remarkable_save_transcript",
            "remarkable_status",
            # deprecated aliases, kept for one release
            "remarkable_search",
            "remarkable_browse",
            "remarkable_recent",
            "remarkable_image",
        ]

        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Tool {tool_name} not found"

    @pytest.mark.asyncio
    async def test_tools_count(self):
        """7 v2 tools, 4 deprecated aliases and 3 organising tools."""
        tools = await mcp.list_tools()
        assert len(tools) == 14, f"Expected 14 tools, got {len(tools)}"

    @pytest.mark.asyncio
    async def test_only_organizing_tools_are_writes(self):
        """Only the organising tools and the local transcript store write."""
        tools = await mcp.list_tools()
        writers = {t.name for t in tools if not t.annotations.read_only_hint}
        assert writers == {
            "remarkable_rename",
            "remarkable_move",
            "remarkable_create_folder",
            "remarkable_save_transcript",
        }
        # No tool is allowed to advertise itself as destructive
        assert not any(t.annotations.destructive_hint for t in tools)

    @pytest.mark.asyncio
    async def test_every_tool_has_title_and_hints(self):
        """Every tool carries a title (top-level and in annotations) and explicit hints."""
        tools = await mcp.list_tools()
        writes = {
            "remarkable_rename",
            "remarkable_move",
            "remarkable_create_folder",
            "remarkable_save_transcript",
        }
        for tool in tools:
            assert tool.title, f"{tool.name} has no title"
            assert tool.annotations.title == tool.title
            assert tool.annotations.read_only_hint is (tool.name not in writes)
            assert tool.annotations.destructive_hint is False
        wire = [t.model_dump(by_alias=True, mode="json", exclude_none=True) for t in tools]
        assert all("readOnlyHint" in t["annotations"] for t in wire)

    def test_resource_uri_normalization(self):
        """VS Code's ?version= suffix and %20 encoding map onto the registered URI."""
        from rm_mcp.server import normalize_resource_uri

        want = "remarkable:///Work/November 2025.txt"
        assert normalize_resource_uri("remarkable:///Work/November%202025.txt") == want
        assert normalize_resource_uri("remarkable:///Work/November 2025.txt?version=17") == want
        assert normalize_resource_uri(want) == want

    @pytest.mark.asyncio
    async def test_tool_schemas(self):
        """Test that tools have proper schemas."""
        tools = await mcp.list_tools()

        for tool in tools:
            assert tool.name, "Tool should have a name"
            assert tool.description, "Tool should have a description"
            assert hasattr(tool, "input_schema"), "Tool should have input_schema"


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
    @patch(
        "rm_mcp.cache.get_cached_collection",
        side_effect=RuntimeError("Not authenticated. Run: uvx rm-mcp --setup"),
    )
    @pytest.mark.asyncio
    async def test_status_returns_setup_instructions(self, mock_cached, mock_rmapi):
        """Status never fails: unauthenticated, it says how to set up."""
        result = await mcp.call_tool("remarkable_status", {})
        data = result.structured_content

        assert data["authenticated"] is False
        assert "--setup" in data["hint"]

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
        idx.upsert_page("doc-1", 1, "old content", "page")
        assert idx.needs_reindex("doc-1", "hash-2") is True
        assert not idx.has_indexed_content("doc-1")
        assert idx.search("content") == []
        idx.close()

    def test_clear_keeps_saved_transcripts(self):
        """REMARKABLE_INDEX_REBUILD must not throw away transcripts agents saved."""
        idx = self._make_index()
        idx.put_page_ocr_by_hash("page-hash", "transcript", "my handwriting")
        idx.upsert_page("doc-1", 1, "my handwriting", "ocr")
        idx.clear()
        assert idx.search("handwriting") == []
        assert idx.get_page_ocr_by_hash("page-hash", "transcript") == "my handwriting"
        idx.close()

    def test_replace_document_pages(self):
        """A new version's pages replace the old ones, with metadata, atomically."""
        idx = self._make_index()
        idx.replace_document_pages("doc-1", "h1", [(1, "page", "first version")], name="N")
        idx.replace_document_pages(
            "doc-1",
            "h2",
            [(1, "page", "second version"), (2, "ocr", "a transcript")],
            name="N",
            page_count=2,
        )
        assert idx.search("first") == []
        assert [r["page_number"] for r in idx.search("second")] == [1]
        assert [r["content_type"] for r in idx.search("transcript")] == ["ocr"]
        assert idx.get_document_hash("doc-1") == "h2"
        assert idx.get_page_count("doc-1") == 2
        assert idx.indexed_versions() == {"doc-1": "h2"}
        idx.close()

    def test_fts5_search_basic(self):
        """Test basic FTS5 search."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1", name="Notes")
        idx.upsert_page("doc-1", 3, "The quantum physics experiment yielded results", "page")

        results = idx.search("quantum")
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc-1"
        assert results[0]["page_number"] == 3
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

    def test_fts5_search_returns_each_matching_page(self):
        """Every matching page is a row, so results can cite page numbers."""
        idx = self._make_index()
        idx.upsert_document(doc_id="doc-1", doc_hash="h1", name="Notes", path="/Notes")
        idx.upsert_page("doc-1", 1, "Python programming basics", "page")
        idx.upsert_page("doc-1", 2, "Python advanced features", "ocr")

        results = idx.search("python")
        assert sorted((r["page_number"], r["content_type"]) for r in results) == [
            (1, "page"),
            (2, "ocr"),
        ]
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
