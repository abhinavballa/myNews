# Feedback-driven profile edits

**Date:** 2026-10-03, revised 2026-10-18
**Status:** approved, implemented

## Problem

Personalization was static. A user's `interests_text` is compiled once into a
`compiled_profile` (persona, edge_for, sections[]), and every brief comes from that
fixed prompt. If the briefs are consistently wrong for someone — too much crypto,
generic Edge clauses, a filler section — nothing changes unless they rewrite their
interests from scratch.

## What this builds

When a user writes a comment on a brief, one LLM call edits their `compiled_profile`
to act on it. The previous profiles are kept (last 5) so a bad edit can be undone.

That's the whole loop. No grading, no scores, no batch job.

### Why not the earlier design

The first draft (2026-10-03) was framed as on-policy distillation: a stronger "teacher"
model graded the brief span by span, a weekly job batched 3+ feedback items per user,
and a watermark tracked which feedback had been folded in. It was dropped because:

- The grading step produced nothing the system used — the edited profile was the only
  output that mattered. A critique-then-edit prompt is just a longer edit prompt.
- Batching meant a user's comment took up to a week to have any effect, so they never
  saw cause and effect.
- The watermark, minimum window, and separate workflow existed only to serve the
  batch. With per-comment edits, "don't apply the same comment twice" reduces to
  "skip if the comment text didn't change".

The safety the batch provided ("one grumpy morning can't rewrite a profile") is
covered instead by bounded edits plus undo.

### The loop

1. The user reads a brief (Today or Archive) and writes a comment, optionally with a
   thumb, then taps Save.
2. `POST /api/feedback` upserts the `digest_feedback` row.
3. If the comment is new text, it sends the comment, that brief, the user's current
   profile, and their `interests_text` to the LLM, which returns an edited profile
   and a one-line rationale.
4. The edit passes the guardrails and is inserted into `profile_versions`; the DB
   trigger makes it active and prunes to the newest 5.
5. Tomorrow's brief uses it. Settings shows the rationale and an **Undo last change**
   button.

A thumb tap alone saves directly from the client and triggers nothing — a thumb
doesn't say what to change.

## Schema

### `digest_feedback`

One editable row per user per brief: `rating smallint (-1|1, nullable)`, `notes text`,
`unique (user_id, digest_id)` so it is an upsert. Feedback is per-brief, not per-item:
the brief is unstructured HTML with no item identity, and free text lets the user
quote the items they mean.

### `profile_versions`

```sql
create table profile_versions (
  id               bigserial primary key,
  user_id          uuid  references profiles(id) on delete cascade,
  version          int   not null,
  compiled_profile jsonb not null,
  source           text  not null check (source in ('user', 'learned')),
  rationale        text,
  created_at       timestamptz default now(),
  unique (user_id, version)
);
```

One row per (user, version) rather than a wide user × date table: a column per date
would need a schema change for every new day, and "the version before this one" is a
one-row lookup in the long shape.

`profiles.compiled_profile` stays the *active* profile, so the worker is unchanged.
`profiles.active_version` points at the live row.

### Triggers

- **Before insert** (`activate_profile_version`): assigns `version = max + 1` for the
  user (no client-side race) and copies the row into `profiles.compiled_profile` /
  `active_version`. Inserting a version *is* activating it.
- **After insert** (`prune_profile_versions`): deletes all but the newest 5 versions
  for that user. The just-inserted row is newest, so it always survives.

Undo is not an insert: it points `profiles` at the previous existing version, so it
doesn't create history. Because of the cap, a user's original compiled profile is
eventually pruned; "Rebuild brief" from their interests text is the full reset.

### Attribution columns

`digests.profile_version`, `digests.model`, `digests.prompt_version` record what
produced each brief, so a thumb can later be tied to the profile version (or model
fallback, or prompt change) behind it. Cheap, and can't be backfilled.

## Components

- `news_bot/refine.py` — `build_refine_prompt` (pure), `check_revision` (guardrails),
  `refine_profile(interests_text, compiled_profile, brief_html, notes, rating=, local_date=)`
  using the normal `generate_content_resilient` model chain.
- `news_bot/feedback.py` — `save_feedback(rest, ...)`: the endpoint's flow, with the
  Supabase client injected so it's tested without a network.
- `news_bot/user_api.py` — shared "call Supabase REST as the signed-in user" helpers
  for both Vercel endpoints. Requests carry the user's token, so RLS applies.
- `api/feedback.py` — thin HTTP handler.
- PWA — Save posts to `/api/feedback` and toasts the rationale; Settings shows the last
  change and Undo.

## Guardrails

- **`interests_text` is invariant.** Passed as a hard constraint; only the derived
  profile moves. Otherwise feedback drift slowly turns an AI brief into a sports brief.
- **Schema validation** via `validate_compiled_profile`.
- **Bounded change.** 6–9 sections and within ±2 of the current count.
- **Feedback is never lost.** If the LLM call fails or the edit is rejected, the
  comment is still saved and the profile is unchanged.
- **No double-apply.** Re-saving the same comment text doesn't edit again.
- **Undoable.** Last 5 versions kept.
- **Off the delivery path.** Runs in the user's request, not the hourly worker.
- **Bounded input.** The brief is truncated before it goes to the model.

## Testing

`tests/test_refine.py` (prompt, guardrails, stubbed LLM call) and
`tests/test_feedback.py` (save + apply, no-comment, unchanged comment, failed edit,
no profile yet). The PWA has no JS harness; verify manually after deploy.

## Rollout

1. Run `supabase/migrations/2026-10-03-feedback-and-profile-versions.sql` in the
   Supabase SQL editor (safe to re-run; drops the old `feedback_through` column if an
   earlier draft was applied).
2. Push — Vercel deploys the PWA and the new `/api/feedback` function (it uses the
   existing `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_ANON_KEY` env vars).
3. Leave a comment on a brief and check that Settings shows the change and Undo works.
