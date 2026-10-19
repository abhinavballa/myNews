"""Phoenix / OpenTelemetry tracing for the worker.

Off by default. Turned on when PHOENIX_COLLECTOR_ENDPOINT (and, for Phoenix
Cloud, PHOENIX_API_KEY) is set. Then:

  - every Gemini call is captured automatically by the OpenInference Google
    GenAI instrumentor (prompt, output, model, token counts, latency),
  - span() adds our own parent spans so one user's run reads as a tree:
    deliver -> generate -> (gemini call) / email / push / evaluate,
  - annotate() attaches eval scores to a span, shown as columns in Phoenix.

Every function here is a safe no-op when tracing is off or the packages
(requirements-worker.txt) aren't installed, so the Vercel functions and local
tests never depend on them.
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import Any, Iterator

_provider = None
_pending_annotations: list[dict[str, Any]] = []

PROJECT_NAME = os.environ.get("PHOENIX_PROJECT_NAME", "mynews")


def enabled() -> bool:
    return _provider is not None


def traces_endpoint() -> str:
    """PHOENIX_COLLECTOR_ENDPOINT as the OTLP/HTTP traces URL.
    Accepts the base URL (as Phoenix Cloud shows it) or the full route."""
    base = os.environ["PHOENIX_COLLECTOR_ENDPOINT"].strip().rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def init_tracing() -> bool:
    """Start exporting traces to Phoenix if configured. Returns True if on."""
    global _provider
    if _provider is not None:
        return True
    if not os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"):
        return False
    try:
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
        from phoenix.otel import register
    except ImportError as exc:
        print(f"Tracing configured but packages missing ({exc}); continuing without it.")
        return False
    try:
        # Given a bare base URL, register() falls back to gRPC on :4317, which
        # neither Phoenix Cloud nor a plain `phoenix serve` URL answers. Point
        # it at the HTTP traces route explicitly.
        _provider = register(project_name=PROJECT_NAME, batch=True, verbose=False,
                             endpoint=traces_endpoint(), protocol="http/protobuf")
        GoogleGenAIInstrumentor().instrument(tracer_provider=_provider)
    except Exception as exc:  # tracing must never stop a delivery run
        print(f"Tracing setup failed ({exc!r}); continuing without it.")
        _provider = None
        return False
    print(f"Tracing to Phoenix project '{PROJECT_NAME}'.")
    return True


@contextlib.contextmanager
def span(name: str, kind: str = "CHAIN", **attributes: Any) -> Iterator[Any]:
    """A child span with OpenInference `kind` (CHAIN, LLM, TOOL, EVALUATOR...).

    Yields the span (or None when tracing is off). Exceptions are recorded on
    the span and re-raised unchanged.
    """
    if _provider is None:
        yield None
        return
    tracer = _provider.get_tracer("news_bot")
    with tracer.start_as_current_span(name) as s:
        s.set_attribute("openinference.span.kind", kind)
        set_attributes(s, **attributes)
        yield s


def set_attributes(s: Any, **attributes: Any) -> None:
    """Set attributes, skipping None and coercing non-primitive values to str."""
    if s is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = [v if isinstance(v, (str, bool, int, float)) else str(v) for v in value]
        elif not isinstance(value, (str, bool, int, float)):
            value = str(value)
        s.set_attribute(key, value)


def span_id(s: Any) -> str | None:
    """Hex span id as Phoenix expects it, or None."""
    if s is None:
        return None
    return format(s.get_span_context().span_id, "016x")


def annotate(s: Any, name: str, *, kind: str, label: str | None = None,
             score: float | None = None, explanation: str | None = None) -> None:
    """Queue an eval result to attach to span `s` in Phoenix.

    Queued rather than sent now: the span itself is still open and batched, so
    Phoenix may not know it yet. flush() exports spans first, then sends these.
    """
    sid = span_id(s)
    if sid is None:
        return
    result = {k: v for k, v in
              {"label": label, "score": score, "explanation": explanation}.items()
              if v is not None}
    _pending_annotations.append({
        "span_id": sid, "name": name, "annotator_kind": kind, "result": result,
    })


def flush() -> None:
    """Export all spans, then send queued annotations. Call before exit —
    GitHub Actions kills the process as soon as main() returns."""
    if _provider is None:
        return
    try:
        _provider.force_flush()
    except Exception as exc:
        print(f"Trace export failed: {exc!r}")
    if not _pending_annotations:
        return
    try:
        from phoenix.client import Client
        _send_annotations(Client(), list(_pending_annotations))
        print(f"Sent {len(_pending_annotations)} eval annotation(s) to Phoenix.")
    except Exception as exc:
        print(f"Sending eval annotations failed: {exc!r}")
    finally:
        _pending_annotations.clear()


def _send_annotations(client: Any, annotations: list[dict[str, Any]], *,
                      delays: tuple[float, ...] = (1, 2, 4, 8),
                      sleep_fn=time.sleep) -> None:
    """Phoenix ingests exported spans asynchronously and answers 404 for an
    annotation whose span it hasn't stored yet. Retry 404s with backoff."""
    for delay in (*delays, None):
        try:
            client.spans.log_span_annotations(span_annotations=annotations, sync=True)
            return
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status != 404 or delay is None:
                raise
            sleep_fn(delay)
