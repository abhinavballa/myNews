"""Worker entry point: deliver each due user's personalized brief.

Runs hourly (GitHub Actions cron `0 * * * *`). Each run fetches active
profiles, keeps the ones whose current local hour is at or past their delivery
hour and who have no digest yet for their local date, then generates, delivers,
and evaluates. With Phoenix configured, each user's run is one trace.

Usage:
    python -m news_bot.main                 # normal hourly run
    python -m news_bot.main --user <uuid>   # force one user now (testing)
"""

from __future__ import annotations

import datetime
import sys

from news_bot import db, evals, push, tracing
from news_bot.email_send import build_email_html, send_email
from news_bot.generate import build_teaser, generate_digest
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
    """Generate, persist, deliver, and evaluate one user's brief (one trace)."""
    with tracing.span("deliver", "CHAIN", **{
        "user.id": profile.id, "mynews.local_date": local_date,
        "mynews.profile_version": profile.active_version,
    }) as root:
        _deliver(profile, local_date, root)


def _deliver(profile: Profile, local_date: str, root) -> None:
    if db.digest_exists(profile.id, local_date):
        print(f"  [{profile.id}] already has a digest for {local_date}; skipping.")
        tracing.set_attributes(root, **{"mynews.outcome": "already_delivered"})
        return

    print(f"  [{profile.id}] generating digest for {local_date}...")
    with tracing.span("generate_digest", "CHAIN", **{
        "input.value": _human_date(local_date),
    }) as gen:
        digest = generate_digest(profile.compiled_profile, _human_date(local_date))
        grounding = digest.grounding or {}
        tracing.set_attributes(gen, **{
            "output.value": digest.html,
            "llm.model_name": digest.model,
            "mynews.prompt_version": digest.prompt_version,
            "mynews.search_queries": grounding.get("queries") or None,
            "mynews.source_count": len(grounding.get("sources") or []),
        })
    fragment = digest.html
    print(f"  [{profile.id}] generated {len(fragment)} chars "
          f"(model={digest.model}, profile v{profile.active_version}).")

    # Insert first: the UNIQUE (user_id, local_date) constraint is what makes a
    # concurrent/re-run not double-deliver. If a row already snuck in, stop.
    # The provenance fields are what let feedback on this brief be attributed to
    # the profile/model/prompt that produced it; they can't be reconstructed.
    teaser = build_teaser(fragment)
    inserted = db.insert_digest(
        profile.id, local_date, fragment,
        teaser=teaser,
        profile_version=profile.active_version,
        model=digest.model,
        prompt_version=digest.prompt_version,
    )
    if not inserted:
        print(f"  [{profile.id}] digest row already existed; not delivering.")
        tracing.set_attributes(root, **{"mynews.outcome": "already_delivered"})
        return

    if profile.wants_email and profile.email:
        with tracing.span("send_email", "TOOL"):
            html = build_email_html(fragment, _human_date(local_date))
            send_email(html, profile.email, _subject_date(local_date))
        print(f"  [{profile.id}] EMAILED -> {_mask_email(profile.email)}")
    elif not profile.wants_email:
        print(f"  [{profile.id}] NOT emailing: wants_email is False "
              f"(enable 'Email me the brief' in Settings).")
    elif not profile.email:
        print(f"  [{profile.id}] NOT emailing: profile has no email address.")

    if profile.wants_push:
        with tracing.span("send_push", "TOOL"):
            _deliver_push(profile, teaser)
    else:
        print(f"  [{profile.id}] NOT pushing: wants_push is False.")

    tracing.set_attributes(root, **{"mynews.outcome": "delivered"})
    _evaluate(profile, digest, root)


def _evaluate(profile: Profile, digest, root) -> None:
    """Score the brief after it's sent. Never raises, never blocks delivery."""
    with tracing.span("evaluate", "EVALUATOR"):
        results = evals.run_evals(profile.compiled_profile, digest.html, digest.grounding)
    for r in results:
        score = "n/a" if r.score is None else f"{r.score:.2f}"
        print(f"  [{profile.id}] eval {r.name}: {score} ({r.label}) — {r.explanation[:160]}")
        tracing.annotate(root, r.name, kind=r.kind, label=r.label,
                         score=r.score, explanation=r.explanation)


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

    tracing.init_tracing()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    try:
        return run(now_utc, only_user=only_user)
    finally:
        tracing.flush()  # export spans + eval annotations before the job exits


if __name__ == "__main__":
    raise SystemExit(main())
