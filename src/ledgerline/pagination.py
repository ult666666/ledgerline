"""Opaque cursors for keyset pagination.

A cursor records the sort key of the last item on a page, and the next page
starts strictly after it. Unlike LIMIT/OFFSET this stays fast on large tables
and does not skip or repeat rows when new rows are added while a client pages.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Self
from uuid import UUID

from ledgerline.errors import InvalidCursor


def _encode(payload: dict[str, str]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(token: str) -> dict[str, str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor("cursor is not a value returned by this API") from exc
    if not isinstance(payload, dict):
        raise InvalidCursor("cursor is not a value returned by this API")
    return {str(key): str(value) for key, value in payload.items()}


@dataclass(frozen=True, slots=True)
class AccountCursor:
    """Accounts are listed oldest first, ordered by (created_at, id)."""

    created_at: datetime
    id: UUID

    def encode(self) -> str:
        return _encode({"created_at": self.created_at.isoformat(), "id": str(self.id)})

    @classmethod
    def decode(cls, token: str) -> Self:
        payload = _decode(token)
        try:
            created_at = datetime.fromisoformat(payload["created_at"])
            account_id = UUID(payload["id"])
        except (KeyError, ValueError) as exc:
            raise InvalidCursor("cursor is not a value returned by this API") from exc
        if created_at.tzinfo is None:
            raise InvalidCursor("cursor is not a value returned by this API")
        return cls(created_at=created_at, id=account_id)


@dataclass(frozen=True, slots=True)
class EntryCursor:
    """Statement lines are listed in posting order, by entry id."""

    entry_id: int

    def encode(self) -> str:
        return _encode({"entry_id": str(self.entry_id)})

    @classmethod
    def decode(cls, token: str) -> Self:
        payload = _decode(token)
        try:
            return cls(entry_id=int(payload["entry_id"]))
        except (KeyError, ValueError) as exc:
            raise InvalidCursor("cursor is not a value returned by this API") from exc
