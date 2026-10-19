"""POST /api/feedback logic: save, apply the comment once, never lose feedback."""

from __future__ import annotations

import pytest

from news_bot import refine
from news_bot.feedback import save_feedback

PROFILE = {"persona": "p", "edge_for": "e", "sections": []}
EDITED = {"persona": "p2", "edge_for": "e", "sections": []}


class FakeRest:
    """Records writes; answers the three GETs save_feedback makes."""

    def __init__(self, previous_notes=None, compiled_profile=PROFILE):
        self.previous_notes = previous_notes
        self.compiled_profile = compiled_profile
        self.writes = []

    def __call__(self, method, path, body=None, prefer="return=minimal"):
        if method == "POST":
            self.writes.append((path.split("?")[0], body))
            return None
        if path.startswith("digest_feedback"):
            return [{"notes": self.previous_notes}] if self.previous_notes is not None else []
        if path.startswith("profiles"):
            return [{"interests_text": "AI", "compiled_profile": self.compiled_profile}]
        if path.startswith("digests"):
            return [{"html": "<li>brief</li>", "local_date": "2026-10-18"}]
        raise AssertionError(path)

    def tables_written(self):
        return [table for table, _ in self.writes]


def fake_refine(calls):
    def fn(interests, current, html, notes, *, rating=None, local_date=None):
        calls.append({"interests": interests, "current": current, "html": html,
                      "notes": notes, "rating": rating, "local_date": local_date})
        return {"compiled_profile": EDITED, "rationale": "less crypto"}
    return fn


def test_comment_is_saved_and_applied_as_a_new_version():
    rest, calls = FakeRest(), []
    out = save_feedback(rest, "u1", 7, -1, " crypto was filler ", refine_fn=fake_refine(calls))

    assert out == {"saved": True, "updated": True, "rationale": "less crypto"}
    assert rest.tables_written() == ["digest_feedback", "profile_versions"]
    feedback = rest.writes[0][1]
    assert (feedback["digest_id"], feedback["rating"], feedback["notes"]) == (7, -1, "crypto was filler")
    version = rest.writes[1][1]
    assert version == {"user_id": "u1", "compiled_profile": EDITED,
                       "source": "learned", "rationale": "less crypto"}
    assert calls[0]["current"] == PROFILE
    assert calls[0]["html"] == "<li>brief</li>"
    assert calls[0]["rating"] == -1


def test_no_comment_saves_without_an_llm_call():
    rest, calls = FakeRest(), []
    out = save_feedback(rest, "u1", 7, 1, "", refine_fn=fake_refine(calls))
    assert out == {"saved": True, "updated": False}
    assert rest.tables_written() == ["digest_feedback"]
    assert calls == []


def test_resaving_the_same_comment_does_not_apply_it_twice():
    rest, calls = FakeRest(previous_notes="crypto was filler"), []
    out = save_feedback(rest, "u1", 7, -1, "crypto was filler", refine_fn=fake_refine(calls))
    assert out["updated"] is False
    assert calls == []
    assert rest.tables_written() == ["digest_feedback"]


def test_an_edited_comment_is_applied():
    rest, calls = FakeRest(previous_notes="crypto was filler"), []
    out = save_feedback(rest, "u1", 7, -1, "crypto was filler, more chips",
                        refine_fn=fake_refine(calls))
    assert out["updated"] is True


@pytest.mark.parametrize("exc", [refine.RefinementRejected("too few sections"),
                                 RuntimeError("gemini 503")])
def test_failed_edit_keeps_the_feedback_and_the_profile(exc):
    def boom(*a, **kw):
        raise exc
    rest = FakeRest()
    out = save_feedback(rest, "u1", 7, None, "more chips", refine_fn=boom)
    assert out["saved"] is True and out["updated"] is False
    assert str(exc) in out["reason"]
    assert rest.tables_written() == ["digest_feedback"]


def test_user_without_a_profile_yet_is_not_edited():
    rest, calls = FakeRest(compiled_profile=None), []
    out = save_feedback(rest, "u1", 7, None, "more chips", refine_fn=fake_refine(calls))
    assert out["updated"] is False
    assert calls == []
