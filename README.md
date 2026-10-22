# Research Synthesist

A local research agent that turns a research question into an auditable evidence map: it searches real
scholarly sources, removes duplicates, grades each source, traces citation dependencies, synthesizes findings
into **well-established / contested / single-study / evidence gaps**, and writes a report with a calibrated
confidence level and clickable citations.

It implements the methodology in `research-synthesist.md` (trace claims to origin, grade every source, volume ≠
strength, report disagreement, state gaps, disclose search boundaries, cap confidence at the weakest link).

## Quick start

Requires Python 3.10+.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```
(macOS/Linux: `source .venv/bin/activate`)

```bash
pip install -r requirements.txt
```

```bash
copy .env.example .env
```
(macOS/Linux: `cp .env.example .env`) — then put your free Groq key in `GROQ_API_KEY` (https://console.groq.com/keys),
or paste it later in the app's **Settings** page.

```bash
python run.py --open
```

The UI opens at http://127.0.0.1:8000. Run the tests with `pip install -r requirements-dev.txt` then `pytest`.

> On Windows, if `pip install` fails with *"filename or extension is too long"*, the project folder path is too
> deep — create the virtualenv somewhere shorter (e.g. `python -m venv C:\venvs\rs`) and use that Python.

## What happens in a research run

| Stage | What it does | Uses LLM? |
|---|---|---|
| Frame question | Subject / comparison / outcome / scope, ambiguities, 3–5 keyword queries (editable in the UI) | Yes (falls back to keyword extraction) |
| Search providers | Every query × every enabled provider, in parallel; each call's result count or error is logged | No |
| Deduplicate & screen | Merges same-DOI / same-arXiv-id / near-identical-title records (preprint ↔ published versions linked); applies date range, source types, inclusion/exclusion terms, retraction flags; ranks by keyword relevance and keeps the top *N* (every exclusion keeps its reason) | No |
| Evaluate sources | Citation tracing via OpenAlex reference lists (who cites whom inside the set); LLM extracts design, sample size, findings, limitations, COI **from abstracts only**; rule-based tier, peer-review status, method quality, independence and weight | Extraction only |
| Synthesize evidence | LLM groups findings by theme with source labels; a validator then removes unknown citations, drops unsupported statements, downgrades "established" findings that lack ≥2 *independent* lines of Moderate/High-weight evidence, and caps confidence | Yes |
| Write report | 9-section Markdown report, versioned; export to Markdown or PDF | No |

### Honesty guarantees built into the code

- Citations can only reference sources that were actually retrieved; anything else is removed and the removal is disclosed in the report.
- Sample sizes the model extracts are discarded unless the number appears in the abstract.
- Peer-review status is always labelled "per indexing metadata (not independently verified)"; preprints are labelled as not peer reviewed.
- Reviews/commentaries that cite other included sources are marked **not independent** and counted as one line of evidence with them.
- Confidence is the *lower* of the model's proposal and rule-based caps (fewer than 3 sources → Low; no established finding → max Moderate; mostly preprints/web → capped; the weakest source the key findings rely on caps the level — Rule 8). Applied caps are listed in the report.
- Without a Groq key the app still searches, screens and grades by metadata, but shows **"Not synthesized / Not assessed"** instead of conclusions.
- Retrieved metadata, model-extracted details and model interpretation are labelled separately throughout.
- The report states plainly that this is **not** a PRISMA-compliant systematic review (no registered protocol, no dual screening, no formal risk-of-bias tool, abstracts only).

## Research providers and limits

| Provider | Key | Coverage | Notes |
|---|---|---|---|
| OpenAlex | none (optional `OPENALEX_API_KEY`) | ~250M works, all fields | Also used for citation tracing. Add `CONTACT_EMAIL` for the polite pool. |
| Semantic Scholar | optional `SEMANTIC_SCHOLAR_API_KEY` | CS, biomed, broad | Keyless access shares a small global rate limit — **429 errors are common**; they are reported as coverage gaps. A free key fixes this. |
| arXiv | none | Preprints (physics, CS, math, q-bio…) | Throttled to 1 request / 3 s per arXiv's terms. Always labelled as not peer reviewed. |
| Europe PMC | none | Biomedical & life sciences incl. preprints | |
| Crossref | none | DOI metadata, all publishers | Abstracts often missing; news-style "posted content" is classified as web, not preprint. |
| Tavily (optional) | `TAVILY_API_KEY` | Web pages, documentation, grey literature | Free tier ≈1,000 searches/month. Off until a key is set. |

**Groq:** the free tier is limited by tokens per minute. The app batches extraction (5 sources per call), truncates
abstracts, retries 429s honouring `retry-after`, and validates every response against a schema (one repair attempt).
Keeping *max sources* around 25 avoids long rate-limit waits. If the default model is retired, pick another in
**Settings → Load available models**.

## Daily-use features

- **Dashboard** – quick-start box, recent sessions, saved reports.
- **New research** – question analysis, editable queries, date range, providers, source types, inclusion/exclusion terms, max sources, and *related previous research* you can reuse (reused sources keep their original retrieval date and are re-screened, never treated as current).
- **Progress** – real stage status from the backend, per-provider request log with errors, search funnel, limitations.
- **Source explorer** – metadata, tier, peer-review status, method quality, independence, cited-by links, merged duplicates and other versions, abstracts and model-extracted details with provenance; include/exclude manually; add sources by URL/DOI/arXiv link or upload PDF/TXT/MD/HTML.
- **Evidence summary** – established / contested / single-study / gaps, disagreements, dependencies, confidence with caps.
- **Report** – versioned reports, Markdown/PDF export, print.
- **Follow-up Q&A** – answers grounded only in the session's sources; unknown citations removed; "not covered" stated.
- **Refresh** – re-searches from the year before the last run to find newly published/indexed work; new sources are flagged and a new report version is written. **Re-analyze** re-runs evaluation + synthesis after you add or exclude sources.
- **History** – search across sessions, reports and sources; rename and delete.

## Project layout

```
app/
  config.py          settings + .env secrets (never returned to the UI)
  models.py          Pydantic schemas for API input and LLM output
  storage.py         SQLite (sessions, runs, sources, reports, follow-ups, settings)
  llm.py             Groq client: retries, rate limits, timeouts, schema validation
  retrieval/         providers.py (OpenAlex, Semantic Scholar, arXiv, Europe PMC, Crossref, Tavily), manual.py (URL/upload, SSRF-guarded)
  research/          framing, screening, evaluation, synthesis, report (+PDF), followup, pipeline (background runs)
  api.py, main.py    FastAPI routes and app factory
static/              index.html, app.js, styles.css (no build step)
tests/               34 tests: methodology rules, Groq client, parsers, full API workflow with fakes
```

Data lives in `data/research.db` (SQLite) and logs in `data/app.log` (API keys are redacted from logs).
# PythonAgent
# PythonAgent
