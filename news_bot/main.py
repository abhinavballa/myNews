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

from news_bot import db, push
from news_bot.email_send import build_email_html, send_email
from news_bot.generate import build_teaser, generate_digest_html
from news_bot.profiles import Profile, select_due_profiles


def _mask_email(email: str | None) -> str:
    """Partially mask an address for CI logs (the repo may be public), while
    keeping enough to confirm which account: 'abhinav...@gmail.com'."""
    if not email or "@" not in email:
        return repr(email)
    local, _, domain = email.partition("@")
    shown = local[:3] if len(local) > 3 else local[:1]
    return f"{shown}...@{domain}"


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
        print(f"  [{profile.id}] EMAILED -> {_mask_email(profile.email)}")
    elif not profile.wants_email:
        print(f"  [{profile.id}] NOT emailing: wants_email is False "
              f"(enable 'Email me the brief' in Settings).")
    elif not profile.email:
        print(f"  [{profile.id}] NOT emailing: profile has no email address.")

    if profile.wants_push:
        _deliver_push(profile, build_teaser(fragment))
    else:
        print(f"  [{profile.id}] NOT pushing: wants_push is False.")


def _deliver_push(profile: Profile, teaser: str) -> None:
    """Send the day's teaser to each of the user's devices; prune dead ones.

    Isolated from email: a push failure never affects email delivery, and one
    dead device never blocks the others.
    """
    subs = db.fetch_push_subscriptions(profile.id)
    sent = 0
    for sub in subs:
        try:
            result = push.send_push(sub, "📰 Your Morning Brief", teaser, "/")
        except Exception as exc:  # never let push break the run
            print(f"  [{profile.id}] push error on sub {sub.get('id')}: {exc!r}")
            continue
        if result == "prune":
            db.delete_push_subscription(sub["id"])
            print(f"  [{profile.id}] pruned dead subscription {sub['id']}.")
        elif result == "sent":
            sent += 1
    if subs:
        print(f"  [{profile.id}] pushed to {sent}/{len(subs)} device(s).")


def run(now_utc: datetime.datetime, only_user: str | None = None) -> int:
    rows = db.fetch_active_profiles()
    profiles = [Profile.from_row(r) for r in rows]
    print(f"[{now_utc:%Y-%m-%d %H:%M} UTC] Fetched {len(profiles)} active profile(s).")

    # One line per profile so the logs show WHY someone is or isn't due, and
    # which channels they'd receive on — the quickest way to debug "no email".
    for p in profiles:
        local = p.local_datetime(now_utc)
        print(
            f"  - {p.id} email={_mask_email(p.email)} "
            f"tz={p.timezone} local={local:%H:%M} "
            f"deliver_hour={p.delivery_hour} due={p.is_due_hour(now_utc)} "
            f"wants_email={p.wants_email} wants_push={p.wants_push}"
        )

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
