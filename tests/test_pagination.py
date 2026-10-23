"""Opaque pagination cursors."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ledgerline.errors import InvalidCursor
from ledgerline.pagination import AccountCursor, EntryCursor


def test_cursors_round_trip() -> None:
    account_cursor = AccountCursor(created_at=datetime.now(UTC), id=uuid4())
    assert AccountCursor.decode(account_cursor.encode()) == account_cursor
    assert EntryCursor.decode(EntryCursor(entry_id=42).encode()) == EntryCursor(entry_id=42)


def test_cursors_are_url_safe() -> None:
    token = AccountCursor(created_at=datetime.now(UTC), id=uuid4()).encode()
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_account_cursor_requires_a_timezone() -> None:
    naive = datetime(2026, 1, 1)  # noqa: DTZ001 - deliberately naive
    token = AccountCursor(created_at=naive, id=uuid4()).encode()
    with pytest.raises(InvalidCursor):
        AccountCursor.decode(token)


def test_a_cursor_of_one_kind_is_rejected_by_the_other() -> None:
    with pytest.raises(InvalidCursor):
        EntryCursor.decode(AccountCursor(created_at=datetime.now(UTC), id=uuid4()).encode())
    with pytest.raises(InvalidCursor):
        AccountCursor.decode(EntryCursor(entry_id=1).encode())
