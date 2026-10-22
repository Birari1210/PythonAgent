"""Step 4 — synthesize evidence by theme and calibrate confidence.

The LLM proposes themes/findings citing source labels. A validator then enforces the methodology:
- citations must reference included sources (unknown labels are removed; unsupported statements dropped);
- "established" requires >= 2 *independent* supporting lines of evidence (sources linked by citation
  dependency or derived from one another count as one line); otherwise it is downgraded;
- confidence is capped by coverage, the share of non-peer-reviewed evidence, and the weakest source the
  key findings rely on (Rule 8: never above the weakest well-used source).
"""
from __future__ import annotations

from ..llm import GroqClient
from ..models import SynthesisOut

LEVELS = ["Low", "Moderate", "High"]
WEIGHT_TO_LEVEL = {"None": "Low", "Low": "Low", "Moderate": "Moderate", "High": "High"}

SYSTEM = """You are Research Synthesist, a skeptical research methodologist. Synthesize ONLY the evidence provided.
Critical rules:
1. Every statement must cite the source labels (e.g. "S3") that support it, using only labels provided. Never invent
   sources, numbers, or findings not present in the evidence.
2. Volume is not strength: ten weak or derivative sources do not outweigh one strong one. Sources that review or
   cite each other are NOT independent confirmation.
3. Categorize each finding: "established" (multiple independent, adequately strong sources agree),
   "contested" (credible sources disagree — list both supporting and contradicting labels), or "single_study"
   (rests on one source or one line of evidence).
4. Report disagreement; do not silently pick a side. Explain why it matters.
5. Recency is not automatically better; weigh method and replication.
6. List evidence gaps: relevant sub-questions the provided evidence does not answer.
7. Calibrate confidence (Low/Moderate/High) to the weakest necessary link, explaining why.
8. In "dependencies", list pairs where one source appears to repeat or derive from another (same data, same claim
   origin), only when the evidence indicates it.
Organize findings by theme, not by source. Write concisely."""


def _source_brief(r: dict) -> str:
    ev = r.get("evaluation", {})
    lines = [f"[{r['label']}] {r.get('title')} ({r.get('year') or 'n.d.'}) — {r.get('source_type')}, tier {ev.get('tier')}, "
             f"weight {ev.get('weight')}, {ev.get('peer_review')}"]
    if ev.get("design"):
        lines.append(f"  design: {ev['design']}" + (f"; sample: {ev['sample_size']}" if ev.get("sample_size") else ""))
    if ev.get("key_findings"):
        lines.append("  reported findings: " + " | ".join(ev["key_findings"][:3]))
    else:
        lines.append("  abstract: " + (r.get("abstract") or r.get("content_excerpt") or "(none)")[:500])
    if ev.get("limitations"):
        lines.append("  stated limitations: " + "; ".join(ev["limitations"][:2]))
    if ev.get("cites_in_set"):
        lines.append("  cites in set: " + ", ".join(ev["cites_in_set"]))
    return "\n".join(lines)


def build_prompt(question: str, framing: dict | None, sources: list[dict], gaps_hint: list[str]) -> str:
    parts = [f"RESEARCH QUESTION: {question}"]
    if framing:
        parts.append(f"Structured: subject={framing.get('subject')}; comparison={framing.get('comparison')}; "
                     f"outcome={framing.get('outcome')}; scope={framing.get('scope')}")
    if gaps_hint:
        parts.append("Search notes: " + "; ".join(gaps_hint))
    parts.append(f"EVIDENCE ({len(sources)} included sources):")
    parts += [_source_brief(s) for s in sources]
    return "\n\n".join(parts)


def independent_lines(labels: list[str], dep_pairs: set[frozenset]) -> int:
    """Count connected components among `labels` where an edge means 'not independent'."""
    parent = {l: l for l in labels}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pair in dep_pairs:
        a, b = tuple(pair)
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    return len({find(l) for l in labels})


def dependency_pairs(sources: list[dict], model_deps: list[dict]) -> set[frozenset]:
    pairs: set[frozenset] = set()
    for s in sources:
        ev = s.get("evaluation", {})
        if not ev.get("independent", True):
            for c in ev.get("cites_in_set", []):
                pairs.add(frozenset((s["label"], c)))
    for d in model_deps:
        pairs.add(frozenset((d["source"], d["depends_on"])))
    return pairs


def validate(raw: SynthesisOut, sources: list[dict]) -> dict:
    by_label = {s["label"]: s for s in sources}
    valid = set(by_label)
    notes: list[str] = []
    deps = [d.model_dump() for d in raw.dependencies
            if d.source in valid and d.depends_on in valid and d.source != d.depends_on]
    for d in deps:
        d["provenance"] = "model-inferred"
    dep_pairs = dependency_pairs(sources, deps)

    themes = []
    for t in raw.themes:
        findings = []
        for f in t.findings:
            sup = [l for l in dict.fromkeys(f.supporting) if l in valid]
            con = [l for l in dict.fromkeys(f.contradicting) if l in valid and l not in sup]
            dropped = (set(f.supporting) | set(f.contradicting)) - valid
            if dropped:
                notes.append(f"Removed unknown citation(s) {', '.join(sorted(dropped))} from: \"{f.statement[:80]}\"")
            if not sup and not con:
                notes.append(f"Dropped statement with no valid supporting source: \"{f.statement[:80]}\"")
                continue
            cat = f.category
            lines = independent_lines(sup, dep_pairs) if sup else 0
            validator_note = None
            if cat == "contested" and not con:
                cat = "established" if lines >= 2 else "single_study"
                validator_note = "Labelled contested but no contradicting source was cited; re-categorized"
            if cat == "established" and lines < 2:
                cat = "single_study"
                validator_note = (f"Downgraded from established: {len(sup)} supporting source(s) form only "
                                  f"{lines} independent line(s) of evidence")
            if cat == "established":
                strong = [l for l in sup if by_label[l]["evaluation"].get("weight") in ("Moderate", "High")]
                if independent_lines(strong, dep_pairs) < 2:
                    cat = "single_study"
                    validator_note = ("Downgraded from established: fewer than two independent Moderate/High-weight "
                                      "sources support it (limited evidence)")
            findings.append({"statement": f.statement, "category": cat, "supporting": sup, "contradicting": con,
                             "independent_lines": lines, "explanation": f.explanation,
                             "validator_note": validator_note})
        if findings:
            themes.append({"name": t.name, "findings": findings})

    disagreements = []
    for d in raw.disagreements:
        srcs = [l for l in d.sources if l in valid]
        disagreements.append({"topic": d.topic, "positions": d.positions, "sources": srcs,
                              "significance": d.significance})

    synthesis = {
        "themes": themes, "gaps": [g.model_dump() for g in raw.gaps], "disagreements": disagreements,
        "dependencies": deps, "summary": raw.summary, "practical_conclusions": raw.practical_conclusions,
        "validator_notes": notes,
    }
    synthesis["confidence"] = calibrate_confidence(raw.confidence.level, raw.confidence.rationale, synthesis, sources,
                                                   dep_pairs)
    return synthesis


def calibrate_confidence(model_level: str, model_rationale: str, synthesis: dict, sources: list[dict],
                         dep_pairs: set[frozenset]) -> dict:
    caps: list[tuple[str, str]] = []
    n = len(sources)
    if n < 3:
        caps.append(("Low", f"Only {n} source(s) included"))
    established = [f for t in synthesis["themes"] for f in t["findings"] if f["category"] == "established"]
    if not established:
        caps.append(("Moderate", "No finding is supported by two or more independent lines of evidence"))
    unreviewed = [s for s in sources if s.get("source_type") in ("preprint", "web", "other")]
    if n and len(unreviewed) / n > 0.6:
        caps.append(("Low" if len(unreviewed) == n else "Moderate",
                     f"{len(unreviewed)} of {n} sources are preprints, web pages or unclassified"))
    key = established or [f for t in synthesis["themes"] for f in t["findings"]]
    used = {l for f in key for l in f["supporting"]}
    if used:
        by_label = {s["label"]: s for s in sources}
        weakest = min((by_label[l]["evaluation"].get("weight", "Low") for l in used),
                      key=lambda w: ["None", "Low", "Moderate", "High"].index(w))
        caps.append((WEIGHT_TO_LEVEL[weakest], f"Weakest source relied on for key findings has weight '{weakest}' (Rule 8)"))
    if not synthesis["themes"]:
        caps.append(("Low", "No synthesized findings survived validation"))
    level = model_level if model_level in LEVELS else "Low"
    applied = []
    for cap, why in caps:
        if LEVELS.index(cap) < LEVELS.index(level):
            level = cap
            applied.append(why)
    return {"level": level, "model_level": model_level, "rationale": model_rationale,
            "caps_applied": applied, "all_checks": [why for _, why in caps]}


def synthesize(client: GroqClient, question: str, framing: dict | None, sources: list[dict],
               search_notes: list[str]) -> dict:
    prompt = build_prompt(question, framing, sources, search_notes)
    raw = client.structured(SYSTEM, prompt, SynthesisOut, max_tokens=3500)
    out = validate(raw, sources)
    out["model"] = client.model
    return out
