"""API-key authentication.

Deliberately minimal: a fixed set of bearer tokens from configuration, all with
the same permissions. A real deployment would put an identity provider,
per-client scopes and key rotation in front of a ledger (see the README).
"""

import hashlib
import hmac
from collections.abc import Sequence


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
