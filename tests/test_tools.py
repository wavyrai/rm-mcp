"""The v2 read tools end to end: the real cloud client on the in-process fake cloud.

Every call goes through ``mcp.call_tool`` so the wire shapes (structured
content, images, errors) are what a client sees.
"""

import asyncio
import base64
import io
import json
import zipfile
from pathlib import Path
from typing import Dict

import fitz
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image
from test_sync import FakeCloud, make_client

import rm_mcp.api as api_mod
import rm_mcp.cache as cache_mod
import rm_mcp.index as index_mod
from rm_mcp import reader
from rm_mcp.clients.cloud import serialize_index
from rm_mcp.server import mcp

FIXTURES = Path(__file__).parent / "fixtures"
WORK = "f0000000-0000-4000-8000-00000000work"
NOTEBOOK = "n0000000-0000-4000-8000-000000000001"
ANNOTATED = "a0a0a0a0-0000-4000-8000-000000000001"
HIGHLIGHTED = "h0000000-0000-4000-8000-000000000001"
BOOK = "e0000000-0000-4000-8000-000000000001"
SKETCHES = "s0000000-0000-4000-8000-000000000001"

TYPED = (FIXTURES / "typed_text.rm").read_bytes()
HANDWRITING = (FIXTURES / "handwriting.rm").read_bytes()
HIGHLIGHT = (FIXTURES / "highlight.rm").read_bytes()


def _rmdoc_members() -> Dict[str, bytes]:
    with zipfile.ZipFile(FIXTURES / "annotated_pdf.rmdoc") as zf:
        return {name: zf.read(name) for name in zf.namelist()}


EDITED_INK = _rmdoc_members()[f"{ANNOTATED}/pg-inserted.rm"]


class Cloud(FakeCloud):
    def set_files(
        self,
        doc_id: str,
        name: str,
        files: Dict[str, bytes],
        parent: str = "",
        modified: int = 1750000000000,
        folder: bool = False,
    ) -> None:
        metadata = json.dumps(
            {
                "visibleName": name,
                "type": "CollectionType" if folder else "DocumentType",
                "parent": parent,
                "lastModified": str(modified),
            }
        ).encode()
        entries = [
            {"hash": self._put(data), "type": "0", "id": fid, "subfiles": 0, "size": len(data)}
            for fid, data in [(f"{doc_id}.metadata", metadata), *files.items()]
        ]
        self.entries[doc_id] = {
            "hash": self._put(serialize_index(entries, doc_id)),
            "type": "0",
            "id": doc_id,
            "subfiles": len(entries),
            "size": sum(e["size"] for e in entries),
        }


def _content(pages) -> bytes:
    entries = []
    for pid, redir in pages:
        entry = {"id": pid}
        if redir is not None:
            entry["redir"] = {"timestamp": "1:2", "value": redir}
        entries.append(entry)
    return json.dumps({"cPages": {"pages": entries}}).encode()


def notebook_files(doc_id: str, pages: Dict[str, bytes]) -> Dict[str, bytes]:
    files = {f"{doc_id}.content": _content([(pid, None) for pid in pages])}
    files.update({f"{doc_id}/{pid}.rm": data for pid, data in pages.items()})
    return files


def pdf_bytes(texts) -> bytes:
    doc = fitz.open()
    for text in texts:
        page = doc.new_page()
        if text:
            page.insert_textbox(fitz.Rect(36, 36, 560, 800), text, fontsize=8)
    data = doc.tobytes()
    doc.close()
    return data


def build_cloud() -> Cloud:
    cloud = Cloud()
    cloud.set_files(WORK, "Work", {}, folder=True)
    cloud.set_files(
        NOTEBOOK,
        "Standup",
        notebook_files(NOTEBOOK, {"p-typed": TYPED, "p-hand": HANDWRITING}),
        parent=WORK,
        modified=1750000300000,
    )
    cloud.set_files(ANNOTATED, "Annotated paper", _rmdoc_members(), modified=1750000200000)
    cloud.set_files(
        HIGHLIGHTED,
        "Highlighted",
        {
            f"{HIGHLIGHTED}.content": _content([("h1", 0), ("h2", 1), ("h3", 2)]),
            f"{HIGHLIGHTED}.pdf": (FIXTURES / "three_pages.pdf").read_bytes(),
            f"{HIGHLIGHTED}/h1.rm": HIGHLIGHT,
            f"{HIGHLIGHTED}.highlights/h2.json": json.dumps(
                {"highlights": [[{"text": "real extractable text", "start": 3, "length": 21}]]}
            ).encode(),
        },
        modified=1750000100000,
    )
    cloud.set_files(
        BOOK,
        "Field guide",
        {
            f"{BOOK}.content": b"{}",
            f"{BOOK}.epub": (FIXTURES / "small.epub").read_bytes(),
        },
        modified=1750000000000,
    )
    cloud.set_files(
        SKETCHES,
        "Sketches",
        notebook_files(SKETCHES, {f"s{n}": HANDWRITING for n in range(6)}),
        modified=1740000000000,
    )
    cloud.commit()
    return cloud


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    for var in (
        "REMARKABLE_ROOT_PATH",
        "REMARKABLE_COMPACT",
        "REMARKABLE_MAX_OUTPUT_CHARS",
        "REMARKABLE_BACKGROUND_COLOR",
    ):
        monkeypatch.delenv(var, raising=False)
    saved = index_mod._instance
    index_mod._instance = None
    index = index_mod.initialize(str(tmp_path / "index.db"))
    cache_mod.clear_page_ocr_cache()
    cache_mod.invalidate_collection_cache()
    reader.clear_caches()

    fake = build_cloud()
    monkeypatch.setattr(api_mod, "_client_singleton", make_client(fake, index, tmp_path))
    yield fake
    cache_mod.invalidate_collection_cache()
    cache_mod.clear_page_ocr_cache()
    reader.clear_caches()
    index_mod.close()
    index_mod._instance = saved


def edit(cloud: Cloud, doc_id: str, name: str, files: Dict[str, bytes], **kwargs) -> None:
    cloud.set_files(doc_id, name, files, **kwargs)
    cloud.commit()
    cache_mod.invalidate_collection_cache()


async def call(tool: str, **arguments):
    result = await mcp.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result


async def data(tool: str, **arguments) -> dict:
    result = await call(tool, **arguments)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


async def startup_indexing() -> None:
    from rm_mcp import loader

    await loader.load_library(asyncio.Event(), pause_seconds=0)


def png_of(block) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(block.data)))


# =============================================================================
# tools/list
# =============================================================================


NEW_TOOLS = {
    "remarkable_find",
    "remarkable_list",
    "remarkable_read",
    "remarkable_view",
    "remarkable_annotations",
    "remarkable_save_transcript",
    "remarkable_status",
}
ALIASES = {"remarkable_search", "remarkable_browse", "remarkable_recent", "remarkable_image"}


async def test_tools_list_has_the_new_tools_with_schemas():
    tools = {t.name: t for t in await mcp.list_tools()}
    assert NEW_TOOLS | ALIASES <= set(tools)
    for name in NEW_TOOLS - {"remarkable_view"}:
        assert tools[name].output_schema, f"{name} has no outputSchema"
    for name in NEW_TOOLS - {"remarkable_save_transcript"}:
        assert tools[name].annotations.read_only_hint is True, name
    save = tools["remarkable_save_transcript"].annotations
    assert save.read_only_hint is False and save.destructive_hint is False
    for name in ALIASES:
        assert "(deprecated)" in tools[name].title


# =============================================================================
# remarkable_read
# =============================================================================


async def test_read_typed_page_returns_its_text(cloud):
    result = await data("remarkable_read", id=NOTEBOOK, pages="1")
    assert result["id"] == NOTEBOOK and result["page_count"] == 2
    [page] = result["pages"]
    assert page["page"] == 1 and page["handwriting"] is False
    assert page["text"].splitlines() == [
        "Meeting notes",
        "Ship the fixture set",
        "Fix typed text extraction",
    ]


async def test_read_handwritten_page_says_handwriting(cloud):
    result = await data("remarkable_read", id=NOTEBOOK, pages="2")
    [page] = result["pages"]
    assert page["page"] == 2 and page["handwriting"] is True and page["transcript"] is None
    assert "remarkable_view" in result["hint"]


async def test_read_accepts_path_and_name_and_returns_the_id(cloud):
    by_path = await data("remarkable_read", id="/Work/Standup", pages="1")
    by_name = await data("remarkable_read", id="standup", pages="1")
    assert by_path["id"] == by_name["id"] == NOTEBOOK
    assert by_path["path"] == "/Work/Standup"


async def test_read_pdf_with_inserted_page_numbers_tablet_pages(cloud):
    result = await data("remarkable_read", id=ANNOTATED, pages="1-4")
    pages = {p["page"]: p for p in result["pages"]}
    assert sorted(pages) == [1, 2, 3, 4]
    assert "Page one" in pages[1]["text"] and pages[1]["source_page"] == 1
    assert pages[2]["inserted"] is True and pages[2]["text"] == ""
    assert pages[2]["handwriting"] is True
    assert "Page two" in pages[3]["text"] and pages[3]["source_page"] == 2
    assert "zebras" in pages[4]["text"] and pages[4]["source_page"] == 3


async def test_read_epub_pages(cloud):
    result = await data("remarkable_read", id=BOOK, pages="all")
    text = " ".join(p["text"] for p in result["pages"])
    assert "lighthouses" in text and "tidal pools" in text


async def test_read_output_cap_names_the_cut_pages(cloud, monkeypatch):
    long_id = "l0000000-0000-4000-8000-000000000001"
    words = " ".join(f"word{n}" for n in range(120))
    edit(
        cloud,
        long_id,
        "Long paper",
        {
            f"{long_id}.content": _content([("a", 0), ("b", 1), ("c", 2)]),
            f"{long_id}.pdf": pdf_bytes([words, words, words]),
        },
    )
    monkeypatch.setenv("REMARKABLE_MAX_OUTPUT_CHARS", "1000")
    result = await data("remarkable_read", id=long_id, pages="1-3")
    assert [p["page"] for p in result["pages"]] == [1, 2]
    assert result["pages"][1]["truncated"] is True
    assert result["cut_pages"] == [3]
    assert sum(len(p["text"]) for p in result["pages"]) <= 1000
    assert "3" in result["hint"]


async def test_read_unknown_document_is_a_clear_error(cloud):
    with pytest.raises(ToolError) as exc:
        await mcp.call_tool("remarkable_read", {"id": "Quarterly Repot"})
    assert "remarkable_find" in str(exc.value)


async def test_read_page_out_of_range_names_the_count(cloud):
    with pytest.raises(ToolError) as exc:
        await mcp.call_tool("remarkable_read", {"id": NOTEBOOK, "pages": "9"})
    assert "2 pages" in str(exc.value)


# =============================================================================
# remarkable_save_transcript + find
# =============================================================================


async def test_save_transcript_then_find_and_read_see_it(cloud):
    saved = await data(
        "remarkable_save_transcript", id=NOTEBOOK, page=2, text="Zeppelin budget review"
    )
    assert saved["saved"] is True and saved["page"] == 2

    found = await data("remarkable_find", query="zeppelin")
    assert found["results"][0]["id"] == NOTEBOOK
    assert found["results"][0]["hits"][0]["page"] == 2

    [page] = (await data("remarkable_read", id=NOTEBOOK, pages="2"))["pages"]
    assert page["transcript"] == "Zeppelin budget review"


async def test_editing_the_page_drops_its_transcript(cloud):
    await call("remarkable_save_transcript", id=NOTEBOOK, page=2, text="Zeppelin budget review")
    edit(
        cloud,
        NOTEBOOK,
        "Standup",
        notebook_files(NOTEBOOK, {"p-typed": TYPED, "p-hand": EDITED_INK}),
        parent=WORK,
    )

    [page] = (await data("remarkable_read", id=NOTEBOOK, pages="2"))["pages"]
    assert page["handwriting"] is True and page["transcript"] is None
    assert (await data("remarkable_find", query="zeppelin"))["results"] == []


async def test_editing_another_page_keeps_the_transcript(cloud):
    await call("remarkable_save_transcript", id=NOTEBOOK, page=2, text="Zeppelin budget review")
    edit(
        cloud,
        NOTEBOOK,
        "Standup",
        notebook_files(NOTEBOOK, {"p-typed": HIGHLIGHT, "p-hand": HANDWRITING}),
        parent=WORK,
    )
    one, two = (await data("remarkable_read", id=NOTEBOOK, pages="1-2"))["pages"]
    assert one["text"] == ""  # the new version is read, not a cached old one
    assert two["transcript"] == "Zeppelin budget review"


async def test_save_transcript_rejects_a_blank_page(cloud):
    blank_id = "b0000000-0000-4000-8000-000000000001"
    edit(cloud, blank_id, "Blank", {f"{blank_id}.content": _content([("x", None)])})
    with pytest.raises(ToolError) as exc:
        await mcp.call_tool("remarkable_save_transcript", {"id": blank_id, "page": 1, "text": "x"})
    assert "blank" in str(exc.value)


# =============================================================================
# remarkable_find
# =============================================================================


async def test_find_searches_pdf_text_by_tablet_page(cloud):
    await startup_indexing()
    found = await data("remarkable_find", query="zebras")
    ids = [r["id"] for r in found["results"]]
    assert ANNOTATED in ids
    annotated = found["results"][ids.index(ANNOTATED)]
    assert annotated["hits"][0]["page"] == 4  # PDF page 3, after the inserted page
    assert "zebras" in annotated["hits"][0]["snippet"]
    assert annotated["path"] == "/Annotated paper" and annotated["type"] == "pdf"


async def test_find_searches_typed_text_highlights_and_epub(cloud):
    await startup_indexing()
    assert (await data("remarkable_find", query="fixture set"))["results"][0]["id"] == NOTEBOOK
    assert (await data("remarkable_find", query="lighthouses"))["results"][0]["id"] == BOOK
    highlighted = await data("remarkable_find", query="quick brown", type="pdf")
    assert HIGHLIGHTED in [r["id"] for r in highlighted["results"]]


async def test_find_matches_names(cloud):
    found = await data("remarkable_find", query="standup")
    assert found["results"][0]["id"] == NOTEBOOK
    assert found["results"][0]["matched_on"] == ["name"]


async def test_find_without_query_lists_recent_documents(cloud):
    found = await data("remarkable_find")
    assert [r["id"] for r in found["results"]][:3] == [NOTEBOOK, ANNOTATED, HIGHLIGHTED]
    assert all(r["id"] != WORK for r in found["results"])


async def test_find_filters(cloud):
    pdfs = await data("remarkable_find", type="pdf")
    assert {r["id"] for r in pdfs["results"]} == {ANNOTATED, HIGHLIGHTED}
    in_work = await data("remarkable_find", folder="/Work")
    assert [r["id"] for r in in_work["results"]] == [NOTEBOOK]
    recent = await data("remarkable_find", modified_after="2025-06-15T15:09:00Z")
    assert [r["id"] for r in recent["results"]] == [NOTEBOOK, ANNOTATED]
    limited = await data("remarkable_find", limit=2)
    assert len(limited["results"]) == 2 and limited["total"] == 5


async def test_find_rejects_a_bad_date(cloud):
    with pytest.raises(ToolError) as exc:
        await mcp.call_tool("remarkable_find", {"modified_after": "last tuesday"})
    assert "ISO" in str(exc.value)


async def test_find_respects_the_root_path(cloud, monkeypatch):
    await startup_indexing()
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
    found = await data("remarkable_find")
    assert [r["id"] for r in found["results"]] == [NOTEBOOK]
    assert found["results"][0]["path"] == "/Standup"
    assert (await data("remarkable_find", query="zebras"))["results"] == []
    with pytest.raises(ToolError):
        await mcp.call_tool("remarkable_read", {"id": ANNOTATED})


# =============================================================================
# remarkable_list
# =============================================================================


async def test_list_root_one_level(cloud):
    listed = await data("remarkable_list")
    names = [i["name"] for i in listed["items"]]
    assert names[0] == "Work"  # folders first
    assert "Standup" not in names
    work = listed["items"][0]
    assert work["id"] == WORK and work["type"] == "folder"
    paper = next(i for i in listed["items"] if i["id"] == ANNOTATED)
    assert paper["type"] == "pdf" and paper["modified"]


async def test_list_deeper_and_by_id(cloud):
    deep = await data("remarkable_list", depth=2)
    standup = next(i for i in deep["items"] if i["id"] == NOTEBOOK)
    assert standup["path"] == "/Work/Standup" and standup["depth"] == 2
    by_id = await data("remarkable_list", folder=WORK)
    assert [i["id"] for i in by_id["items"]] == [NOTEBOOK]


async def test_list_sorts_by_modified_and_pages(cloud):
    first = await data("remarkable_list", sort="modified", limit=2)
    assert [i["id"] for i in first["items"]] == [WORK, ANNOTATED]
    assert first["next_cursor"]
    rest = await data("remarkable_list", sort="modified", limit=10, cursor=first["next_cursor"])
    assert [i["id"] for i in rest["items"]] == [HIGHLIGHTED, BOOK, SKETCHES]
    assert rest["next_cursor"] is None


async def test_list_unknown_folder_is_a_clear_error(cloud):
    with pytest.raises(ToolError) as exc:
        await mcp.call_tool("remarkable_list", {"folder": "/Nope"})
    assert "remarkable_list" in str(exc.value)


# =============================================================================
# remarkable_view
# =============================================================================


async def test_view_returns_an_opaque_full_page_png_under_budget(cloud):
    result = await call("remarkable_view", id=NOTEBOOK, pages="2", size="large")
    images = [b for b in result.content if b.type == "image"]
    text = " ".join(b.text for b in result.content if b.type == "text")
    assert len(images) == 1 and images[0].mime_type == "image/png"
    img = png_of(images[0])
    assert img.size == (1404, 1872)
    rgba = img.convert("RGBA")
    assert rgba.getchannel("A").getextrema() == (255, 255)
    assert rgba.getpixel((5, 5))[:3] == (251, 251, 251)
    assert sum(len(i.data) for i in images) < 1_000_000
    assert "remarkable_save_transcript" in text


async def test_view_normal_size_is_downscaled_whole_page(cloud):
    result = await call("remarkable_view", id=NOTEBOOK, pages="2")
    [image] = [b for b in result.content if b.type == "image"]
    width, height = png_of(image).size
    assert abs(width - 1404 * 0.6) <= 1 and abs(height - 1872 * 0.6) <= 1
    result = await call("remarkable_view", id=ANNOTATED, pages="1")
    [image] = [b for b in result.content if b.type == "image"]
    width, height = png_of(image).size
    assert height < 1872 and abs(width / height - 595 / 842) < 0.01  # the whole A4 page


async def test_view_honours_the_background_setting(cloud, monkeypatch):
    monkeypatch.setenv("REMARKABLE_BACKGROUND_COLOR", "#FFFFFF")
    result = await call("remarkable_view", id=NOTEBOOK, pages="1")
    [image] = [b for b in result.content if b.type == "image"]
    assert png_of(image).convert("RGB").getpixel((3, 3)) == (255, 255, 255)


async def test_view_caps_pages_per_call(cloud):
    result = await call("remarkable_view", id=SKETCHES, pages="1-6")
    images = [b for b in result.content if b.type == "image"]
    text = " ".join(b.text for b in result.content if b.type == "text")
    assert len(images) == 4
    assert "5" in text and "6" in text and "4 pages" in text


# =============================================================================
# remarkable_annotations
# =============================================================================


async def test_annotations_reads_both_highlight_formats(cloud):
    result = await data("remarkable_annotations", id=HIGHLIGHTED)
    highlights = {(h["page"], h["text"]) for h in result["highlights"]}
    assert (1, "quick brown fox") in highlights
    assert (2, "real extractable text") in highlights


async def test_annotations_lists_ink_pages(cloud):
    result = await data("remarkable_annotations", id=ANNOTATED)
    assert [p["page"] for p in result["ink_pages"]] == [1, 2, 4]
    assert result["ink_pages"][1]["inserted"] is True
    only_two = await data("remarkable_annotations", id=ANNOTATED, pages="2-3")
    assert [p["page"] for p in only_two["ink_pages"]] == [2]


# =============================================================================
# remarkable_status
# =============================================================================


async def test_status_is_structured(cloud):
    status = await data("remarkable_status")
    assert status["authenticated"] is True and status["document_count"] == 5
    assert status["config"]["background_color"] == "#FBFBFB"


# =============================================================================
# Deprecated aliases
# =============================================================================


async def test_alias_search(cloud):
    await startup_indexing()
    result = await data("remarkable_search", query="zebras")
    assert "remarkable_find" in result["deprecated"]
    assert ANNOTATED in [r["id"] for r in result["results"]]


async def test_alias_recent_and_browse(cloud):
    recent = await data("remarkable_recent", limit=2)
    assert "remarkable_find" in recent["deprecated"] and len(recent["results"]) == 2
    browse = await data("remarkable_browse", path="/Work")
    assert "remarkable_list" in browse["deprecated"]
    assert [i["id"] for i in browse["items"]] == [NOTEBOOK]
    redirected = await data("remarkable_browse", path="/Work/Standup")
    assert redirected["id"] == NOTEBOOK and redirected["pages"][0]["page"] == 1


async def test_alias_image(cloud):
    result = await call("remarkable_image", document="Standup", page=2)
    assert any(b.type == "image" for b in result.content)
    assert "remarkable_view" in result.content[0].text


async def test_old_read_parameters_still_answer(cloud):
    result = await data("remarkable_read", document="Standup", page=1)
    assert result["id"] == NOTEBOOK and [p["page"] for p in result["pages"]] == [1]
    assert "remarkable_read(id" in result["deprecated"]
    grepped = await data("remarkable_read", document=ANNOTATED, grep="zebra")
    assert [p["page"] for p in grepped["pages"]] == [4]


# =============================================================================
# Resources and prompts
# =============================================================================


async def test_resource_templates_resolve_live(cloud):
    templates = {t.uri_template for t in await mcp.list_resource_templates()}
    assert templates == {"remarkable://doc/{id}", "remarkable://doc/{id}/page/{n}.png"}
    assert await mcp.list_resources() == []

    [text] = await mcp.read_resource(f"remarkable://doc/{NOTEBOOK}")
    assert "Meeting notes" in text.content and "--- Page 2" in text.content
    [png] = await mcp.read_resource(f"remarkable://doc/{NOTEBOOK}/page/2.png")
    img = Image.open(io.BytesIO(png.content))
    assert img.size == (1404, 1872) and png.mime_type == "image/png"


async def test_three_prompts():
    names = {p.name for p in await mcp.list_prompts()}
    assert names == {"transcribe_notebook", "weekly_review", "find_topic"}


def test_no_sampling_left():
    import importlib.util

    assert importlib.util.find_spec("rm_mcp.ocr") is None
    assert importlib.util.find_spec("rm_mcp.capabilities") is None
