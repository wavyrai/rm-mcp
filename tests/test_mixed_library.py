"""Libraries as real accounts hold them: schema 3 indexes, Methods templates, the trash."""

import base64
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from rm_mcp.blobstore import BlobHashMismatch, BlobStore, blob_matches, schema3_index_hash
from rm_mcp.clients.cloud import RemarkableClient
from rm_mcp.extract.document import read_document_pages
from rm_mcp.extract.render import render_page_from_document_zip
from rm_mcp.paths import get_item_path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def schema3_index(files: dict) -> bytes:
    """A schema 3 index as the cloud stores it: version line, then entries, no summary."""
    lines = ["3"] + [f"{sha(d)}:0:{name}:0:{len(d)}" for name, d in sorted(files.items())]
    return ("\n".join(lines) + "\n").encode()


FILES = {
    "doc.metadata": b'{"visibleName": "Old"}',
    "doc.content": b"{}",
    "doc/page.rm": b"ink",
}


class TestSchema3Hash:
    def test_hash_is_over_the_sorted_entry_hashes_not_the_bytes(self):
        body = schema3_index(FILES)
        expected = hashlib.sha256(
            b"".join(bytes.fromhex(sha(FILES[n])) for n in sorted(FILES))
        ).hexdigest()
        assert schema3_index_hash(body) == expected
        assert sha(body) != expected

    def test_entry_order_in_the_file_does_not_matter(self):
        body = schema3_index(FILES)
        head, *entries = body.decode().rstrip("\n").split("\n")
        shuffled = ("\n".join([head, *reversed(entries)]) + "\n").encode()
        assert schema3_index_hash(shuffled) == schema3_index_hash(body)

    def test_schema4_and_plain_blobs_are_not_schema3(self):
        assert schema3_index_hash(b"4\n0:.:0:0\n") is None
        assert schema3_index_hash(b"%PDF-1.7") is None

    def test_blob_matches_accepts_either_rule_and_nothing_else(self):
        body = schema3_index(FILES)
        assert blob_matches(body, schema3_index_hash(body))
        assert blob_matches(body, sha(body))
        assert not blob_matches(body, sha(b"something else"))

    def test_blob_store_keeps_and_serves_schema3_indexes(self, tmp_path):
        store = BlobStore(tmp_path / "s", max_bytes=1 << 20)
        body = schema3_index(FILES)
        digest = schema3_index_hash(body)
        store.put(digest, body)
        assert store.get(digest) == body
        other = schema3_index({**FILES, "doc.content": b'{"x": 1}'})
        with pytest.raises(BlobHashMismatch):
            store.put(digest, other)

    def test_schema3_hash_does_not_cover_sizes(self):
        # Inherent to schema 3: only entry hashes are hashed. download() checks
        # sizes against the bytes instead.
        body = schema3_index(FILES)
        wrong_size = body.replace(b":0:doc.content:0:2", b":0:doc.content:0:3")
        assert wrong_size != body
        assert schema3_index_hash(wrong_size) == schema3_index_hash(body)

    def test_download_rejects_a_size_its_index_misstates(self):
        blobs = {sha(d): d for d in FILES.values()}
        body = schema3_index(FILES).replace(b":0:doc.content:0:2", b":0:doc.content:0:3")
        blobs[schema3_index_hash(body)] = body
        client = RemarkableClient(user_token="t", blob_store=None, state_store=None)
        client._request = lambda url, **kw: SimpleNamespace(
            content=blobs[url.rsplit("/", 1)[-1]], raise_for_status=lambda: None
        )
        doc = SimpleNamespace(id="doc", hash=schema3_index_hash(body))
        with pytest.raises(RuntimeError, match="its index says 3"):
            client.download(doc)

    def test_client_accepts_a_schema3_document_index(self):
        body = schema3_index(FILES)
        digest = schema3_index_hash(body)
        client = RemarkableClient(user_token="t", blob_store=None, state_store=None)
        client._request = lambda url, **kw: SimpleNamespace(
            content=body, raise_for_status=lambda: None
        )
        assert client._get_file(digest, "doc.docSchema") == body

    def test_client_still_rejects_corrupted_index(self):
        body = schema3_index(FILES)
        digest = schema3_index_hash(body)
        client = RemarkableClient(user_token="t", blob_store=None, state_store=None)
        client._request = lambda url, **kw: SimpleNamespace(
            content=body[:-5], raise_for_status=lambda: None
        )
        with pytest.raises(RuntimeError, match="hash check"):
            client._get_file(digest, "doc.docSchema")


SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="150" height="200">'
    b'<rect x="10" y="10" width="130" height="180" fill="black"/></svg>'
)


def template_zip() -> io.BytesIO:
    template = {
        "name": "Meeting minutes",
        "categories": ["Productivity"],
        "labels": ["Meetings"],
        "iconData": base64.b64encode(SVG).decode(),
        "items": [
            {
                "type": "group",
                "children": [
                    {"type": "text", "text": "Name:"},
                    {"type": "text", "text": "Date"},
                    {"type": "text", "text": "Date"},
                ],
            }
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("t.content", "{}")
        zf.writestr("t.metadata", json.dumps({"type": "TemplateType", "visibleName": "M"}))
        zf.writestr("t.template", json.dumps(template))
    buf.seek(0)
    return buf


class TestTemplates:
    def test_template_reads_as_one_page_of_its_labels(self):
        doc = read_document_pages(template_zip())
        assert doc.kind == "template"
        assert len(doc.pages) == 1
        text = doc.pages[0].text
        assert "Meeting minutes" in text
        assert "Labels: Meetings" in text
        assert "Name: · Date" in text  # repeated labels once
        assert not doc.pages[0].has_ink

    def test_template_page_renders_its_preview(self):
        png = render_page_from_document_zip(template_zip(), 1, scale=0.2)
        assert png is not None and png.startswith(b"\x89PNG")
        assert render_page_from_document_zip(template_zip(), 2) is None


class TestTrashPaths:
    @staticmethod
    def item(id_, name, parent=""):
        return SimpleNamespace(ID=id_, VissibleName=name, Parent=parent)

    def test_trashed_items_live_under_trash(self):
        folder = self.item("f", "Old folder", "trash")
        inside = self.item("d", "Note", "f")
        top = self.item("t", "Note", "trash")
        by_id = {i.ID: i for i in (folder, inside, top)}
        assert get_item_path(folder, by_id) == "/Trash/Old folder"
        assert get_item_path(inside, by_id) == "/Trash/Old folder/Note"
        assert get_item_path(top, by_id) == "/Trash/Note"

    def test_library_items_are_unchanged(self):
        folder = self.item("f", "Work")
        doc = self.item("d", "Note", "f")
        by_id = {"f": folder, "d": doc}
        assert get_item_path(doc, by_id) == "/Work/Note"
