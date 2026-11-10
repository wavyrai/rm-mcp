"""Daily read-only canary against the real reMarkable Cloud.

Runs status, find, read one page and view one page through the real tools,
then checks that the root index hashes to the root hash, parses, and
re-serialises byte for byte (what every write relies on), and that a sample
of document indexes verify too.

It cannot write: REMARKABLE_READ_ONLY=1 is forced, and every HTTP request
passes through a guard at the transport layer that refuses anything but
GET/HEAD and the token-renewal POST. The run fails if a write was attempted.

Output carries counts and status codes only, never document names, ids or
text, because Actions logs of a public repository are public. Set
CANARY_VERBOSE=1 on a private run to see full error messages.

Usage: REMARKABLE_TOKEN=... uv run python scripts/canary.py
"""

import asyncio
import base64
import os
import re
import sys
import tempfile
import traceback
from typing import Any, Dict, List

import requests.adapters

ALLOWED_POSTS = ("/token/json/2/user/new",)
SAMPLE_DOCUMENTS = 5


class WriteAttempt(RuntimeError):
    """A request that could change the library reached the transport."""


class RequestGuard:
    """Wraps HTTPAdapter.send: GET/HEAD pass, the token renewal POST passes, all else raises."""

    def __init__(self) -> None:
        self.methods: Dict[str, int] = {}
        self.refused: List[str] = []
        self._original = None

    def check(self, method: str, url: str) -> None:
        method = method.upper()
        self.methods[method] = self.methods.get(method, 0) + 1
        if method in ("GET", "HEAD"):
            return
        if method == "POST" and any(url.split("?")[0].endswith(p) for p in ALLOWED_POSTS):
            return
        self.refused.append(method)
        raise WriteAttempt(f"canary refused a {method} request")

    def install(self) -> "RequestGuard":
        original = requests.adapters.HTTPAdapter.send
        guard = self

        def send(adapter, request, *args, **kwargs):
            guard.check(request.method or "", request.url or "")
            return original(adapter, request, *args, **kwargs)

        self._original = original
        requests.adapters.HTTPAdapter.send = send
        return self

    def uninstall(self) -> None:
        if self._original is not None:
            requests.adapters.HTTPAdapter.send = self._original


def redact(e: BaseException) -> str:
    if os.environ.get("CANARY_VERBOSE") == "1":
        return f"{type(e).__name__}: {e}"
    codes = sorted(set(re.findall(r"\b[45]\d\d\b", str(e))))
    return type(e).__name__ + (f" (HTTP {', '.join(codes)})" if codes else "")


async def call(tool: str, **arguments) -> Any:
    from rm_mcp.server import mcp

    return await mcp.call_tool(tool, arguments)


def structured(result: Any) -> Dict[str, Any]:
    import json

    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


async def probe(report: Dict[str, Any]) -> None:
    status = structured(await call("remarkable_status"))
    report["status.authenticated"] = status["authenticated"]
    if not status["authenticated"]:
        raise RuntimeError("status: not authenticated: " + str(status.get("error")))
    report["status.documents"] = status.get("document_count")

    found = structured(await call("remarkable_find", limit=50))
    results = found.get("results", [])
    report["find.results"] = len(results)
    if not results:
        raise RuntimeError("find returned no documents")

    def pages(r):
        return r.get("page_count") or r.get("pages") or 0

    target = next((r for r in results if pages(r) >= 1), results[0])
    doc_id = target["id"]
    report["target.type"] = target.get("type")

    read = structured(await call("remarkable_read", id=doc_id, pages="1"))
    report["read.pages"] = len(read.get("pages", []))

    view = await call("remarkable_view", id=doc_id, pages="1")
    images = [b for b in view.content if getattr(b, "type", "") == "image"]
    if not images:
        raise RuntimeError("view returned no image")
    png = base64.b64decode(images[0].data)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("view did not return a PNG")
    report["view.png_bytes"] = len(png)


def check_root_index(report: Dict[str, Any]) -> None:
    from rm_mcp.api import get_rmapi
    from rm_mcp.clients.cloud import blob_hash, parse_index, serialize_index

    client = get_rmapi()
    client._blob_store_arg = None  # always fetch from the cloud here
    root_hash = client.get_root_hash()
    body = client._get_file(root_hash, "root.docSchema")  # raises on a hash mismatch
    assert blob_hash(body) == root_hash
    entries = parse_index(body)
    report["root.entries"] = len(entries)
    if serialize_index(entries, ".") != body:
        raise RuntimeError("root index does not re-serialise byte for byte")
    for entry in entries[:SAMPLE_DOCUMENTS]:
        doc_body = client._get_file(entry["hash"], f"{entry['id']}.docSchema")
        files = parse_index(doc_body)
        if serialize_index(files, entry["id"]) != doc_body:
            raise RuntimeError("a document index does not re-serialise byte for byte")
        if len(files) != int(entry["subfiles"]) or sum(int(f["size"]) for f in files) != int(
            entry["size"]
        ):
            raise RuntimeError("a root entry's subfiles/size disagree with its index")
    report["root.documents_verified"] = min(SAMPLE_DOCUMENTS, len(entries))


def main() -> int:
    if not os.environ.get("REMARKABLE_TOKEN", "").strip():
        print("REMARKABLE_TOKEN is not set; nothing to probe.")
        return 2
    work = tempfile.mkdtemp(prefix="rm-mcp-canary-")
    os.environ["REMARKABLE_READ_ONLY"] = "1"
    os.environ["REMARKABLE_INDEX_CONTENT"] = "0"
    os.environ["REMARKABLE_INDEX_PATH"] = os.path.join(work, "index.db")
    os.environ["REMARKABLE_BLOB_CACHE_DIR"] = os.path.join(work, "blobs")
    # Our token file must not exist here, so REMARKABLE_TOKEN is what is used.
    os.environ["XDG_CONFIG_HOME"] = os.path.join(work, "config")
    os.environ["APPDATA"] = os.path.join(work, "config")

    guard = RequestGuard().install()
    report: Dict[str, Any] = {}
    failed = None
    try:
        from rm_mcp import index

        index.initialize()
        asyncio.run(probe(report))
        check_root_index(report)
    except Exception as e:
        failed = e
    finally:
        guard.uninstall()

    for key, value in report.items():
        print(f"{key}: {value}")
    print("requests:", ", ".join(f"{m}={n}" for m, n in sorted(guard.methods.items())))
    if guard.refused:
        print(f"FAIL: refused write requests: {guard.refused}")
        return 1
    if failed is not None:
        print("FAIL:", redact(failed))
        if os.environ.get("CANARY_VERBOSE") == "1":
            traceback.print_exception(failed)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
