"""Worker entry point: deliver each due user's personalized brief.

Runs hourly (GitHub Actions cron `0 * * * *`). Each run fetches active
profiles, keeps the ones whose current local hour equals their delivery hour
and who have no digest yet for their local date, then generates + delivers.

Usage:
    python -m news_bot.main                 # normal hourly run
    python -m news_bot.main --user <uuid>   # force one user now (testing)
"""

from __future__ import annotations

import datetime
import sys

from news_bot import db
from news_bot.email_send import build_email_html, send_email
from news_bot.generate import generate_digest_html
from news_bot.profiles import Profile, select_due_profiles


def _human_date(local_date: str) -> str:
    d = datetime.date.fromisoformat(local_date)
    return d.strftime("%A, %B %d, %Y")


def _subject_date(local_date: str) -> str:
    d = datetime.date.fromisoformat(local_date)
    return d.strftime("%b %d")


def deliver(profile: Profile, local_date: str) -> None:
    """Generate, persist, and deliver one user's brief. Idempotent per date."""
    if db.digest_exists(profile.id, local_date):
        print(f"  [{profile.id}] already has a digest for {local_date}; skipping.")
        return

    print(f"  [{profile.id}] generating digest for {local_date}...")
    fragment = generate_digest_html(profile.compiled_profile, _human_date(local_date))
    print(f"  [{profile.id}] generated {len(fragment)} chars.")

    # Insert first: the UNIQUE (user_id, local_date) constraint is what makes a
    # concurrent/re-run not double-deliver. If a row already snuck in, stop.
    inserted = db.insert_digest(profile.id, local_date, fragment)
    if not inserted:
        print(f"  [{profile.id}] digest row already existed; not delivering.")
        return

    if profile.wants_email and profile.email:
        html = build_email_html(fragment, _human_date(local_date))
        send_email(html, profile.email, _subject_date(local_date))
        print(f"  [{profile.id}] emailed {profile.email}.")
    # wants_push delivery lands in Phase 3.


def run(now_utc: datetime.datetime, only_user: str | None = None) -> int:
    rows = db.fetch_active_profiles()
    profiles = [Profile.from_row(r) for r in rows]
    print(f"Fetched {len(profiles)} active profile(s).")

    if only_user:
        due = [p for p in profiles if p.id == only_user]
        if not due:
            print(f"No active profile with id {only_user}.")
            return 1
    else:
        due = select_due_profiles(profiles, now_utc, already_delivered=set())
    print(f"{len(due)} user(s) due this run.")

    failures = 0
    for profile in due:
        local_date = profile.local_date(now_utc)
        try:
            deliver(profile, local_date)
        except Exception as exc:  # one bad profile must never abort the run
            failures += 1
            print(f"  [{profile.id}] FAILED: {exc!r}")
    return 1 if failures else 0


def main() -> int:
    only_user = None
    if "--user" in sys.argv:
        i = sys.argv.index("--user")
        try:
            only_user = sys.argv[i + 1]
        except IndexError:
            print("--user requires a uuid argument.")
            return 2

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    return run(now_utc, only_user=only_user)


if __name__ == "__main__":
    raise SystemExit(main())
