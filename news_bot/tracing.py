"""OpenTelemetry tracing for the worker, sent to Arize AX (or self-hosted Phoenix).

Off by default. Which backend is used depends on what's configured:

  Arize AX (hosted)     ARIZE_SPACE_ID + ARIZE_API_KEY. Exports to Arize's
                        collector (otlp.arize.com) — no endpoint needed.
  Phoenix (self-hosted) PHOENIX_COLLECTOR_ENDPOINT (+ PHOENIX_API_KEY).

When on:
  - every Gemini call is captured automatically by the OpenInference Google
    GenAI instrumentor (prompt, output, model, token counts, latency),
  - span() adds our own parent spans so one user's run reads as a tree:
    deliver -> generate -> (gemini call) / email / push / evaluate,
  - annotate() queues eval scores; flush() attaches them to their spans.

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
_backend: str | None = None
_pending_annotations: list[dict[str, Any]] = []

PROJECT_NAME = os.environ.get("TRACING_PROJECT_NAME") or os.environ.get(
    "PHOENIX_PROJECT_NAME", "mynews")


def enabled() -> bool:
    return _provider is not None


def configured_backend() -> str | None:
    """'arize', 'phoenix', or None, from the environment. Arize wins if both."""
    if os.environ.get("ARIZE_SPACE_ID") and os.environ.get("ARIZE_API_KEY"):
        return "arize"
    if os.environ.get("PHOENIX_COLLECTOR_ENDPOINT"):
        return "phoenix"
    return None


def traces_endpoint() -> str:
    """PHOENIX_COLLECTOR_ENDPOINT as the OTLP/HTTP traces URL.
    Accepts the base URL or the full route."""
    base = os.environ["PHOENIX_COLLECTOR_ENDPOINT"].strip().rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def _register(backend: str):
    if backend == "arize":
        from arize.otel import register
        return register(space_id=os.environ["ARIZE_SPACE_ID"],
                        api_key=os.environ["ARIZE_API_KEY"],
                        project_name=PROJECT_NAME, batch=True, verbose=False)
    from phoenix.otel import register
    # Given a bare base URL, phoenix register() falls back to gRPC on :4317,
    # which a Phoenix URL doesn't answer. Point it at the HTTP traces route.
    return register(project_name=PROJECT_NAME, batch=True, verbose=False,
                    endpoint=traces_endpoint(), protocol="http/protobuf")


def init_tracing() -> bool:
    """Start exporting traces if a backend is configured. Returns True if on."""
    global _provider, _backend
    if _provider is not None:
        return True
    backend = configured_backend()
    if backend is None:
        return False
    try:
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
        provider = _register(backend)
        GoogleGenAIInstrumentor().instrument(tracer_provider=provider)
    except ImportError as exc:
        print(f"Tracing configured but packages missing ({exc}); continuing without it.")
        return False
    except Exception as exc:  # tracing must never stop a delivery run
        print(f"Tracing setup failed ({exc!r}); continuing without it.")
        return False
    _provider, _backend = provider, backend
    print(f"Tracing to {'Arize AX' if backend == 'arize' else 'Phoenix'} "
          f"project '{PROJECT_NAME}'.")
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
    """Hex span id, the form both Arize and Phoenix expect, or None."""
    if s is None:
        return None
    return format(s.get_span_context().span_id, "016x")


def annotate(s: Any, name: str, *, kind: str, label: str | None = None,
             score: float | None = None, explanation: str | None = None) -> None:
    """Queue an eval result to attach to span `s`.

    Queued rather than sent now: the span itself is still open and batched, so
    the backend doesn't have it yet. flush() exports spans first, then sends.
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
    """Export all spans, then send queued eval results. Call before exit —
    GitHub Actions kills the process as soon as main() returns."""
    if _provider is None:
        return
    try:
        _provider.force_flush()
    except Exception as exc:
        print(f"Trace export failed: {exc!r}")
    if not _pending_annotations:
        return
    pending = list(_pending_annotations)
    _pending_annotations.clear()
    try:
        if _backend == "arize":
            _send_to_arize(pending)
        else:
            from phoenix.client import Client
            _send_annotations(Client(), pending)
        print(f"Sent {len(pending)} eval result(s) to "
              f"{'Arize AX' if _backend == 'arize' else 'Phoenix'}.")
    except Exception as exc:
        print(f"Sending eval results failed: {exc!r}")


# --- Arize AX ---------------------------------------------------------------

def arize_evals_rows(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per span, with Arize's eval columns:
    context.span_id, eval.<name>.label / .score / .explanation."""
    rows: dict[str, dict[str, Any]] = {}
    for a in annotations:
        row = rows.setdefault(a["span_id"], {"context.span_id": a["span_id"]})
        for field, value in a["result"].items():
            row[f"eval.{a['name']}.{field}"] = value
    return list(rows.values())


def _send_to_arize(annotations: list[dict[str, Any]], *,
                   delays: tuple[float, ...] = (5, 10, 20),
                   sleep_fn=time.sleep, client: Any = None) -> None:
    """Upload eval results for spans just exported.

    Arize ingests spans asynchronously, so an eval for a span it hasn't stored
    yet can fail; retry with backoff (and wait once up front for ingestion).
    """
    import pandas as pd

    if client is None:
        from arize import ArizeClient
        client = ArizeClient(api_key=os.environ["ARIZE_API_KEY"])
    df = pd.DataFrame(arize_evals_rows(annotations))
    last_exc: Exception | None = None
    for delay in delays:
        sleep_fn(delay)
        try:
            client.spans.update_evaluations(
                space_id=os.environ["ARIZE_SPACE_ID"], project_name=PROJECT_NAME,
                dataframe=df)
            return
        except Exception as exc:
            if type(exc).__name__ in {"AuthenticationError", "ValidationFailure"}:
                raise  # retrying can't fix bad credentials or a malformed frame
            last_exc = exc
    assert last_exc is not None
    raise last_exc


# --- Phoenix ----------------------------------------------------------------

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


# --- config check -------------------------------------------------------------

def check() -> int:
    """Send one test trace with an eval result, to verify configuration.

    Usage: python -m news_bot.tracing --check
    """
    if not init_tracing():
        print("Tracing is not configured (set ARIZE_SPACE_ID + ARIZE_API_KEY).")
        return 1
    with span("tracing_check", "CHAIN", **{"input.value": "config check"}) as s:
        annotate(s, "tracing_check", kind="CODE", label="ok", score=1.0,
                 explanation="Test eval from news_bot.tracing --check.")
    sid = span_id(s)
    flush()
    print(f"Sent test span {sid} to project '{PROJECT_NAME}'. "
          "Look for 'tracing_check' in the trace list.")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(check() if "--check" in sys.argv else 0)
