"""Generate a personalized daily digest with Gemini + Google Search grounding.

The digest is built from a user's `compiled_profile` (persona, edge focus, and
section list) rather than a single hardcoded prompt, so each user gets a brief
shaped around their own interests.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

# Gemini sheds load with transient 5xx/429s ("model is experiencing high
# demand"). Retry a couple times, then fall back to another model that may have
# capacity, before giving up on a user for the day.
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_STATUS = {"UNAVAILABLE", "RESOURCE_EXHAUSTED", "INTERNAL",
                     "DEADLINE_EXCEEDED", "OVERLOADED"}


def _is_transient(exc: Exception) -> bool:
    """True for retryable Gemini failures (overload / rate limit / 5xx)."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _RETRYABLE_CODES:
        return True
    status = getattr(exc, "status", None)
    if isinstance(status, str) and status.upper() in _RETRYABLE_STATUS:
        return True
    text = str(exc).upper()
    if any(s in text for s in _RETRYABLE_STATUS):
        return True
    return any(f" {c} " in f" {text} " for c in map(str, _RETRYABLE_CODES))


def model_chain() -> list[str]:
    """Primary model first, then fallbacks (GEMINI_FALLBACK_MODELS, comma-sep)."""
    primary = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    fallbacks = os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-pro")
    chain = [primary]
    for m in (x.strip() for x in fallbacks.split(",")):
        if m and m not in chain:
            chain.append(m)
    return chain


def generate_content_resilient(client, prompt: str, config, *,
                               models: list[str] | None = None,
                               attempts_per_model: int = 2,
                               sleep_seconds: float = 3.0,
                               sleep_fn=time.sleep):
    """generate_content with per-model retries and cross-model fallback.

    Non-transient errors raise immediately; transient ones retry the same model,
    then move to the next model in the chain. Raises the last error if all fail.
    """
    chain = models or model_chain()
    last_exc: Exception | None = None
    for model in chain:
        for attempt in range(attempts_per_model):
            try:
                return client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
            except Exception as exc:  # noqa: BLE001 - classify then re-raise
                if not _is_transient(exc):
                    raise
                last_exc = exc
                if attempt < attempts_per_model - 1:
                    sleep_fn(sleep_seconds)
    assert last_exc is not None
    raise last_exc

# The invariant scaffolding around every brief. Persona, edge focus, and the
# sections are injected from the user's compiled_profile.
PROMPT_TEMPLATE = """You are a sharp research analyst AND opportunity scout writing a \
daily morning briefing for {persona}. Today is {date}.

Use Google Search to gather REAL, UP-TO-DATE information from roughly the last 24-48 \
hours. Do NOT invent, guess, or use stale knowledge. Every item must reflect something \
that actually happened recently. Prefer primary/reputable sources. Include real \
numbers (prices, %, dates) where relevant.

Produce ONLY an HTML fragment (no <html>, <head>, or <body> tags, no markdown code \
fences). Use the EXACT section structure below, in order. Each section is an <h2> \
followed by the content described for it. Bold the key entity in each bullet using \
<strong>. Keep it tight and scannable — aim for about a 4-5 minute read.

CRITICAL RULE — every bullet must be ACTIONABLE, not just informative. Write each \
bullet as TWO parts:
1. THE FACT: one sentence on what actually happened (with real names/numbers).
2. A "<strong>→ Edge:</strong>" clause (one or two sentences) that draws the \
conclusion: what this could mean for the future of the world / the industry, AND \
specifically how the reader could leverage it. Tailor the Edge to: {edge_for}. Avoid \
generic filler like "worth watching"; give a real, specific move.

Structure:

{sections}

<p style="font-size:12px;color:#888;margin-top:16px">Market reads above are speculative \
analysis for idea generation, not financial advice. Do your own research.</p>

Return only the HTML fragment.
"""


def compile_prompt(compiled_profile: dict[str, Any], date: str) -> str:
    """Turn a compiled_profile into the full Gemini prompt for `date`."""
    sections = "\n\n".join(
        f"<h2>{s['emoji']} {s['title']}</h2>\n{s['guidance']}"
        for s in compiled_profile["sections"]
    )
    return PROMPT_TEMPLATE.format(
        persona=compiled_profile["persona"],
        edge_for=compiled_profile["edge_for"],
        sections=sections,
        date=date,
    )


def build_teaser(fragment: str) -> str:
    """A one-line push hook derived from the digest — the first bolded entity.

    Deterministic (no extra LLM call): bullets bold their key entity with
    <strong>, so the first non-"Edge" bold is a good stand-in for the day's top
    item. Falls back to a generic line if nothing usable is found.
    """
    for match in re.finditer(r"<strong>(.*?)</strong>", fragment, flags=re.DOTALL):
        text = re.sub(r"<[^>]+>", "", match.group(1))
        text = text.replace("→", "").strip().rstrip(":").strip()
        if text and "edge" not in text.lower() and len(text) > 1:
            teaser = f"Today: {text}"
            return teaser[:120]
    return "Your morning brief is ready ☕"


def _clean_fragment(text: str) -> str:
    """Strip markdown code fences and stray html/body wrappers if present."""
    text = text.strip()
    text = re.sub(r"^```(?:html)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    for tag in ("html", "head", "body"):
        text = re.sub(rf"</?{tag}[^>]*>", "", text, flags=re.IGNORECASE)
    return text.strip()


def generate_digest_html(compiled_profile: dict[str, Any], date: str) -> str:
    """Call Gemini with search grounding and return an HTML fragment.

    `date` is the user's local date string (e.g. "Monday, July 21, 2026").
    """
    from google import genai
    from google.genai import types

    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)

    prompt = compile_prompt(compiled_profile, date)

    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.4,
    )
    response = generate_content_resilient(client, prompt, config)

    fragment = _clean_fragment(response.text or "")
    if len(fragment) < 200:
        raise RuntimeError(
            f"Gemini returned too little content ({len(fragment)} chars): {fragment!r}"
        )
    return fragment
