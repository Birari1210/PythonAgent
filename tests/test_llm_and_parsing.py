"""Groq client behaviour (with a mock transport) and provider response parsing."""
import json

import httpx
import pytest

from app import llm
from app.models import FramingOut
from app.retrieval.providers import ArXiv, openalex_abstract, parse_openalex_work


def _client(handler, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_testkey_not_real_123456")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    return llm.GroqClient("test-model", transport=httpx.MockTransport(handler), max_retries=2)


def _ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


FRAMING = json.dumps({"subject": "x", "queries": ["a b"], "keywords": ["a"]})


def test_missing_key_raises_setup_help():
    c = llm.GroqClient("m")
    with pytest.raises(llm.LLMNotConfigured) as e:
        c.chat([{"role": "user", "content": "hi"}])
    assert "console.groq.com" in str(e.value)


def test_structured_success_and_key_sent_only_in_header(monkeypatch):
    seen = {}

    def h(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.content.decode()
        return _ok(FRAMING)

    out = _client(h, monkeypatch).structured("sys", "user", FramingOut)
    assert out.queries == ["a b"]
    assert seen["auth"].startswith("Bearer gsk_") and "gsk_" not in seen["body"]


def test_rate_limit_retry_then_success(monkeypatch):
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "2"}) if calls["n"] == 1 else _ok(FRAMING)

    assert _client(h, monkeypatch).structured("s", "u", FramingOut).subject == "x"
    assert calls["n"] == 2


def test_rate_limit_gives_up_with_clear_error(monkeypatch):
    c = _client(lambda req: httpx.Response(429), monkeypatch)
    with pytest.raises(llm.LLMError, match="rate limit"):
        c.chat([{"role": "user", "content": "x"}])


def test_auth_error_not_retried(monkeypatch):
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "bad"}})

    with pytest.raises(llm.LLMError, match="401"):
        _client(h, monkeypatch).chat([{"role": "user", "content": "x"}])
    assert calls["n"] == 1


def test_invalid_json_repaired_once(monkeypatch):
    replies = iter(["not json at all", "```json\n" + FRAMING + "\n```"])
    assert _client(lambda r: _ok(next(replies)), monkeypatch).structured("s", "u", FramingOut).queries == ["a b"]


def test_invalid_output_twice_fails(monkeypatch):
    with pytest.raises(llm.LLMError, match="valid FramingOut"):
        _client(lambda r: _ok('{"subject": "x"}'), monkeypatch).structured("s", "u", FramingOut)


def test_timeout_reported(monkeypatch):
    def h(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(llm.LLMError, match="timed out"):
        _client(h, monkeypatch).chat([{"role": "user", "content": "x"}])


# ------------------------------------------------------------------ parsing
def test_openalex_abstract_and_type_mapping():
    assert openalex_abstract({"world": [1], "hello": [0]}) == "hello world"
    rec = parse_openalex_work({
        "id": "https://openalex.org/W1", "display_name": "A systematic review of X", "publication_year": 2020,
        "doi": "https://doi.org/10.1/ABC", "type": "article", "primary_location": {"source": {"display_name": "J", "type": "journal"}},
        "authorships": [{"author": {"display_name": "Ann Lee"}}], "referenced_works": ["W2"], "is_retracted": False})
    assert rec["doi"] == "10.1/abc" and rec["source_type"] == "review" and rec["references"] == ["W2"]
    pre = parse_openalex_work({"id": "W3", "display_name": "Model", "type": "article",
                               "primary_location": {"source": {"display_name": "PubMed Central", "type": "repository"}}})
    assert pre["source_type"] != "preprint"  # repositories hosting published articles are not preprints


ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/2301.00001v2</id><published>2023-01-02T00:00:00Z</published>
<title>Retrieval  Augmented
 Generation</title><summary>We study RAG.</summary><author><name>A. Author</name></author>
<link title="pdf" href="http://arxiv.org/pdf/2301.00001v2"/></entry></feed>"""


def test_arxiv_parse():
    [rec] = ArXiv.parse(ARXIV_XML)
    assert rec["title"] == "Retrieval Augmented Generation" and rec["year"] == 2023
    assert rec["source_type"] == "preprint" and rec["provider_ids"]["arxiv"] == "2301.00001v2"
    assert "submittedDate:[2019" in ArXiv.build_query("rag hallucination", 2019, None)
