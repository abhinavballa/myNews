"""Entry point: gate on Pacific time, generate the digest, and email it.

Usage:
    python -m news_bot.main            # runs only if it's ~8am Pacific
    python -m news_bot.main --force    # bypass the time gate (testing)
"""

from __future__ import annotations

import sys
from zoneinfo import ZoneInfo
import datetime

from news_bot.generate import generate_digest_html
from news_bot.email_send import build_email_html, send_email

SEND_HOUR_PACIFIC = 8


def is_send_time() -> bool:
    now = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
    return now.hour == SEND_HOUR_PACIFIC


def main() -> int:
    force = "--force" in sys.argv

    if not force and not is_send_time():
        now = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
        print(f"Not 8am Pacific (currently {now:%H:%M %Z}); skipping this run.")
        return 0

    print("Generating digest via Gemini with Google Search grounding...")
    fragment = generate_digest_html()
    print(f"Generated {len(fragment)} chars of content.")

    html = build_email_html(fragment)
    print("Sending email via Gmail SMTP...")
    send_email(html)
    print("Sent. ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
