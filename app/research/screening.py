"""Step 2b — de-duplicate retrieved records and apply inclusion/exclusion criteria consistently."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

TITLE_SIM_THRESHOLD = 0.93
# Higher number = preferred as the canonical version of a work.
TYPE_RANK = {"journal-article": 5, "review": 5, "conference": 4, "book": 3, "preprint": 2, "dataset": 2,
             "documentation": 1, "web": 1, "other": 0}


def norm_title(t: str | None) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def same_work(a: dict, b: dict) -> str | None:
    """Return a reason string if a and b are the same work (or versions of it)."""
    if a.get("doi") and b.get("doi") and a["doi"] == b["doi"]:
        return "same DOI"
    ida, idb = a.get("provider_ids", {}), b.get("provider_ids", {})
    if ida.get("arxiv") and idb.get("arxiv") and ida["arxiv"].split("v")[0] == idb["arxiv"].split("v")[0]:
        return "same arXiv id"
    if a.get("openalex_id") and a.get("openalex_id") == b.get("openalex_id"):
        return "same OpenAlex id"
    # Different DOIs mean different works, except a preprint and its published version.
    if a.get("doi") and b.get("doi") and a.get("source_type") == b.get("source_type"):
        return None
    ta, tb = norm_title(a.get("title")), norm_title(b.get("title"))
    if len(ta) < 15 or len(tb) < 15:
        return None
    if re.findall(r"\d+", ta) != re.findall(r"\d+", tb):  # "Part 1" vs "Part 2", different years/cohorts
        return None
    if a.get("year") and b.get("year") and abs(a["year"] - b["year"]) > 2:
        return None
    if ta == tb or SequenceMatcher(None, ta, tb).ratio() >= TITLE_SIM_THRESHOLD:
        if {a.get("source_type"), b.get("source_type")} >= {"preprint"} and a.get("source_type") != b.get("source_type"):
            return "preprint and published version of the same work"
        return "near-identical title"
    return None


def merge_into(keep: dict, other: dict) -> None:
    """Fill gaps in `keep` with metadata from `other` and record provenance."""
    keep["providers"] = sorted(set(keep.get("providers", [])) | set(other.get("providers", [])))
    keep["matched_queries"] = list(dict.fromkeys(keep.get("matched_queries", []) + other.get("matched_queries", [])))
    keep.setdefault("provider_ids", {}).update({k: v for k, v in other.get("provider_ids", {}).items()
                                                 if k not in keep["provider_ids"]})
    for f in ("doi", "abstract", "venue", "pub_date", "year", "pdf_url", "openalex_id", "is_retracted", "url"):
        if not keep.get(f) and other.get(f):
            keep[f] = other[f]
    if not keep.get("references") and other.get("references"):
        keep["references"] = other["references"]
    if len(other.get("abstract") or "") > len(keep.get("abstract") or "") * 1.5:
        keep["abstract"] = other["abstract"]
    keep["funders"] = sorted(set(keep.get("funders") or []) | set(other.get("funders") or []))
    if other.get("cited_by_count") and (keep.get("cited_by_count") or 0) < other["cited_by_count"]:
        keep["cited_by_count"] = other["cited_by_count"]
    if keep.get("source_type") != "preprint" and other.get("source_type") == "preprint":
        keep.setdefault("versions", []).append({"type": "preprint", "url": other.get("url"), "venue": other.get("venue")})


def dedupe(records: list[dict]) -> tuple[list[dict], list[tuple[dict, int, str]]]:
    """Returns (unique records, [(duplicate record, index of kept record, reason)])."""
    unique: list[dict] = []
    dups: list[tuple[dict, int, str]] = []
    for rec in records:
        match = None
        for i, u in enumerate(unique):
            reason = same_work(u, rec)
            if reason:
                match = (i, reason)
                break
        if not match:
            unique.append(rec)
            continue
        i, reason = match
        kept = unique[i]
        if set(rec.get("providers", [])) <= set(kept.get("providers", [])) and "preprint" not in reason:
            # Same provider returned the same work for another query: merge silently, it is not a new record.
            merge_into(kept, rec)
            continue
        if TYPE_RANK.get(rec.get("source_type"), 0) > TYPE_RANK.get(kept.get("source_type"), 0):
            merge_into(rec, kept)
            unique[i] = rec
            dups.append((kept, i, reason))
        else:
            merge_into(kept, rec)
            dups.append((rec, i, reason))
    return unique, dups


def _contains(text: str, term: str) -> bool:
    return re.search(r"\b" + re.escape(term.lower()) + r"\b", text) is not None


def relevance_score(rec: dict, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    title = norm_title(rec.get("title"))
    abstract = norm_title(rec.get("abstract") or rec.get("content_excerpt"))
    score = 0.0
    for k in keywords:
        k = norm_title(k)
        if not k:
            continue
        if _contains(title, k):
            score += 2
        if _contains(abstract, k):
            score += 1
    score = score / (3 * len(keywords))
    score += 0.05 * min(len(rec.get("matched_queries") or []), 4)  # found by several phrasings
    if not rec.get("abstract"):
        score -= 0.1
    return round(score, 3)


def screen(rec: dict, criteria: dict) -> tuple[bool, str]:
    """Apply criteria. Returns (included, reason). The reason is recorded for auditability."""
    yf, yt = criteria.get("year_from"), criteria.get("year_to")
    year = rec.get("year")
    if year and yf and year < yf:
        return False, f"Published {year}, before date range ({yf})"
    if year and yt and year > yt:
        return False, f"Published {year}, after date range ({yt})"
    if rec.get("is_retracted"):
        return False, "Marked as retracted in OpenAlex metadata"
    types = criteria.get("source_types") or []
    if types and rec.get("source_type") not in types:
        return False, f"Source type '{rec.get('source_type')}' not in selected types"
    text = " ".join(filter(None, [norm_title(rec.get("title")), norm_title(rec.get("abstract")),
                                  norm_title(rec.get("content_excerpt"))]))
    for term in criteria.get("exclude_terms") or []:
        if _contains(text, norm_title(term)):
            return False, f"Matches exclusion term '{term}'"
    inc = criteria.get("include_terms") or []
    if inc and not any(_contains(text, norm_title(t)) for t in inc):
        return False, "Does not mention any required inclusion term"
    if not rec.get("abstract") and not rec.get("content_excerpt") and not rec.get("title"):
        return False, "No title or abstract to evaluate"
    note = "Meets criteria" + ("" if year else " (publication year unknown)")
    return True, note
