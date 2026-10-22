"""Step 3 — evaluate each source: evidence tier, peer-review status, method quality, independence, weight.

Tier, peer-review status and weight are assigned by explicit, deterministic rules so every grade is explainable.
The LLM only *extracts* what an abstract states (design, sample size, findings, limitations, conflicts); those
extractions are labelled as model-extracted and sample sizes are checked against the abstract text.
"""
from __future__ import annotations

import logging
import re

from ..llm import GroqClient, LLMError
from ..models import ExtractionOut
from ..retrieval.base import ProviderError
from ..retrieval.providers import OpenAlex

log = logging.getLogger(__name__)

WEIGHTS = ["None", "Low", "Moderate", "High"]

STRONG_DESIGNS = re.compile(r"randomi[sz]ed|\brct\b|controlled trial|meta-analy|systematic review|pre-?registered|"
                            r"cohort|benchmark(ed)? across|large-scale evaluation", re.I)
WEAK_DESIGNS = re.compile(r"case report|case study|anecdot|opinion|editorial|commentary|position paper|pilot|"
                          r"qualitative interview|single[- ]site|proof[- ]of[- ]concept|perspective", re.I)
NO_DATA_DESIGNS = re.compile(r"opinion|editorial|commentary|perspective|position paper|news", re.I)


def evidence_tier(rec: dict) -> str:
    t = rec.get("source_type")
    raw = (rec.get("raw_type") or "").lower()
    if "commentary" in raw or raw == "editorial":
        return "tertiary"
    if t == "review" or t == "book":
        return "secondary"
    if t in ("journal-article", "conference", "preprint", "dataset"):
        return "primary"
    if t == "documentation":
        return "documentation"
    if t == "web":
        return "tertiary"
    return "unclassified"


def peer_review_status(rec: dict) -> str:
    t = rec.get("source_type")
    if t == "preprint":
        return "Preprint — not peer reviewed" + (f" (journal ref reported: {rec['journal_ref']})" if rec.get("journal_ref") else "")
    if t in ("journal-article", "review") and rec.get("venue") and rec.get("origin") != "manual-upload":
        return "Published in a journal per indexing metadata (peer review not independently verified)"
    if t == "conference":
        return "Conference paper — review process varies by venue (not verified)"
    if t == "documentation":
        return "Technical documentation — not peer reviewed"
    if t == "web":
        return "Web page — not peer reviewed"
    if t == "book":
        return "Book / chapter — editorial review status unknown"
    return "Unknown — not verifiable from available metadata"


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*", text or "")}


def verify_sample_size(sample_size: str | None, source_text: str | None) -> tuple[str | None, bool, str | None]:
    """Keep a model-extracted sample size only if its number appears in the source text."""
    if not sample_size or sample_size.strip().lower() in ("", "null", "none", "n/a", "not reported", "unknown"):
        return None, False, None
    nums = _numbers(sample_size)
    if nums and nums & _numbers(source_text or ""):
        return sample_size, True, None
    return None, False, f"Model reported sample size '{sample_size}' but it could not be found in the abstract; discarded"


def method_quality(tier: str, design: str | None, sample_size: str | None, rec: dict) -> tuple[str, list[str]]:
    notes = []
    if tier in ("tertiary",) or (design and NO_DATA_DESIGNS.search(design)):
        return "N/A (no new data)", ["Commentary / web content without original data"]
    if tier == "documentation":
        return "N/A (documentation)", ["Authoritative for what the system is specified to do, not for measured outcomes"]
    if not design:
        return "Not assessable", ["Study design not stated in the available abstract/metadata"]
    score = 1
    if STRONG_DESIGNS.search(design):
        score += 1
        notes.append(f"Design: {design}")
    elif WEAK_DESIGNS.search(design):
        score -= 1
        notes.append(f"Weaker design: {design}")
    else:
        notes.append(f"Design: {design}")
    n = max((int(x) for x in _numbers(sample_size or "") if x.isdigit() and len(x) < 9), default=None)
    if n is not None:
        if n >= 1000:
            score += 1
            notes.append(f"Large sample (n≈{n})")
        elif n < 50:
            score -= 1
            notes.append(f"Small sample (n≈{n})")
        else:
            notes.append(f"Sample n≈{n}")
    if rec.get("source_type") == "preprint":
        notes.append("Not peer reviewed")
    return ["Weak", "Weak", "Moderate", "Strong", "Strong"][max(0, min(score + 1, 4))], notes


def compute_weight(ev: dict, rec: dict) -> tuple[str, list[str]]:
    """Deterministic weight in synthesis, with the reasons that produced it."""
    basis = []
    tier, mq = ev["tier"], ev["method_quality"]
    if ev.get("relevance") == "irrelevant":
        return "None", ["Judged off-topic for the question"]
    if tier == "tertiary":
        return "None" if ev.get("cites_in_set") else "Low", ["Tertiary source: no original data"] + (
            ["Repeats sources already in the set"] if ev.get("cites_in_set") else [])
    level = {"Strong": 3, "Moderate": 2, "Weak": 1}.get(mq, 1)
    basis.append(f"Method quality: {mq}")
    if tier == "secondary":
        level = max(level, 2) if rec.get("source_type") == "review" and ev.get("design") and \
            re.search(r"systematic|meta-analy", ev["design"], re.I) else min(level, 2)
        basis.append("Secondary source (review) — weight reflects synthesis quality, not independent data")
    if rec.get("source_type") == "preprint":
        level = min(level, 2)
        basis.append("Capped at Moderate: preprint")
    if tier == "documentation":
        level = 2
    if not rec.get("abstract") and not rec.get("content_excerpt"):
        level = min(level, 1)
        basis.append("Capped at Low: no abstract or text available to assess")
    if ev.get("relevance") == "low":
        level = min(level, 1)
        basis.append("Capped at Low: only tangentially relevant")
    if ev.get("conflicts"):
        basis.append(f"Disclosed conflict/funding noted: {ev['conflicts'][:120]}")
    return WEIGHTS[max(1, min(level, 3))], basis


EXTRACT_SYSTEM = """You extract study information from abstracts for an evidence review. For each source, using ONLY
the text provided (never outside knowledge):
- study_design: the design as stated or clearly implied (e.g. "randomized controlled trial", "systematic review and
  meta-analysis", "retrospective cohort", "benchmark evaluation", "survey", "opinion"); null if not determinable.
- sample_size: exactly as stated in the text (e.g. "n=1,204 patients", "12 datasets"); null if not stated.
- key_findings: up to 3 short factual statements of what the source reports, phrased as the source states them.
- limitations: limitations stated in the text (empty if none stated). Do not invent limitations.
- conflicts_or_funding: conflicts of interest or funding stated in the text, else null.
- relevance to the research question: high / medium / low / irrelevant, with a one-line reason.
Return {"items": [...]} with one item per source_id given."""


def _source_text(rec: dict, limit: int = 1100) -> str:
    return ((rec.get("abstract") or rec.get("content_excerpt") or "")[:limit]).strip()


def extract_with_llm(client: GroqClient, question: str, records: list[dict], batch_size: int = 5,
                     progress=None) -> tuple[dict[str, dict], list[str]]:
    """Returns ({label: extraction}, [error messages])."""
    results: dict[str, dict] = {}
    errors: list[str] = []
    todo = [r for r in records if _source_text(r)]
    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        payload = "\n\n".join(f"[source_id: {r['label']}]\nTitle: {r.get('title')}\nYear: {r.get('year')}\n"
                              f"Text: {_source_text(r)}" for r in batch)
        try:
            out = client.structured(EXTRACT_SYSTEM, f"Research question: {question}\n\n{payload}", ExtractionOut,
                                    max_tokens=1800)
        except LLMError as e:
            errors.append(f"Extraction for {', '.join(r['label'] for r in batch)} failed: {e}")
            continue
        valid = {r["label"] for r in batch}
        for item in out.items:
            if item.source_id in valid:
                results[item.source_id] = item.model_dump()
        if progress:
            progress(min(i + batch_size, len(todo)), len(todo))
    return results, errors


def trace_citations(records: list[dict]) -> tuple[int, list[str]]:
    """Resolve OpenAlex reference lists for records with DOIs but no references. Returns (#resolved, errors)."""
    need = [r for r in records if r.get("doi") and not r.get("references")]
    if not need:
        return 0, []
    try:
        found = OpenAlex().lookup_dois([r["doi"] for r in need])
    except ProviderError as e:
        return 0, [f"Citation tracing via OpenAlex failed: {e}"]
    n = 0
    for r in need:
        w = found.get(r["doi"])
        if w:
            r["references"] = w.get("references") or []
            r["openalex_id"] = r.get("openalex_id") or w.get("openalex_id")
            r.setdefault("provider_ids", {})["openalex"] = w.get("openalex_id")
            if r.get("is_retracted") is None:
                r["is_retracted"] = w.get("is_retracted")
            n += 1
    return n, []


def citation_edges(records: list[dict]) -> list[tuple[str, str]]:
    """(citing label, cited label) pairs among the records, from OpenAlex reference lists."""
    by_oa = {r["openalex_id"]: r["label"] for r in records if r.get("openalex_id")}
    edges = []
    for r in records:
        for ref in r.get("references") or []:
            if ref in by_oa and by_oa[ref] != r["label"]:
                edges.append((r["label"], by_oa[ref]))
    return edges


def evaluate(records: list[dict], extractions: dict[str, dict]) -> None:
    """Attach an `evaluation` dict to every record (in place)."""
    edges = citation_edges(records)
    for rec in records:
        ex = extractions.get(rec["label"], {})
        tier = evidence_tier(rec)
        text = _source_text(rec, 5000)
        sample, verified, sample_note = verify_sample_size(ex.get("sample_size"), text)
        design = ex.get("study_design")
        mq, mq_notes = method_quality(tier, design, sample, rec)
        cites = sorted({b for a, b in edges if a == rec["label"]}, key=lambda s: int(s[1:]))
        cited_by = sorted({a for a, b in edges if b == rec["label"]}, key=lambda s: int(s[1:]))
        if tier in ("secondary", "tertiary") and cites:
            independence = f"Not independent of {', '.join(cites)} — it reviews/repeats them (citation trace)"
            independent = False
        elif cites:
            independence = f"Own data per source type; cites {', '.join(cites)} (shared background, not shared data)"
            independent = True
        else:
            independence = "No citation links to other included sources found" + (
                "" if rec.get("references") else " (reference list unavailable)")
            independent = True
        ev = {
            "tier": tier, "peer_review": peer_review_status(rec),
            "design": design, "sample_size": sample, "sample_size_verified": verified,
            "method_quality": mq, "method_notes": mq_notes,
            "key_findings": ex.get("key_findings") or [], "limitations": ex.get("limitations") or [],
            "conflicts": ex.get("conflicts_or_funding"), "funders": rec.get("funders") or [],
            "relevance": ex.get("relevance") or ("unknown" if not ex else "medium"),
            "relevance_reason": ex.get("relevance_reason") or "",
            "cites_in_set": cites, "cited_by_in_set": cited_by,
            "independence": independence, "independent": independent,
            "extraction": "model-extracted from abstract" if ex else (
                "not extracted (no abstract)" if not text else "not extracted (LLM unavailable)"),
            "notes": [n for n in [sample_note, rec.get("year_note")] if n],
        }
        ev["weight"], ev["weight_basis"] = compute_weight(ev, rec)
        rec["evaluation"] = ev
