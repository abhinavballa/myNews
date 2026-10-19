"""Digest provenance: every brief records what produced it.

Without these columns a change in user ratings can't be attributed — a silent
model fallback would look like a profile effect — and they can't be backfilled.
"""

from __future__ import annotations

from types import SimpleNamespace

import news_bot.main as worker
from news_bot.generate import PROMPT_VERSION, Digest, generate_content_with_model
from news_bot.profiles import Profile


class FakeError(Exception):
    def __init__(self, msg="", code=None):
        super().__init__(msg)
        self.code = code


class FakeClient:
    def __init__(self, script):
        self.script = list(script)

    @property
    def models(self):
        return self

    def generate_content(self, model, contents, config):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_reports_the_model_that_actually_answered():
    ok = SimpleNamespace(text="brief")
    client = FakeClient([FakeError(code=503), ok])
    _resp, model = generate_content_with_model(
        client, "p", None, models=["flash", "pro"],
        attempts_per_model=1, sleep_fn=lambda s: None,
    )
    assert model == "pro"  # the primary failed; the fallback is what ran


def test_reports_the_primary_when_it_succeeds():
    ok = SimpleNamespace(text="brief")
    client = FakeClient([ok])
    _resp, model = generate_content_with_model(
        client, "p", None, models=["flash", "pro"], sleep_fn=lambda s: None,
    )
    assert model == "flash"


def test_deliver_records_profile_version_model_and_prompt_version(monkeypatch):
    recorded = {}

    profile = Profile(
        id="u1", email="a@b.com",
        compiled_profile={"persona": "p", "edge_for": "e",
                          "sections": [{"emoji": "x", "title": "T", "guidance": "g"}]},
        delivery_hour=8, timezone="America/Los_Angeles",
        wants_push=False, wants_email=False, active_version=4,
    )

    monkeypatch.setattr(worker.db, "digest_exists", lambda uid, d: False)
    monkeypatch.setattr(worker, "generate_digest", lambda *a, **k: Digest(
        html="fragment", model="gemini-3.6-flash", prompt_version=PROMPT_VERSION))

    def fake_insert(user_id, local_date, html, teaser=None, **kw):
        recorded.update(kw)
        recorded["teaser"] = teaser
        return True
    monkeypatch.setattr(worker.db, "insert_digest", fake_insert)

    worker.deliver(profile, "2026-10-03")

    assert recorded["profile_version"] == 4
    assert recorded["model"] == "gemini-3.6-flash"
    assert recorded["prompt_version"] == PROMPT_VERSION
    # The teaser column was previously never populated; push and the stored row
    # should agree on one value.
    assert recorded["teaser"]


def test_profile_carries_active_version_from_the_row():
    p = Profile.from_row({"id": "u1", "active_version": 3,
                          "compiled_profile": {"persona": "p"}})
    assert p.active_version == 3


def test_profile_tolerates_missing_active_version():
    """Rows written before the migration have no active_version."""
    p = Profile.from_row({"id": "u1", "compiled_profile": {"persona": "p"}})
    assert p.active_version is None
