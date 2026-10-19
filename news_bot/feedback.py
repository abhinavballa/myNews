"""Save a user's feedback on a brief and, if they wrote a comment, apply it to
their profile right away.

The flow behind POST /api/feedback:
  1. upsert the digest_feedback row (one per user per brief),
  2. if the comment is new text, send it + the brief + the current profile to
     the LLM (news_bot.refine) for an edited profile,
  3. insert the edit as a profile_versions row — the DB trigger makes it the
     active profile and prunes history to the newest few versions.

The feedback is saved even when step 2 fails or is rejected: the user's words
are never lost just because the model returned something unusable.

`rest` is injected (news_bot.user_api.request bound to the caller's token in
production) so this is testable without a network.
"""

from __future__ import annotations

import datetime
from typing import Any, Callable

from news_bot import refine
from news_bot.profiles import CompiledProfileError

# rest(method, path, body=None, prefer=...) -> parsed JSON
Rest = Callable[..., Any]


def save_feedback(rest: Rest, user_id: str, digest_id: int, rating: Any,
                  notes: str, *, refine_fn: Callable = refine.refine_profile) -> dict:
    """Returns {"saved": True, "updated": bool, "rationale"?: str, "reason"?: str}."""
    notes = (notes or "").strip()

    existing = rest("GET", f"digest_feedback?user_id=eq.{user_id}"
                           f"&digest_id=eq.{digest_id}&select=notes") or []
    previous_notes = ((existing[0].get("notes") if existing else None) or "").strip()

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rest("POST", "digest_feedback?on_conflict=user_id,digest_id",
         body={"user_id": user_id, "digest_id": digest_id, "rating": rating,
               "notes": notes or None, "updated_at": now},
         prefer="resolution=merge-duplicates,return=minimal")

    if not notes:
        return {"saved": True, "updated": False}
    # Re-saving the same comment (or just changing the thumb) must not apply
    # the same edit to the profile a second time.
    if notes == previous_notes:
        return {"saved": True, "updated": False, "reason": "comment unchanged"}

    profiles = rest("GET", f"profiles?id=eq.{user_id}"
                           "&select=interests_text,compiled_profile") or []
    digests = rest("GET", f"digests?id=eq.{digest_id}&select=html,local_date") or []
    profile = profiles[0] if profiles else {}
    digest = digests[0] if digests else {}
    current = profile.get("compiled_profile") or {}
    if not current:
        return {"saved": True, "updated": False, "reason": "no profile to update yet"}

    try:
        result = refine_fn(profile.get("interests_text") or "", current,
                           digest.get("html") or "", notes, rating=rating,
                           local_date=digest.get("local_date"))
    except (refine.RefinementRejected, CompiledProfileError) as exc:
        return {"saved": True, "updated": False, "reason": f"edit rejected: {exc}"}
    except Exception as exc:  # LLM/network failure: feedback is still saved
        return {"saved": True, "updated": False, "reason": f"update failed: {exc}"}

    rest("POST", "profile_versions",
         body={"user_id": user_id, "compiled_profile": result["compiled_profile"],
               "source": "learned", "rationale": result["rationale"] or None})
    return {"saved": True, "updated": True, "rationale": result["rationale"]}
