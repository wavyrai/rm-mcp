# ruff: noqa: F811  (the cloud fixture is imported from test_tools and named as a parameter)
"""The write tools end to end on the in-process fake cloud.

remarkable_send, remarkable_organize and remarkable_undo through
``mcp.call_tool``, with the real client, planner, root guard and writer. The
fake cloud enforces the real API's upload and commit rules and counts every
request by method, so "zero uploads" and "no DELETE" are measured, not assumed.
"""

import json
import re
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_tools import (  # noqa: F401  (cloud is a fixture)
    ANNOTATED,
    BOOK,
    NOTEBOOK,
    WORK,
    cloud,
    data,
    edit,
    notebook_files,
    pdf_bytes,
)

import rm_mcp.clients.organize as organize_client
from rm_mcp import sources
from rm_mcp.clients.cloud import parse_index
from rm_mcp.clients.organize import (
    ChangedSinceRead,
    LibraryWriter,
    MetadataEdit,
    RootGuardError,
    verify_root_change,
)
from rm_mcp.server import mcp

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _write_env(monkeypatch):
    for var in ("REMARKABLE_WRITE_ROOT", "REMARKABLE_READ_ONLY", "REMARKABLE_ROOT_PATH"):
        monkeypatch.delenv(var, raising=False)


def puts(cloud) -> int:
    return cloud.methods["PUT"]


def names(cloud):
    """{id: (visibleName, parent)} read back from the cloud's current root."""
    out = {}
    for entry in parse_index(cloud.blobs[cloud.root_hash]):
        for f in parse_index(cloud.blobs[entry["hash"]]):
            if f["id"].endswith(".metadata"):
                meta = json.loads(cloud.blobs[f["hash"]])
                out[entry["id"]] = (meta["visibleName"], meta["parent"])
    return out


def metadata(cloud, doc_id):
    entry = next(e for e in parse_index(cloud.blobs[cloud.root_hash]) if e["id"] == doc_id)
    for f in parse_index(cloud.blobs[entry["hash"]]):
        if f["id"].endswith(".metadata"):
            return json.loads(cloud.blobs[f["hash"]])


async def refused(tool: str, **arguments) -> str:
    with pytest.raises(ToolError) as exc:
        await mcp.call_tool(tool, arguments)
    return str(exc.value)


# =============================================================================
# remarkable_send
# =============================================================================


async def test_send_local_pdf_then_find_read_view(cloud, tmp_path):
    path = tmp_path / "Quarterly report.pdf"
    path.write_bytes(pdf_bytes(["Revenue grew by eleven percent.", "Second page."]))

    sent = await data("remarkable_send", source=str(path), folder="/Inbox")

    assert sent["name"] == "Quarterly report" and sent["type"] == "pdf"
    assert sent["path"] == "/Inbox/Quarterly report" and sent["page_count"] == 2
    assert sent["created_folders"] == ["/Inbox"]
    assert cloud.root_updates == 1
    # The blob set: the folder's metadata and index, the document's file,
    # .content, .metadata and index, then the root index.
    doc = sent["id"]
    assert sorted(u for u in cloud.uploads if u.startswith(doc)) == sorted(
        [f"{doc}.pdf", f"{doc}.content", f"{doc}.metadata", f"{doc}.docSchema"]
    )
    assert cloud.uploads[-1] == "root.docSchema"
    content = json.loads(cloud.blobs[_file_hash(cloud, doc, ".content")])
    assert content["fileType"] == "pdf" and content["pageCount"] == 2

    found = await data("remarkable_find", query="Quarterly")
    assert found["results"][0]["id"] == doc and found["results"][0]["page_count"] == 2
    read = await data("remarkable_read", id=doc, pages="1")
    assert "eleven percent" in read["pages"][0]["text"]
    view = await mcp.call_tool("remarkable_view", {"id": doc, "pages": "1"})
    assert any(block.type == "image" for block in view.content)


def _file_hash(cloud, doc_id, suffix):
    entry = next(e for e in parse_index(cloud.blobs[cloud.root_hash]) if e["id"] == doc_id)
    return next(
        f["hash"] for f in parse_index(cloud.blobs[entry["hash"]]) if f["id"].endswith(suffix)
    )


async def test_send_epub_file(cloud, tmp_path):
    path = tmp_path / "guide.epub"
    path.write_bytes((FIXTURES / "small.epub").read_bytes())
    sent = await data("remarkable_send", source=str(path), folder="/Work", title="Tide guide")
    assert sent["type"] == "epub" and sent["path"] == "/Work/Tide guide"
    read = await data("remarkable_read", id=sent["id"], pages="1-2")
    assert "lighthouses" in " ".join(p["text"] for p in read["pages"])


class FakeHTTP:
    def __init__(self, body: bytes, ctype: str, status: int = 200, headers=None):
        self.status_code = status
        self.headers = {"content-type": ctype, **(headers or {})}
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        self.closed = True


@pytest.fixture
def web(monkeypatch):
    """Route sources' HTTP and DNS to a dict of url -> FakeHTTP."""
    pages = {}
    resolved = {"example.org": "93.184.216.34", "internal.example": "10.0.0.5"}
    fetched = []

    def get(url):
        fetched.append(url)
        return pages[url]

    def getaddrinfo(host, port):
        return [(2, 1, 6, "", (resolved.get(host, "127.0.0.1"), 0))]

    monkeypatch.setattr(sources, "_http_get", get)
    monkeypatch.setattr(sources.socket, "getaddrinfo", getaddrinfo)
    return pages, fetched


async def test_send_url_pdf(cloud, web):
    pages, fetched = web
    pages["https://example.org/papers/attention.pdf"] = FakeHTTP(
        pdf_bytes(["Attention is what you need."]), "application/pdf"
    )
    sent = await data(
        "remarkable_send", source="https://example.org/papers/attention.pdf", folder="/Work"
    )
    assert sent["name"] == "attention" and sent["source"] == "url"
    read = await data("remarkable_read", id=sent["id"], pages="1")
    assert "Attention" in read["pages"][0]["text"]


async def test_send_url_follows_a_redirect_and_checks_it(cloud, web):
    pages, fetched = web
    pages["https://example.org/a"] = FakeHTTP(
        b"", "text/html", status=302, headers={"location": "https://internal.example/x.pdf"}
    )
    message = await refused("remarkable_send", source="https://example.org/a")
    assert "non-public" in message
    assert puts(cloud) == 0 and fetched == ["https://example.org/a"]


@pytest.mark.parametrize(
    "body,ctype,why",
    [
        (b"<html>hi</html>", "text/html", "not a PDF or EPUB"),
        (b"not really a pdf", "application/pdf", "does not contain a valid pdf"),
    ],
)
async def test_send_url_refuses_what_is_not_a_document(cloud, web, body, ctype, why):
    pages, _ = web
    pages["https://example.org/doc"] = FakeHTTP(body, ctype)
    assert why in await refused("remarkable_send", source="https://example.org/doc")
    assert puts(cloud) == 0


async def test_send_url_size_cap(cloud, web, monkeypatch):
    pages, _ = web
    monkeypatch.setattr(sources, "MAX_BYTES", 1000)
    pages["https://example.org/big.pdf"] = FakeHTTP(pdf_bytes(["x" * 3000] * 3), "application/pdf")
    assert "100 MB" in await refused("remarkable_send", source="https://example.org/big.pdf")
    assert puts(cloud) == 0


async def test_send_url_to_a_private_host_is_refused(cloud, web):
    _, fetched = web
    message = await refused("remarkable_send", source="http://localhost:8080/x.pdf")
    assert "non-public" in message and fetched == [] and puts(cloud) == 0


async def test_send_markdown_is_typeset(cloud):
    text = (
        "# Trip notes\n\nPack the **tent** and the [map](https://example.org/map).\n\n"
        "- water\n- stove\n\n```\nalt = 2400\n```\n"
    )
    sent = await data("remarkable_send", source=text)
    # No write root and no root path: the default write folder.
    assert sent["path"] == "/rm-mcp/Trip notes" and sent["source"] == "markdown"
    read = await data("remarkable_read", id=sent["id"], pages="1")
    page = read["pages"][0]["text"]
    assert "Pack the tent" in page and "stove" in page and "alt = 2400" in page
    view = await mcp.call_tool("remarkable_view", {"id": sent["id"], "pages": "1"})
    assert any(block.type == "image" for block in view.content)


async def test_send_missing_file_is_a_clear_error(cloud, tmp_path):
    message = await refused("remarkable_send", source=str(tmp_path / "nope.pdf"))
    assert "No file" in message and puts(cloud) == 0


async def test_send_outside_the_write_root_is_refused_with_zero_uploads(
    cloud, monkeypatch, tmp_path
):
    monkeypatch.setenv("REMARKABLE_WRITE_ROOT", "/rm-mcp test")
    path = tmp_path / "x.pdf"
    path.write_bytes(pdf_bytes(["x"]))
    for folder in ("/Work", "/", "/rm-mcp testing", "/Work/rm-mcp test"):
        message = await refused("remarkable_send", source=str(path), folder=folder)
        assert "outside the write root" in message
    assert puts(cloud) == 0 and cloud.root_updates == 0


async def test_send_into_the_write_root_creates_it(cloud, monkeypatch, tmp_path):
    monkeypatch.setenv("REMARKABLE_WRITE_ROOT", "/rm-mcp test")
    path = tmp_path / "x.pdf"
    path.write_bytes(pdf_bytes(["x"]))
    sent = await data("remarkable_send", source=str(path))
    assert sent["path"] == "/rm-mcp test/x" and sent["created_folders"] == ["/rm-mcp test"]
    again = await data("remarkable_send", source=str(path), folder="sub")
    assert again["path"] == "/rm-mcp test/sub/x" and again["created_folders"] == [
        "/rm-mcp test/sub"
    ]


# =============================================================================
# remarkable_organize
# =============================================================================


async def test_organize_dry_run_makes_zero_write_requests(cloud):
    root = cloud.root_hash
    plan = await data(
        "remarkable_organize",
        changes=[
            {"op": "create_folder", "path": "/Archive"},
            {"op": "move", "item": ANNOTATED, "to": "/Archive"},
            {"op": "rename", "item": "/Work/Standup", "name": "Daily"},
        ],
    )
    assert plan["dry_run"] is True and plan["applied"] is False
    assert [(c["op"], c["before"], c["after"]) for c in plan["changes"]] == [
        ("create_folder", None, "/Archive"),
        ("move", "/Annotated paper", "/Archive/Annotated paper"),
        ("rename", "/Work/Standup", "/Work/Daily"),
    ]
    assert puts(cloud) == 0 and cloud.root_hash == root
    assert set(cloud.methods) == {"GET"}


async def test_organize_applies_in_one_commit(cloud):
    plan = await data(
        "remarkable_organize",
        changes=[
            {"op": "create_folder", "path": "/Archive"},
            {"op": "move", "item": ANNOTATED, "to": "/Archive"},
            {"op": "rename", "item": NOTEBOOK, "name": "Daily"},
        ],
        dry_run=False,
    )
    assert plan["applied"] and plan["undo_token"] and cloud.root_updates == 1
    folder = plan["changes"][0]["id"]
    after = names(cloud)
    assert after[folder] == ("Archive", "")
    assert after[ANNOTATED] == ("Annotated paper", folder)
    assert after[NOTEBOOK] == ("Daily", WORK)
    listed = await data("remarkable_list", folder="/Archive")
    assert [i["id"] for i in listed["items"]] == [ANNOTATED]


async def test_apply_then_undo_restores_the_identical_root_index(cloud):
    original_root = cloud.root_hash
    original_body = cloud.blobs[original_root]
    modified = metadata(cloud, NOTEBOOK)["lastModified"]

    applied = await data(
        "remarkable_organize",
        changes=[
            {"op": "rename", "item": NOTEBOOK, "name": "Daily"},
            {"op": "move", "item": BOOK, "to": "/Work"},
        ],
        dry_run=False,
    )
    assert cloud.root_hash != original_root
    assert metadata(cloud, NOTEBOOK)["lastModified"] == modified
    assert applied["previous_root"] == original_root

    undone = await data("remarkable_undo", token=applied["undo_token"])

    assert cloud.root_hash == original_root
    assert cloud.blobs[cloud.root_hash] == original_body
    assert {(c["before"], c["after"]) for c in undone["reverted"]} == {
        ("/Work/Daily", "/Work/Standup"),
        ("/Work/Field guide", "/Field guide"),
    }
    assert "already undone" in await refused("remarkable_undo", token=applied["undo_token"])


async def test_undo_refuses_when_a_document_changed_since(cloud):
    applied = await data(
        "remarkable_organize",
        changes=[{"op": "rename", "item": NOTEBOOK, "name": "Daily"}],
        dry_run=False,
    )
    # The tablet edits the notebook afterwards.
    edit(cloud, NOTEBOOK, "Daily", notebook_files(NOTEBOOK, {"p": b"new ink"}), parent=WORK)
    root, updates = cloud.root_hash, cloud.root_updates

    message = await refused("remarkable_undo", token=applied["undo_token"])

    assert "changed since" in message
    assert cloud.root_hash == root and cloud.root_updates == updates


async def test_undo_leaves_created_folders(cloud):
    applied = await data(
        "remarkable_organize",
        changes=[
            {"op": "create_folder", "path": "/Archive"},
            {"op": "move", "item": BOOK, "to": "/Archive"},
        ],
        dry_run=False,
    )
    folder = applied["changes"][0]["id"]
    undone = await data("remarkable_undo", token=applied["undo_token"])
    assert undone["left_in_place"] == ["/Archive"]
    assert names(cloud)[folder] == ("Archive", "") and names(cloud)[BOOK][1] == ""


async def test_send_has_no_undo(cloud, tmp_path):
    import rm_mcp.index as index_mod

    path = tmp_path / "x.pdf"
    path.write_bytes(pdf_bytes(["x"]))
    await data("remarkable_send", source=str(path))
    conn = index_mod.get_instance()._get_connection()
    [token] = [r["token"] for r in conn.execute("SELECT token FROM write_log WHERE kind='send'")]
    assert "deleting" in await refused("remarkable_undo", token=token)


async def test_organize_refuses_the_whole_batch_outside_the_write_root(cloud, monkeypatch):
    monkeypatch.setenv("REMARKABLE_WRITE_ROOT", "/Work")
    root = cloud.root_hash
    cases = [
        [{"op": "move", "item": BOOK, "to": "/Work"}],  # source outside
        [{"op": "move", "item": NOTEBOOK, "to": "/"}],  # destination outside
        [{"op": "rename", "item": WORK, "name": "Job"}],  # the write root itself
        [{"op": "create_folder", "path": "/Elsewhere"}],
        # one bad change refuses the good one before it
        [
            {"op": "rename", "item": NOTEBOOK, "name": "Fine"},
            {"op": "rename", "item": ANNOTATED, "name": "Not fine"},
        ],
    ]
    for changes in cases:
        for dry_run in (True, False):
            message = await refused("remarkable_organize", changes=changes, dry_run=dry_run)
            assert "outside the write root" in message, (changes, message)
    assert puts(cloud) == 0 and cloud.root_hash == root
    ok = await data(
        "remarkable_organize",
        changes=[{"op": "rename", "item": NOTEBOOK, "name": "Fine"}],
        dry_run=False,
    )
    assert ok["applied"]


async def test_write_root_must_be_inside_the_root_path(cloud, monkeypatch):
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
    monkeypatch.setenv("REMARKABLE_WRITE_ROOT", "/Other")
    message = await refused(
        "remarkable_organize", changes=[{"op": "rename", "item": NOTEBOOK, "name": "x"}]
    )
    assert "outside REMARKABLE_ROOT_PATH" in message


async def test_root_path_is_the_default_write_root(cloud, monkeypatch):
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
    applied = await data(
        "remarkable_organize",
        changes=[{"op": "create_folder", "path": "Inbox"}],
        dry_run=False,
    )
    assert applied["changes"][0]["after"] == "/Inbox"  # shown relative to /Work
    assert names(cloud)[applied["changes"][0]["id"]] == ("Inbox", WORK)


async def test_same_item_twice_is_refused(cloud):
    message = await refused(
        "remarkable_organize",
        changes=[
            {"op": "rename", "item": BOOK, "name": "A"},
            {"op": "move", "item": BOOK, "to": "/Work"},
        ],
    )
    assert "changed twice" in message


# =============================================================================
# Read-only, the root guard, conflicts, no deletes
# =============================================================================


async def test_read_only_blocks_every_write(cloud, monkeypatch, tmp_path):
    applied = await data(
        "remarkable_organize",
        changes=[{"op": "rename", "item": BOOK, "name": "B"}],
        dry_run=False,
    )
    monkeypatch.setenv("REMARKABLE_READ_ONLY", "1")
    cloud.methods.clear()
    path = tmp_path / "x.pdf"
    path.write_bytes(pdf_bytes(["x"]))
    calls = [
        ("remarkable_send", {"source": str(path)}),
        ("remarkable_send", {"source": "# hi"}),
        ("remarkable_organize", {"changes": [{"op": "rename", "item": BOOK, "name": "C"}]}),
        (
            "remarkable_organize",
            {"changes": [{"op": "create_folder", "path": "/X"}], "dry_run": False},
        ),
        ("remarkable_undo", {"token": applied["undo_token"]}),
    ]
    for tool, arguments in calls:
        assert "read-only" in await refused(tool, **arguments)
    for tool, arguments in [
        ("remarkable_rename", {"item": BOOK, "new_name": "D"}),
        ("remarkable_move", {"item": BOOK, "destination": "/Work"}),
        ("remarkable_create_folder", {"path": "/Y"}),
    ]:
        result = await mcp.call_tool(tool, arguments)
        assert json.loads(result.content[0].text)["_error"]["type"] == "writes_disabled"
    assert cloud.methods["PUT"] == 0


def _break_root_serialization(monkeypatch, how: str):
    """Inject a bug: the root index the writer builds loses or alters an unrelated entry."""
    real = organize_client.serialize_index

    def buggy(entries, owner):
        if owner == ".":
            entries = [dict(e) for e in entries]
            victim = next(i for i, e in enumerate(entries) if e["id"] == ANNOTATED)
            if how == "lose":
                entries.pop(victim)
            else:
                entries[victim]["size"] = int(entries[victim]["size"]) + 1
        return real(entries, owner)

    monkeypatch.setattr(organize_client, "serialize_index", buggy)


@pytest.mark.parametrize("how", ["lose", "alter"])
async def test_root_guard_aborts_on_an_unrelated_difference(cloud, monkeypatch, tmp_path, how):
    root = cloud.root_hash
    _break_root_serialization(monkeypatch, how)

    message = await refused(
        "remarkable_organize",
        changes=[{"op": "rename", "item": BOOK, "name": "Renamed"}],
        dry_run=False,
    )
    assert "Nothing was committed" in message
    path = tmp_path / "x.pdf"
    path.write_bytes(pdf_bytes(["x"]))
    assert "Nothing was committed" in await refused("remarkable_send", source=str(path))

    assert cloud.root_hash == root and cloud.root_updates == 0
    assert "root.docSchema" not in cloud.uploads


def test_verify_root_change_cases():
    from rm_mcp.clients.cloud import serialize_index

    def body(*entries):
        return serialize_index(
            [{"hash": h, "type": "0", "id": i, "subfiles": 1, "size": 10} for i, h in entries], "."
        )

    old = body(("a", "h1"), ("b", "h2"))
    verify_root_change(old, body(("a", "h1"), ("b", "h9")), [], ["b"])
    verify_root_change(old, body(("a", "h1"), ("b", "h2"), ("c", "h3")), ["c"], [])
    for new, added, changed in [
        (body(("a", "h1")), [], []),  # lost b
        (body(("a", "h9"), ("b", "h2")), [], ["b"]),  # a changed, b was intended
        (body(("a", "h1"), ("b", "h2"), ("c", "h3")), [], []),  # unexpected add
        (body(("a", "h1"), ("b", "h2")), ["c"], []),  # intended add missing
        (body(("b", "h2"), ("a", "h1")), [], []),  # unsorted
        (old.replace(b"0:.:2:20", b"0:.:2:21"), [], []),  # header lies
    ]:
        with pytest.raises(RootGuardError):
            verify_root_change(old, new, added, changed)


async def test_generation_conflict_is_retried_then_aborts_cleanly(cloud):
    cloud.conflicts = 1
    ok = await data(
        "remarkable_organize",
        changes=[{"op": "rename", "item": BOOK, "name": "Once"}],
        dry_run=False,
    )
    assert ok["applied"] and names(cloud)[BOOK][0] == "Once"

    root = cloud.root_hash
    cloud.conflicts = 99
    updates = cloud.root_updates
    message = await refused(
        "remarkable_organize",
        changes=[{"op": "rename", "item": BOOK, "name": "Never"}],
        dry_run=False,
    )
    assert "Nothing was modified" in message
    assert cloud.root_hash == root and cloud.root_updates == updates + 3


def test_writer_refuses_a_document_changed_since_the_plan(cloud):
    import rm_mcp.api as api_mod

    client = api_mod._client_singleton
    client.get_meta_items()
    stale = cloud.entries[BOOK]["hash"]
    edit(cloud, BOOK, "Edited on the tablet", {f"{BOOK}.content": b"{}"})
    root = cloud.root_hash
    with pytest.raises(ChangedSinceRead):
        LibraryWriter(client).apply([MetadataEdit(BOOK, {"visibleName": "Mine"}, stale)])
    assert cloud.root_hash == root


async def test_no_request_is_ever_a_delete(cloud, tmp_path):
    path = tmp_path / "x.pdf"
    path.write_bytes(pdf_bytes(["x"]))
    await data("remarkable_send", source=str(path), folder="/Inbox")
    applied = await data(
        "remarkable_organize",
        changes=[{"op": "move", "item": BOOK, "to": "/Inbox"}],
        dry_run=False,
    )
    await data("remarkable_undo", token=applied["undo_token"])
    assert set(cloud.methods) == {"GET", "PUT"}
    # Nothing left the root index either.
    assert {NOTEBOOK, ANNOTATED, BOOK, WORK} <= set(names(cloud))


def test_no_code_path_issues_a_delete():
    """The package never sends DELETE, calls a delete method or marks metadata deleted."""
    package = Path(__file__).parent.parent / "rm_mcp"
    patterns = [
        r"""["']DELETE["']""",
        r"\.delete\(",
        r"""["']deleted["']\s*\]\s*=""",
        r"""["']deleted["']\s*:\s*True""",
    ]
    offenders = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == []


async def test_not_connected_is_a_clear_tool_error(monkeypatch):
    import rm_mcp.cache as cache_mod

    def no_client():
        raise RuntimeError("Not authenticated. Run: uvx rm-mcp --setup")

    monkeypatch.setattr(cache_mod, "get_cached_collection", no_client)
    from rm_mcp.tools import _helpers

    monkeypatch.setattr(_helpers, "get_cached_collection", no_client)
    message = await refused(
        "remarkable_organize", changes=[{"op": "rename", "item": "x", "name": "y"}]
    )
    assert "Not authenticated" in message and "--setup" in message


# =============================================================================
# Undo safety: no undo log, no commit
# =============================================================================

_RENAME = [{"op": "rename", "item": NOTEBOOK, "name": "Daily"}]


async def test_organize_refuses_when_the_index_is_unavailable(cloud, monkeypatch):
    import rm_mcp.index as index_mod

    monkeypatch.setattr(index_mod, "_instance", None)
    root, before = cloud.root_hash, names(cloud)
    message = await refused("remarkable_organize", changes=_RENAME, dry_run=False)
    assert "index is unavailable" in message and "Nothing was changed" in message
    assert puts(cloud) == 0 and cloud.root_hash == root and names(cloud) == before


async def test_organize_refuses_when_the_undo_log_cannot_be_written(cloud, monkeypatch):
    import sqlite3

    import rm_mcp.index as index_mod

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(index_mod.get_instance(), "save_write", broken)
    root = cloud.root_hash
    message = await refused("remarkable_organize", changes=_RENAME, dry_run=False)
    assert "readonly database" in message and "Nothing was changed" in message
    assert puts(cloud) == 0 and cloud.root_hash == root


async def test_deprecated_rename_refuses_without_an_undo_log(cloud, monkeypatch):
    import rm_mcp.index as index_mod

    monkeypatch.setattr(index_mod, "_instance", None)
    root = cloud.root_hash
    raw = await mcp.call_tool("remarkable_rename", {"item": NOTEBOOK, "new_name": "Daily"})
    assert "undo" in str(raw)
    assert puts(cloud) == 0 and cloud.root_hash == root


async def test_dry_run_still_works_without_an_index(cloud, monkeypatch):
    import rm_mcp.index as index_mod

    monkeypatch.setattr(index_mod, "_instance", None)
    plan = await data("remarkable_organize", changes=_RENAME)
    assert plan["dry_run"] is True and puts(cloud) == 0
