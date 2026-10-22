"""Report generation (Markdown) and PDF export."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

CATEGORY_LABEL = {"established": "Well-established", "contested": "Contested", "single_study": "Single-study / limited"}
PROVIDER_LABEL = {"openalex": "OpenAlex", "semantic_scholar": "Semantic Scholar", "arxiv": "arXiv",
                  "europepmc": "Europe PMC", "crossref": "Crossref", "tavily": "Tavily web search", "manual": "added manually"}

METHOD_LIMITATIONS = [
    "Screening and grading were automated from titles, abstracts and indexing metadata — full texts were not read.",
    "This is a structured, auditable literature scan, not a PRISMA-compliant systematic review: there is no "
    "registered protocol, dual independent screening, or formal risk-of-bias tool.",
    "Peer-review status is inferred from indexing metadata and is not independently verified.",
    "Search results depend on each provider's relevance ranking; only the top results per query were retrieved.",
]


def _esc(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def source_link(s: dict) -> str:
    return s.get("url") or (f"https://doi.org/{s['doi']}" if s.get("doi") else "")


def cite(labels: list[str], by_label: dict) -> str:
    out = []
    for l in labels:
        s = by_label.get(l)
        url = source_link(s) if s else ""
        out.append(f"[{l}]({url})" if url else f"[{l}]")
    return ", ".join(out) if out else "—"


def short_authors(s: dict) -> str:
    a = s.get("authors") or []
    if not a:
        return "Unknown author"
    last = a[0].split()[-1] if a[0].split() else a[0]
    return last + (" et al." if len(a) > 2 else (f" & {a[1].split()[-1]}" if len(a) == 2 and a[1].split() else ""))


def build_report(session: dict, runs: list[dict], sources: list[dict], synthesis: dict | None,
                 synthesis_error: str | None, report_version: int, followups_note: str | None = None) -> str:
    included = [s for s in sources if s["status"] == "included"]
    by_label = {s["label"]: s for s in sources}
    params = session.get("params") or {}
    framing = session.get("framing") or {}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L: list[str] = []
    L.append(f"# Research Report: {session['title']}")
    L.append(f"Generated {ts} · report v{report_version} · session `{session['id']}`"
             + (f" · LLM: {synthesis.get('model')}" if synthesis else ""))
    L.append("> **Provenance legend.** *Retrieved* = metadata returned by the search providers. *Extracted* = "
             "model-extracted from abstracts (verify against the full text). *Interpretation* = model-generated "
             "synthesis, checked by rule-based validation against the cited sources.")

    # 1 — question and scope
    L.append("## 1. Research question and scope")
    L.append(f"**Question:** {session['question']}")
    if framing:
        L.append(f"- **Subject:** {framing.get('subject') or '—'}")
        L.append(f"- **Comparison / intervention:** {framing.get('comparison') or 'none stated'}")
        L.append(f"- **Outcome:** {framing.get('outcome') or 'none stated'}")
        L.append(f"- **Scope:** {framing.get('scope') or '—'}")
        L.append(f"- **What would count as sufficient evidence:** {framing.get('sufficient_evidence') or '—'}")
        if framing.get("ambiguities"):
            L.append(f"- **Ambiguities noted:** " + "; ".join(framing["ambiguities"]))
        L.append(f"- *Framing method: {framing.get('method', 'unknown')}*")

    # 2 — methodology
    L.append("## 2. Search methodology and boundaries")
    for run in runs:
        st = run.get("strategy", {})
        c = run.get("counts", {})
        L.append(f"### {'Initial search' if run['kind'] == 'initial' else 'Refresh search'} — "
                 f"{(run.get('started_at') or '')[:10]} ({run.get('status')})")
        L.append("```text")
        L.append(f"Sources searched:   {', '.join(PROVIDER_LABEL.get(p, p) for p in st.get('providers', [])) or '—'}")
        L.append(f"Search terms:       {' | '.join(st.get('queries', [])) or '—'}")
        L.append(f"Date range:         {st.get('year_from') or 'any'} – {st.get('year_to') or 'present'}")
        L.append(f"Source types:       {', '.join(st.get('source_types') or []) or 'all'}")
        L.append(f"Inclusion criteria: {', '.join(st.get('include_terms') or []) or 'within date range, on-topic'}")
        L.append(f"Exclusion criteria: {', '.join(st.get('exclude_terms') or []) or 'retracted works; outside date range/types'}")
        L.append(f"Results:            {c.get('retrieved', 0)} retrieved → {c.get('unique', 0)} after dedup → "
                 f"{c.get('screened_in', 0)} met criteria → {c.get('included', 0)} "
                 + ("new included" if run["kind"] == "refresh" else "included")
                 + (f" ({c['off_topic']} later excluded as off-topic)" if c.get("off_topic") else "")
                 + (f"; {c.get('already_known', 0)} results already in session" if run["kind"] == "refresh" else ""))
        L.append("```")
        failures = [p for p in run.get("provider_log", []) if p.get("error")]
        if failures:
            L.append("**Provider failures (coverage gaps):**")
            for p in failures:
                L.append(f"- {PROVIDER_LABEL.get(p['provider'], p['provider'])} — query \"{p['query']}\": {p['error']}")
    L.append("**Limitations of this method:**")
    lims = list(METHOD_LIMITATIONS)
    for run in runs:
        lims += [l for l in run.get("limitations", []) if l not in lims]
    L += [f"- {l}" for l in lims]

    # 3 — source evaluation table
    L.append("## 3. Source evaluation table")
    L.append("*Tier, peer-review status and weight are assigned by explicit rules; design and sample size are "
             "extracted from abstracts.*")
    L.append("| # | Source | Type | Evidence tier | Peer review | Method quality | Independent of other sources? | Weight |")
    L.append("|---|---|---|---|---|---|---|---|")
    for s in included:
        ev = s.get("evaluation", {})
        name = f"{short_authors(s)} {s.get('year') or 'n.d.'} — {s.get('title', '')[:90]}"
        mq = ev.get("method_quality", "—")
        if ev.get("design"):
            mq += f" ({ev['design']}" + (f", {ev['sample_size']}" if ev.get("sample_size") else "") + ")"
        L.append(f"| {cite([s['label']], by_label)}{' 🆕' if s.get('is_new') else ''} | {_esc(name)} | "
                 f"{_esc(s.get('source_type'))} | {_esc(ev.get('tier'))} | {_esc(ev.get('peer_review'))} | {_esc(mq)} | "
                 f"{_esc(ev.get('independence'))} | **{_esc(ev.get('weight'))}** |")
    dups = [s for s in sources if s["status"] == "duplicate"]
    if dups:
        L.append(f"*{len(dups)} duplicate record(s) merged:* " + "; ".join(
            f"{d['label']} → {by_label.get(d.get('duplicate_of'), {}).get('label', '?')} ({d.get('reason')})" for d in dups[:15]))
    excluded = [s for s in sources if s["status"] == "excluded"]
    if excluded:
        reasons: dict[str, int] = {}
        for s in excluded:
            key = re.sub(r"\d{4}", "YYYY", s.get("reason") or "unspecified")
            reasons[key] = reasons.get(key, 0) + 1
        L.append(f"*{len(excluded)} record(s) excluded:* " + "; ".join(f"{k} ×{v}" for k, v in reasons.items()))

    if not synthesis:
        why = synthesis_error or "LLM synthesis not available"
        L.append("## 4. Key findings by theme")
        L.append(f"**Not synthesized:** {why}. The evaluated sources above and their abstracts are available in "
                 "the Source Explorer; no conclusions are drawn without synthesis.")
        L.append("## 5. Agreement and disagreement between sources\n_Not assessed (no synthesis)._")
        L.append("## 6. Evidence gaps and limitations\n_Not assessed (no synthesis)._")
        L.append("## 7. Overall confidence assessment\n**Not assessed.** Confidence cannot be stated without synthesis.")
    else:
        L.append("## 4. Key findings by theme *(Interpretation)*")
        for t in synthesis["themes"]:
            L.append(f"### {t['name']}")
            for f in t["findings"]:
                line = f"- **[{CATEGORY_LABEL[f['category']]}]** {f['statement']} — supporting: {cite(f['supporting'], by_label)}"
                if f["contradicting"]:
                    line += f"; contradicting: {cite(f['contradicting'], by_label)}"
                line += f" · {f['independent_lines']} independent line(s)"
                L.append(line)
                if f.get("explanation"):
                    L.append(f"  - {f['explanation']}")
                if f.get("validator_note"):
                    L.append(f"  - ⚠️ *Validator:* {f['validator_note']}")
        L.append("## 5. Agreement and disagreement between sources")
        L.append("```text")
        L.append(f"CLAIM: {session['question']}")
        L.append("=" * 40)
        for cat, label in (("established", "Well-established"), ("contested", "Contested"),
                           ("single_study", "Single-study only")):
            items = [f for t in synthesis["themes"] for f in t["findings"] if f["category"] == cat]
            L.append(f"{label + ':':<20}" + (str(len(items)) + " finding(s)" if items else "none"))
            for f in items:
                L.append(f"    • {f['statement'][:160]} [{', '.join(f['supporting'] + f['contradicting'])}]")
        L.append(f"{'Evidence gap:':<20}" + (f"{len(synthesis['gaps'])} identified" if synthesis["gaps"] else "none identified"))
        L.append(f"{'Confidence:':<20}{synthesis['confidence']['level']}")
        L.append("```")
        if synthesis["disagreements"]:
            L.append("**Disagreements:**")
            for d in synthesis["disagreements"]:
                L.append(f"- **{d['topic']}** ({cite(d['sources'], by_label)}): " + " vs. ".join(d["positions"])
                         + (f" — *Why it matters:* {d['significance']}" if d.get("significance") else ""))
        deps = [(s["label"], c) for s in included for c in s.get("evaluation", {}).get("cites_in_set", [])
                if not s["evaluation"].get("independent", True)]
        if deps or synthesis["dependencies"]:
            L.append("**Dependent / non-independent sources:**")
            for a, b in deps:
                L.append(f"- {a} reviews or repeats {b} (citation trace via OpenAlex — retrieved)")
            for d in synthesis["dependencies"]:
                L.append(f"- {d['source']} appears to derive from {d['depends_on']}: {d.get('reason', '')} (model-inferred)")
        L.append("## 6. Evidence gaps and limitations")
        if synthesis["gaps"]:
            for g in synthesis["gaps"]:
                L.append(f"- **{g['question']}**" + (f" — {g['note']}" if g.get("note") else ""))
        else:
            L.append("- No gaps were identified by the synthesis — this does not mean the question is settled.")
        singles = [f for t in synthesis["themes"] for f in t["findings"] if f["category"] == "single_study"]
        if singles:
            L.append(f"- {len(singles)} finding(s) rest on limited evidence and need replication.")
        if synthesis.get("validator_notes"):
            L.append("**Validation notes:** " + "; ".join(synthesis["validator_notes"]))
        conf = synthesis["confidence"]
        L.append("## 7. Overall confidence assessment")
        L.append(f"**Confidence: {conf['level']}**" + (f" (model proposed {conf['model_level']}; capped by rules)"
                                                        if conf["model_level"] != conf["level"] else ""))
        L.append(f"- *Model rationale:* {conf['rationale']}")
        for c in conf.get("caps_applied", []):
            L.append(f"- *Cap applied:* {c}")
        if not conf.get("caps_applied") and conf.get("all_checks"):
            L.append("- *Checks:* " + "; ".join(conf["all_checks"]))

    # 8 — citations
    L.append("## 8. Sources")
    for s in included:
        prov = ", ".join(PROVIDER_LABEL.get(p, p) for p in s.get("providers", []))
        authors = ", ".join((s.get("authors") or [])[:6]) + (" et al." if len(s.get("authors") or []) > 6 else "")
        link = source_link(s)
        line = f"- **{s['label']}.** {authors or 'Unknown author'} ({s.get('year') or 'n.d.'}). {s.get('title')}."
        if s.get("venue"):
            line += f" *{s['venue']}*."
        if s.get("doi"):
            line += f" DOI: [{s['doi']}](https://doi.org/{s['doi']})."
        elif link:
            line += f" [{link}]({link})"
        line += f" — retrieved via {prov} on {(s.get('retrieved_at') or '')[:10]}"
        if s.get("reused_from"):
            line += f" (reused from earlier session; re-check currency)"
        if s.get("is_new"):
            line += " · **new in latest refresh**"
        L.append(line)

    L.append("## 9. Summary and practical conclusions *(Interpretation)*")
    if synthesis:
        L.append(synthesis["summary"])
        if synthesis["practical_conclusions"]:
            L += [f"- {c}" for c in synthesis["practical_conclusions"]]
        L.append(f"\n*These conclusions are bounded by the confidence level ({synthesis['confidence']['level']}) and "
                 "the search limitations above.*")
    else:
        L.append("_No synthesis available — conclusions are not drawn._")
    if followups_note:
        L.append(followups_note)
    return _join(L)


def _join(lines: list[str]) -> str:
    """Blank line between blocks, but keep list items, table rows and code fences contiguous."""
    out: list[str] = []
    in_code = False
    prev_kind = None
    for line in lines:
        for sub in line.split("\n"):
            if sub.startswith("```"):
                if not in_code and out:
                    out.append("")
                out.append(sub)
                in_code = not in_code
                prev_kind = "code"
                continue
            if in_code:
                out.append(sub)
                continue
            kind = "list" if sub.lstrip().startswith("- ") else "table" if sub.startswith("|") else "block"
            if out and not (kind == prev_kind and kind in ("list", "table")):
                out.append("")
            out.append(sub)
            prev_kind = kind
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ PDF
def _find_fonts() -> tuple[str, str] | None:
    candidates = [
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ]
    extra = os.environ.get("RS_PDF_FONT")
    if extra and Path(extra).exists():
        return extra, os.environ.get("RS_PDF_FONT_BOLD", extra)
    for reg, bold in candidates:
        if Path(reg).exists() and Path(bold).exists():
            return reg, bold
    return None


_LINK = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")


_SYMBOLS = {"🆕": "(new)", "⚠️": "!", "→": "->", "•": "-", "≈": "~", "–": "-", "—": "-", "“": '"', "”": '"', "’": "'",
            "‐": "-", "‑": "-", " ": " ", " ": " "}


def _symbols(text: str) -> str:
    for k, v in _SYMBOLS.items():
        text = text.replace(k, v)
    return text


def _plain(text: str) -> str:
    """Strip Markdown emphasis/links for PDF text, leaving underscores inside words (e.g. semantic_scholar)."""
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<![\w*])[*_](?=\S)(.+?)(?<=\S)[*_](?![\w*])", r"\1", text)
    return text.replace("`", "").replace("🆕", "(new)").replace("⚠️", "!")


def markdown_to_pdf(md: str) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_margins(15, 15, 15)
    fonts = _find_fonts()
    if fonts:
        pdf.add_font("Body", "", fonts[0])
        pdf.add_font("Body", "B", fonts[1])
        family, clean = "Body", _symbols
    else:
        family = "Helvetica"
        clean = lambda s: _symbols(s).encode("latin-1", "replace").decode("latin-1")  # noqa: E731
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def para(text: str, size=10, style="", indent=0.0):
        pdf.set_font(family, style, size)
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(width - indent, size * 0.5, clean(text), new_x="LMARGIN", new_y="NEXT")

    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            pdf.set_font("Courier", "", 8)
            pdf.set_fill_color(242, 242, 245)
            for b in block:
                pdf.multi_cell(width, 4, _symbols(b).encode("latin-1", "replace").decode("latin-1"), fill=True,
                               new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", lines[i].strip())[1:-1]]
                if not all(re.fullmatch(r"-+", c) for c in cells):
                    rows.append([clean(_plain(c)) for c in cells])
                i += 1
            if rows:
                pdf.set_font(family, "", 6.5)
                ncol = len(rows[0])
                widths = (6, 26, 10, 9, 15, 13, 15, 7)[:ncol] if ncol == 8 else None
                with pdf.table(col_widths=widths, text_align="LEFT", line_height=3.2, first_row_as_headings=True) as table:
                    for r in rows:
                        row = table.row()
                        for c in (r + [""] * ncol)[:ncol]:
                            row.cell(c)
                pdf.ln(2)
            continue
        elif line.startswith("# "):
            para(_plain(line[2:]), 16, "B")
            pdf.ln(1)
        elif line.startswith("## "):
            pdf.ln(2)
            para(_plain(line[3:]), 13, "B")
        elif line.startswith("### "):
            para(_plain(line[4:]), 11, "B")
        elif line.lstrip().startswith("- "):
            depth = (len(line) - len(line.lstrip())) / 2
            para("• " + _plain(line.lstrip()[2:]), 9.5, indent=3 + depth * 5)
        elif line.startswith("> "):
            pdf.set_text_color(90, 90, 90)
            para(_plain(line[2:]), 9, indent=3)
            pdf.set_text_color(0, 0, 0)
        elif line.strip():
            para(_plain(line), 10)
        i += 1
    return bytes(pdf.output())
