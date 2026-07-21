"""Digest idempotency — a second worker run in the same local date must send
nothing and generate nothing."""

from __future__ import annotations

import datetime

import news_bot.main as worker
from news_bot.profiles import Profile


def make_profile():
    return Profile(
        id="u1", email="a@b.com", compiled_profile={"persona": "p", "edge_for": "e",
        "sections": [{"emoji": "x", "title": "T", "guidance": "g"}]},
        delivery_hour=8, timezone="America/Los_Angeles",
        wants_push=False, wants_email=True,
    )


def test_deliver_skips_when_digest_exists(monkeypatch):
    calls = {"generate": 0, "insert": 0, "send": 0}

    monkeypatch.setattr(worker.db, "digest_exists", lambda uid, d: True)
    monkeypatch.setattr(worker, "generate_digest_html",
                        lambda *a, **k: calls.__setitem__("generate", calls["generate"] + 1) or "x")
    monkeypatch.setattr(worker.db, "insert_digest",
                        lambda *a, **k: calls.__setitem__("insert", calls["insert"] + 1) or True)
    monkeypatch.setattr(worker, "send_email",
                        lambda *a, **k: calls.__setitem__("send", calls["send"] + 1))

    worker.deliver(make_profile(), "2026-07-21")

    assert calls == {"generate": 0, "insert": 0, "send": 0}


def test_deliver_does_not_send_when_insert_loses_race(monkeypatch):
    """digest_exists said no, but the INSERT hit the unique constraint (another
    concurrent run won). We must not email off an un-persisted digest."""
    sent = []

    monkeypatch.setattr(worker.db, "digest_exists", lambda uid, d: False)
    monkeypatch.setattr(worker, "generate_digest_html", lambda *a, **k: "fragment")
    monkeypatch.setattr(worker.db, "insert_digest", lambda *a, **k: False)  # duplicate
    monkeypatch.setattr(worker, "send_email", lambda *a, **k: sent.append(a))

    worker.deliver(make_profile(), "2026-07-21")

    assert sent == []


def test_deliver_generates_and_sends_on_first_run(monkeypatch):
    sent = []

    monkeypatch.setattr(worker.db, "digest_exists", lambda uid, d: False)
    monkeypatch.setattr(worker, "generate_digest_html", lambda *a, **k: "fragment")
    monkeypatch.setattr(worker.db, "insert_digest", lambda *a, **k: True)
    monkeypatch.setattr(worker, "send_email",
                        lambda html, recipient, subject_date: sent.append(recipient))

    worker.deliver(make_profile(), "2026-07-21")

    assert sent == ["a@b.com"]


def test_run_isolates_a_failing_profile(monkeypatch):
    good = make_profile()
    bad = Profile(id="u2", email="c@d.com", compiled_profile=good.compiled_profile,
                  delivery_hour=8, timezone="America/Los_Angeles",
                  wants_push=False, wants_email=True)

    monkeypatch.setattr(worker.db, "fetch_active_profiles",
                        lambda: [{"id": "u1"}, {"id": "u2"}])
    monkeypatch.setattr(worker.Profile, "from_row",
                        classmethod(lambda cls, row: good if row["id"] == "u1" else bad))

    now_utc = datetime.datetime(2026, 7, 21, 15, tzinfo=datetime.timezone.utc)

    delivered = []
    def fake_deliver(profile, local_date):
        if profile.id == "u2":
            raise RuntimeError("boom")
        delivered.append(profile.id)
    monkeypatch.setattr(worker, "deliver", fake_deliver)

    rc = worker.run(now_utc)

    assert delivered == ["u1"]   # good one still delivered
    assert rc == 1               # non-zero because one failed
