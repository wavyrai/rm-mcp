"""Shared test setup: keep every test's on-disk caches out of the real home directory."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """No test reads or writes the real token file, REMARKABLE_TOKEN or ~/.rmapi."""
    import rm_mcp.api as api_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    monkeypatch.delenv("REMARKABLE_ONE_TIME_CODE", raising=False)
    monkeypatch.setattr(api_mod, "_client_singleton", None)
    monkeypatch.setattr(api_mod, "_client_source", None)
    monkeypatch.setattr(api_mod, "_client_signature", None)
    yield home


@pytest.fixture(autouse=True)
def _isolated_blob_store(tmp_path, monkeypatch):
    """Point the content-addressed blob store at a per-test directory."""
    from rm_mcp import blobstore

    monkeypatch.setenv("REMARKABLE_BLOB_CACHE_DIR", str(tmp_path / "blobs"))
    blobstore.reset_default_store()
    yield
    blobstore.reset_default_store()


def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Rewrite tests/fixtures/goldens/*.png from the current renderer.",
    )
