"""One comment -> revised profile: prompt assembly, guardrails, and the call.

The Gemini call is stubbed; what matters is that the prompt carries the right
inputs and that an unsafe edit can never reach a user's profile.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from news_bot import refine
from news_bot.profiles import CompiledProfileError


def make_profile(n_sections=7):
    return {
        "persona": "an AI researcher who also invests",
        "edge_for": "what to build or invest in",
        "sections": [
            {"emoji": "🤖", "title": f"Section {i}", "guidance": f"guidance {i}"}
            for i in range(n_sections)
        ],
    }


BRIEF = "<h2>Chips</h2><li>Nvidia earnings</li><h2>Crypto</h2><li>BTC up</li>"
NOTES = "the Nvidia item was great, crypto section was filler"


def prompt(**kw):
    args = dict(interests_text="AI news plus what I could build",
                compiled_profile=make_profile(), brief_html=BRIEF, notes=NOTES)
    args.update(kw)
    return refine.build_refine_prompt(**args)


# --- prompt assembly ------------------------------------------------------

def test_prompt_carries_interests_text_as_the_invariant():
    p = prompt()
    assert "AI news plus what I could build" in p
    assert "do NOT contradict" in p


def test_prompt_includes_profile_brief_and_feedback():
    p = prompt(compiled_profile=make_profile(2), local_date="2026-10-01")
    assert "an AI researcher who also invests" in p
    assert "Section 0" in p and "Section 1" in p
    assert "Nvidia earnings" in p
    assert NOTES in p
    assert "2026-10-01" in p


@pytest.mark.parametrize("rating,label", [(1, "thumbs UP"), (-1, "thumbs DOWN"),
                                          (None, "no rating given")])
def test_prompt_labels_the_rating(rating, label):
    assert label in prompt(rating=rating)


def test_prompt_states_the_section_bounds():
    assert f"Keep {refine.MIN_SECTIONS} to {refine.MAX_SECTIONS} sections" in prompt()


def test_prompt_truncates_long_briefs(monkeypatch):
    monkeypatch.setattr(refine, "MAX_BRIEF_CHARS", 50)
    p = prompt(brief_html="x" * 5000)
    assert "x" * 50 in p
    assert "x" * 51 not in p


# --- guardrails -----------------------------------------------------------

def test_valid_revision_passes():
    refine.check_revision(make_profile(7), make_profile(7))


def test_schema_violation_is_rejected():
    bad = make_profile()
    bad["sections"][0]["guidance"] = ""
    with pytest.raises(CompiledProfileError):
        refine.check_revision(make_profile(), bad)


def test_missing_persona_is_rejected():
    bad = make_profile()
    del bad["persona"]
    with pytest.raises(CompiledProfileError):
        refine.check_revision(make_profile(), bad)


def test_too_few_sections_is_rejected():
    with pytest.raises(refine.RefinementRejected, match="must be"):
        refine.check_revision(make_profile(7), make_profile(3))


def test_too_many_sections_is_rejected():
    with pytest.raises(refine.RefinementRejected, match="must be"):
        refine.check_revision(make_profile(7), make_profile(12))


def test_large_section_count_swing_is_rejected():
    """Both counts are inside 6-9, but the jump is a runaway, not a refinement."""
    with pytest.raises(refine.RefinementRejected, match="more than the allowed"):
        refine.check_revision(make_profile(9), make_profile(6))


def test_small_section_count_swing_is_allowed():
    refine.check_revision(make_profile(7), make_profile(8))


# --- the call -------------------------------------------------------------

@pytest.fixture
def fake_gemini(monkeypatch):
    """Stub the google.genai import and the resilient call; return a setter
    for what the 'model' replies with."""
    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    genai.Client = lambda **kw: object()
    genai.types = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw)
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setenv("GEMINI_API_KEY", "test")

    reply = {}

    def fake_call(client, prompt_text, config, **kw):
        reply["prompt"] = prompt_text
        return types.SimpleNamespace(text=json.dumps(reply["payload"]))

    import news_bot.generate as generate
    monkeypatch.setattr(generate, "generate_content_resilient", fake_call)
    return reply


def test_refine_profile_returns_the_edited_profile(fake_gemini):
    edited = make_profile(7)
    edited["sections"][0]["guidance"] = "more Nvidia, less crypto"
    fake_gemini["payload"] = {"rationale": "cut crypto", "compiled_profile": edited}
    out = refine.refine_profile("interests", make_profile(), BRIEF, NOTES)
    assert out == {"compiled_profile": edited, "rationale": "cut crypto"}
    assert NOTES in fake_gemini["prompt"]


def test_refine_profile_rejects_an_unsafe_edit(fake_gemini):
    fake_gemini["payload"] = {"rationale": "x", "compiled_profile": make_profile(3)}
    with pytest.raises(refine.RefinementRejected):
        refine.refine_profile("interests", make_profile(), BRIEF, NOTES)


def test_refine_profile_rejects_a_reply_with_no_profile(fake_gemini):
    fake_gemini["payload"] = {"rationale": "x"}
    with pytest.raises(refine.RefinementRejected, match="no compiled_profile"):
        refine.refine_profile("interests", make_profile(), BRIEF, NOTES)


def test_refine_profile_needs_feedback_text():
    with pytest.raises(refine.RefinementRejected, match="no feedback"):
        refine.refine_profile("interests", make_profile(), BRIEF, "   ")
