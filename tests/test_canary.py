# ruff: noqa: F811  (the cloud fixture is imported from test_tools and named as a parameter)
"""The daily canary (scripts/canary.py): its write guard, and its probe on the fake cloud."""

import importlib.util
from pathlib import Path

import pytest
import requests
from test_tools import cloud  # noqa: F401  (fixture)

_spec = importlib.util.spec_from_file_location(
    "canary", Path(__file__).parent.parent / "scripts" / "canary.py"
)
canary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(canary)


@pytest.fixture
def guard():
    g = canary.RequestGuard().install()
    yield g
    g.uninstall()


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_guard_refuses_writes_before_they_leave_the_process(guard, method):
    with pytest.raises(canary.WriteAttempt):
        requests.request(method, "http://127.0.0.1:9/sync/v3/root")
    assert guard.refused == [method]


def test_guard_refuses_a_post_other_than_token_renewal(guard):
    with pytest.raises(canary.WriteAttempt):
        requests.post("http://127.0.0.1:9/token/json/2/device/new")
    with pytest.raises(requests.ConnectionError):  # allowed through; nothing listens
        requests.post("http://127.0.0.1:9/token/json/2/user/new")
    with pytest.raises(requests.ConnectionError):
        requests.get("http://127.0.0.1:9/sync/v3/root")
    assert guard.refused == ["POST"] and guard.methods == {"POST": 2, "GET": 1}


def test_guard_uninstalls(guard):
    guard.uninstall()
    with pytest.raises(requests.ConnectionError):
        requests.put("http://127.0.0.1:9/x")


def test_redact_hides_the_message():
    e = RuntimeError("Document 'Diary 2026' failed (HTTP 400)")
    assert canary.redact(e) == "RuntimeError (HTTP 400)"


async def test_probe_and_root_check_on_the_fake_cloud(cloud, monkeypatch):
    monkeypatch.setenv("REMARKABLE_READ_ONLY", "1")
    report = {}
    await canary.probe(report)
    canary.check_root_index(report)
    assert report["status.authenticated"] is True
    assert report["find.results"] >= 1 and report["read.pages"] == 1
    assert report["view.png_bytes"] > 0 and report["root.entries"] >= 1
    assert set(cloud.methods) == {"GET"}


def test_root_check_fails_when_the_index_does_not_round_trip(cloud, monkeypatch):
    monkeypatch.setattr(canary, "SAMPLE_DOCUMENTS", 0)
    import rm_mcp.clients.cloud as cloud_mod

    monkeypatch.setattr(cloud_mod, "serialize_index", lambda entries, owner: b"changed")
    with pytest.raises(RuntimeError, match="byte for byte"):
        canary.check_root_index({})
