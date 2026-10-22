"""End-to-end workflow through the HTTP API with fake providers and a fake Groq transport."""


def _run(client, body):
    r = client.post("/api/sessions", json=body)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    client.app_ref.state.jobs.wait(sid, timeout=30)
    return sid, client.get(f"/api/sessions/{sid}").json()


BODY = {"question": "Does intermittent fasting improve weight loss?", "year_from": 2015,
        "providers": ["openalex", "crossref"]}


def test_full_workflow_with_llm(client, fake_providers, fake_groq):
    sid, s = _run(client, BODY)
    assert s["status"] == "partial"  # crossref failed -> honest partial result
    run = s["runs"][-1]
    assert [st["status"] for st in run["stages"]][0] == "done"
    assert any(p["error"] for p in run["provider_log"])
    assert any("Crossref" in l for l in run["limitations"])

    sources = client.get(f"/api/sessions/{sid}/sources").json()
    by_status = {}
    for x in sources:
        by_status.setdefault(x["status"], []).append(x)
    assert len(by_status["duplicate"]) == 1
    assert any("2010" in x["reason"] for x in by_status["excluded"])
    review = next(x for x in by_status["included"] if x["source_type"] == "review")
    assert review["evaluation"]["independent"] is False  # cites A and B in set
    assert all(x["evaluation"]["sample_size_verified"] for x in by_status["included"])

    rep = client.get(f"/api/sessions/{sid}/report").json()
    md = rep["markdown"]
    for section in ("## 1. Research question", "## 2. Search methodology", "## 3. Source evaluation table",
                    "## 4. Key findings", "## 5. Agreement", "## 6. Evidence gaps", "## 7. Overall confidence",
                    "## 8. Sources", "## 9. Summary"):
        assert section in md
    assert "](https://example.org/A)" in md  # clickable citation
    findings_section = md.split("## 4.")[1].split("## 5.")[0]
    assert "Invented claim" not in findings_section  # fabricated citation dropped by validator
    assert "Dropped statement with no valid supporting source" in md  # ...and the drop is disclosed
    assert "not a PRISMA-compliant" in md
    assert rep["synthesis"]["confidence"]["model_level"] == "High"
    assert rep["confidence"] in ("Low", "Moderate", "High")

    md_export = client.get(f"/api/sessions/{sid}/report/export?format=md")
    assert md_export.status_code == 200 and "attachment" in md_export.headers["content-disposition"]
    pdf = client.get(f"/api/sessions/{sid}/report/export?format=pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"

    fu = client.post(f"/api/sessions/{sid}/followups", json={"question": "How long were trials?"}).json()
    assert "S42" not in fu["cited"] and "citation removed" in fu["answer"] and fu["not_covered"]


def test_without_groq_key_reports_honestly(client, fake_providers):
    sid, s = _run(client, BODY)
    stages = {st["name"]: st for st in s["runs"][-1]["stages"]}
    assert stages["synthesize"]["status"] == "skipped"
    assert "Heuristic" in stages["frame"]["detail"]
    rep = client.get(f"/api/sessions/{sid}/report").json()
    assert rep["synthesis"] is None and rep["confidence"] is None
    assert "Not synthesized" in rep["markdown"] and "Not assessed" in rep["markdown"]
    r = client.post(f"/api/sessions/{sid}/followups", json={"question": "anything?"})
    assert r.status_code == 400 and "GROQ_API_KEY" in r.json()["detail"]


def test_status_and_settings_never_leak_secrets(client, fake_groq):
    r = client.put("/api/settings", json={"secrets": {"TAVILY_API_KEY": "tvly-secretvalue123"}, "results_per_query": 7})
    assert r.status_code == 200
    for path in ("/api/settings", "/api/status"):
        text = client.get(path).text
        assert "gsk_testkey" not in text and "tvly-secretvalue123" not in text
    s = client.get("/api/settings").json()
    assert s["secrets"]["TAVILY_API_KEY"]["configured"] and s["settings"]["results_per_query"] == 7
    assert s["secrets"]["GROQ_API_KEY"]["configured"]


def test_session_crud_search_and_validation(client, fake_providers, fake_groq):
    assert client.post("/api/sessions", json={"question": "hi"}).status_code == 422
    assert client.post("/api/sessions", json={"question": "valid question", "year_from": 2020, "year_to": 2010}).status_code == 422
    assert client.post("/api/sessions", json={"question": "valid question", "providers": ["google"]}).status_code == 422
    sid, _ = _run(client, BODY)
    assert client.patch(f"/api/sessions/{sid}", json={"title": "Fasting review"}).json()["title"] == "Fasting review"
    found = client.get("/api/search?q=fasting").json()
    assert found["sessions"] and found["sources"] and found["reports"]
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_refresh_marks_only_new_sources(client, fake_providers, fake_groq):
    sid, _ = _run(client, BODY)
    before = len(client.get(f"/api/sessions/{sid}/sources").json())
    client.post(f"/api/sessions/{sid}/run?kind=refresh")
    client.app_ref.state.jobs.wait(sid, timeout=30)
    s = client.get(f"/api/sessions/{sid}").json()
    run = s["runs"][-1]
    assert run["kind"] == "refresh" and run["counts"]["new"] == 0 and run["counts"]["already_known"] > 0
    assert len(client.get(f"/api/sessions/{sid}/sources").json()) == before
    assert len(client.get(f"/api/sessions/{sid}/reports").json()) == 2


def test_manual_upload_and_reanalysis(client, fake_providers, fake_groq):
    sid, _ = _run(client, BODY)
    content = b"# Field notes on fasting\n\nAbstract: An observational cohort of 340 workers who fasted showed weight change over a year. Keywords: fasting"
    r = client.post(f"/api/sessions/{sid}/sources/upload", files={"file": ("notes.md", content, "text/markdown")})
    assert r.status_code == 201, r.text
    assert r.json()["origin"] == "manual-upload" and r.json()["status"] == "included"
    dup = client.post(f"/api/sessions/{sid}/sources/upload", files={"file": ("notes.md", content, "text/markdown")})
    assert dup.status_code == 409
    bad = client.post(f"/api/sessions/{sid}/sources/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 400
    client.post(f"/api/sessions/{sid}/run?kind=reanalysis")
    client.app_ref.state.jobs.wait(sid, timeout=30)
    srcs = client.get(f"/api/sessions/{sid}/sources?status=included").json()
    assert all(x.get("evaluation") for x in srcs)


def test_url_import_blocks_private_addresses(client):
    sid = client.post("/api/sessions?start=false", json={"question": "valid question here"}).json()["id"]
    r = client.post(f"/api/sessions/{sid}/sources/url", json={"url": "http://127.0.0.1:8000/admin"})
    assert r.status_code == 400 and "private" in r.json()["detail"]


def test_llm_failure_during_synthesis_is_reported(client, fake_providers, fake_groq):
    fake_groq.synthesis_override = lambda ids: {"bad": "shape"}
    sid, s = _run(client, BODY)
    stages = {st["name"]: st for st in s["runs"][-1]["stages"]}
    assert stages["synthesize"]["status"] == "failed"
    rep = client.get(f"/api/sessions/{sid}/report").json()
    assert "Synthesis failed" in rep["markdown"]


def test_refresh_adds_new_sources_even_at_cap(client, fake_providers, fake_groq, monkeypatch):
    from conftest import make_record
    from app.retrieval.base import ProviderResult
    good, _ = fake_providers
    sid, s = _run(client, {**BODY, "max_sources": 3})
    assert s["runs"][-1]["counts"]["included"] == 3

    def newer(self, q):
        rec = make_record("NEW", year=2026, title="A 2026 trial of alternate-day fasting and weight loss outcomes")
        rec["matched_queries"] = [q.text]
        return ProviderResult(provider="openalex", query=q.text, records=[rec])

    monkeypatch.setattr(good, "search", newer)
    client.post(f"/api/sessions/{sid}/run?kind=refresh")
    client.app_ref.state.jobs.wait(sid, timeout=30)
    run = client.get(f"/api/sessions/{sid}").json()["runs"][-1]
    assert run["counts"]["new"] == 1
    new = [x for x in client.get(f"/api/sessions/{sid}/sources").json() if x["is_new"]]
    assert len(new) == 1 and new[0]["status"] == "included" and new[0]["year"] == 2026
