"""Thin Supabase (PostgREST) client using the service-role key.

Deliberately small and dependency-free: the worker only needs to read active
profiles, check for an existing digest, and insert a digest. Keeping the HTTP
surface here means the scheduling and generation logic stays pure and testable.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class SupabaseError(RuntimeError):
    pass


def _base_and_headers() -> tuple[str, dict[str, str]]:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    return url, headers


def _request(method: str, path: str, *, params: dict[str, str] | None = None,
             body: Any | None = None, extra_headers: dict[str, str] | None = None) -> Any:
    base, headers = _base_and_headers()
    if extra_headers:
        headers = {**headers, **extra_headers}
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}/rest/v1/{path}{query}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:  # pragma: no cover - network error path
        detail = exc.read().decode(errors="replace")
        raise SupabaseError(f"{method} {path} failed ({exc.code}): {detail}") from exc
    return json.loads(raw) if raw else None


def fetch_active_profiles() -> list[dict[str, Any]]:
    """All profiles with active=true and a compiled_profile present."""
    return _request(
        "GET", "profiles",
        params={"active": "eq.true", "compiled_profile": "not.is.null", "select": "*"},
    )


def digest_exists(user_id: str, local_date: str) -> bool:
    rows = _request(
        "GET", "digests",
        params={
            "user_id": f"eq.{user_id}",
            "local_date": f"eq.{local_date}",
            "select": "id",
        },
    )
    return bool(rows)


def insert_digest(user_id: str, local_date: str, html: str,
                  teaser: str | None = None) -> bool:
    """Insert a digest row, ignoring duplicates at the DB level.

    Returns True if a new row was inserted, False if one already existed for
    (user_id, local_date). This is the idempotency guard against re-runs.
    """
    rows = _request(
        "POST", "digests",
        body={"user_id": user_id, "local_date": local_date, "html": html,
              "teaser": teaser},
        extra_headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
    )
    return bool(rows)
