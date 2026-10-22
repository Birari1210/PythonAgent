"""Unit tests for screening, evaluation, synthesis validation and confidence calibration."""
from conftest import make_record

from app.models import SynthesisOut
from app.research import evaluation, screening, synthesis


# ------------------------------------------------------------------ screening
def test_dedupe_by_doi_and_title_merges_provenance():
    a = make_record("A")
    b = make_record("B", doi="10.1000/a", provider="crossref", title="Totally different title here for sure")
    c = make_record("C", doi=None, title=a["title"].upper() + ".", provider="semantic_scholar")
    unique, dups = screening.dedupe([a, b, c])
    assert len(unique) == 1
    assert {d[2] for d in dups} == {"same DOI", "near-identical title"}
    assert set(unique[0]["providers"]) == {"openalex", "crossref", "semantic_scholar"}


def test_dedupe_prefers_published_over_preprint():
    pre = make_record("P", source_type="preprint", doi=None, venue="arXiv", title="Deep nets for fasting prediction study")
    pub = make_record("Q", source_type="journal-article", doi="10.1/x", title="Deep nets for fasting prediction study")
    unique, dups = screening.dedupe([pre, pub])
    assert len(unique) == 1 and unique[0]["source_type"] == "journal-article"
    assert dups[0][2] == "preprint and published version of the same work"
    assert unique[0]["versions"][0]["type"] == "preprint"


def test_different_works_not_merged():
    unique, _ = screening.dedupe([make_record("A"), make_record("B", title="Exercise and sleep quality in older adults")])
    assert len(unique) == 2


def test_screen_criteria():
    crit = {"year_from": 2015, "year_to": 2024, "exclude_terms": ["mice"], "include_terms": [], "source_types": []}
    assert screening.screen(make_record("A"), crit)[0]
    assert not screening.screen(make_record("A", year=2010), crit)[0]
    assert "exclusion term" in screening.screen(make_record("A", abstract="A study in mice"), crit)[1]
    assert not screening.screen(make_record("A", is_retracted=True), crit)[0]
    assert not screening.screen(make_record("A"), {**crit, "source_types": ["review"]})[0]
    assert not screening.screen(make_record("A"), {**crit, "include_terms": ["insulin"]})[0]
    ok, reason = screening.screen(make_record("A", year=None), crit)
    assert ok and "year unknown" in reason


def test_relevance_prefers_title_matches():
    kw = ["fasting", "weight"]
    hi = screening.relevance_score(make_record("A"), kw)
    lo = screening.relevance_score(make_record("B", title="Unrelated sleep paper", abstract="sleep only"), kw)
    assert hi > lo


# ------------------------------------------------------------------ evaluation
def test_tiers_and_peer_review_labels_do_not_overclaim():
    assert evaluation.evidence_tier(make_record("A")) == "primary"
    assert evaluation.evidence_tier(make_record("A", source_type="review")) == "secondary"
    assert evaluation.evidence_tier(make_record("A", source_type="web")) == "tertiary"
    assert "not independently verified" in evaluation.peer_review_status(make_record("A"))
    assert "not peer reviewed" in evaluation.peer_review_status(make_record("A", source_type="preprint"))


def test_sample_size_must_appear_in_abstract():
    assert evaluation.verify_sample_size("n=1,200", "trial with 1,200 adults")[1] is True
    kept, ok, note = evaluation.verify_sample_size("n=5000", "trial with 1,200 adults")
    assert kept is None and not ok and "discarded" in note


def _evaluated(records, extractions=None):
    for i, r in enumerate(records, 1):
        r["label"] = f"S{i}"
    evaluation.evaluate(records, extractions or {})
    return records


def test_review_citing_set_members_is_not_independent():
    a = make_record("A", openalex_id="W1")
    rev = make_record("R", source_type="review", openalex_id="W9", references=["W1"])
    a, rev = _evaluated([a, rev])
    assert rev["evaluation"]["independent"] is False
    assert "S1" in rev["evaluation"]["independence"]
    assert a["evaluation"]["cited_by_in_set"] == ["S2"]


def test_weight_rules():
    ex = {"S1": {"study_design": "randomized controlled trial", "sample_size": "n=1,200", "relevance": "high"},
          "S2": {"study_design": "randomized controlled trial", "sample_size": "n=1,200", "relevance": "high"},
          "S3": {"study_design": "opinion", "relevance": "high"}}
    rct, pre, op = _evaluated([make_record("A"), make_record("B", source_type="preprint"),
                               make_record("C", source_type="web")], ex)
    assert rct["evaluation"]["weight"] == "High"
    assert pre["evaluation"]["weight"] == "Moderate"  # preprint cap
    assert op["evaluation"]["weight"] in ("Low", "None")
    unassessed = _evaluated([make_record("A")])[0]
    assert unassessed["evaluation"]["method_quality"] == "Not assessable"
    assert unassessed["evaluation"]["extraction"].startswith("not extracted")


# ------------------------------------------------------------------ synthesis validation
def _sources(n, weight="High", dependent_pairs=()):
    out = []
    for i in range(1, n + 1):
        out.append({"label": f"S{i}", "source_type": "journal-article",
                    "evaluation": {"weight": weight, "independent": True, "cites_in_set": []}})
    for a, b in dependent_pairs:
        src = next(s for s in out if s["label"] == a)
        src["evaluation"].update(independent=False, cites_in_set=[b])
    return out


def _raw(findings, level="High"):
    return SynthesisOut.model_validate({"themes": [{"name": "T", "findings": findings}],
                                        "confidence": {"level": level, "rationale": "r"}, "summary": "s"})


def test_unknown_citations_removed_and_unsupported_dropped():
    out = synthesis.validate(_raw([
        {"statement": "ok", "category": "single_study", "supporting": ["S1", "S77"]},
        {"statement": "fabricated", "category": "established", "supporting": ["S88"]}]), _sources(3))
    statements = [f["statement"] for t in out["themes"] for f in t["findings"]]
    assert statements == ["ok"]
    assert out["themes"][0]["findings"][0]["supporting"] == ["S1"]
    assert any("S77" in n for n in out["validator_notes"])


def test_established_requires_independent_lines():
    srcs = _sources(3, dependent_pairs=[("S2", "S1")])  # S2 repeats S1
    out = synthesis.validate(_raw([{"statement": "x", "category": "established", "supporting": ["S1", "S2"]}]), srcs)
    f = out["themes"][0]["findings"][0]
    assert f["category"] == "single_study" and f["independent_lines"] == 1 and "Downgraded" in f["validator_note"]
    ok = synthesis.validate(_raw([{"statement": "x", "category": "established", "supporting": ["S1", "S3"]}]), srcs)
    assert ok["themes"][0]["findings"][0]["category"] == "established"


def test_model_inferred_dependencies_count():
    srcs = _sources(3)
    raw = SynthesisOut.model_validate({
        "themes": [{"name": "T", "findings": [{"statement": "x", "category": "established", "supporting": ["S1", "S2"]}]}],
        "dependencies": [{"source": "S2", "depends_on": "S1", "reason": "same dataset"}],
        "confidence": {"level": "High", "rationale": "r"}, "summary": "s"})
    out = synthesis.validate(raw, srcs)
    assert out["themes"][0]["findings"][0]["category"] == "single_study"
    assert out["dependencies"][0]["provenance"] == "model-inferred"


def test_confidence_capped_by_weakest_source_and_coverage():
    srcs = _sources(4)
    srcs[1]["evaluation"]["weight"] = "Low"
    out = synthesis.validate(_raw([{"statement": "x", "category": "established", "supporting": ["S1", "S2", "S3"]}]), srcs)
    assert out["confidence"]["level"] == "Low" and out["confidence"]["model_level"] == "High"
    few = synthesis.validate(_raw([{"statement": "x", "category": "single_study", "supporting": ["S1"]}]), _sources(2))
    assert few["confidence"]["level"] == "Low"
    no_est = synthesis.validate(_raw([{"statement": "x", "category": "single_study", "supporting": ["S1"]}]), _sources(5))
    assert no_est["confidence"]["level"] == "Moderate"


def test_contested_without_contradicting_is_recategorized():
    out = synthesis.validate(_raw([{"statement": "x", "category": "contested", "supporting": ["S1"]}]), _sources(3))
    assert out["themes"][0]["findings"][0]["category"] == "single_study"


def test_similar_titles_with_different_dois_or_numbers_are_distinct():
    a = make_record("A")
    b = make_record("B")  # same title pattern, different DOI
    unique, _ = screening.dedupe([a, b])
    assert len(unique) == 2
    p1 = make_record("X", doi=None, title="Fasting outcomes in adults: results from cohort part 1")
    p2 = make_record("Y", doi=None, title="Fasting outcomes in adults: results from cohort part 2")
    assert len(screening.dedupe([p1, p2])[0]) == 2
