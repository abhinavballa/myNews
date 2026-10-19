"""Supabase REST calls made AS the signed-in user, for the Vercel endpoints.

The endpoints forward the caller's own access token, so RLS still decides what
they can read and write — the server never needs the service-role key.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any


def user_id_from_jwt(token: str) -> str | None:
    """Extract the `sub` claim without verifying — RLS is the real gate; this is
    only used to filter queries to the caller's own rows."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad base64url
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("sub")
    except Exception:
        return None


def rest_base() -> str:
    base = os.environ["SUPABASE_URL"].strip().rstrip("/")
    if base.endswith("/rest/v1"):
        base = base[: -len("/rest/v1")]
    return base


def as_user_headers(access_token: str, prefer: str = "return=minimal") -> dict:
    return {
        "apikey": os.environ["SUPABASE_ANON_KEY"],
        "Authorization": f"Bearer {access_token}",  # act as the user (RLS)
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def request(access_token: str, method: str, path: str, *, body: Any = None,
            prefer: str = "return=minimal") -> Any:
    """One PostgREST call as the user. `path` includes the query string.
    Returns parsed JSON, or None for an empty response. HTTP errors raise."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{rest_base()}/rest/v1/{path}", data=data, method=method,
        headers=as_user_headers(access_token, prefer),
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None
