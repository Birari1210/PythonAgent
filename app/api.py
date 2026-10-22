"""HTTP API. The frontend talks only to these routes; secrets never appear in responses."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from . import config
from . import llm as llm_mod
from .llm import LLMError
from .models import (FollowupRequest, FrameRequest, RenameRequest, ResearchRequest, SettingsUpdate,
                     UrlSourceRequest)
from .research import followup, framing
from .research.report import markdown_to_pdf
from .retrieval import manual
from .retrieval.providers import provider_catalog

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _store(request: Request):
    return request.app.state.store


def _jobs(request: Request):
    return request.app.state.jobs


def _session_or_404(request: Request, sid: str) -> dict:
    s = _store(request).get_session(sid)
    if not s:
        raise HTTPException(404, "Research session not found")
    return s


def _not_running(request: Request, sid: str):
    if _jobs(request).is_running(sid):
        raise HTTPException(409, "A research run is in progress for this session; wait for it to finish or cancel it")


# ------------------------------------------------------------------ status & settings
@router.get("/status")
def status(request: Request):
    settings = _store(request).get_settings()
    configured = bool(config.get_secret("GROQ_API_KEY"))
    return {"llm": {"configured": configured, "model": settings["groq_model"],
                    "setup_help": None if configured else llm_mod.SETUP_HELP},
            "providers": provider_catalog(), "enabled_providers": settings["providers"]}


@router.get("/settings")
def get_settings(request: Request):
    return {"settings": _store(request).get_settings(), "secrets": config.secret_status(),
            "providers": provider_catalog(), "storage": {"database": str(config.db_path())}}


@router.put("/settings")
def put_settings(request: Request, body: SettingsUpdate):
    for name, value in body.secrets.items():
        if name not in config.SECRET_KEYS:
            raise HTTPException(400, f"Unknown secret {name}")
        try:
            config.write_secret(name, value)
        except ValueError as e:
            raise HTTPException(400, str(e))
    values = body.model_dump(exclude={"secrets"}, exclude_none=True)
    settings = _store(request).update_settings(values)
    return {"settings": settings, "secrets": config.secret_status(), "providers": provider_catalog()}


@router.get("/settings/models")
def list_models(request: Request):
    client = llm_mod.client_from_settings(_store(request).get_settings())
    try:
        return {"models": client.list_models()}
    except LLMError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not list Groq models: {type(e).__name__}")


@router.post("/settings/test-llm")
def test_llm(request: Request):
    client = llm_mod.client_from_settings(_store(request).get_settings())
    try:
        reply = client.chat([{"role": "user", "content": 'Reply with the JSON {"ok": true}'}], max_tokens=20)
        return {"ok": True, "model": client.model, "reply": reply[:100]}
    except LLMError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ framing preview
@router.post("/frame")
def frame(request: Request, body: FrameRequest):
    store = _store(request)
    client = llm_mod.client_from_settings(store.get_settings())
    warning = None
    if client.configured:
        try:
            fr = framing.llm_frame(client, body.question)
        except LLMError as e:
            fr, warning = framing.heuristic_frame(body.question), f"LLM framing failed: {e}. Showing keyword-based framing."
    else:
        fr, warning = framing.heuristic_frame(body.question), "Groq not configured — keyword-based framing shown."
    related = {}
    for kw in fr.get("keywords", [])[:4]:
        for s in store.search(kw, limit=10)["sessions"]:
            related[s["id"]] = s
    return {"framing": fr, "warning": warning, "related_sessions": list(related.values())[:6]}


# ------------------------------------------------------------------ sessions
@router.get("/sessions")
def list_sessions(request: Request):
    jobs = _jobs(request)
    out = _store(request).list_sessions()
    for s in out:
        s["running"] = jobs.is_running(s["id"])
    return out


@router.post("/sessions", status_code=201)
def create_session(request: Request, body: ResearchRequest, start: bool = True):
    store = _store(request)
    for other in body.reuse_session_ids:
        if not store.get_session(other):
            raise HTTPException(400, f"Session to reuse not found: {other}")
    params = body.model_dump(exclude={"question", "title", "framing"})
    s = store.create_session(body.question, body.title, params)
    if body.framing:
        store.update_session(s["id"], framing=body.framing)
    if start:
        _jobs(request).start(s["id"], "initial")
    return _session_detail(request, s["id"])


def _session_detail(request: Request, sid: str) -> dict:
    s = _session_or_404(request, sid)
    s["runs"] = _store(request).list_runs(sid)
    s["running"] = _jobs(request).is_running(sid)
    return s


@router.get("/sessions/{sid}")
def get_session(request: Request, sid: str):
    return _session_detail(request, sid)


@router.patch("/sessions/{sid}")
def rename_session(request: Request, sid: str, body: RenameRequest):
    _session_or_404(request, sid)
    _store(request).update_session(sid, title=body.title.strip())
    return _session_detail(request, sid)


@router.delete("/sessions/{sid}")
def delete_session(request: Request, sid: str):
    _not_running(request, sid)
    if not _store(request).delete_session(sid):
        raise HTTPException(404, "Research session not found")
    return {"deleted": sid}


@router.post("/sessions/{sid}/run")
def run_session(request: Request, sid: str, kind: str = Query("auto", pattern="^(auto|initial|refresh|reanalysis)$")):
    s = _session_or_404(request, sid)
    runs = _store(request).list_runs(sid)
    if kind == "auto":
        kind = "refresh" if any(r["kind"] == "initial" and r["status"] in ("completed", "partial") for r in runs) else "initial"
    if kind == "refresh" and not s.get("last_run_at"):
        kind = "initial"
    if kind == "reanalysis" and not s["source_counts"].get("included"):
        raise HTTPException(400, "No included sources to analyze")
    try:
        _jobs(request).start(sid, kind)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return _session_detail(request, sid)


@router.post("/sessions/{sid}/cancel")
def cancel(request: Request, sid: str):
    _session_or_404(request, sid)
    return {"cancelling": _jobs(request).cancel(sid)}


# ------------------------------------------------------------------ sources
@router.get("/sessions/{sid}/sources")
def list_sources(request: Request, sid: str, status: str | None = Query(None, pattern="^(included|excluded|duplicate)$")):
    _session_or_404(request, sid)
    return _store(request).list_sources(sid, status)


def _add_manual(request: Request, sid: str, rec: dict) -> dict:
    store = _store(request)
    existing = store.list_sources(sid)
    from .research.screening import same_work
    for e in existing:
        if same_work(e, rec):
            raise HTTPException(409, f"This source is already in the session as {e['label']} ({e['status']})")
    row = store.add_source(sid, rec, "included", None, "Added manually by user", is_new=True)
    store.update_session(sid)
    return row


@router.post("/sessions/{sid}/sources/url", status_code=201)
def add_source_url(request: Request, sid: str, body: UrlSourceRequest):
    _session_or_404(request, sid)
    _not_running(request, sid)
    try:
        rec = manual.record_from_url(body.url)
    except manual.ManualSourceError as e:
        raise HTTPException(400, str(e))
    return _add_manual(request, sid, rec)


@router.post("/sessions/{sid}/sources/upload", status_code=201)
async def add_source_upload(request: Request, sid: str, file: UploadFile = File(...)):
    _session_or_404(request, sid)
    _not_running(request, sid)
    data = await file.read(manual.MAX_UPLOAD_BYTES + 1)
    name = re.sub(r"[^\w.\- ]", "_", file.filename or "upload")[:150]
    try:
        rec = manual.record_from_document(name, data)
    except manual.ManualSourceError as e:
        raise HTTPException(400, str(e))
    return _add_manual(request, sid, rec)


@router.patch("/sessions/{sid}/sources/{src_id}")
def set_source_status(request: Request, sid: str, src_id: str,
                      status: str = Query(..., pattern="^(included|excluded)$")):
    _not_running(request, sid)
    store = _store(request)
    src = store.get_source(src_id)
    if not src or src["session_id"] != sid:
        raise HTTPException(404, "Source not found")
    store.update_source(src_id, status=status, reason=f"Manually {status} by user")
    store.update_session(sid)
    return store.get_source(src_id)


# ------------------------------------------------------------------ reports
@router.get("/sessions/{sid}/reports")
def list_reports(request: Request, sid: str):
    _session_or_404(request, sid)
    return _store(request).list_reports(sid)


@router.get("/sessions/{sid}/report")
def get_report(request: Request, sid: str, report_id: int | None = None):
    _session_or_404(request, sid)
    rep = _store(request).get_report(sid, report_id)
    if not rep:
        raise HTTPException(404, "No report yet for this session")
    return rep


@router.get("/sessions/{sid}/report/export")
def export_report(request: Request, sid: str, format: str = Query("md", pattern="^(md|pdf)$"),
                  report_id: int | None = None):
    s = _session_or_404(request, sid)
    rep = _store(request).get_report(sid, report_id)
    if not rep:
        raise HTTPException(404, "No report yet for this session")
    base = re.sub(r"[^A-Za-z0-9]+", "-", s["title"]).strip("-")[:60] or "report"
    fname = f"{base}-v{rep['id']}"
    if format == "md":
        return Response(rep["markdown"], media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fname}.md"'})
    try:
        pdf = markdown_to_pdf(rep["markdown"])
    except Exception as e:
        log.exception("PDF export failed")
        raise HTTPException(500, f"PDF export failed ({type(e).__name__}). Markdown export is still available.")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.pdf"'})


# ------------------------------------------------------------------ follow-ups
@router.get("/sessions/{sid}/followups")
def list_followups(request: Request, sid: str):
    _session_or_404(request, sid)
    return _store(request).list_followups(sid)


@router.post("/sessions/{sid}/followups", status_code=201)
def ask_followup(request: Request, sid: str, body: FollowupRequest):
    s = _session_or_404(request, sid)
    store = _store(request)
    client = llm_mod.client_from_settings(store.get_settings())
    if not client.configured:
        raise HTTPException(400, llm_mod.SETUP_HELP)
    sources = store.list_sources(sid, "included")
    if not sources:
        raise HTTPException(400, "This session has no included sources to answer from")
    rep = store.get_report(sid)
    try:
        out = followup.answer(client, body.question, s["question"], sources, rep["synthesis"] if rep else None)
    except LLMError as e:
        raise HTTPException(502, str(e))
    return store.add_followup(sid, body.question, out["answer"], out["cited"], out["not_covered"])


# ------------------------------------------------------------------ search
@router.get("/search")
def search(request: Request, q: str = Query(..., min_length=2, max_length=200)):
    return _store(request).search(q)
