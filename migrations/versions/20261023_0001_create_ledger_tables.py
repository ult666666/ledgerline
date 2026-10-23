"""Create ledger tables.

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "asset",
                "liability",
                "equity",
                "revenue",
                "expense",
                name="account_type",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "normal_balance",
            sa.Enum(
                "debit",
                "credit",
                name="normal_balance",
                native_enum=False,
                create_constraint=True,
                length=6,
            ),
            nullable=False,
        ),
        sa.Column("allow_negative", sa.Boolean(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("balance", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "allow_negative OR balance >= 0", name=op.f("ck_accounts_balance_not_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    op.create_index(op.f("ix_accounts_created_at_id"), "accounts", ["created_at", "id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("principal", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("principal", "key", name=op.f("pk_idempotency_keys")),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reverses_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["reverses_id"],
            ["transactions.id"],
            name=op.f("fk_transactions_reverses_id_transactions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint("reverses_id", name=op.f("uq_transactions_reverses_id")),
    )

    op.create_table(
        "entries",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_entries_amount_not_zero")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_entries_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_entries_transaction_id_transactions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entries")),
        sa.UniqueConstraint(
            "transaction_id", "account_id", name=op.f("uq_entries_transaction_id_account_id")
        ),
    )
    op.create_index(op.f("ix_entries_account_id_id"), "entries", ["account_id", "id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_entries_account_id_id"), table_name="entries")
    op.drop_table("entries")
    op.drop_table("transactions")
    op.drop_table("idempotency_keys")
    op.drop_index(op.f("ix_accounts_created_at_id"), table_name="accounts")
    op.drop_table("accounts")
