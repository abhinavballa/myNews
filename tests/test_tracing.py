"""Tracing: a no-op when off, and one trace tree per user when on.

The "on" tests need the worker packages (requirements-worker.txt) and are
skipped without them.
"""

from __future__ import annotations

import pytest

import news_bot.main as worker
from news_bot import tracing
from news_bot.generate import PROMPT_VERSION, Digest
from news_bot.profiles import Profile


@pytest.fixture(autouse=True)
def _reset_tracing(monkeypatch):
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(tracing, "_pending_annotations", [])


def test_off_without_endpoint(monkeypatch):
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    assert tracing.init_tracing() is False
    with tracing.span("x", foo=1) as s:
        assert s is None
    tracing.annotate(s, "e", kind="CODE", score=1.0)
    tracing.flush()  # must not raise
    assert tracing._pending_annotations == []


def profile():
    return Profile(
        id="u1", email="a@b.com",
        compiled_profile={"persona": "p", "edge_for": "e",
                          "sections": [{"emoji": "🤖", "title": "AI", "guidance": "g"}]},
        delivery_hour=8, timezone="America/Los_Angeles",
        wants_push=False, wants_email=False, active_version=4,
    )


def stub_worker(monkeypatch, grounding=None):
    monkeypatch.setattr(worker.db, "digest_exists", lambda uid, d: False)
    monkeypatch.setattr(worker.db, "insert_digest", lambda *a, **k: True)
    monkeypatch.setattr(worker, "generate_digest", lambda *a, **k: Digest(
        html="<h2>🤖 AI</h2><ul><li><strong>Nvidia</strong> up</li></ul>",
        model="gemini-3.5-flash", prompt_version=PROMPT_VERSION,
        grounding=grounding or {"queries": ["nvidia"], "sources": [{"title": "t", "uri": "u"}],
                                "supports": [(0, 10)], "text_length": 20}))


def test_deliver_works_and_still_evaluates_with_tracing_off(monkeypatch, capsys):
    stub_worker(monkeypatch)
    worker.deliver(profile(), "2026-10-18")
    out = capsys.readouterr().out
    assert "eval grounding_coverage: 0.50" in out
    assert "eval section_structure: 1.00" in out


def test_a_crashing_eval_module_cannot_break_delivery(monkeypatch):
    stub_worker(monkeypatch)
    inserted = []
    monkeypatch.setattr(worker.db, "insert_digest", lambda *a, **k: inserted.append(1) or True)

    def boom(*a, **k):
        raise RuntimeError("judge exploded")
    monkeypatch.setattr(worker.evals, "grounding_coverage", boom)
    worker.deliver(profile(), "2026-10-18")
    assert inserted == [1]


# --- with the OpenTelemetry SDK installed -----------------------------------

@pytest.fixture
def exporter(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(tracing, "_provider", provider)
    return exp


def test_deliver_produces_one_trace_tree(monkeypatch, exporter):
    stub_worker(monkeypatch)
    worker.deliver(profile(), "2026-10-18")

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"deliver", "generate_digest", "evaluate"}
    root = spans["deliver"]
    assert root.parent is None
    for child in ("generate_digest", "evaluate"):
        assert spans[child].parent.span_id == root.context.span_id
        assert spans[child].context.trace_id == root.context.trace_id

    assert root.attributes["openinference.span.kind"] == "CHAIN"
    assert root.attributes["user.id"] == "u1"
    assert root.attributes["mynews.profile_version"] == 4
    assert root.attributes["mynews.outcome"] == "delivered"
    gen = spans["generate_digest"].attributes
    assert gen["llm.model_name"] == "gemini-3.5-flash"
    assert tuple(gen["mynews.search_queries"]) == ("nvidia",)
    assert gen["mynews.source_count"] == 1
    assert spans["evaluate"].attributes["openinference.span.kind"] == "EVALUATOR"


def test_eval_scores_are_queued_as_annotations_on_the_root(monkeypatch, exporter):
    stub_worker(monkeypatch)
    worker.deliver(profile(), "2026-10-18")
    root = next(s for s in exporter.get_finished_spans() if s.name == "deliver")

    queued = {a["name"]: a for a in tracing._pending_annotations}
    assert set(queued) == {"grounding_coverage", "section_structure"}
    a = queued["grounding_coverage"]
    assert a["span_id"] == format(root.context.span_id, "016x")
    assert a["annotator_kind"] == "CODE"
    assert a["result"]["score"] == 0.5
    assert "explanation" in a["result"]


def test_failed_delivery_is_recorded_on_the_span(monkeypatch, exporter):
    monkeypatch.setattr(worker.db, "digest_exists", lambda uid, d: False)

    def boom(*a, **k):
        raise RuntimeError("gemini down")
    monkeypatch.setattr(worker, "generate_digest", boom)
    with pytest.raises(RuntimeError):
        worker.deliver(profile(), "2026-10-18")
    root = next(s for s in exporter.get_finished_spans() if s.name == "deliver")
    assert root.status.status_code.name == "ERROR"


def test_flush_sends_queued_annotations(monkeypatch, exporter):
    pytest.importorskip("phoenix.client")
    sent = {}

    class FakeSpans:
        def log_span_annotations(self, *, span_annotations, sync):
            sent["annotations"] = span_annotations

    class FakeClient:
        spans = FakeSpans()

    import phoenix.client
    monkeypatch.setattr(phoenix.client, "Client", FakeClient)
    with tracing.span("deliver") as s:
        tracing.annotate(s, "section_structure", kind="CODE", score=1.0, label="complete")
    tracing.flush()
    assert sent["annotations"][0]["result"] == {"score": 1.0, "label": "complete"}
    assert tracing._pending_annotations == []


class _NotFound(Exception):
    def __init__(self):
        super().__init__("404")
        self.response = type("R", (), {"status_code": 404})()


def test_annotations_retry_until_phoenix_has_the_span():
    calls = []

    class Spans:
        def log_span_annotations(self, *, span_annotations, sync):
            calls.append(1)
            if len(calls) < 3:
                raise _NotFound()

    client = type("C", (), {"spans": Spans()})()
    slept = []
    tracing._send_annotations(client, [{}], sleep_fn=slept.append)
    assert len(calls) == 3 and slept == [1, 2]


def test_annotations_give_up_after_the_last_retry():
    class Spans:
        def log_span_annotations(self, *, span_annotations, sync):
            raise _NotFound()

    client = type("C", (), {"spans": Spans()})()
    with pytest.raises(_NotFound):
        tracing._send_annotations(client, [{}], delays=(1, 2), sleep_fn=lambda s: None)


def test_other_errors_are_not_retried():
    calls = []

    class Spans:
        def log_span_annotations(self, *, span_annotations, sync):
            calls.append(1)
            raise ValueError("bad request")

    client = type("C", (), {"spans": Spans()})()
    with pytest.raises(ValueError):
        tracing._send_annotations(client, [{}], sleep_fn=lambda s: None)
    assert calls == [1]


@pytest.mark.parametrize("value,expected", [
    ("https://app.phoenix.arize.com/s/me", "https://app.phoenix.arize.com/s/me/v1/traces"),
    ("http://localhost:6006/", "http://localhost:6006/v1/traces"),
    ("http://localhost:6006/v1/traces", "http://localhost:6006/v1/traces"),
])
def test_traces_endpoint_accepts_base_or_full_url(monkeypatch, value, expected):
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", value)
    assert tracing.traces_endpoint() == expected
