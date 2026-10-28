"""API-key authentication.

Deliberately minimal: a fixed set of bearer tokens from configuration, all with
the same permissions. A real deployment would put an identity provider,
per-client scopes and key rotation in front of a ledger (see the README).

Keys are meant to be random tokens from ``ledgerline generate-key`` (32 random
bytes), not passwords. For such tokens one SHA-256 pass is the usual choice: it
gives fixed-length values for the constant-time comparison and a stable
fingerprint for logs and idempotency records that cannot be reversed. A slow
password hash would add work to every request without making a 256-bit token
any harder to guess. It would only help a weak, hand-typed key, so the service
warns about short keys at startup instead (see ``short_key_count``).
"""

import hashlib
import hmac
from collections.abc import Sequence

# `ledgerline generate-key` prints 43 characters. Shorter keys are more likely to
# be typed by hand, so the service warns about them at startup. Length is only a
# rough check: it cannot measure how random a key is.
MIN_KEY_LENGTH = 32


def short_key_count(keys: Sequence[str]) -> int:
    """How many configured keys are shorter than ``MIN_KEY_LENGTH``."""
    return sum(len(key) < MIN_KEY_LENGTH for key in keys)


def key_fingerprint(api_key: str) -> str:
    """A short, non-secret identifier for an API key, safe to log and store."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def authenticate_api_key(presented: str, configured: Sequence[str]) -> str | None:
    """Return the fingerprint of the configured key equal to ``presented``, or None."""
    presented_digest = hashlib.sha256(presented.encode()).digest()
    match: str | None = None
    for key in configured:
        # compare_digest on fixed-length digests takes the same time however
        # many characters match and whatever the key's length; checking every
        # key, without breaking early, hides which one matched.
        if hmac.compare_digest(presented_digest, hashlib.sha256(key.encode()).digest()):
            match = key
    return None if match is None else key_fingerprint(match)
