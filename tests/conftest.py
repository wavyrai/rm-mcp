"""Shared test setup: keep every test's on-disk caches out of the real home directory."""

import pytest


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
