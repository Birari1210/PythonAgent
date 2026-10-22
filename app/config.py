"""Configuration management.

Secrets (API keys) come only from environment variables / the `.env` file and never
leave the backend. Non-secret preferences (model, enabled providers, limits) are stored
in the database settings table so they can be changed from the UI.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.environ.get("RS_ENV_FILE", BASE_DIR / ".env"))

load_dotenv(ENV_PATH, override=False)

# Keys the Settings UI is allowed to write to .env. Values are never read back to the UI.
SECRET_KEYS = {
    "GROQ_API_KEY": "Groq (LLM analysis)",
    "SEMANTIC_SCHOLAR_API_KEY": "Semantic Scholar (optional, raises rate limit)",
    "TAVILY_API_KEY": "Tavily (optional web / grey-literature search)",
    "OPENALEX_API_KEY": "OpenAlex (optional, higher daily budget)",
}

DEFAULT_SETTINGS: dict[str, object] = {
    "groq_model": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
    "llm_temperature": 0.1,
    "llm_timeout_seconds": 60,
    "providers": ["openalex", "semantic_scholar", "arxiv", "europepmc", "crossref"],
    "results_per_query": 10,
    "max_screened_sources": 25,
    "contact_email": os.environ.get("CONTACT_EMAIL", ""),
}


def data_dir() -> Path:
    path = Path(os.environ.get("RS_DATA_DIR", BASE_DIR / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return Path(os.environ.get("RS_DB_PATH", data_dir() / "research.db"))


def get_secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def secret_status() -> dict[str, dict[str, object]]:
    """Report which secrets are configured without revealing their values."""
    return {k: {"label": label, "configured": bool(get_secret(k))} for k, label in SECRET_KEYS.items()}


_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def write_secret(name: str, value: str) -> None:
    """Persist a secret to the .env file and the current process environment."""
    if name not in SECRET_KEYS:
        raise ValueError(f"Unknown setting {name}")
    value = value.strip()
    if any(c in value for c in "\r\n\"'"):
        raise ValueError("Key contains invalid characters")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, replaced = [], False
    for line in lines:
        m = _ENV_LINE.match(line)
        if m and m.group(1) == name:
            if value:
                out.append(f"{name}={value}")
            replaced = True
        else:
            out.append(line)
    if value and not replaced:
        out.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
