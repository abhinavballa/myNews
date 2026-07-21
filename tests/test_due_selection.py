"""Due-user selection across timezones and DST boundaries — the most likely
source of real bugs, per the design doc."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from news_bot.profiles import Profile, select_due_profiles


def make(tz="America/Los_Angeles", hour=8, uid="u1", email="a@b.com"):
    return Profile(
        id=uid, email=email, compiled_profile={"sections": []},
        delivery_hour=hour, timezone=tz, wants_push=False, wants_email=True,
    )


def utc(y, m, d, h):
    return datetime.datetime(y, m, d, h, 0, tzinfo=datetime.timezone.utc)


# --- Timezone correctness -------------------------------------------------

def test_pacific_8am_in_summer_is_15_utc():
    p = make(hour=8)
    assert p.is_due_hour(utc(2026, 7, 21, 15))       # PDT: 8am local
    assert not p.is_due_hour(utc(2026, 7, 21, 16))


def test_pacific_8am_in_winter_is_16_utc():
    p = make(hour=8)
    assert p.is_due_hour(utc(2026, 1, 21, 16))       # PST: 8am local
    assert not p.is_due_hour(utc(2026, 1, 21, 15))


def test_two_users_different_timezones_same_local_hour():
    la = make(tz="America/Los_Angeles", hour=8, uid="la")
    ny = make(tz="America/New_York", hour=8, uid="ny")
    # NY 8am (EDT) is 12:00 UTC; LA is not yet due.
    assert ny.is_due_hour(utc(2026, 7, 21, 12))
    assert not la.is_due_hour(utc(2026, 7, 21, 12))


# --- DST boundary ---------------------------------------------------------

def test_dst_spring_forward_keeps_8am_correct():
    """PDT begins 2026-03-08. The day before (PST) 8am is 16 UTC; the day of
    and after (PDT) 8am is 15 UTC. is_due_hour must track the actual offset."""
    p = make(hour=8)
    assert p.is_due_hour(utc(2026, 3, 7, 16))        # PST
    assert p.is_due_hour(utc(2026, 3, 9, 15))        # PDT
    assert not p.is_due_hour(utc(2026, 3, 9, 16))


def test_dst_fall_back_keeps_8am_correct():
    """PST resumes 2026-11-01. Before: PDT 8am = 15 UTC. After: PST 8am = 16 UTC."""
    p = make(hour=8)
    assert p.is_due_hour(utc(2026, 10, 31, 15))      # PDT
    assert p.is_due_hour(utc(2026, 11, 2, 16))       # PST


# --- Selection + local_date ----------------------------------------------

def test_select_only_due_users():
    due = make(hour=8, uid="due")
    not_due = make(hour=9, uid="notdue")
    picked = select_due_profiles([due, not_due], utc(2026, 7, 21, 15), set())
    assert [p.id for p in picked] == ["due"]


def test_already_delivered_is_excluded():
    p = make(hour=8, uid="u1")
    local_date = p.local_date(utc(2026, 7, 21, 15))
    picked = select_due_profiles([p], utc(2026, 7, 21, 15), {f"u1:{local_date}"})
    assert picked == []


def test_local_date_uses_user_timezone():
    # 03:00 UTC on the 22nd is still the 21st in Los Angeles (PDT, UTC-7).
    p = make(tz="America/Los_Angeles")
    assert p.local_date(utc(2026, 7, 22, 3)) == "2026-07-21"
