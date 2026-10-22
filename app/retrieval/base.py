"""Shared types and helpers for search providers."""
from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "ResearchSynthesist/1.0 (local research tool; https://github.com/)"


class ProviderError(Exception):
    """A provider failed in a way the user should see (rate limit, outage, bad config)."""


@dataclass
class SearchQuery:
    text: str
    year_from: int | None = None
    year_to: int | None = None
    limit: int = 10


@dataclass
class ProviderResult:
    provider: str
    query: str
    records: list[dict] = field(default_factory=list)
    error: str | None = None
    elapsed: float = 0.0


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi)
    return doi if doi.startswith("10.") else None


_TAG = re.compile(r"<[^>]+>")


def strip_markup(text: str | None) -> str | None:
    if not text:
        return None
    text = html.unescape(_TAG.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(abstract|summary)[:.\s]+", "", text, flags=re.I)
    return text or None


def empty_record(provider: str) -> dict:
    return {
        "title": None, "authors": [], "year": None, "pub_date": None, "venue": None, "venue_type": None,
        "doi": None, "url": None, "pdf_url": None, "abstract": None, "raw_type": None, "source_type": "other",
        "cited_by_count": None, "openalex_id": None, "references": [], "is_retracted": None, "funders": [],
        "providers": [provider], "provider_ids": {}, "retrieved_at": now_iso(), "matched_queries": [],
        "origin": "search",
    }


def http_get(client: httpx.Client, url: str, provider: str, *, params=None, headers=None,
             retries: int = 2) -> httpx.Response:
    """GET with polite retry on 429/5xx. Raises ProviderError with a readable message."""
    last = ""
    for attempt in range(retries + 1):
        try:
            r = client.get(url, params=params, headers=headers)
        except httpx.TimeoutException:
            last = "request timed out"
        except httpx.HTTPError as e:
            last = f"network error ({type(e).__name__})"
        else:
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                last = "rate limited (429)" if r.status_code == 429 else f"server error ({r.status_code})"
                ra = r.headers.get("retry-after", "")
                wait = float(ra) if ra.replace(".", "", 1).isdigit() else 1.5 * (attempt + 1)
                if attempt < retries:
                    time.sleep(min(wait, 8))
                    continue
            else:
                raise ProviderError(f"{provider}: HTTP {r.status_code}")
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    raise ProviderError(f"{provider}: {last}")


def make_client(timeout: float = 20) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
