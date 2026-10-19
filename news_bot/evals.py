"""Quality evals run on each brief right after it's delivered.

Three scores per brief:

  profile_adherence   LLM judge (Phoenix evals, Gemini). Did the brief follow
                      this user's persona, sections, and Edge angle?
  grounding_coverage  Code. Share of the brief's text that Gemini attributed to
                      a Google Search source. The hallucination signal: text
                      with no citation behind it is where invented facts live.
  section_structure   Code. Share of the profile's sections present as <h2>
                      headings, in order.

Why not Phoenix's Faithfulness/Hallucination evaluators: they judge a response
against source *text*, and search grounding returns only titles and URLs. A
judge given titles would flag every real number as unsupported. The citation
map Gemini does return is exact, so coverage is computed from it directly.

Evals never block or break delivery: they run after the brief is sent, each one
is isolated, and failures are logged and skipped. Set MYNEWS_LLM_EVALS=off to
skip the LLM judge (the two code evals are free and always run).
"""

from __future__ import annotations

import html as html_lib
import os
import re
from typing import Any, NamedTuple

EVAL_MODEL = os.environ.get("EVAL_MODEL", "gemini-3.5-flash")

# Brief HTML is truncated before judging, same reason as refine.py.
MAX_JUDGE_CHARS = 12000


class EvalResult(NamedTuple):
    name: str
    kind: str            # "LLM" or "CODE" (Phoenix annotator kind)
    score: float | None
    label: str | None
    explanation: str


# --- code evals -------------------------------------------------------------

def _merged_length(spans: list[tuple[int, int]], limit: int) -> int:
    """Total length covered by possibly-overlapping [start, end) spans."""
    covered, cur_start, cur_end = 0, None, None
    for start, end in sorted((max(0, s), min(limit, e)) for s, e in spans):
        if end <= start:
            continue
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                covered += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        covered += cur_end - cur_start
    return covered


def grounding_coverage(grounding: dict[str, Any] | None) -> EvalResult:
    if not grounding or not grounding.get("text_length"):
        return EvalResult("grounding_coverage", "CODE", None, "no_grounding_data",
                          "Response carried no grounding metadata.")
    length = grounding["text_length"]
    covered = _merged_length(grounding.get("supports") or [], length)
    score = round(covered / length, 3)
    label = "grounded" if score >= 0.5 else "weakly_grounded" if score >= 0.2 else "ungrounded"
    return EvalResult(
        "grounding_coverage", "CODE", score, label,
        f"{score:.0%} of the brief is attributed to {len(grounding.get('sources') or [])} "
        f"search source(s) from {len(grounding.get('queries') or [])} query(ies).",
    )


def _norm(text: str) -> str:
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\s+", " ", text).strip().lower()


def section_structure(compiled_profile: dict[str, Any], brief_html: str) -> EvalResult:
    expected = [_norm(s.get("title", "")) for s in compiled_profile.get("sections") or []]
    if not expected:
        return EvalResult("section_structure", "CODE", None, "no_sections", "Profile has no sections.")
    headings = [_norm(h) for h in re.findall(r"<h2[^>]*>(.*?)</h2>", brief_html,
                                             flags=re.IGNORECASE | re.DOTALL)]
    # Walk headings in order so a section only counts if it isn't out of order.
    found, pos = [], 0
    for title in expected:
        for i in range(pos, len(headings)):
            if title and title in headings[i]:
                found.append(title)
                pos = i + 1
                break
    score = round(len(found) / len(expected), 3)
    missing = [t for t in expected if t not in found]
    label = "complete" if score == 1 else "partial" if score > 0 else "missing"
    return EvalResult("section_structure", "CODE", score, label,
                      "All sections present in order." if not missing
                      else f"Missing or out of order: {', '.join(missing)}")


# --- LLM judge --------------------------------------------------------------

ADHERENCE_TEMPLATE = """You are grading a personalized daily news briefing against the \
configuration that was supposed to shape it.

READER: {{persona}}
EDGE ANGLE ("→ Edge:" clauses should be tailored to this): {{edge_for}}

REQUIRED SECTIONS, in order, with what each should contain:
{{sections}}

THE BRIEF (HTML):
{{brief}}

Judge adherence only — not whether the news is true. Check: does each section \
deliver what its guidance asks for, are the bullets aimed at this reader, and do \
the Edge clauses give specific moves for the stated angle rather than generic \
filler? Missing sections or off-topic content count against adherence."""

ADHERENCE_CHOICES = {
    "adherent": (1.0, "Follows the configuration closely; Edge clauses are specific to the angle."),
    "partially_adherent": (0.5, "Mostly on target, with weak or off-topic sections or generic Edge clauses."),
    "not_adherent": (0.0, "Largely ignores the configuration."),
}


def _format_sections(compiled_profile: dict[str, Any]) -> str:
    return "\n".join(
        f"{i}. {s.get('emoji', '')} {s.get('title', '')}: {s.get('guidance', '')}"
        for i, s in enumerate(compiled_profile.get("sections") or [], 1)
    )


def _build_judge():
    from phoenix.evals import LLM, create_classifier

    llm = LLM(provider="google", model=EVAL_MODEL, api_key=os.environ["GEMINI_API_KEY"])
    return create_classifier(name="profile_adherence", prompt_template=ADHERENCE_TEMPLATE,
                             llm=llm, choices=ADHERENCE_CHOICES)


def profile_adherence(compiled_profile: dict[str, Any], brief_html: str,
                      judge: Any = None) -> EvalResult:
    judge = judge or _build_judge()
    scores = judge.evaluate({
        "persona": compiled_profile.get("persona", ""),
        "edge_for": compiled_profile.get("edge_for", ""),
        "sections": _format_sections(compiled_profile),
        "brief": brief_html[:MAX_JUDGE_CHARS],
    })
    s = scores[0]
    return EvalResult("profile_adherence", "LLM", s.score, s.label, s.explanation or "")


def llm_evals_enabled() -> bool:
    return os.environ.get("MYNEWS_LLM_EVALS", "on").strip().lower() not in {"off", "0", "false", "no"}


# --- runner -----------------------------------------------------------------

def run_evals(compiled_profile: dict[str, Any], brief_html: str,
              grounding: dict[str, Any] | None, *, judge: Any = None) -> list[EvalResult]:
    """Run every eval; one failing never stops the others or raises."""
    checks = [
        ("grounding_coverage", lambda: grounding_coverage(grounding)),
        ("section_structure", lambda: section_structure(compiled_profile, brief_html)),
    ]
    if llm_evals_enabled():
        checks.append(("profile_adherence",
                       lambda: profile_adherence(compiled_profile, brief_html, judge)))
    results = []
    for name, check in checks:
        try:
            results.append(check())
        except Exception as exc:  # an eval failure is a log line, nothing more
            print(f"    eval {name} failed: {exc!r}")
    return results
