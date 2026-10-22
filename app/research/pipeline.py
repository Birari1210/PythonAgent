"""Research orchestration: frame → search → screen → evaluate → synthesize → report.

Runs in a background thread per session. Every stage writes its real status to the database, so the UI
only ever shows progress that has actually happened.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .. import llm as llm_mod
from ..llm import LLMError
from ..retrieval.base import SearchQuery, now_iso
from ..retrieval.providers import ALL_PROVIDERS
from ..storage import Storage
from . import evaluation, framing, report, screening, synthesis

log = logging.getLogger(__name__)

STAGES = [("frame", "Frame question"), ("search", "Search providers"), ("screen", "Deduplicate & screen"),
          ("evaluate", "Evaluate sources"), ("synthesize", "Synthesize evidence"), ("report", "Write report")]
REANALYSIS_STAGES = STAGES[3:]
REFRESH_NEW_SOURCES = 10  # minimum number of new sources a refresh may add, even when the session is at its cap


class Cancelled(Exception):
    pass


class RunContext:
    def __init__(self, store: Storage, sid: str, run_id: int, stages: list[dict], cancel: threading.Event):
        self.store, self.sid, self.run_id, self.stages, self.cancel = store, sid, run_id, stages, cancel
        self.provider_log: list[dict] = []
        self.limitations: list[str] = []
        self.counts: dict = {}
        self.partial = False

    def _stage(self, name):
        return next(s for s in self.stages if s["name"] == name)

    def start(self, name, detail=""):
        if self.cancel.is_set():
            raise Cancelled()
        s = self._stage(name)
        s.update(status="running", detail=detail, started_at=now_iso())
        self.flush()

    def update(self, name, detail):
        self._stage(name)["detail"] = detail
        self.flush()

    def finish(self, name, detail="", status="done"):
        s = self._stage(name)
        s.update(status=status, detail=detail or s.get("detail", ""), finished_at=now_iso())
        if status in ("warning", "failed", "skipped"):
            self.partial = True if status != "skipped" else self.partial
        self.flush()

    def flush(self):
        self.store.update_run(self.run_id, stages=self.stages, provider_log=self.provider_log,
                              counts=self.counts, limitations=self.limitations)


class JobManager:
    def __init__(self, store: Storage):
        self.store = store
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def is_running(self, sid: str) -> bool:
        t = self._threads.get(sid)
        return bool(t and t.is_alive())

    def start(self, sid: str, kind: str = "initial") -> int:
        with self._lock:
            if self.is_running(sid):
                raise RuntimeError("A run is already in progress for this session")
            stage_defs = REANALYSIS_STAGES if kind == "reanalysis" else STAGES
            stages = [{"name": n, "label": l, "status": "pending", "detail": ""} for n, l in stage_defs]
            run_id = self.store.create_run(sid, kind, {}, stages)
            self.store.update_session(sid, status="running", error=None)
            cancel = threading.Event()
            self._cancel[sid] = cancel
            t = threading.Thread(target=self._run, args=(sid, run_id, kind, stages, cancel), daemon=True,
                                 name=f"research-{sid}")
            self._threads[sid] = t
            t.start()
            return run_id

    def cancel(self, sid: str) -> bool:
        ev = self._cancel.get(sid)
        if ev and self.is_running(sid):
            ev.set()
            return True
        return False

    def wait(self, sid: str, timeout: float | None = None) -> None:
        t = self._threads.get(sid)
        if t:
            t.join(timeout)

    def _run(self, sid, run_id, kind, stages, cancel):
        ctx = RunContext(self.store, sid, run_id, stages, cancel)
        try:
            run_pipeline(ctx, kind)
            status = "partial" if ctx.partial else "completed"
            self.store.update_run(run_id, status=status, finished_at=now_iso())
            self.store.update_session(sid, status=status, last_run_at=now_iso())
        except Cancelled:
            for s in stages:
                if s["status"] in ("pending", "running"):
                    s["status"] = "cancelled"
            ctx.flush()
            self.store.update_run(run_id, status="cancelled", finished_at=now_iso())
            self.store.update_session(sid, status="cancelled", error="Cancelled by user")
        except Exception as e:  # surface any failure honestly
            log.exception("Research run %s failed", run_id)
            msg = str(e) if isinstance(e, (LLMError, RunError)) else f"Unexpected error: {type(e).__name__}: {e}"
            for s in stages:
                if s["status"] == "running":
                    s.update(status="failed", detail=msg, finished_at=now_iso())
            ctx.flush()
            self.store.update_run(run_id, status="failed", finished_at=now_iso())
            self.store.update_session(sid, status="failed", error=msg)


class RunError(Exception):
    pass


def _client(store: Storage):
    c = llm_mod.client_from_settings(store.get_settings())
    return c if c.configured else None


def run_pipeline(ctx: RunContext, kind: str) -> None:
    store, sid = ctx.store, ctx.sid
    session = store.get_session(sid)
    settings = store.get_settings()
    params = session["params"]
    client = _client(store)
    if not client:
        ctx.limitations.append("Groq LLM not configured: no extraction, synthesis or confidence assessment.")

    if kind != "reanalysis":
        fr = stage_frame(ctx, session, params, client)
        strategy = stage_search_strategy(ctx, session, params, settings, fr, kind)
        records = stage_search(ctx, strategy, settings)
        stage_screen(ctx, records, strategy, params, settings, kind)
    stage_evaluate(ctx, session, client)
    synth, synth_err = stage_synthesize(ctx, session, client)
    stage_report(ctx, synth, synth_err)


def stage_frame(ctx, session, params, client) -> dict:
    ctx.start("frame")
    if session.get("framing") and (params.get("queries") or session["framing"].get("queries")):
        fr = session["framing"]
        if params.get("queries"):
            fr["queries"] = params["queries"]
        ctx.finish("frame", f"Using saved framing ({len(fr['queries'])} queries)")
        return fr
    fr, status, detail = None, "done", ""
    if client:
        try:
            fr = framing.llm_frame(client, session["question"], params)
            detail = f"LLM framing: {len(fr['queries'])} queries"
        except LLMError as e:
            ctx.limitations.append(f"LLM framing failed ({e}); heuristic keywords used instead.")
            status = "warning"
    if not fr:
        fr = framing.heuristic_frame(session["question"])
        detail = detail or f"Heuristic framing (no LLM): {len(fr['queries'])} queries"
    if params.get("queries"):
        fr["queries"] = params["queries"]
        detail += " — user-provided queries used"
    store = ctx.store
    store.update_session(ctx.sid, framing=fr)
    session["framing"] = fr
    ctx.finish("frame", detail, status)
    return fr


def stage_search_strategy(ctx, session, params, settings, fr, kind) -> dict:
    providers = params.get("providers") or settings["providers"]
    strategy = {
        "queries": fr["queries"][:5], "providers": providers, "year_from": params.get("year_from"),
        "year_to": params.get("year_to"), "source_types": params.get("source_types") or [],
        "include_terms": params.get("include_terms") or [], "exclude_terms": params.get("exclude_terms") or [],
        "results_per_query": int(settings["results_per_query"]),
        "keywords": fr.get("keywords") or framing.keywords(session["question"]),
    }
    if kind == "refresh" and session.get("last_run_at"):
        # Indexes lag publication, so look back one year before the last run; never leave the user's range.
        since = datetime.fromisoformat(session["last_run_at"]).year - 1
        year_from = max(strategy["year_from"] or 0, since)
        if strategy["year_to"] and year_from > strategy["year_to"]:
            year_from = strategy["year_to"]
            ctx.limitations.append(f"The session's date range ends in {strategy['year_to']}, so the refresh could only "
                                   f"re-check {year_from} publications for newly indexed sources.")
        strategy["year_from"] = year_from
        strategy["refresh_since"] = session["last_run_at"]
        ctx.limitations.append(f"Refresh searched publications from {year_from} onward to find newly published or "
                               f"newly indexed sources; earlier sources were retained from the previous run(s).")
    ctx.store.update_run(ctx.run_id, strategy=strategy)
    return strategy


def stage_search(ctx, strategy, settings) -> list[dict]:
    ctx.start("search")
    contact = settings.get("contact_email") or ""
    active, skipped = [], []
    for name in strategy["providers"]:
        cls = ALL_PROVIDERS.get(name)
        if not cls:
            continue
        ok, why = cls(contact).available()
        (active if ok else skipped).append((name, why))
    for name, why in skipped:
        ctx.provider_log.append({"provider": name, "query": "*", "count": 0, "error": f"skipped: {why}"})
        ctx.limitations.append(f"{ALL_PROVIDERS[name].label} not searched: {why}.")
    if not active:
        raise RunError("No search providers are available. Enable at least one provider in Settings.")
    tasks = [(name, q) for q in strategy["queries"] for name, _ in active]
    records: list[dict] = []
    done = 0
    ctx.update("search", f"0/{len(tasks)} provider queries complete")
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(ALL_PROVIDERS[name](contact).search,
                            SearchQuery(q, strategy["year_from"], strategy["year_to"], strategy["results_per_query"])):
                (name, q) for name, q in tasks}
        for fut in as_completed(futs):
            name, q = futs[fut]
            res = fut.result()
            done += 1
            ctx.provider_log.append({"provider": name, "query": q, "count": len(res.records), "error": res.error,
                                     "elapsed": res.elapsed})
            records.extend(res.records)
            ctx.update("search", f"{done}/{len(tasks)} provider queries complete · {len(records)} records")
            if ctx.cancel.is_set():
                for f in futs:
                    f.cancel()
                raise Cancelled()
    failed = [p for p in ctx.provider_log if p["error"] and not p["error"].startswith("skipped")]
    ok_calls = [p for p in ctx.provider_log if not p["error"]]
    for name in {p["provider"] for p in failed}:
        n = sum(1 for p in failed if p["provider"] == name)
        errs = sorted({p["error"] for p in failed if p["provider"] == name})
        ctx.limitations.append(f"{ALL_PROVIDERS[name].label}: {n} of {len(strategy['queries'])} queries failed "
                               f"({'; '.join(errs)[:200]}).")
    ctx.counts["retrieved"] = len(records)
    if not ok_calls:
        raise RunError("All search requests failed: " + "; ".join(sorted({p["error"] for p in failed}))[:400])
    ctx.finish("search", f"{len(records)} records from {len(ok_calls)}/{len(tasks)} successful queries"
               + (f" · {len(failed)} failed" if failed else ""), "warning" if failed else "done")
    return records


def stage_screen(ctx, records, strategy, params, settings, kind) -> None:
    ctx.start("screen")
    store, sid = ctx.store, ctx.sid
    existing = store.list_sources(sid)
    # Reused sources from earlier sessions keep their original retrieval date.
    for other in params.get("reuse_session_ids") or []:
        for s in store.list_sources(other, status="included"):
            rec = {k: v for k, v in s.items() if k not in ("id", "label", "status", "reason", "duplicate_of",
                                                         "is_new", "run_id", "added_at", "session_id", "evaluation")}
            rec["reused_from"] = other
            records.append(rec)
    known_new = 0
    fresh = []
    for r in records:
        if any(screening.same_work(e, r) for e in existing):
            known_new += 1
        else:
            fresh.append(r)
    unique, dups = screening.dedupe(fresh)
    ctx.counts["unique"] = len(unique)
    if kind == "refresh":
        ctx.counts["already_known"] = known_new
    keywords = strategy["keywords"]
    passed, failed = [], []
    for r in unique:
        ok, reason = screening.screen(r, strategy)
        r["relevance_score"] = screening.relevance_score(r, keywords)
        (passed if ok else failed).append((r, reason))
    passed.sort(key=lambda x: x[0]["relevance_score"], reverse=True)
    cap = int(params.get("max_sources") or settings["max_screened_sources"])
    if kind == "refresh":
        # A refresh exists to add new evidence, so it always has room for some new sources.
        store.clear_new_flags(sid)
        already_included = len([e for e in existing if e["status"] == "included"])
        room = max(cap - already_included, REFRESH_NEW_SOURCES)
    else:
        room = cap
    ctx.counts["screened_in"] = len(passed)
    stored: dict[int, dict] = {}
    n_inc = 0
    for idx, (r, reason) in enumerate(passed):
        if idx < room:
            row = store.add_source(sid, r, "included", ctx.run_id, reason, is_new=kind == "refresh")
            n_inc += 1
        else:
            row = store.add_source(sid, r, "excluded", ctx.run_id,
                                   f"Below relevance cutoff (rank {idx + 1}; this run could include {room})")
        stored[id(r)] = row
    for r, reason in failed:
        stored[id(r)] = store.add_source(sid, r, "excluded", ctx.run_id, reason)
    for dup, kept_idx, reason in dups:
        kept = unique[kept_idx]
        kept_row = stored.get(id(kept))
        store.add_source(sid, dup, "duplicate", ctx.run_id, reason, duplicate_of=kept_row["id"] if kept_row else None)
    ctx.counts.update(included=n_inc, duplicates=len(dups), excluded=len(unique) - n_inc)
    if kind == "refresh":
        ctx.counts["new"] = n_inc
    if passed and len(passed) > room:
        ctx.limitations.append(f"{len(passed) - room} sources met the criteria but were excluded by this run's "
                               f"{room}-source limit (lowest keyword relevance); they remain listed as excluded.")
    ctx.finish("screen", f"{len(unique)} unique ({len(dups)} duplicates merged) → {len(passed)} met criteria → "
               f"{n_inc} included" + (f" · {known_new} already in session" if kind == "refresh" else ""))


def stage_evaluate(ctx, session, client) -> None:
    ctx.start("evaluate")
    store, sid = ctx.store, ctx.sid
    included = store.list_sources(sid, status="included")
    if not included:
        ctx.finish("evaluate", "No included sources to evaluate", "warning")
        return
    traced, errs = evaluation.trace_citations(included)
    ctx.limitations += [e for e in errs if e not in ctx.limitations]
    ctx.update("evaluate", f"Citation tracing: {traced} reference lists resolved")
    extractions = {s["label"]: s["extraction_raw"] for s in included if s.get("extraction_raw")}
    todo = [s for s in included if not s.get("extraction_raw")]
    status = "done"
    if client and todo:
        def progress(n, total):
            ctx.update("evaluate", f"Extracting study details: {n}/{total} sources")
        new, errors = evaluation.extract_with_llm(client, session["question"], todo, progress=progress)
        extractions.update(new)
        for s in todo:
            if s["label"] in new:
                s["extraction_raw"] = new[s["label"]]
        if errors:
            ctx.limitations += errors
            status = "warning"
    evaluation.evaluate(included, extractions)
    off_topic = 0
    for s in included:
        data = {k: v for k, v in s.items() if k not in ("status", "reason", "duplicate_of", "is_new", "run_id",
                                                     "added_at", "session_id")}
        if s["evaluation"].get("relevance") == "irrelevant":
            store.update_source(s["id"], data, status="excluded",
                                reason="Judged off-topic from its abstract (model judgment): "
                                       + (s["evaluation"].get("relevance_reason") or "")[:200])
            off_topic += 1
        else:
            store.update_source(s["id"], data)
    ctx.counts["total_included"] = len(included) - off_topic
    ctx.counts["off_topic"] = off_topic
    weights = {}
    for s in included:
        w = s["evaluation"]["weight"]
        weights[w] = weights.get(w, 0) + 1
    ctx.finish("evaluate", f"{len(included) - off_topic} evaluated · weights " +
               ", ".join(f"{k}: {v}" for k, v in sorted(weights.items())) +
               (f" · {off_topic} excluded as off-topic" if off_topic else "") +
               ("" if client else " · LLM extraction skipped (not configured)"), status)


def stage_synthesize(ctx, session, client) -> tuple[dict | None, str | None]:
    ctx.start("synthesize")
    included = ctx.store.list_sources(ctx.sid, status="included")
    if not client:
        ctx.finish("synthesize", "Skipped: Groq API key not configured", "skipped")
        return None, "Groq API key is not configured (see Settings)"
    if not included:
        ctx.finish("synthesize", "Skipped: no included sources", "warning")
        return None, "No sources met the inclusion criteria"
    ctx.update("synthesize", f"Synthesizing {len(included)} sources with {client.model}")
    try:
        out = synthesis.synthesize(client, session["question"], session.get("framing"), included, ctx.limitations)
    except LLMError as e:
        ctx.finish("synthesize", f"Failed: {e}", "failed")
        return None, f"Synthesis failed: {e}"
    n = sum(len(t["findings"]) for t in out["themes"])
    ctx.finish("synthesize", f"{len(out['themes'])} themes, {n} findings, {len(out['gaps'])} gaps · confidence "
               f"{out['confidence']['level']}" + (f" · {len(out['validator_notes'])} validator corrections"
                                                   if out["validator_notes"] else ""))
    return out, None


def stage_report(ctx, synth, synth_err) -> None:
    ctx.start("report")
    store, sid = ctx.store, ctx.sid
    ctx.flush()
    session = store.get_session(sid)
    runs = store.list_runs(sid)
    for r in runs:
        if r["id"] == ctx.run_id:
            r.update(counts=ctx.counts, provider_log=ctx.provider_log, limitations=ctx.limitations,
                     status="partial" if ctx.partial else "completed")
    runs = [r for r in runs if r["kind"] != "reanalysis"
            and (r["status"] in ("completed", "partial") or r["id"] == ctx.run_id)]
    sources = store.list_sources(sid)
    version = len(store.list_reports(sid)) + 1
    md = report.build_report(session, runs, sources, synth, synth_err, version)
    store.add_report(sid, ctx.run_id, md, synth, synth["confidence"]["level"] if synth else None)
    ctx.finish("report", f"Report v{version} saved")
