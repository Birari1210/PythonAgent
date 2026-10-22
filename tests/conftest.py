import json
import os
import re
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own database/.env and no real API keys."""
    monkeypatch.setenv("RS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RS_DB_PATH", str(tmp_path / "test.db"))
    from app import config
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    for k in list(config.SECRET_KEYS):
        monkeypatch.delenv(k, raising=False)
    yield


def make_record(label_hint: str, **kw) -> dict:
    from app.retrieval.base import empty_record
    rec = empty_record(kw.pop("provider", "openalex"))
    rec.update(title=f"Study {label_hint} on intermittent fasting and weight loss outcomes", year=2021,
               venue="Journal of Testing", source_type="journal-article",
               abstract=f"We conducted a randomized controlled trial (n=1,200 adults). Fasting reduced weight. {label_hint}",
               doi=f"10.1000/{label_hint.lower()}", url=f"https://example.org/{label_hint}")
    rec.update(kw)
    return rec


class FakeGroq:
    """httpx transport that answers like Groq based on which prompt it receives."""

    def __init__(self, synthesis_override=None, fail_with: int | None = None):
        self.synthesis_override = synthesis_override
        self.fail_with = fail_with
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"error": {"message": "fail"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile", "active": True}]})
        body = json.loads(request.content)
        self.requests.append(body)
        system, user = body["messages"][0]["content"], body["messages"][-1]["content"]
        if "Turn the user's research question" in system:
            out = {"subject": "adults", "comparison": "fasting vs continuous restriction", "outcome": "weight loss",
                   "scope": "human trials", "keywords": ["intermittent fasting", "weight loss"],
                   "queries": ["intermittent fasting weight loss", "time restricted eating obesity"],
                   "ambiguities": [], "sufficient_evidence": "multiple RCTs"}
        elif "extract study information" in system:
            ids = re.findall(r"\[source_id: (S\d+)\]", user)
            out = {"items": [{"source_id": i, "study_design": "randomized controlled trial", "sample_size": "n=1,200 adults",
                              "key_findings": [f"Fasting reduced weight ({i})"], "limitations": ["short follow-up"],
                              "conflicts_or_funding": None, "relevance": "high", "relevance_reason": "on topic"}
                             for i in ids]}
        elif "skeptical research methodologist" in system:
            ids = re.findall(r"^\[(S\d+)\]", user, re.M)
            out = self.synthesis_override(ids) if self.synthesis_override else {
                "themes": [{"name": "Weight outcomes", "findings": [
                    {"statement": "Fasting reduces weight in the short term", "category": "established",
                     "supporting": ids[:2], "contradicting": [], "explanation": "two trials agree"},
                    {"statement": "Invented claim", "category": "single_study", "supporting": ["S99"], "contradicting": []}]}],
                "gaps": [{"question": "Long-term effects beyond 12 months", "note": "not studied"}],
                "disagreements": [], "dependencies": [],
                "confidence": {"level": "High", "rationale": "consistent trials"},
                "summary": "Short-term benefit is supported.", "practical_conclusions": ["Consider as one option"]}
        elif "follow-up question" in system:
            out = {"answer": "Per [S1] and [S42], fasting reduced weight.", "cited": ["S1", "S42"], "not_covered": "Long-term data"}
        else:
            out = {}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})


@pytest.fixture
def fake_groq(monkeypatch):
    fake = FakeGroq()
    monkeypatch.setenv("GROQ_API_KEY", "gsk_testkey_not_real_123456")
    from app import llm
    original = llm.client_from_settings

    def patched(settings):
        c = original(settings)
        c._transport = httpx.MockTransport(fake)
        c.max_retries = 1
        return c

    monkeypatch.setattr(llm, "client_from_settings", patched)
    monkeypatch.setattr("app.research.pipeline.llm_mod.client_from_settings", patched)
    return fake


@pytest.fixture
def fake_providers(monkeypatch):
    """Replace network providers with deterministic fakes; one provider fails to exercise partial results."""
    from app.retrieval import providers
    from app.retrieval.base import ProviderResult

    class GoodProvider(providers.Provider):
        name, label = "openalex", "OpenAlex"

        def search(self, q):
            recs = [make_record("A", openalex_id="W1", references=["W2"]),
                    make_record("B", openalex_id="W2"),
                    make_record("C", year=2010),  # outside date range when year_from set
                    make_record("D", title="Study A on intermittent fasting and weight loss outcomes", doi="10.1000/a",
                                provider="semantic_scholar"),  # duplicate of A
                    make_record("E", source_type="review", title="A systematic review of fasting and weight loss",
                                openalex_id="W3", references=["W1", "W2"])]
            for r in recs:
                r["matched_queries"] = [q.text]
            return ProviderResult(provider=self.name, query=q.text, records=recs)

    class BadProvider(providers.Provider):
        name, label = "crossref", "Crossref"

        def search(self, q):
            return ProviderResult(provider=self.name, query=q.text, error="crossref: rate limited (429)")

    monkeypatch.setitem(providers.ALL_PROVIDERS, "openalex", GoodProvider)
    monkeypatch.setitem(providers.ALL_PROVIDERS, "crossref", BadProvider)
    monkeypatch.setattr("app.research.evaluation.OpenAlex.lookup_dois", lambda self, dois: {})
    return GoodProvider, BadProvider


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.storage import Storage
    app = create_app(Storage(tmp_path / "api.db"))
    with TestClient(app) as c:
        c.app_ref = app
        yield c
