"""Add holds (two-phase transfers) and link captures to the journal.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01

Creates the ``holds`` table and adds ``transactions.hold_id``, the link from a
capture's journal transaction to its hold. The column is nullable, so existing
transactions keep their meaning (none of them captured a hold). SQLite cannot
add a foreign key to an existing table, so there the batch operation rebuilds
``transactions`` (copy, drop, rename); see ``migrations/env.py`` for how that
stays safe with foreign keys enforced.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("from_account_id", sa.Uuid(), nullable=False),
        sa.Column("to_account_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("captured_amount", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name=op.f("ck_holds_amount_positive")),
        sa.CheckConstraint(
            "from_account_id <> to_account_id", name=op.f("ck_holds_distinct_accounts")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'captured', 'voided')", name=op.f("ck_holds_status_stored")
        ),
        sa.CheckConstraint(
            "(status = 'active' AND closed_at IS NULL AND captured_amount IS NULL)"
            " OR (status = 'captured' AND closed_at IS NOT NULL AND captured_amount IS NOT NULL"
            " AND captured_amount > 0 AND captured_amount <= amount)"
            " OR (status = 'voided' AND closed_at IS NOT NULL AND captured_amount IS NULL)",
            name=op.f("ck_holds_status_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["from_account_id"], ["accounts.id"], name=op.f("fk_holds_from_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["to_account_id"], ["accounts.id"], name=op.f("fk_holds_to_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_holds")),
    )
    op.create_index(
        op.f("ix_holds_from_account_id_status_expires_at"),
        "holds",
        ["from_account_id", "status", "expires_at"],
    )
    with op.batch_alter_table("transactions", copy_from=_transactions()) as batch_op:
        batch_op.add_column(sa.Column("hold_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_transactions_hold_id_holds"), "holds", ["hold_id"], ["id"]
        )
        batch_op.create_unique_constraint(batch_op.f("uq_transactions_hold_id"), ["hold_id"])


def downgrade() -> None:
    # Downgrading discards every hold and the capture links; the capture
    # transactions themselves stay in the journal as ordinary transactions.
    with op.batch_alter_table(
        "transactions", copy_from=_transactions(with_hold_id=True)
    ) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_transactions_hold_id"), type_="unique")
        batch_op.drop_constraint(batch_op.f("fk_transactions_hold_id_holds"), type_="foreignkey")
        batch_op.drop_column("hold_id")
    op.drop_index(op.f("ix_holds_from_account_id_status_expires_at"), table_name="holds")
    op.drop_table("holds")


def _transactions(*, with_hold_id: bool = False) -> sa.Table:
    """The ``transactions`` table before this revision (or after it, ``with_hold_id``).

    Giving the SQLite rebuild the table definition means it needs no
    reflection, so ``alembic upgrade head --sql`` still works offline.
    """
    columns: list[sa.Column[Any] | sa.Constraint] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reverses_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["reverses_id"], ["transactions.id"], name="fk_transactions_reverses_id_transactions"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
        sa.UniqueConstraint("reverses_id", name="uq_transactions_reverses_id"),
    ]
    if with_hold_id:
        columns += [
            sa.Column("hold_id", sa.Uuid(), nullable=True),
            sa.ForeignKeyConstraint(
                ["hold_id"], ["holds.id"], name="fk_transactions_hold_id_holds"
            ),
            sa.UniqueConstraint("hold_id", name="uq_transactions_hold_id"),
        ]
    return sa.Table("transactions", sa.MetaData(), *columns)
