"""Groq LLM client (OpenAI-compatible chat completions) with schema-validated JSON output.

The API key is read from GROQ_API_KEY on the backend only. It is never logged or returned.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from . import config

log = logging.getLogger(__name__)

GROQ_BASE = "https://api.groq.com/openai/v1"
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """A user-presentable LLM failure."""


class LLMNotConfigured(LLMError):
    pass


SETUP_HELP = ("Groq is not configured. Create a free key at https://console.groq.com/keys, then either add "
              "GROQ_API_KEY=... to the .env file and restart, or paste it in Settings. Retrieval still works "
              "without it, but extraction, synthesis and follow-up answers need the LLM.")


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


class GroqClient:
    def __init__(self, model: str, temperature: float = 0.1, timeout: float = 60, max_retries: int = 3,
                 transport: httpx.BaseTransport | None = None):
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self._transport = transport
        self.calls = 0

    @property
    def configured(self) -> bool:
        return bool(config.get_secret("GROQ_API_KEY"))

    def _headers(self) -> dict[str, str]:
        key = config.get_secret("GROQ_API_KEY")
        if not key:
            raise LLMNotConfigured(SETUP_HELP)
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=GROQ_BASE, timeout=self.timeout, transport=self._transport)

    def list_models(self) -> list[str]:
        with self._client() as c:
            r = c.get("/models", headers=self._headers())
        if r.status_code == 401:
            raise LLMError("Groq rejected the API key (401). Check GROQ_API_KEY.")
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []) if m.get("active", True))

    def chat(self, messages: list[dict], json_mode: bool = True, max_tokens: int = 2048) -> str:
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = self._headers()
        last_err = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                self.calls += 1
                with self._client() as c:
                    r = c.post("/chat/completions", headers=headers, json=body)
            except httpx.TimeoutException:
                last_err = f"Groq request timed out after {self.timeout}s"
                log.warning("%s (attempt %d)", last_err, attempt + 1)
                time.sleep(min(2 ** attempt, 10))
                continue
            except httpx.HTTPError as e:
                last_err = f"Network error contacting Groq: {type(e).__name__}"
                log.warning("%s (attempt %d)", last_err, attempt + 1)
                time.sleep(min(2 ** attempt, 10))
                continue
            if r.status_code == 200:
                try:
                    return r.json()["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, ValueError):
                    raise LLMError("Groq returned an unexpected response shape")
            if r.status_code == 401:
                raise LLMError("Groq rejected the API key (401). Check GROQ_API_KEY in .env or Settings.")
            text_l = r.text.lower()
            if r.status_code == 404 or (r.status_code == 400 and "model" in text_l
                                        and ("decommission" in text_l or "does not exist" in text_l)):
                raise LLMError(f"Groq model '{self.model}' is unavailable. Pick another model in Settings.")
            if r.status_code == 413:
                raise LLMError("Request too large for this Groq model's limits; reduce max sources in Settings.")
            if r.status_code == 429 or r.status_code >= 500:
                wait = self._retry_after(r, attempt)
                last_err = f"Groq {'rate limit' if r.status_code == 429 else 'server error'} ({r.status_code})"
                if attempt < self.max_retries:
                    log.warning("%s; retrying in %.1fs", last_err, wait)
                    time.sleep(wait)
                    continue
                break
            # Other 4xx: do not retry.
            msg = ""
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                pass
            raise LLMError(f"Groq error {r.status_code}: {msg[:300]}")
        raise LLMError(f"{last_err}; gave up after {self.max_retries + 1} attempts")

    @staticmethod
    def _retry_after(r: httpx.Response, attempt: int) -> float:
        for h in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            v = r.headers.get(h)
            if not v:
                continue
            m = re.match(r"^(?:(\d+)m)?([\d.]+)s?$", v.strip())
            if m:
                secs = float(m.group(2)) + 60 * float(m.group(1) or 0)
                return min(max(secs, 1.0), 60.0)
        return min(2 ** (attempt + 1), 30)

    def structured(self, system: str, user: str, schema: type[T], max_tokens: int = 2048) -> T:
        """Ask for JSON matching `schema`; one repair attempt if validation fails."""
        schema_hint = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        messages = [
            {"role": "system", "content": system + "\n\nRespond ONLY with a JSON object matching this JSON Schema:\n"
                                                   + schema_hint},
            {"role": "user", "content": user},
        ]
        for attempt in range(2):
            raw = self.chat(messages, json_mode=True, max_tokens=max_tokens)
            try:
                return schema.model_validate(_extract_json(raw))
            except (json.JSONDecodeError, ValidationError) as e:
                log.warning("LLM output failed validation for %s (attempt %d): %s", schema.__name__, attempt + 1,
                            str(e)[:300])
                messages += [{"role": "assistant", "content": raw[:4000]},
                             {"role": "user", "content": f"That output was invalid: {str(e)[:600]}. "
                                                         "Return corrected JSON only."}]
        raise LLMError(f"The model did not return valid {schema.__name__} output after a repair attempt")


def client_from_settings(settings: dict) -> GroqClient:
    return GroqClient(model=str(settings.get("groq_model") or config.DEFAULT_SETTINGS["groq_model"]),
                      temperature=float(settings.get("llm_temperature", 0.1)),
                      timeout=float(settings.get("llm_timeout_seconds", 60)))
