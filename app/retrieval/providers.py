"""Search providers. Each returns normalized records (see base.empty_record).

Free / keyless: OpenAlex, arXiv, Europe PMC, Crossref, Semantic Scholar (keyless is heavily
rate-limited; an optional free key raises the limit). Optional: Tavily web search (free tier key).
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET

import httpx

from .. import config
from .base import (ProviderError, ProviderResult, SearchQuery, empty_record, http_get, make_client,
                   normalize_doi, strip_markup)

log = logging.getLogger(__name__)

REVIEW_PAT = re.compile(r"\b(systematic review|meta-analy|literature review|scoping review|umbrella review|"
                        r"narrative review|review of the literature|state[- ]of[- ]the[- ]art review)\b|"
                        r"^(a )?(comprehensive |systematic |critical )?(review|survey) (of|on) (the )?(methods|approaches|"
                        r"techniques|literature|research|evidence|advances|progress|recent)|"
                        r"^(a )?(comprehensive |systematic |critical )?review (of|on)\b|"
                        r":\s*an?\s+(comprehensive\s+|systematic\s+|critical\s+)?(review|survey|overview)\b", re.I)


PREPRINT_SERVERS = re.compile(r"arxiv|biorxiv|medrxiv|ssrn|research square|preprints|osf|chemrxiv|psyarxiv|techrxiv|zenodo", re.I)


def _title_says_review(title: str | None) -> bool:
    return bool(title and REVIEW_PAT.search(title))


class Provider:
    name = "base"
    label = "Base"
    requires_key: str | None = None
    optional_key: str | None = None
    usage_note = ""

    def __init__(self, contact_email: str = ""):
        self.contact_email = contact_email

    def available(self) -> tuple[bool, str]:
        if self.requires_key and not config.get_secret(self.requires_key):
            return False, f"{self.requires_key} is not set"
        return True, ""

    def search(self, q: SearchQuery) -> ProviderResult:
        start = time.time()
        res = ProviderResult(provider=self.name, query=q.text)
        try:
            with make_client() as client:
                res.records = self._search(client, q)
        except ProviderError as e:
            res.error = str(e)
        except (httpx.HTTPError, ValueError, KeyError, ET.ParseError) as e:
            log.exception("Provider %s failed", self.name)
            res.error = f"{self.name}: unexpected response ({type(e).__name__})"
        for r in res.records:
            r["matched_queries"] = [q.text]
        res.elapsed = round(time.time() - start, 2)
        return res

    def _search(self, client: httpx.Client, q: SearchQuery) -> list[dict]:  # pragma: no cover
        raise NotImplementedError


# ------------------------------------------------------------------ OpenAlex
def openalex_abstract(inv: dict | None) -> str | None:
    if not inv:
        return None
    pos: dict[int, str] = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos)) or None


def parse_openalex_work(w: dict) -> dict:
    rec = empty_record("openalex")
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    rec.update(
        title=w.get("display_name") or w.get("title"),
        authors=[a["author"]["display_name"] for a in (w.get("authorships") or [])
                 if a.get("author", {}).get("display_name")][:25],
        year=w.get("publication_year"), pub_date=w.get("publication_date"),
        venue=src.get("display_name"), venue_type=src.get("type"),
        doi=normalize_doi(w.get("doi")), url=loc.get("landing_page_url") or w.get("doi") or w.get("id"),
        pdf_url=(w.get("open_access") or {}).get("oa_url"), abstract=openalex_abstract(w.get("abstract_inverted_index")),
        raw_type=w.get("type"), cited_by_count=w.get("cited_by_count"), openalex_id=w.get("id"),
        references=w.get("referenced_works") or [], is_retracted=w.get("is_retracted"),
        funders=sorted({(g.get("funder_display_name") or g.get("display_name") or "")
                        for g in (w.get("grants") or w.get("funders") or [])} - {""}),
        provider_ids={"openalex": w.get("id")},
    )
    t = (w.get("type") or "").lower()
    if t == "review" or _title_says_review(rec["title"]):
        rec["source_type"] = "review"
    elif t == "preprint" or (src.get("type") == "repository" and PREPRINT_SERVERS.search(src.get("display_name") or "")):
        rec["source_type"] = "preprint"
    elif t in ("article", "letter") and src.get("type") == "conference":
        rec["source_type"] = "conference"
    elif t in ("article", "letter"):
        rec["source_type"] = "journal-article" if src.get("type") == "journal" else "other"
    elif t in ("book", "book-chapter", "monograph"):
        rec["source_type"] = "book"
    elif t == "dataset":
        rec["source_type"] = "dataset"
    elif t in ("editorial", "erratum", "paratext", "other"):
        rec["source_type"] = "other"
    if t == "editorial":
        rec["raw_type"] = "editorial"
    return rec


class OpenAlex(Provider):
    name, label = "openalex", "OpenAlex"
    optional_key = "OPENALEX_API_KEY"
    usage_note = "Free, no key. Polite pool with contact email; daily request budget applies."
    BASE = "https://api.openalex.org"

    def _params(self, extra: dict) -> dict:
        p = dict(extra)
        if self.contact_email:
            p["mailto"] = self.contact_email
        key = config.get_secret("OPENALEX_API_KEY")
        if key:
            p["api_key"] = key
        return p

    def _search(self, client, q):
        filters = ["is_paratext:false"]
        if q.year_from:
            filters.append(f"from_publication_date:{q.year_from}-01-01")
        if q.year_to:
            filters.append(f"to_publication_date:{q.year_to}-12-31")
        r = http_get(client, f"{self.BASE}/works", self.name, params=self._params(
            {"search": q.text, "per-page": q.limit, "filter": ",".join(filters)}))
        return [parse_openalex_work(w) for w in r.json().get("results", []) if w.get("display_name")]

    def lookup_dois(self, dois: list[str]) -> dict[str, dict]:
        """Batch-resolve DOIs to OpenAlex works (used for citation tracing). Returns {doi: record}."""
        out: dict[str, dict] = {}
        dois = [d for d in dict.fromkeys(dois) if d]
        with make_client() as client:
            for i in range(0, len(dois), 40):
                chunk = dois[i:i + 40]
                r = http_get(client, f"{self.BASE}/works", self.name, params=self._params(
                    {"filter": "doi:" + "|".join(chunk), "per-page": 50}))
                for w in r.json().get("results", []):
                    rec = parse_openalex_work(w)
                    if rec["doi"]:
                        out[rec["doi"]] = rec
        return out


# ------------------------------------------------------------------ Semantic Scholar
class SemanticScholar(Provider):
    name, label = "semantic_scholar", "Semantic Scholar"
    optional_key = "SEMANTIC_SCHOLAR_API_KEY"
    usage_note = "Free. Keyless access shares a small global rate limit (429s are common); a free key helps."
    URL = "https://api.semanticscholar.org/graph/v1/paper/search"
    FIELDS = ("title,abstract,year,publicationDate,venue,authors,externalIds,url,citationCount,"
              "publicationTypes,journal,openAccessPdf")

    def _search(self, client, q):
        params = {"query": q.text, "limit": q.limit, "fields": self.FIELDS}
        if q.year_from or q.year_to:
            params["year"] = f"{q.year_from or ''}-{q.year_to or ''}"
        headers = {}
        key = config.get_secret("SEMANTIC_SCHOLAR_API_KEY")
        if key:
            headers["x-api-key"] = key
        r = http_get(client, self.URL, self.name, params=params, headers=headers)
        out = []
        for p in r.json().get("data") or []:
            if not p.get("title"):
                continue
            rec = empty_record(self.name)
            ext = p.get("externalIds") or {}
            types = [t.lower() for t in (p.get("publicationTypes") or [])]
            rec.update(title=p["title"], authors=[a.get("name") for a in p.get("authors") or [] if a.get("name")][:25],
                       year=p.get("year"), pub_date=p.get("publicationDate"),
                       venue=(p.get("journal") or {}).get("name") or p.get("venue") or None,
                       doi=normalize_doi(ext.get("DOI")), url=p.get("url"),
                       pdf_url=(p.get("openAccessPdf") or {}).get("url"), abstract=strip_markup(p.get("abstract")),
                       raw_type=",".join(types) or None, cited_by_count=p.get("citationCount"),
                       provider_ids={"semantic_scholar": p.get("paperId")})
            if "review" in types or "metaanalysis" in types or _title_says_review(rec["title"]):
                rec["source_type"] = "review"
            elif "conference" in types:
                rec["source_type"] = "conference"
            elif "journalarticle" in types and rec["venue"] and "arxiv" not in rec["venue"].lower():
                rec["source_type"] = "journal-article"
            elif ext.get("ArXiv") and not rec["doi"]:
                rec["source_type"] = "preprint"
            elif "book" in types or "booksection" in types:
                rec["source_type"] = "book"
            if ext.get("ArXiv"):
                rec["provider_ids"]["arxiv"] = ext["ArXiv"]
            out.append(rec)
        return out


# ------------------------------------------------------------------ arXiv
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


class ArXiv(Provider):
    name, label = "arxiv", "arXiv"
    usage_note = "Free, no key. Preprints (not peer reviewed). Asks for <=1 request every 3 seconds."
    URL = "https://export.arxiv.org/api/query"
    _last_call = 0.0
    _lock = threading.Lock()

    @staticmethod
    def build_query(text: str, year_from: int | None, year_to: int | None) -> str:
        words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]+", text) if len(w) > 2][:8]
        stop = {"the", "and", "for", "with", "what", "does", "how", "are", "from", "that", "this", "into", "than"}
        words = [w for w in words if w.lower() not in stop] or [text]
        q = " AND ".join(f"all:{w}" for w in words)
        if year_from or year_to:
            q += f" AND submittedDate:[{year_from or 1990}01010000 TO {year_to or 2100}12312359]"
        return q

    def _search(self, client, q):
        with ArXiv._lock:  # arXiv asks clients to space requests ~3s apart
            wait = 3.1 - (time.time() - ArXiv._last_call)
            if wait > 0:
                time.sleep(wait)
            ArXiv._last_call = time.time()
        r = http_get(client, self.URL, self.name, params={
            "search_query": self.build_query(q.text, q.year_from, q.year_to), "start": 0,
            "max_results": q.limit, "sortBy": "relevance"})
        return self.parse(r.text)

    @staticmethod
    def parse(xml_text: str) -> list[dict]:
        root = ET.fromstring(xml_text)
        out = []
        for e in root.findall("a:entry", ATOM):
            title = " ".join((e.findtext("a:title", "", ATOM) or "").split())
            if not title or title.lower() == "error":
                continue
            rec = empty_record("arxiv")
            arxiv_url = e.findtext("a:id", "", ATOM)
            published = e.findtext("a:published", "", ATOM)
            journal_ref = e.findtext("arxiv:journal_ref", None, ATOM)
            pdf = next((l.get("href") for l in e.findall("a:link", ATOM) if l.get("title") == "pdf"), None)
            rec.update(title=title, authors=[a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)][:25],
                       year=int(published[:4]) if published[:4].isdigit() else None, pub_date=published[:10] or None,
                       venue=journal_ref or "arXiv", venue_type="repository",
                       doi=normalize_doi(e.findtext("arxiv:doi", None, ATOM)), url=arxiv_url, pdf_url=pdf,
                       abstract=" ".join((e.findtext("a:summary", "", ATOM) or "").split()) or None,
                       raw_type="preprint", source_type="review" if _title_says_review(title) else "preprint",
                       provider_ids={"arxiv": arxiv_url.rsplit("/abs/", 1)[-1]})
            if journal_ref:
                rec["journal_ref"] = journal_ref
            out.append(rec)
        return out


# ------------------------------------------------------------------ Europe PMC
class EuropePMC(Provider):
    name, label = "europepmc", "Europe PMC"
    usage_note = "Free, no key. Biomedical & life sciences, includes preprints (source PPR)."
    URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def _search(self, client, q):
        query = q.text
        if q.year_from or q.year_to:
            query += f" AND PUB_YEAR:[{q.year_from or 1800} TO {q.year_to or 2100}]"
        r = http_get(client, self.URL, self.name, params={"query": query, "format": "json",
                                                           "resultType": "core", "pageSize": q.limit})
        out = []
        for p in (r.json().get("resultList") or {}).get("result", []):
            if not p.get("title"):
                continue
            rec = empty_record(self.name)
            types = [t.lower() for t in ((p.get("pubTypeList") or {}).get("pubType") or [])]
            journal = ((p.get("journalInfo") or {}).get("journal") or {}).get("title")
            doi = normalize_doi(p.get("doi"))
            src = p.get("source")
            rec.update(title=strip_markup(p["title"]), authors=[a.strip() for a in (p.get("authorString") or "").rstrip(".").split(",") if a.strip()][:25],
                       year=int(p["pubYear"]) if str(p.get("pubYear", "")).isdigit() else None,
                       pub_date=p.get("firstPublicationDate"), venue=journal or (p.get("bookOrReportDetails") or {}).get("publisher"),
                       doi=doi, url=f"https://europepmc.org/article/{src}/{p.get('id')}",
                       abstract=strip_markup(p.get("abstractText")), raw_type=",".join(types) or None,
                       cited_by_count=p.get("citedByCount"),
                       funders=sorted({g.get("agency") for g in ((p.get("grantsList") or {}).get("grant") or []) if g.get("agency")}),
                       provider_ids={"europepmc": f"{src}:{p.get('id')}"})
            if p.get("pmid"):
                rec["provider_ids"]["pmid"] = p["pmid"]
            if any("review" in t or "meta-analysis" in t for t in types) or _title_says_review(rec["title"]):
                rec["source_type"] = "review"
            elif src == "PPR" or "preprint" in types:
                rec["source_type"] = "preprint"
            elif any(t in types for t in ("editorial", "comment", "letter", "news")):
                rec["source_type"] = "other"
                rec["raw_type"] = "commentary:" + ",".join(types)
            elif journal:
                rec["source_type"] = "journal-article"
            out.append(rec)
        return out


# ------------------------------------------------------------------ Crossref
class Crossref(Provider):
    name, label = "crossref", "Crossref"
    usage_note = "Free, no key. Broad DOI metadata; abstracts often missing; relevance ranking is coarse."
    URL = "https://api.crossref.org/works"

    def _search(self, client, q):
        filters = []
        if q.year_from:
            filters.append(f"from-pub-date:{q.year_from}")
        if q.year_to:
            filters.append(f"until-pub-date:{q.year_to}")
        # No `select`: it cannot include `subtype`, which distinguishes preprints from other posted content.
        params = {"query.bibliographic": q.text, "rows": q.limit}
        if filters:
            params["filter"] = ",".join(filters)
        if self.contact_email:
            params["mailto"] = self.contact_email
        r = http_get(client, self.URL, self.name, params=params)
        out = []
        for it in (r.json().get("message") or {}).get("items", []):
            title = (it.get("title") or [None])[0]
            if not title:
                continue
            rec = empty_record(self.name)
            parts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
            year = parts[0] if parts and isinstance(parts[0], int) else None
            t = it.get("type")
            rec.update(title=strip_markup(title),
                       authors=[" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
                                for a in it.get("author") or []][:25],
                       year=year, pub_date="-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p) or None,
                       venue=(it.get("container-title") or [None])[0], doi=normalize_doi(it.get("DOI")),
                       url=it.get("URL"), abstract=strip_markup(it.get("abstract")), raw_type=t,
                       cited_by_count=it.get("is-referenced-by-count"),
                       funders=sorted({f.get("name") for f in it.get("funder") or [] if f.get("name")}),
                       provider_ids={"crossref": it.get("DOI")})
            if _title_says_review(rec["title"]):
                rec["source_type"] = "review"
            elif t == "journal-article":
                rec["source_type"] = "journal-article"
            elif t == "posted-content":
                rec["source_type"] = "preprint" if it.get("subtype") == "preprint" else "web"
                rec["raw_type"] = f"posted-content:{it.get('subtype') or 'unspecified'}"
            elif t == "proceedings-article":
                rec["source_type"] = "conference"
            elif t in ("book", "book-chapter", "monograph", "edited-book"):
                rec["source_type"] = "book"
            elif t == "dataset":
                rec["source_type"] = "dataset"
            out.append(rec)
        return out


# ------------------------------------------------------------------ Tavily (web / grey literature)
class Tavily(Provider):
    name, label = "tavily", "Tavily web search"
    requires_key = "TAVILY_API_KEY"
    usage_note = "Optional. Web pages, docs and grey literature. Free tier ~1,000 searches/month; needs TAVILY_API_KEY."
    URL = "https://api.tavily.com/search"

    def _search(self, client, q):
        key = config.get_secret("TAVILY_API_KEY")
        if not key:
            raise ProviderError("tavily: TAVILY_API_KEY is not set")
        body = {"query": q.text, "max_results": min(q.limit, 20), "search_depth": "basic", "api_key": key}
        try:
            r = client.post(self.URL, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as e:
            raise ProviderError(f"tavily: network error ({type(e).__name__})")
        if r.status_code in (401, 403):
            raise ProviderError("tavily: API key rejected")
        if r.status_code == 429:
            raise ProviderError("tavily: rate limit / monthly quota reached")
        if r.status_code != 200:
            raise ProviderError(f"tavily: HTTP {r.status_code}")
        out = []
        for it in r.json().get("results", []):
            rec = empty_record(self.name)
            pd = it.get("published_date")
            url = it.get("url") or ""
            rec.update(title=it.get("title") or url, url=url, abstract=(it.get("content") or "")[:1500] or None,
                       pub_date=pd[:10] if pd else None,
                       year=int(pd[:4]) if pd and pd[:4].isdigit() else None,
                       venue=re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", url) or None, raw_type="web",
                       source_type="documentation" if re.search(r"(docs?\.|/docs?/|documentation|readthedocs|developer\.)", url) else "web",
                       doi=normalize_doi(m.group(0)) if (m := re.search(r"10\.\d{4,9}/[^\s?#]+", url)) else None,
                       provider_ids={"tavily": url})
            if q.year_from and rec["year"] and rec["year"] < q.year_from:
                continue
            if q.year_to and rec["year"] and rec["year"] > q.year_to:
                continue
            out.append(rec)
        return out


ALL_PROVIDERS: dict[str, type[Provider]] = {p.name: p for p in (OpenAlex, SemanticScholar, ArXiv, EuropePMC, Crossref, Tavily)}


def provider_catalog() -> list[dict]:
    out = []
    for name, cls in ALL_PROVIDERS.items():
        ok, why = cls().available()
        out.append({"name": name, "label": cls.label, "available": ok, "unavailable_reason": why or None,
                    "usage_note": cls.usage_note, "requires_key": cls.requires_key, "optional_key": cls.optional_key})
    return out
