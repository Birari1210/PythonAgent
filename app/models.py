"""Pydantic schemas: API inputs and validated LLM outputs."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SOURCE_TYPES = ["journal-article", "review", "preprint", "conference", "book", "dataset", "documentation", "web", "other"]
PROVIDERS = ["openalex", "semantic_scholar", "arxiv", "europepmc", "crossref", "tavily"]


def _clean_terms(values: list[str]) -> list[str]:
    out = []
    for v in values or []:
        v = " ".join(str(v).split())[:120]
        if v and v.lower() not in {o.lower() for o in out}:
            out.append(v)
    return out[:20]


# ---------------------------------------------------------------- API inputs
class ResearchRequest(BaseModel):
    question: str = Field(min_length=5, max_length=1500)
    title: Optional[str] = Field(default=None, max_length=200)
    year_from: Optional[int] = Field(default=None, ge=1800, le=2100)
    year_to: Optional[int] = Field(default=None, ge=1800, le=2100)
    providers: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)  # empty = all types
    include_terms: list[str] = Field(default_factory=list)
    exclude_terms: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)  # user-approved queries; empty = derive
    max_sources: Optional[int] = Field(default=None, ge=3, le=60)
    reuse_session_ids: list[str] = Field(default_factory=list, max_length=5)
    framing: Optional[dict] = None  # framing previewed in the UI, so the run does not re-frame

    @field_validator("framing")
    @classmethod
    def _framing(cls, v):
        if not v:
            return None
        keep = {}
        for k in ("subject", "comparison", "outcome", "scope", "sufficient_evidence", "method"):
            if v.get(k) is not None:
                keep[k] = str(v[k])[:600]
        for k in ("keywords", "queries", "ambiguities"):
            keep[k] = [str(x)[:300] for x in (v.get(k) or [])][:10]
        return keep

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 5:
            raise ValueError("Question is too short")
        return v

    @field_validator("include_terms", "exclude_terms", "queries")
    @classmethod
    def _terms(cls, v: list[str]) -> list[str]:
        return _clean_terms(v)

    @field_validator("providers")
    @classmethod
    def _providers(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if p not in PROVIDERS]
        if bad:
            raise ValueError(f"Unknown providers: {', '.join(bad)}")
        return list(dict.fromkeys(v))

    @field_validator("source_types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in SOURCE_TYPES]
        if bad:
            raise ValueError(f"Unknown source types: {', '.join(bad)}")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _years(self):
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from must not be after year_to")
        return self


class FrameRequest(BaseModel):
    question: str = Field(min_length=5, max_length=1500)


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class UrlSourceRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class FollowupRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1500)


class SettingsUpdate(BaseModel):
    groq_model: Optional[str] = Field(default=None, max_length=100)
    llm_temperature: Optional[float] = Field(default=None, ge=0, le=1)
    llm_timeout_seconds: Optional[int] = Field(default=None, ge=10, le=300)
    providers: Optional[list[str]] = None
    results_per_query: Optional[int] = Field(default=None, ge=3, le=50)
    max_screened_sources: Optional[int] = Field(default=None, ge=3, le=60)
    contact_email: Optional[str] = Field(default=None, max_length=200)
    secrets: dict[str, str] = Field(default_factory=dict)

    @field_validator("providers")
    @classmethod
    def _providers(cls, v):
        if v is None:
            return v
        bad = [p for p in v if p not in PROVIDERS]
        if bad:
            raise ValueError(f"Unknown providers: {', '.join(bad)}")
        return v


# ---------------------------------------------------------------- LLM outputs
class FramingOut(BaseModel):
    subject: str
    comparison: Optional[str] = None
    outcome: Optional[str] = None
    scope: str = ""
    keywords: list[str] = Field(default_factory=list)
    queries: list[str] = Field(min_length=1, max_length=6)
    ambiguities: list[str] = Field(default_factory=list)
    sufficient_evidence: str = ""

    @field_validator("queries", "keywords")
    @classmethod
    def _q(cls, v):
        return _clean_terms(v)


class ExtractionItem(BaseModel):
    source_id: str
    study_design: Optional[str] = None
    sample_size: Optional[str] = None
    key_findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    conflicts_or_funding: Optional[str] = None
    relevance: Literal["high", "medium", "low", "irrelevant"] = "medium"
    relevance_reason: str = ""

    @field_validator("relevance", mode="before")
    @classmethod
    def _rel(cls, v):
        v = str(v or "medium").lower().strip()
        return v if v in {"high", "medium", "low", "irrelevant"} else "medium"


class ExtractionOut(BaseModel):
    items: list[ExtractionItem]


class SynthFinding(BaseModel):
    statement: str
    category: Literal["established", "contested", "single_study"]
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    explanation: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def _cat(cls, v):
        v = str(v).lower().replace("-", "_").replace(" ", "_")
        aliases = {"well_established": "established", "single": "single_study", "single_study_only": "single_study"}
        return aliases.get(v, v)


class SynthTheme(BaseModel):
    name: str
    findings: list[SynthFinding] = Field(default_factory=list)


class SynthGap(BaseModel):
    question: str
    note: str = ""


class SynthDisagreement(BaseModel):
    topic: str
    positions: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    significance: str = ""


class SynthDependency(BaseModel):
    source: str
    depends_on: str
    reason: str = ""


class SynthConfidence(BaseModel):
    level: Literal["Low", "Moderate", "High"]
    rationale: str

    @field_validator("level", mode="before")
    @classmethod
    def _lvl(cls, v):
        return str(v).strip().capitalize()


class SynthesisOut(BaseModel):
    themes: list[SynthTheme] = Field(default_factory=list)
    gaps: list[SynthGap] = Field(default_factory=list)
    disagreements: list[SynthDisagreement] = Field(default_factory=list)
    dependencies: list[SynthDependency] = Field(default_factory=list)
    confidence: SynthConfidence
    summary: str
    practical_conclusions: list[str] = Field(default_factory=list)


class FollowupOut(BaseModel):
    answer: str
    cited: list[str] = Field(default_factory=list)
    not_covered: Optional[str] = None
