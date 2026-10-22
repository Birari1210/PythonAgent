"""User-supplied sources: URLs and uploaded documents.

URL fetching is guarded against SSRF (only public http/https hosts). DOIs and arXiv links are
resolved to scholarly metadata; other pages are stored as web sources with an extracted excerpt.
"""
from __future__ import annotations

import io
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from .base import ProviderError, empty_record, make_client, normalize_doi, strip_markup
from .providers import ArXiv, OpenAlex

log = logging.getLogger(__name__)

MAX_FETCH_BYTES = 5_000_000
MAX_UPLOAD_BYTES = 15_000_000
EXCERPT_CHARS = 4000


class ManualSourceError(Exception):
    pass


def _assert_public_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ManualSourceError("Only http(s) URLs are supported")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        raise ManualSourceError(f"Could not resolve host {p.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ManualSourceError("URLs pointing to private or local network addresses are not allowed")


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta: dict[str, str] = {}
        self.text: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "nav", "footer", "header", "noscript", "svg"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            k = (a.get("name") or a.get("property") or "").lower()
            if k and a.get("content"):
                self.meta.setdefault(k, a["content"])

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header", "noscript", "svg") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.text.append(data.strip())


def _from_doi(doi: str, url: str) -> dict | None:
    try:
        found = OpenAlex().lookup_dois([doi])
    except ProviderError:
        return None
    rec = found.get(doi)
    if rec:
        rec["url"] = rec.get("url") or url
    return rec


def _from_arxiv(arxiv_id: str) -> dict | None:
    with make_client() as c:
        r = c.get(ArXiv.URL, params={"id_list": arxiv_id})
    if r.status_code != 200:
        return None
    recs = ArXiv.parse(r.text)
    return recs[0] if recs else None


def record_from_url(url: str) -> dict:
    url = url.strip()
    _assert_public_url(url)
    doi_m = re.search(r"10\.\d{4,9}/[^\s?#\"<>]+", url)
    arx_m = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/[0-9]{7})", url)
    rec = None
    if arx_m:
        rec = _from_arxiv(arx_m.group(1))
    elif doi_m:
        rec = _from_doi(normalize_doi(doi_m.group(0).rstrip(".")), url)
    if rec:
        rec["origin"] = "manual-url"
        rec["providers"] = sorted(set(rec.get("providers", [])) | {"manual"})
        return rec

    try:
        with make_client(timeout=20) as c:
            with c.stream("GET", url) as r:
                _assert_public_url(str(r.url))  # re-check after redirects
                if r.status_code != 200:
                    raise ManualSourceError(f"Fetching the URL failed (HTTP {r.status_code})")
                ctype = r.headers.get("content-type", "")
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_FETCH_BYTES:
                        raise ManualSourceError("Page is too large to import (>5 MB)")
    except httpx.HTTPError as e:
        raise ManualSourceError(f"Could not fetch URL: {type(e).__name__}")

    if "pdf" in ctype or url.lower().endswith(".pdf"):
        rec = record_from_document(url.rsplit("/", 1)[-1] or "document.pdf", body)
        rec["url"] = url
        rec["origin"] = "manual-url"
        return rec

    parser = _PageParser()
    parser.feed(body.decode(r.encoding or "utf-8", errors="replace"))
    text = " ".join(parser.text)
    rec = empty_record("manual")
    meta = parser.meta
    meta_doi = normalize_doi(meta.get("citation_doi") or meta.get("dc.identifier"))
    if meta_doi:
        resolved = _from_doi(meta_doi, url)
        if resolved:
            resolved["origin"] = "manual-url"
            resolved["providers"] = sorted(set(resolved.get("providers", [])) | {"manual"})
            return resolved
    date = meta.get("citation_publication_date") or meta.get("article:published_time") or meta.get("date") or ""
    m = re.search(r"(19|20)\d{2}", date)
    rec.update(
        title=(meta.get("citation_title") or meta.get("og:title") or parser.title or url).strip()[:400],
        authors=[a.strip() for a in (meta.get("citation_author") or meta.get("author") or "").split(";") if a.strip()],
        year=int(m.group(0)) if m else None, pub_date=date[:10] or None,
        venue=meta.get("citation_journal_title") or meta.get("og:site_name") or urlparse(url).hostname,
        url=url, abstract=strip_markup(meta.get("citation_abstract") or meta.get("description") or meta.get("og:description")),
        raw_type="web", source_type="documentation" if re.search(r"docs?\.|/docs?/|documentation|readthedocs", url) else "web",
        origin="manual-url", content_excerpt=text[:EXCERPT_CHARS] or None,
    )
    if meta.get("citation_journal_title"):
        rec["source_type"] = "journal-article"
    return rec


def record_from_document(filename: str, data: bytes) -> dict:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ManualSourceError("File is too large (max 15 MB)")
    name = filename.lower()
    rec = empty_record("manual")
    rec.update(origin="manual-upload", raw_type="uploaded-document", source_type="other", file_name=filename)
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = [(p.extract_text() or "") for p in reader.pages[:15]]
            info = reader.metadata or {}
            title = (info.get("/Title") or "").strip() if info else ""
            author = (info.get("/Author") or "").strip() if info else ""
        except Exception as e:  # pypdf raises many types on malformed files
            raise ManualSourceError(f"Could not read PDF: {type(e).__name__}")
        text = re.sub(r"\s+", " ", " ".join(pages)).strip()
        if not text:
            raise ManualSourceError("No extractable text in this PDF (it may be scanned images)")
        rec.update(title=title or filename, authors=[author] if author else [])
    elif name.endswith((".txt", ".md", ".markdown", ".html", ".htm")):
        text = data.decode("utf-8", errors="replace")
        if name.endswith((".html", ".htm")):
            text = strip_markup(text) or ""
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), filename)
        rec["title"] = first[:300] or filename
        text = re.sub(r"\s+", " ", text).strip()
    else:
        raise ManualSourceError("Unsupported file type. Use PDF, TXT, MD or HTML.")
    doi_m = re.search(r"\b10\.\d{4,9}/[^\s\"<>]+", text[:5000])
    if doi_m:
        rec["doi"] = normalize_doi(doi_m.group(0).rstrip(".,;)"))
    abs_m = re.search(r"\babstract\b[:.\s]*(.{200,2000}?)(?:\b(?:keywords|introduction|1\.\s)\b)", text[:8000], re.I)
    rec["abstract"] = (abs_m.group(1).strip() if abs_m else text[:1500]) or None
    rec["content_excerpt"] = text[:EXCERPT_CHARS]
    yr = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", text[:3000])
    rec["year"] = int(yr.group(0)) if yr else None
    rec["year_note"] = "Year guessed from document text" if yr else None
    return rec
