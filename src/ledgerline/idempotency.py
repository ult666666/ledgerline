"""Idempotency keys make retried writes safe.

A client that is unsure whether a request succeeded (a timeout, a dropped
connection) retries it with the same ``Idempotency-Key`` header. The service
stores the first successful response in the same database transaction as the
ledger changes it describes, so a retry with the same body gets that response
back instead of moving money twice, and reusing the key for a different
request is rejected.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from pydantic import BaseModel

from ledgerline.errors import InvalidIdempotencyKey

# Visible ASCII without spaces, up to 255 characters: UUIDs and the usual
# client-generated tokens fit, and the value is safe to store and log.
_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")


@dataclass(frozen=True, slots=True)
class IdempotencyRequest:
    principal: str
    """The API client making the request; keys are scoped per client."""
    key: str
    """The Idempotency-Key header value."""
    fingerprint: str
    """Identifies the exact request the key was first used for."""


def check_key_format(key: str) -> None:
    """Raise InvalidIdempotencyKey unless ``key`` is safe to store and compare."""
    if not _KEY_PATTERN.fullmatch(key):
        raise InvalidIdempotencyKey(
            "Idempotency-Key must be 1 to 255 visible ASCII characters (no spaces)"
        )


def request_fingerprint(method: str, path: str, body: BaseModel | None) -> str:
    """Hash everything that must match for a retry to count as the same request.

    The body is hashed after validation, as canonical JSON, so harmless
    differences between retries (whitespace, key order) do not matter.
    """
    canonical_body = json.dumps(
        None if body is None else body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = f"{method.upper()} {path}\n{canonical_body}"
    return hashlib.sha256(payload.encode()).hexdigest()
