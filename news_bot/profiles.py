"""Profile model, compiled-profile validation, and due-user selection.

Everything here is pure (no network, no clock reads passed implicitly) so the
timezone/DST and schema rules can be tested directly.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo


class CompiledProfileError(ValueError):
    """Raised when a compiled_profile does not match the worker's contract."""


@dataclass(frozen=True)
class Profile:
    id: str
    email: str | None
    compiled_profile: dict[str, Any]
    delivery_hour: int
    timezone: str
    wants_push: bool
    wants_email: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Profile":
        return cls(
            id=row["id"],
            email=row.get("email"),
            compiled_profile=row.get("compiled_profile") or {},
            delivery_hour=int(row.get("delivery_hour", 8)),
            timezone=row.get("timezone") or "America/Los_Angeles",
            wants_push=bool(row.get("wants_push", True)),
            wants_email=bool(row.get("wants_email", False)),
        )

    def local_datetime(self, now_utc: datetime.datetime) -> datetime.datetime:
        return now_utc.astimezone(ZoneInfo(self.timezone))

    def local_date(self, now_utc: datetime.datetime) -> str:
        return self.local_datetime(now_utc).date().isoformat()

    def is_due_hour(self, now_utc: datetime.datetime) -> bool:
        """True when the user's current local hour equals their delivery hour.

        Correct across DST: 8am local is 15:00 UTC in PDT and 16:00 UTC in PST,
        and this comparison follows the offset the zone actually has at now_utc.
        """
        return self.local_datetime(now_utc).hour == self.delivery_hour


def validate_compiled_profile(obj: Any) -> None:
    """Reject malformed compiler output at save time, not at 8am.

    Raises CompiledProfileError on anything that would break prompt compilation.
    """
    if not isinstance(obj, dict):
        raise CompiledProfileError("compiled_profile must be a JSON object")

    for key in ("persona", "edge_for"):
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CompiledProfileError(f"'{key}' must be a non-empty string")

    sections = obj.get("sections")
    if not isinstance(sections, list) or not sections:
        raise CompiledProfileError("'sections' must be a non-empty list")

    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            raise CompiledProfileError(f"sections[{i}] must be an object")
        for key in ("emoji", "title", "guidance"):
            value = section.get(key)
            if not isinstance(value, str) or not value.strip():
                raise CompiledProfileError(
                    f"sections[{i}].{key} must be a non-empty string"
                )


def select_due_profiles(
    profiles: list[Profile],
    now_utc: datetime.datetime,
    already_delivered: set[str],
) -> list[Profile]:
    """Users at their delivery hour with no digest yet for their local date.

    `already_delivered` holds "{id}:{local_date}" keys for digests that already
    exist, so a re-run in the same local date selects nobody.
    """
    due = []
    for p in profiles:
        if not p.is_due_hour(now_utc):
            continue
        if f"{p.id}:{p.local_date(now_utc)}" in already_delivered:
            continue
        due.append(p)
    return due
