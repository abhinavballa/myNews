# Observability (Arize AX tracing) and brief evals

**Date:** 2026-10-18
**Status:** implemented. Revised 2026-10-22: hosted backend is Arize AX (was Phoenix).

## Problem

The worker's only visibility was CI log lines. There was no record of what each
Gemini call cost (tokens), how long it took, what search queries grounding ran, or
whether a brief was any good — so a quality regression (a prompt change, a silent
model fallback, a bad profile edit) would only surface as a user complaint.

## What this builds

### Tracing (`news_bot/tracing.py`)

- `arize.otel.register()` (Arize AX; or `phoenix.otel.register()` for self-hosted
  Phoenix) + the OpenInference **Google GenAI instrumentor**: every
  Gemini call becomes an LLM span with prompt, output, model, token counts, latency.
- Our own spans make each user's run one trace tree:
  `deliver` → `generate_digest` (search queries, source count, model, prompt version)
  → Gemini LLM span; `send_email` / `send_push`; `evaluate`.
- **Off unless configured:** `ARIZE_SPACE_ID` + `ARIZE_API_KEY` → Arize AX (hosted
  collector `otlp.arize.com`, no endpoint needed); else `PHOENIX_COLLECTOR_ENDPOINT` →
  self-hosted Phoenix. Every helper is a no-op when off
  or when the packages are missing, so the Vercel functions and tests don't depend on
  them. Tracing setup failures are logged and never stop a delivery run.
- `flush()` runs before the process exits (GitHub Actions kills it on return), then
  sends queued eval results. Arize AX: one dataframe via
  `ArizeClient.spans.update_evaluations` with `context.span_id` +
  `eval.<name>.label/.score/.explanation` columns (validated offline against Arize's
  own validators), retried with backoff while spans ingest. Phoenix: span annotations.
- `python -m news_bot.tracing --check` (workflow input `check_tracing`) sends one test
  span + eval to confirm configuration without delivering anything.

Two things found by running against a real Phoenix server:

1. Given a bare base URL, `register()` falls back to gRPC on :4317. We pass the
   OTLP/HTTP `/v1/traces` route explicitly (`traces_endpoint()`).
2. Phoenix ingests spans asynchronously and answers **404** to an annotation whose
   span isn't stored yet. Annotation sends retry 404s with backoff (1, 2, 4, 8s).

### Evals (`news_bot/evals.py`)

Run after the brief is delivered, per user:

| Eval | Kind | What it measures |
|---|---|---|
| `profile_adherence` | LLM judge (`phoenix.evals.create_classifier`, Gemini) | Did the brief follow the persona, section guidance, and Edge angle? adherent 1.0 / partial 0.5 / not 0.0 |
| `grounding_coverage` | Code | Share of the brief's bytes that Gemini attributed to a search source (merged `grounding_supports` spans / text length) |
| `section_structure` | Code | Share of profile sections present as `<h2>`, in order |

Results are printed to the log and attached to the user's `deliver` span as evals
(Arize AX) or annotations (Phoenix), so they show as columns in the trace list.

### Why not Phoenix's Hallucination / Faithfulness evaluator

Both judge a response against source *text*. Gemini's Google Search grounding returns
only each source's title and URI, plus which spans of the response each source
supports — never page content. A judge given titles would mark every real number as
unsupported. The citation map is exact, so `grounding_coverage` uses it directly.

**Limit:** coverage flags text with no source behind it; it cannot catch a wrong
number that *is* attributed to a source. Catching that would need fetching the
cited pages and running a faithfulness judge over them — real cost and latency per
brief, deferred until there's evidence it's needed.

### Why record-only, not a blocking gate

Evals run after delivery and never block it. A gate (regenerate or hold a failing
brief) doubles the Gemini cost on failures and risks a user getting nothing because
a judge misfired. The scores come first; with a few weeks of data it's clear which
threshold, if any, is worth gating on.

## Config

| Where | Name | Purpose |
|---|---|---|
| GitHub secret | `ARIZE_SPACE_ID` | Arize AX space — with the key, turns tracing on |
| GitHub secret | `ARIZE_API_KEY` | Arize AX API key |
| env (self-hosted only) | `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_API_KEY` | Use Phoenix instead of AX |
| GitHub variable | `MYNEWS_LLM_EVALS` | `off` skips the LLM judge (1 extra Gemini call per brief) |
| env (optional) | `TRACING_PROJECT_NAME`, `EVAL_MODEL` | default `mynews`, `gemini-3.5-flash` |

Worker-only dependencies live in `requirements-worker.txt` (phoenix-evals pulls in
pandas; keeping it out of `requirements.txt` keeps the Vercel functions small).

## Testing

`tests/test_evals.py` (grounding extraction, coverage math, section order, judge
wiring with a fake, failures never raise) and `tests/test_tracing.py` (no-op when off;
with the OTel SDK: one trace tree per user, attributes, error status, annotations on
the root span, 404 retry). Verified end to end against a local `phoenix serve`:
spans, the instrumentor's LLM span, and both code-eval annotations arrived.

First production run (2026-10-19, 3 users) surfaced a third fix: the judge returned
`PASS`/`FAIL` instead of its labels. With label descriptions, `create_classifier`
emits a JSON-Schema `oneOf`/`const` for the label, which Gemini's `response_schema`
doesn't enforce, and the prompt never named the labels. Now the choices are plain
`{label: score}` (emits an `enum`, which Gemini enforces) and the prompt lists the
labels with their meanings. That run also showed the code evals working: grounding
coverage of 36% for two briefs (15–22 sources) and **0% for one brief generated with
no search at all** — exactly the case the eval exists to catch.
