# Personalized Multi-User Digest + Push Notifications — Design

**Date:** 2026-07-20
**Repo:** abhinavballa/myNews
**Supersedes parts of:** `2026-07-17-daily-news-digest-design.md` (single-user, single-prompt, email-only)

## Purpose

Turn the single-user email bot into a small self-serve product: each user
describes what they want to know about in a free-text box, and receives a daily
brief built around *their* interests — delivered as a phone notification, an
email, or both.

The motivating example: the author wants AI news plus money/build angles. His
sister wants entertainment industry news. Same system, different briefs.

## What changes and what does not

**Preserved:** `email_send.py` — `EMAIL_TEMPLATE`, `FRAGMENT_STYLE`, the SMTP
code, and the `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` secrets. The styled email
looks the same as today. Email is an additional channel alongside push, never
replaced by it.

**Retired:** the `RECIPIENT` env var (the recipient list becomes the `profiles`
table) and the Bcc behaviour in `send_email`. Bcc exists today only because
everyone receives an identical digest; once briefs are personalized there is
nothing to batch, so each email is addressed individually to one recipient.

**Changed:** `generate.py`'s hardcoded `PROMPT` becomes a per-user compiled
profile. `main.py`'s global `SEND_HOUR_PACIFIC` gate becomes a per-user
timezone query.

## Decisions

| Area | Decision |
|---|---|
| Audience | Small private group, self-serve signup (~10-100 users) |
| App | Installable PWA (Add to Home Screen), not a native/App Store app |
| Backend | Supabase (Postgres + magic-link auth) + GitHub Actions cron |
| Hosting | Vercel — static PWA plus one Python serverless function |
| Personalization | Free text compiled once into a structured profile, user confirms |
| Notification | One daily teaser push; tap opens full brief in-app |
| Schedule | Per-user delivery hour in the user's own timezone |

### Why a PWA

No Apple Developer account, no $99/yr, no App Store review, and distribution is
just a URL — which matters for a private group. The entire backend carries over
unchanged if this later becomes a native app.

Accepted tradeoff: on iOS, notifications require the user to add the app to
their home screen first, and the permission prompt must be triggered by a tap.
This is a real onboarding friction point and shapes the onboarding sequence
below.

### Why compile the text box instead of injecting it raw

Injecting raw text into the prompt each day lets section structure drift day to
day, makes quality depend entirely on the user's phrasing, gives the user no
preview of what they will receive, and passes unstructured user text to the
model.

Compiling once on save produces a stable structure, lets the app show the user
their sections for confirmation, and — because the compiler emits a fixed
schema — contains prompt-injection from the text box.

## Architecture

```
news_bot/          Python worker — GitHub Actions, hourly
web/               Static PWA — signup, settings, digest reader
api/               One Python serverless function: POST /api/compile-profile
supabase/          schema.sql
```

Compiling needs a Gemini call while the user waits, and a static page cannot
hold the Gemini API key. Hence exactly one server endpoint. It runs on Vercel's
Python runtime so it imports the existing generation code rather than
introducing TypeScript. The caller passes its Supabase access token; the
function writes as that user so RLS still applies.

## Data model

```sql
profiles
  id uuid primary key references auth.users,
  email text,
  interests_text text,          -- raw text box content
  compiled_profile jsonb,       -- see schema below
  compiled_at timestamptz,
  delivery_hour int default 8,  -- 0-23, user's local time
  timezone text default 'America/Los_Angeles',
  wants_push boolean default true,
  wants_email boolean default false,
  active boolean default true,
  created_at timestamptz default now()

push_subscriptions
  id bigserial primary key,
  user_id uuid references profiles(id) on delete cascade,
  endpoint text unique,
  p256dh text, auth text,
  failure_count int default 0,
  created_at timestamptz default now()

digests
  id bigserial primary key,
  user_id uuid references profiles(id) on delete cascade,
  local_date date,
  teaser text,
  html text,
  created_at timestamptz default now(),
  unique (user_id, local_date)
```

`UNIQUE (user_id, local_date)` is load-bearing. GitHub Actions re-runs and
delayed schedules are both routine; without it a retry double-notifies every
user.

`compiled_profile` is the contract between the compile step and the worker:

```json
{
  "persona": "entertainment industry follower",
  "edge_for": "what to watch, plus industry and culture angles",
  "sections": [
    {"emoji": "🎬", "title": "Box Office & Releases", "guidance": "..."}
  ]
}
```

`wants_push` and `wants_email` are independent — both true is valid.

**RLS:** each user may read and write only rows where `user_id = auth.uid()`.
The worker uses the service-role key (a GitHub secret) and bypasses RLS.

**Seed:** the author's account is seeded with a `compiled_profile` hand-derived
from the current `PROMPT`, and `wants_email = true`, so the existing brief does
not regress.

## Daily flow

The cron changes from two daily triggers to hourly (`0 * * * *`). Each run:

1. Fetch all `active` profiles (~100 rows; filter in Python, no SQL cleverness
   needed at this scale).
2. Keep users whose current local hour equals their `delivery_hour` and who
   have no `digests` row for their current local date.
3. For each due user: generate the digest from `compiled_profile`, insert the
   `digests` row, then deliver — push to each subscription if `wants_push`,
   email if `wants_email`.
4. Wrap each user in try/except. One failing profile must never abort the run.

`is_send_time()` is not deleted; its logic moves from a module constant to a
per-user timezone comparison.

## Notifications

Web Push with VAPID, sent via `pywebpush`. The keypair is generated once: the
public key ships in the PWA, the private key becomes a GitHub secret.

The teaser is produced by the same Gemini call that writes the digest — one
line hooking that day's most notable item.

On HTTP 410 or 404 from a push service, delete that subscription row.

**Onboarding sequence** (ordered around the iOS constraints):
sign in → interests text box → confirm compiled sections → pick delivery hour →
Add to Home Screen instructions → tap "Enable notifications".

## PWA screens

1. **Sign in** — Supabase magic link
2. **Onboarding** — as sequenced above
3. **Today** — the full digest HTML
4. **Archive** — list of past digests
5. **Settings** — edit interests (triggers recompile), hour, timezone, channels

## Error handling

- Gemini returns too little content → existing length check raises; that user
  is skipped for the day and logged loudly. No `digests` row is written.
- Compile endpoint fails → returns an error to the app; the user keeps their
  previous profile.
- Push returns 410/404 → prune that subscription row.
- Email send fails → logged; does not block push delivery, and vice versa.

## Testing

The current codebase has no tests. Add pytest, covering:

- Due-user selection across timezones **and DST boundaries** — the most likely
  source of real bugs.
- `compiled_profile` schema validation — malformed compiler output must be
  rejected at save time, not at 8am.
- Digest idempotency — a second worker run in the same local date sends nothing.
- Push subscription pruning on 410.

## Phasing

Three phases, each ending in something that works.

**Phase 1 — schema + per-user worker, email delivery only.** No PWA, no push.
Ends with the author's brief still arriving at 8am, driven by a database row
instead of a hardcoded constant.

Phase 1 exists to answer the one genuinely uncertain question in this project:
does a compiled profile produce a brief as good as the hand-tuned `PROMPT`?
Everything else here is known-shape work. Judging that early, in the inbox,
avoids building a frontend on a foundation that turns out to be weaker.

**Phase 2 — PWA, auth, compile endpoint.** Self-serve signup, the text box,
the confirmation screen, the reader and archive.

**Phase 3 — push.** VAPID, service worker, subscription management, teaser
delivery.

## Known limitations

- **Gemini cost is unbounded** — one grounded call per user per day with no
  cap. Fine at 10-100 users; needs a guard before growing beyond that.
- iOS web push is somewhat less reliable than native APNs, and requires the
  home-screen install step described above.
- No abuse handling or rate limiting on the compile endpoint. Acceptable for a
  private group; a prerequisite for going public.
