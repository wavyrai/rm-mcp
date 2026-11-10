"""
What remarkable_send can put on the tablet: a local PDF/EPUB/Markdown file, an
http(s) URL to a PDF or EPUB, or Markdown text.

Downloads are capped at 100 MB, must declare a PDF/EPUB content type (or a
generic binary one) and must start like one. Only public addresses are
fetched: a URL whose host resolves to a loopback, private, link-local or
otherwise reserved address is refused, and each redirect is checked again.
"""

import ipaddress
import os
import re
import socket
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from mcp.server.mcpserver.exceptions import ToolError

MAX_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 5
_PDF_TYPES = {"application/pdf", "application/x-pdf"}
_EPUB_TYPES = {"application/epub+zip"}
_GENERIC_TYPES = {"application/octet-stream", "binary/octet-stream", "application/download"}


@dataclass
class Source:
    data: bytes
    kind: str  # pdf or epub
    title: str
    origin: str  # file, url or markdown
    page_count: int


def sniff(data: bytes) -> Optional[str]:
    """pdf, epub or None, from the bytes themselves."""
    if data[:1024].lstrip().startswith(b"%PDF-"):
        return "pdf"
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(BytesIO(data)) as zf:
                if zf.read("mimetype").strip() == b"application/epub+zip":
                    return "epub"
        except (KeyError, zipfile.BadZipFile):
            return None
    return None


def page_count(data: bytes, kind: str) -> int:
    from rm_mcp.extract.render import open_source_document

    try:
        doc = open_source_document(data, kind)
    except Exception as e:
        raise ToolError(f"The {kind.upper()} could not be opened ({e}); nothing was sent.")
    try:
        return doc.page_count
    finally:
        doc.close()


def _title_from_name(name: str) -> str:
    stem = Path(unquote(name)).stem.strip()
    return re.sub(r"\s+", " ", stem) or "Untitled"


def _markdown_title(text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            return match.group(1).replace("/", "-")[:120]
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return (first[:60].replace("/", "-") or "Note").strip()


def _looks_like_path(source: str) -> bool:
    if "\n" in source:
        return False
    s = source.strip()
    return s.startswith(("/", "~", "./", "../")) or bool(
        re.search(r"\.(pdf|epub|md|markdown)$", s, re.IGNORECASE)
    )


def _from_markdown(text: str, title: Optional[str]) -> Source:
    from rm_mcp.extract.typeset import markdown_to_pdf

    title = title or _markdown_title(text)
    try:
        data = markdown_to_pdf(text, title)
    except ValueError as e:
        raise ToolError(f"{e} Nothing was sent.")
    return Source(data, "pdf", title, "markdown", page_count(data, "pdf"))


def _from_file(path: Path, title: Optional[str]) -> Source:
    if not path.is_file():
        raise ToolError(f"No file at '{path}'. Nothing was sent.")
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ToolError(f"'{path}' is {size // (1024 * 1024)} MB; the limit is 100 MB.")
    if path.suffix.lower() in (".md", ".markdown"):
        return _from_markdown(
            path.read_text(encoding="utf-8"), title or _title_from_name(path.name)
        )
    data = path.read_bytes()
    kind = sniff(data)
    if kind is None:
        raise ToolError(
            f"'{path}' is not a PDF or EPUB (checked its contents). Nothing was sent. "
            "Markdown files need a .md extension."
        )
    return Source(data, kind, title or _title_from_name(path.name), "file", page_count(data, kind))


def _check_public(host: str) -> None:
    """Refuse hosts that resolve to non-public addresses."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ToolError(f"Could not resolve '{host}': {e}. Nothing was sent.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ToolError(
                f"Refused to fetch from '{host}': it resolves to a non-public address "
                f"({address}). Download it yourself and pass the file path."
            )


def _http_get(url: str) -> requests.Response:
    """One GET without following redirects (tests replace this)."""
    return requests.get(
        url,
        stream=True,
        timeout=30,
        allow_redirects=False,
        headers={"User-Agent": "rm-mcp", "Accept": "application/pdf, application/epub+zip"},
    )


def _download(url: str) -> Tuple[bytes, str, str]:
    """(bytes, content type, final url)."""
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ToolError(f"Only http(s) URLs can be fetched, not '{url}'.")
        _check_public(parsed.hostname)
        response = _http_get(url)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ToolError(f"{url} redirected without a location.")
                url = urljoin(url, location)
                continue
            if response.status_code >= 400:
                raise ToolError(f"Fetching {url} failed with HTTP {response.status_code}.")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                raise ToolError(f"{url} is larger than the 100 MB limit. Nothing was sent.")
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=1 << 16):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ToolError(f"{url} is larger than the 100 MB limit. Nothing was sent.")
                chunks.append(chunk)
            ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
            return b"".join(chunks), ctype, url
        finally:
            response.close()
    raise ToolError(f"{url} redirected more than {MAX_REDIRECTS} times.")


def _from_url(url: str, title: Optional[str]) -> Source:
    data, ctype, final = _download(url)
    if ctype not in _PDF_TYPES | _EPUB_TYPES | _GENERIC_TYPES:
        raise ToolError(
            f"{final} is '{ctype or 'no content type'}', not a PDF or EPUB. Nothing was sent. "
            "To send a web page, pass its text as Markdown."
        )
    kind = sniff(data)
    expected = "pdf" if ctype in _PDF_TYPES else "epub" if ctype in _EPUB_TYPES else kind
    if kind is None or kind != expected:
        raise ToolError(f"{final} does not contain a valid {expected or 'PDF/EPUB'}.")
    name = os.path.basename(urlparse(final).path) or "Download"
    return Source(data, kind, title or _title_from_name(name), "url", page_count(data, kind))


def load(source: str, title: Optional[str] = None) -> Source:
    """Resolve `source` into document bytes. Raises ToolError with the cause."""
    if not source or not source.strip():
        raise ToolError("source is empty: pass a file path, an http(s) URL or Markdown text.")
    title = title.strip() if title and title.strip() else None
    stripped = source.strip()
    if re.match(r"^https?://\S+$", stripped):
        return _from_url(stripped, title)
    if _looks_like_path(stripped):
        return _from_file(Path(stripped).expanduser(), title)
    return _from_markdown(source, title)
