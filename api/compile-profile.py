"""POST /api/compile-profile  — the one server endpoint in the system.

A static page cannot hold the Gemini API key, so compiling the interests text
box needs a server. This function:

  1. reads the caller's Supabase access token (Bearer),
  2. compiles their interests_text into a validated compiled_profile via Gemini,
  3. writes it back to their profiles row AS that user (their token), so RLS
     still applies and nobody can write anyone else's row.

Request body:  {"interests_text": "..."}
Response:      {"compiled_profile": {...}}  on success
               {"error": "..."}             with a 4xx/5xx status otherwise
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

# Vercel runs this file from the repo root, so news_bot is importable; be
# explicit in case the working directory differs.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot.compile import compile_interests
from news_bot.profiles import CompiledProfileError


def _user_id_from_jwt(token: str) -> str | None:
    """Extract the `sub` claim without verifying — RLS is the real gate; we only
    use it to filter the PATCH to the caller's own row."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad base64url
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("sub")
    except Exception:
        return None


def _write_profile(user_id: str, access_token: str, interests_text: str,
                   compiled: dict) -> None:
    base = os.environ["SUPABASE_URL"].strip().rstrip("/")
    if base.endswith("/rest/v1"):
        base = base[: -len("/rest/v1")]
    body = json.dumps({
        "interests_text": interests_text,
        "compiled_profile": compiled,
        "compiled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }).encode()
    req = urllib.request.Request(
        f"{base}/rest/v1/profiles?id=eq.{user_id}",
        data=body,
        method="PATCH",
        headers={
            "apikey": os.environ["SUPABASE_ANON_KEY"],
            "Authorization": f"Bearer {access_token}",  # act as the user (RLS)
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=30).read()


class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return self._send(401, {"error": "missing bearer token"})
        access_token = auth[len("Bearer "):].strip()

        user_id = _user_id_from_jwt(access_token)
        if not user_id:
            return self._send(401, {"error": "invalid token"})

        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            interests_text = (data.get("interests_text") or "").strip()
        except (ValueError, TypeError):
            return self._send(400, {"error": "invalid JSON body"})

        if not interests_text:
            return self._send(400, {"error": "interests_text is required"})

        try:
            compiled = compile_interests(interests_text)
        except CompiledProfileError as exc:
            return self._send(422, {"error": f"compiled profile invalid: {exc}"})
        except ValueError as exc:
            return self._send(422, {"error": str(exc)})
        except Exception as exc:  # Gemini/network failure — keep old profile
            return self._send(502, {"error": f"compile failed: {exc}"})

        try:
            _write_profile(user_id, access_token, interests_text, compiled)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            return self._send(exc.code, {"error": f"save failed: {detail}"})
        except Exception as exc:
            return self._send(500, {"error": f"save failed: {exc}"})

        return self._send(200, {"compiled_profile": compiled})
