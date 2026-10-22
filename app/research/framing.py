"""Step 1 — frame the question: subject / comparison / outcome, keywords and search queries."""
from __future__ import annotations

import re

from ..llm import GroqClient
from ..models import FramingOut

STOPWORDS = set("""a about above after again against all am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having how i if in
into is it its itself just me more most my no nor not now of off on once only or other our out over own same should so
some such than that the their them then there these they this those through to too under until up very was we were what
when where which while who whom why will with would you your there's what's vs versus compared compare effect effects
impact role use using based study studies research evidence""".split())


def keywords(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]+", text.lower())
    out = []
    for w in words:
        if w not in STOPWORDS and len(w) > 2 and w not in out:
            out.append(w)
    return out[:limit]


def heuristic_frame(question: str) -> dict:
    kws = keywords(question)
    comparison = None
    m = re.search(r"\b(.+?)\s+(?:vs\.?|versus|compared (?:to|with))\s+(.+?)(?:[?.]|$)", question, re.I)
    if m:
        comparison = f"{m.group(1).strip()} vs {m.group(2).strip()}"
    queries = [" ".join(kws[:6])] if kws else [question]
    if len(kws) > 3:
        queries.append(" ".join(kws[:3]))
    ambiguities = []
    if len(kws) < 2:
        ambiguities.append("The question has very few specific terms; results may be broad. Add the population, "
                           "intervention/technology and outcome you care about.")
    if not comparison and not re.search(r"\b(effect|impact|improve|reduce|increase|cause|associat|outcome)", question, re.I):
        ambiguities.append("No outcome or comparison is stated. Consider: 'Does X improve Y compared with Z?'")
    return {
        "subject": question, "comparison": comparison, "outcome": None, "scope": "Not specified",
        "keywords": kws, "queries": queries, "ambiguities": ambiguities,
        "sufficient_evidence": "Multiple independent primary studies or a high-quality systematic review addressing the question.",
        "method": "heuristic (no LLM)",
    }


SYSTEM = """You are a research methodologist. Turn the user's research question into a structured, searchable form
(subject/population, comparison or intervention, outcome, scope) using PICO-style thinking adapted to the domain.
Produce 3-5 concise keyword search queries (not full sentences, 2-7 words each) using different phrasings and
synonyms, suitable for scholarly databases such as OpenAlex, Semantic Scholar and arXiv. List ambiguities that would
change the search if clarified. State what evidence would be sufficient to answer the question. Do not answer the
question itself and do not cite any studies."""


def llm_frame(client: GroqClient, question: str, context: dict | None = None) -> dict:
    ctx = ""
    if context:
        bits = []
        if context.get("year_from") or context.get("year_to"):
            bits.append(f"date range {context.get('year_from') or 'any'}-{context.get('year_to') or 'any'}")
        if context.get("include_terms"):
            bits.append("must include: " + ", ".join(context["include_terms"]))
        if context.get("exclude_terms"):
            bits.append("exclude: " + ", ".join(context["exclude_terms"]))
        if bits:
            ctx = "\nUser constraints: " + "; ".join(bits)
    out = client.structured(SYSTEM, f"Research question: {question}{ctx}", FramingOut, max_tokens=900)
    d = out.model_dump()
    d["method"] = f"LLM ({client.model})"
    if not d["keywords"]:
        d["keywords"] = keywords(question)
    return d
