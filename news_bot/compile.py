"""Compile a user's free-text interests into a structured compiled_profile.

This runs once when the user saves their interests (via the /api/compile-profile
endpoint), not every day. Compiling to a fixed schema gives every brief a stable
section structure, lets the app preview the sections for confirmation, and
contains prompt-injection from the text box — the model can only fill a schema,
not redirect the daily worker.
"""

from __future__ import annotations

import json
import re
from typing import Any

from news_bot.profiles import validate_compiled_profile

COMPILE_PROMPT = """You are configuring a personalized daily news briefing. Turn the \
reader's description of what they want to follow into a JSON configuration.

Reader's description:
\"\"\"
{interests_text}
\"\"\"

Produce ONLY a JSON object (no prose, no markdown fences) with this exact shape:

{{
  "persona": "<one sentence describing this reader, written as a noun phrase, e.g. 'an entertainment industry follower who wants to know what to watch and where the business is heading'>",
  "edge_for": "<one sentence describing the actionable angle every item should be tied to for THIS reader, e.g. 'what to watch, plus the industry and culture implications'>",
  "sections": [
    {{"emoji": "<one relevant emoji>", "title": "<short section title>", "guidance": "<one or two sentences telling the writer exactly what goes in this section: the kind of items, roughly how many bullets (or an HTML table), and the angle>"}}
  ]
}}

Rules:
- Produce 6 to 9 sections, ordered from most to least important for this reader.
- Cover the distinct topics the reader named; do not invent unrelated topics.
- Base each section on the reader's stated interests. If they mention markets,
  money, or investing, you may include a table-based markets section and frame
  guidance around concrete, speculative reads (never guaranteed advice).
- Always make the final section a short educational "one thing to know" style
  section relevant to the reader's field.
- Keep titles short. Keep guidance concrete and specific to this reader.

Return only the JSON object.
"""


def build_compile_prompt(interests_text: str) -> str:
    return COMPILE_PROMPT.format(interests_text=interests_text.strip())


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the model's response into a dict, tolerating code fences/prose."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Fall back to the outermost {...} if the model added stray prose.
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Compiler did not return valid JSON: {exc}") from exc


def compile_interests(interests_text: str, *, model: str | None = None) -> dict[str, Any]:
    """Call Gemini to compile interests, validate the result, and return it.

    Raises ValueError / CompiledProfileError on unusable output — the caller
    surfaces that to the app and keeps the user's previous profile.
    """
    import os

    from google import genai
    from google.genai import types

    if not interests_text or not interests_text.strip():
        raise ValueError("interests_text is empty")

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=model or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
        contents=build_compile_prompt(interests_text),
        config=types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
        ),
    )

    compiled = _extract_json(response.text or "")
    validate_compiled_profile(compiled)
    return compiled
