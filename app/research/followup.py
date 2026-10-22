"""Follow-up questions answered strictly from the session's collected evidence."""
from __future__ import annotations

import re

from ..llm import GroqClient
from ..models import FollowupOut

SYSTEM = """You are Research Synthesist answering a follow-up question about a completed evidence review.
Use ONLY the evidence listed (source summaries and the prior synthesis). Cite source labels inline like [S3].
If the evidence does not answer the question, say so plainly and put what is missing in "not_covered" —
never fill gaps with outside knowledge. Distinguish what sources report from your interpretation, and keep the
confidence no higher than the evidence supports. List every label you cited in "cited"."""


def build_context(sources: list[dict], synthesis: dict | None) -> str:
    parts = []
    for s in sources:
        ev = s.get("evaluation") or {}
        body = " | ".join(ev.get("key_findings") or []) or (s.get("abstract") or s.get("content_excerpt") or "")[:450]
        parts.append(f"[{s['label']}] {s.get('title')} ({s.get('year') or 'n.d.'}; {s.get('source_type')}; "
                     f"weight {ev.get('weight', 'unassessed')}): {body}")
    if synthesis:
        parts.append("PRIOR SYNTHESIS SUMMARY: " + synthesis.get("summary", ""))
        for t in synthesis.get("themes", []):
            for f in t["findings"]:
                parts.append(f"- ({f['category']}) {f['statement']} [{', '.join(f['supporting'])}]")
        parts.append(f"Overall confidence: {synthesis.get('confidence', {}).get('level')}")
    return "\n".join(parts)


def answer(client: GroqClient, question: str, original_question: str, sources: list[dict],
           synthesis: dict | None) -> dict:
    ctx = build_context(sources, synthesis)
    out = client.structured(SYSTEM, f"ORIGINAL QUESTION: {original_question}\n\nEVIDENCE:\n{ctx}\n\n"
                                    f"FOLLOW-UP QUESTION: {question}", FollowupOut, max_tokens=1200)
    valid = {s["label"] for s in sources}
    text = out.answer
    bad = sorted(set(re.findall(r"\bS\d+\b", text)) - valid)
    for b in bad:
        text = re.sub(rf"\[?\b{b}\b\]?", "[citation removed: not in evidence set]", text)
    cited = sorted({c for c in out.cited if c in valid} | (set(re.findall(r"\bS\d+\b", text)) & valid),
                   key=lambda x: int(x[1:]))
    return {"answer": text, "cited": cited, "not_covered": out.not_covered}
