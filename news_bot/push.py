"""Send Web Push notifications via VAPID, and decide when to prune dead endpoints.

Kept isolated from the worker loop so a push failure for one device never blocks
email delivery or other devices.
"""

from __future__ import annotations

import json
import os
from typing import Any

# When a push service returns one of these, the subscription is permanently
# gone (user cleared data / uninstalled) — delete the row.
_PRUNE_STATUSES = {404, 410}


def _should_prune(status_code: int | None) -> bool:
    return status_code in _PRUNE_STATUSES


def send_push(subscription: dict[str, Any], title: str, body: str,
              url: str = "/") -> str:
    """Deliver one notification. Returns 'sent', 'prune', or 'failed'.

    'prune' means the caller should delete this subscription row.
    """
    from pywebpush import WebPushException, webpush

    sub_info = {
        "endpoint": subscription["endpoint"],
        "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
    }
    payload = json.dumps({"title": title, "body": body, "url": url})
    try:
        webpush(
            subscription_info=sub_info,
            data=payload,
            vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
            vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")},
        )
        return "sent"
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return "prune" if _should_prune(status) else "failed"
