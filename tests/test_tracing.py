"""Tracing: a no-op when off, and one trace tree per user when on.

The "on" tests need the worker packages (requirements-worker.txt) and are
skipped without them.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

import news_bot.main as worker
from news_bot import tracing
from news_bot.generate import PROMPT_VERSION, Digest
from news_bot.profiles import Profile


@pytest.fixture(autouse=True)
def _reset_tracing(monkeypatch):
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(tracing, "_backend", None)
    monkeypatch.setattr(tracing, "_pending_annotations", [])
    for var in ("ARIZE_SPACE_ID", "ARIZE_API_KEY", "PHOENIX_COLLECTOR_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)


def test_off_without_endpoint(monkeypatch):
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


def test_every_span_has_an_input_and_output(monkeypatch, exporter):
    """Arize's trace list shows the root span's input/output; blank = useless."""
    stub_worker(monkeypatch)
    worker.deliver(profile(), "2026-10-18")
    spans = {s.name: s.attributes for s in exporter.get_finished_spans()}

    root = spans["deliver"]
    assert "Daily brief for Sunday, October 18, 2026" in root["input.value"]
    assert "AI: g" in root["input.value"]  # the profile's sections
    assert root["output.value"].startswith("<h2>")  # the brief itself

    gen = spans["generate_digest"]
    assert "You are a sharp research analyst" in gen["input.value"]  # the real prompt
    assert gen["output.value"].startswith("<h2>")

    ev = spans["evaluate"]
    assert ev["input.value"].startswith("<h2>")
    scores = json.loads(ev["output.value"])
    assert {s["name"] for s in scores} == {"grounding_coverage", "section_structure"}


def test_skipped_delivery_says_why_in_the_output(monkeypatch, exporter):
    monkeypatch.setattr(worker.db, "digest_exists", lambda uid, d: True)
    worker.deliver(profile(), "2026-10-18")
    root = exporter.get_finished_spans()[0].attributes
    assert "already delivered" in root["output.value"]


def test_email_and_push_spans_record_what_was_sent(monkeypatch, exporter):
    stub_worker(monkeypatch)
    monkeypatch.setattr(worker, "send_email", lambda *a, **k: None)
    monkeypatch.setattr(worker.db, "fetch_push_subscriptions", lambda uid: [])
    p = dataclasses.replace(profile(), wants_email=True, wants_push=True)
    worker.deliver(p, "2026-10-18")
    spans = {s.name: s.attributes for s in exporter.get_finished_spans()}
    assert spans["send_email"]["input.value"].startswith("To a...@b.com")
    assert spans["send_email"]["output.value"] == "sent"
    assert spans["send_push"]["output.value"] == "pushed to 0/0 device(s)"


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


# --- backend selection + Arize AX -------------------------------------------

def test_arize_needs_both_space_and_key(monkeypatch):
    monkeypatch.setenv("ARIZE_SPACE_ID", "space")
    assert tracing.configured_backend() is None
    monkeypatch.setenv("ARIZE_API_KEY", "key")
    assert tracing.configured_backend() == "arize"


def test_arize_wins_over_phoenix(monkeypatch):
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    assert tracing.configured_backend() == "phoenix"
    monkeypatch.setenv("ARIZE_SPACE_ID", "space")
    monkeypatch.setenv("ARIZE_API_KEY", "key")
    assert tracing.configured_backend() == "arize"


def test_arize_rows_group_evals_by_span():
    rows = tracing.arize_evals_rows([
        {"span_id": "a", "name": "grounding_coverage", "result": {"score": 0.5, "label": "grounded"}},
        {"span_id": "a", "name": "section_structure", "result": {"score": 1.0}},
        {"span_id": "b", "name": "section_structure", "result": {"score": 0.0}},
    ])
    assert rows == [
        {"context.span_id": "a", "eval.grounding_coverage.score": 0.5,
         "eval.grounding_coverage.label": "grounded", "eval.section_structure.score": 1.0},
        {"context.span_id": "b", "eval.section_structure.score": 0.0},
    ]


class FakeArizeSpans:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = []

    def update_evaluations(self, *, space_id, project_name, dataframe):
        self.calls.append((space_id, project_name, dataframe))
        if self.errors:
            raise self.errors.pop(0)


def test_arize_upload_sends_one_frame(monkeypatch):
    pytest.importorskip("pandas")
    monkeypatch.setenv("ARIZE_SPACE_ID", "space")
    spans = FakeArizeSpans()
    client = type("C", (), {"spans": spans})()
    tracing._send_to_arize(
        [{"span_id": "a", "name": "e", "result": {"score": 1.0}}],
        client=client, sleep_fn=lambda s: None)
    space, project, df = spans.calls[0]
    assert (space, project) == ("space", tracing.PROJECT_NAME)
    assert list(df.columns) == ["context.span_id", "eval.e.score"]


def test_arize_upload_retries_until_the_span_is_ingested(monkeypatch):
    pytest.importorskip("pandas")
    monkeypatch.setenv("ARIZE_SPACE_ID", "space")
    spans = FakeArizeSpans(errors=[RuntimeError("span not found")])
    client = type("C", (), {"spans": spans})()
    slept = []
    tracing._send_to_arize([{"span_id": "a", "name": "e", "result": {"score": 1.0}}],
                           client=client, sleep_fn=slept.append)
    assert len(spans.calls) == 2 and slept == [5, 15]


def test_arize_upload_does_not_retry_bad_credentials(monkeypatch):
    pytest.importorskip("pandas")
    monkeypatch.setenv("ARIZE_SPACE_ID", "space")

    class AuthenticationError(Exception):
        pass
    spans = FakeArizeSpans(errors=[AuthenticationError("401")])
    client = type("C", (), {"spans": spans})()
    with pytest.raises(AuthenticationError):
        tracing._send_to_arize([{"span_id": "a", "name": "e", "result": {"score": 1.0}}],
                               client=client, sleep_fn=lambda s: None)
    assert len(spans.calls) == 1


def test_check_reports_when_not_configured(capsys):
    assert tracing.check() == 1
    assert "not configured" in capsys.readouterr().out


# --- preview mode -------------------------------------------------------------

def test_preview_generates_and_evaluates_but_saves_and_sends_nothing(monkeypatch, capsys):
    stub_worker(monkeypatch)
    monkeypatch.setattr(worker.db, "digest_exists", lambda uid, d: True)  # even if delivered

    def must_not_call(*a, **k):
        raise AssertionError("preview must not write or send")
    monkeypatch.setattr(worker.db, "insert_digest", must_not_call)
    monkeypatch.setattr(worker, "send_email", must_not_call)
    monkeypatch.setattr(worker, "_deliver_push", must_not_call)
    p = dataclasses.replace(profile(), wants_email=True, wants_push=True)

    worker.deliver(p, "2026-10-18", preview=True)
    out = capsys.readouterr().out
    assert "PREVIEW" in out
    assert "eval section_structure" in out


def test_preview_requires_a_user(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["main", "--preview"])
    assert worker.main() == 2
    assert "--preview requires --user" in capsys.readouterr().out
