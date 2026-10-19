"""POST /api/feedback — save feedback on a brief and apply the comment to the
user's profile with one LLM call (see news_bot/feedback.py for the flow).

Request body:  {"digest_id": 123, "rating": 1 | -1 | null, "notes": "..."}
Response:      {"saved": true, "updated": bool, "rationale"?: str, "reason"?: str}
               {"error": "..."} with a 4xx/5xx status if the feedback itself
               could not be saved
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import user_api
from news_bot.feedback import save_feedback


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

        user_id = user_api.user_id_from_jwt(access_token)
        if not user_id:
            return self._send(401, {"error": "invalid token"})

        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            digest_id = int(data["digest_id"])
            rating = data.get("rating")
            notes = data.get("notes") or ""
        except (ValueError, TypeError, KeyError):
            return self._send(400, {"error": "digest_id is required"})
        if rating not in (1, -1, None) or not isinstance(notes, str):
            return self._send(400, {"error": "rating must be 1, -1 or null"})

        def rest(method, path, body=None, prefer="return=minimal"):
            return user_api.request(access_token, method, path, body=body, prefer=prefer)

        try:
            result = save_feedback(rest, user_id, digest_id, rating, notes)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            return self._send(exc.code, {"error": f"save failed: {detail}"})
        except Exception as exc:
            return self._send(500, {"error": f"save failed: {exc}"})

        if result.get("reason"):
            print(f"[{user_id}] profile not updated: {result['reason']}", file=sys.stderr)
        return self._send(200, result)
