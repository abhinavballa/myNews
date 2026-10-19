"""Apply one piece of user feedback to their compiled_profile with one LLM call.

When a user writes a comment on a brief ("the crypto section was filler, more
on chips"), the comment, the brief it is about, and their current profile go to
the model, which returns an edited profile. The edit is saved as a new
profile_versions row, so a bad edit is undone by pointing back at the previous
version — no grading or scoring step.

The guardrails below are the only safety net, so they stay strict: an edit that
breaks the schema or throws away most of the brief is rejected and the user
keeps their current profile.
"""

from __future__ import annotations

import os
from typing import Any

from news_bot.compile import _extract_json
from news_bot.profiles import CompiledProfileError, validate_compiled_profile

# Truncate the brief so a long one can't blow up the context or the bill.
MAX_BRIEF_CHARS = int(os.environ.get("REFINE_MAX_BRIEF_CHARS", "6000"))

# The compile step produces 6-9 sections; an edit must stay in that band...
MIN_SECTIONS, MAX_SECTIONS = 6, 9

# ...and must not lurch. One comment adjusts the brief; it doesn't rebuild it.
MAX_SECTION_DELTA = 2


class RefinementRejected(ValueError):
    """The model's edit failed a guardrail; keep the current profile."""


REFINE_PROMPT = """You maintain the configuration of a personalized daily news \
briefing. The reader just left feedback on one brief. Edit the configuration so \
future briefs follow that feedback.

THE READER'S OWN DESCRIPTION OF WHAT THEY WANT (authoritative — do NOT \
contradict it or extend it into topics they did not ask for):
\"\"\"
{interests_text}
\"\"\"

THE CURRENT CONFIGURATION:
{current_profile}

THE BRIEF THEY RECEIVED ({local_date}):
{brief}

THEIR FEEDBACK ({rating}):
\"\"\"
{notes}
\"\"\"

Edit the configuration to act on the feedback: adjust `guidance` on the \
sections they reacted to, `edge_for` if the actionable angle missed, `persona` \
only if they are plainly mischaracterised. Reorder, merge, or replace sections \
if the feedback calls for it. Leave everything they did not comment on alone.

Rules:
- Stay inside the reader's description above.
- Keep {min_sections} to {max_sections} sections, within {max_delta} of the \
current count.
- Keep the same JSON shape. Titles short; guidance concrete.

Produce ONLY a JSON object (no prose, no markdown fences):

{{
  "rationale": "<one or two sentences the reader could read: what you changed>",
  "compiled_profile": {{
    "persona": "<one sentence>",
    "edge_for": "<one sentence>",
    "sections": [
      {{"emoji": "<emoji>", "title": "<short title>", "guidance": "<one or two concrete sentences>"}}
    ]
  }}
}}
"""


def _format_profile(compiled_profile: dict[str, Any]) -> str:
    """Readable text rather than JSON, so the model edits it instead of echoing it."""
    lines = [
        f"persona: {compiled_profile.get('persona', '')}",
        f"edge_for: {compiled_profile.get('edge_for', '')}",
        "sections:",
    ]
    for i, section in enumerate(compiled_profile.get("sections") or [], 1):
        lines.append(
            f"  {i}. {section.get('emoji', '')} {section.get('title', '')}"
            f" — {section.get('guidance', '')}"
        )
    return "\n".join(lines)


def _format_rating(rating: Any) -> str:
    if rating == 1:
        return "thumbs UP"
    if rating == -1:
        return "thumbs DOWN"
    return "no rating given"


def build_refine_prompt(interests_text: str, compiled_profile: dict[str, Any],
                        brief_html: str, notes: str, *, rating: Any = None,
                        local_date: str | None = None) -> str:
    """Pure: assemble the prompt for one piece of feedback."""
    return REFINE_PROMPT.format(
        interests_text=(interests_text or "").strip(),
        current_profile=_format_profile(compiled_profile),
        local_date=local_date or "unknown date",
        brief=(brief_html or "(brief unavailable)")[:MAX_BRIEF_CHARS],
        rating=_format_rating(rating),
        notes=(notes or "").strip(),
        min_sections=MIN_SECTIONS,
        max_sections=MAX_SECTIONS,
        max_delta=MAX_SECTION_DELTA,
    )


def check_revision(current: dict[str, Any], revised: Any) -> None:
    """Guardrails on the model's output. Raises on anything unsafe to activate.

    Schema validity is reused from the compile path so the worker's contract is
    enforced in one place. The section-count rules catch what schema validation
    cannot: a valid profile that quietly threw away most of the brief.
    """
    validate_compiled_profile(revised)  # raises CompiledProfileError

    sections = revised["sections"]
    if not (MIN_SECTIONS <= len(sections) <= MAX_SECTIONS):
        raise RefinementRejected(
            f"revision has {len(sections)} sections; must be "
            f"{MIN_SECTIONS}-{MAX_SECTIONS}"
        )

    current_count = len(current.get("sections") or [])
    if current_count and abs(len(sections) - current_count) > MAX_SECTION_DELTA:
        raise RefinementRejected(
            f"revision changes section count {current_count} -> {len(sections)}, "
            f"more than the allowed {MAX_SECTION_DELTA}"
        )


def refine_profile(interests_text: str, compiled_profile: dict[str, Any],
                   brief_html: str, notes: str, *, rating: Any = None,
                   local_date: str | None = None) -> dict[str, Any]:
    """Apply one comment to the profile. Returns {"compiled_profile", "rationale"}.

    Raises RefinementRejected / CompiledProfileError on unusable output — the
    caller keeps the current profile, same as compile_interests.
    """
    if not (notes or "").strip():
        raise RefinementRejected("no feedback text to apply")

    from google import genai
    from google.genai import types

    from news_bot.generate import generate_content_resilient

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(
        temperature=0.3,
        response_mime_type="application/json",
    )
    prompt = build_refine_prompt(interests_text, compiled_profile, brief_html,
                                 notes, rating=rating, local_date=local_date)
    response = generate_content_resilient(client, prompt, config)

    payload = _extract_json(response.text or "")
    revised = payload.get("compiled_profile")
    if revised is None:
        raise RefinementRejected("model returned no compiled_profile")

    check_revision(compiled_profile, revised)

    return {
        "compiled_profile": revised,
        "rationale": (payload.get("rationale") or "").strip(),
    }


__all__ = [
    "CompiledProfileError",
    "RefinementRejected",
    "build_refine_prompt",
    "check_revision",
    "refine_profile",
]
