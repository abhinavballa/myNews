"""Brief evals: grounding coverage, section structure, the LLM judge wiring,
and that no eval failure can ever reach delivery."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from news_bot import evals
from news_bot.generate import extract_grounding

PROFILE = {
    "persona": "an AI researcher who invests",
    "edge_for": "what to build or invest in",
    "sections": [
        {"emoji": "🤖", "title": "AI Models", "guidance": "new releases"},
        {"emoji": "💸", "title": "Markets", "guidance": "chip stocks"},
        {"emoji": "🧪", "title": "Research", "guidance": "papers"},
    ],
}

BRIEF = ("<h2>🤖 AI Models</h2><ul><li>x</li></ul>"
         "<h2>💸 Markets</h2><ul><li>y</li></ul>"
         "<h2>🧪 Research</h2><ul><li>z</li></ul>")


# --- grounding extraction ---------------------------------------------------

def fake_response(text="hello world", supports=None, chunks=None, queries=None):
    meta = {
        "web_search_queries": queries or ["nvidia earnings"],
        "grounding_chunks": chunks if chunks is not None else [
            {"web": {"title": "reuters.com", "uri": "https://r/1"}}],
        "grounding_supports": supports if supports is not None else [
            {"segment": {"start_index": 0, "end_index": 5}, "grounding_chunk_indices": [0]}],
    }
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(grounding_metadata=meta)])


def test_extract_grounding_reads_queries_sources_and_supports():
    g = extract_grounding(fake_response())
    assert g["queries"] == ["nvidia earnings"]
    assert g["sources"] == [{"title": "reuters.com", "uri": "https://r/1"}]
    assert g["supports"] == [(0, 5)]
    assert g["text_length"] == len("hello world")


def test_extract_grounding_ignores_supports_without_a_source():
    g = extract_grounding(fake_response(supports=[
        {"segment": {"start_index": 0, "end_index": 5}, "grounding_chunk_indices": []}]))
    assert g["supports"] == []


def test_extract_grounding_tolerates_no_metadata():
    g = extract_grounding(SimpleNamespace(text="abc", candidates=[]))
    assert g == {"queries": [], "sources": [], "supports": [], "text_length": 3}


def test_extract_grounding_counts_utf8_bytes():
    assert extract_grounding(fake_response(text="é"))["text_length"] == 2


# --- grounding coverage -----------------------------------------------------

def test_coverage_merges_overlapping_spans():
    r = evals.grounding_coverage({"text_length": 100, "supports": [(0, 30), (20, 50), (60, 70)],
                                  "sources": [{}], "queries": ["q"]})
    assert r.score == 0.6  # [0,50) + [60,70)
    assert r.label == "grounded"
    assert r.kind == "CODE"


def test_coverage_clamps_spans_past_the_end():
    r = evals.grounding_coverage({"text_length": 10, "supports": [(5, 99)]})
    assert r.score == 0.5


@pytest.mark.parametrize("covered,label", [(10, "ungrounded"), (30, "weakly_grounded"),
                                           (80, "grounded")])
def test_coverage_labels(covered, label):
    assert evals.grounding_coverage(
        {"text_length": 100, "supports": [(0, covered)]}).label == label


def test_coverage_without_grounding_has_no_score():
    r = evals.grounding_coverage(None)
    assert r.score is None and r.label == "no_grounding_data"


# --- section structure ------------------------------------------------------

def test_all_sections_in_order_is_complete():
    r = evals.section_structure(PROFILE, BRIEF)
    assert (r.score, r.label) == (1.0, "complete")


def test_missing_section_is_partial_and_named():
    brief = BRIEF.replace("<h2>💸 Markets</h2>", "")
    r = evals.section_structure(PROFILE, brief)
    assert r.score == round(2 / 3, 3)
    assert "markets" in r.explanation


def test_out_of_order_section_does_not_count():
    brief = ("<h2>🧪 Research</h2><h2>🤖 AI Models</h2><h2>💸 Markets</h2>")
    r = evals.section_structure(PROFILE, brief)
    assert r.score == round(2 / 3, 3)


def test_heading_match_ignores_tags_and_entities():
    profile = {"sections": [{"title": "R&D"}]}
    assert evals.section_structure(profile, "<h2><strong>R&amp;D</strong></h2>").score == 1.0


# --- LLM judge --------------------------------------------------------------

class FakeJudge:
    def __init__(self, label="adherent", score=1.0, raise_exc=None):
        self.label, self.score, self.raise_exc = label, score, raise_exc
        self.inputs = None

    def evaluate(self, eval_input):
        if self.raise_exc:
            raise self.raise_exc
        self.inputs = eval_input
        return [SimpleNamespace(label=self.label, score=self.score, explanation="fine")]


def test_judge_gets_the_profile_and_the_brief():
    judge = FakeJudge()
    r = evals.profile_adherence(PROFILE, BRIEF, judge=judge)
    assert (r.name, r.kind, r.score, r.label) == ("profile_adherence", "LLM", 1.0, "adherent")
    assert judge.inputs["persona"] == PROFILE["persona"]
    assert "2. 💸 Markets: chip stocks" in judge.inputs["sections"]
    assert judge.inputs["brief"] == BRIEF


def test_judge_input_is_truncated(monkeypatch):
    monkeypatch.setattr(evals, "MAX_JUDGE_CHARS", 10)
    judge = FakeJudge()
    evals.profile_adherence(PROFILE, "x" * 100, judge=judge)
    assert judge.inputs["brief"] == "x" * 10


def test_template_placeholders_match_the_inputs():
    for name in ("persona", "edge_for", "sections", "brief"):
        assert "{{" + name + "}}" in evals.ADHERENCE_TEMPLATE


def test_prompt_names_every_allowed_label():
    """Gemini only returns valid labels if it's told them (see ADHERENCE_CHOICES)."""
    for label in evals.ADHERENCE_CHOICES:
        assert f"- {label}:" in evals.ADHERENCE_TEMPLATE


def test_judge_schema_constrains_the_label_with_an_enum(monkeypatch):
    """oneOf/const isn't enforced by Gemini; an enum is."""
    pytest.importorskip("phoenix.evals")
    import phoenix.evals.llm.adapters.google.adapter as ga
    captured = {}

    def fake_generate_object(self, prompt, schema, **kw):
        captured["schema"] = schema
        return {"label": "adherent", "explanation": "x"}
    monkeypatch.setattr(ga.GoogleGenAIAdapter, "generate_object", fake_generate_object)
    monkeypatch.setenv("GEMINI_API_KEY", "test")

    r = evals.profile_adherence(PROFILE, BRIEF)
    assert captured["schema"]["properties"]["label"]["enum"] == list(evals.ADHERENCE_CHOICES)
    assert (r.label, r.score) == ("adherent", 1.0)


# --- runner -----------------------------------------------------------------

def test_runner_skips_the_judge_when_disabled():
    judge = FakeJudge()
    names = [r.name for r in evals.run_evals(PROFILE, BRIEF, None, judge=judge)]
    assert names == ["grounding_coverage", "section_structure"]
    assert judge.inputs is None


def test_runner_includes_the_judge_when_enabled(monkeypatch):
    monkeypatch.setenv("MYNEWS_LLM_EVALS", "on")
    names = [r.name for r in evals.run_evals(PROFILE, BRIEF, None, judge=FakeJudge())]
    assert names == ["grounding_coverage", "section_structure", "profile_adherence"]


def test_a_failing_eval_never_raises(monkeypatch):
    monkeypatch.setenv("MYNEWS_LLM_EVALS", "on")
    results = evals.run_evals(PROFILE, BRIEF, None,
                              judge=FakeJudge(raise_exc=RuntimeError("gemini 503")))
    assert [r.name for r in results] == ["grounding_coverage", "section_structure"]


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false"])
def test_llm_evals_can_be_turned_off(monkeypatch, value):
    monkeypatch.setenv("MYNEWS_LLM_EVALS", value)
    assert not evals.llm_evals_enabled()


def test_llm_evals_default_on_when_unset_or_blank(monkeypatch):
    monkeypatch.delenv("MYNEWS_LLM_EVALS")
    assert evals.llm_evals_enabled()
    monkeypatch.setenv("MYNEWS_LLM_EVALS", "")  # an unset GitHub var arrives as ""
    assert evals.llm_evals_enabled()
