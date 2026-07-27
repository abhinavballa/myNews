"""Generate a VAPID keypair for Web Push. Run once.

    python scripts/gen_vapid.py

Then:
  - put VAPID_PUBLIC_KEY into public/config.js (shipped to the browser)
  - store VAPID_PRIVATE_KEY (the PEM block) as a GitHub secret
  - set VAPID_SUBJECT to a mailto: you own (e.g. mailto:you@example.com)
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01


def main() -> None:
    v = Vapid01()
    v.generate_keys()

    private_pem = v.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    raw_public = v.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode()

    print("=" * 70)
    print("VAPID_PUBLIC_KEY  (put in public/config.js -> VAPID_PUBLIC_KEY)")
    print("=" * 70)
    print(public_b64)
    print()
    print("=" * 70)
    print("VAPID_PRIVATE_KEY (GitHub secret; paste the whole PEM block)")
    print("=" * 70)
    print(private_pem)
    print("VAPID_SUBJECT: set to a mailto: address you own, e.g. mailto:you@example.com")


if __name__ == "__main__":
    main()
