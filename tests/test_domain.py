"""Unit tests for the pure double-entry and hold rules in ledgerline.domain."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgerline.domain import (
    HOLD_TRANSITIONS,
    MAX_MAGNITUDE,
    STORED_HOLD_STATUSES,
    AccountState,
    AccountType,
    EntryDraft,
    HoldState,
    HoldStatus,
    NormalBalance,
    apply_entries,
    balance_change,
    capture_amount,
    check_currencies,
    check_hold_request,
    default_normal_balance,
    hold_status,
    require_active,
    reversal_entries,
    transfer_entries,
    validate_entries,
)
from ledgerline.errors import (
    HoldExpired,
    HoldNotActive,
    InsufficientFunds,
    InvalidHold,
    InvalidTransaction,
    InvalidTransfer,
    UnbalancedTransaction,
)

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
TICK = timedelta(microseconds=1)


def account(
    *,
    normal_balance: NormalBalance = NormalBalance.DEBIT,
    balance: int = 0,
    allow_negative: bool = False,
    currency: str = "USD",
    held: int = 0,
) -> AccountState:
    return AccountState(
        id=uuid4(),
        currency=currency,
        normal_balance=normal_balance,
        allow_negative=allow_negative,
        balance=balance,
        version=0,
        held=held,
    )


def hold(status: HoldStatus = HoldStatus.ACTIVE, amount: int = 1_000) -> HoldState:
    return HoldState(id=uuid4(), status=status, amount=amount, expires_at=LATER)


def draft(account_id: UUID, amount: int, currency: str = "USD") -> EntryDraft:
    return EntryDraft(account_id=account_id, amount=amount, currency=currency)


@pytest.mark.parametrize(
    ("account_type", "expected"),
    [
        (AccountType.ASSET, NormalBalance.DEBIT),
        (AccountType.EXPENSE, NormalBalance.DEBIT),
        (AccountType.LIABILITY, NormalBalance.CREDIT),
        (AccountType.EQUITY, NormalBalance.CREDIT),
        (AccountType.REVENUE, NormalBalance.CREDIT),
    ],
)
def test_default_normal_balance(account_type: AccountType, expected: NormalBalance) -> None:
    assert default_normal_balance(account_type) is expected


def test_debits_grow_debit_normal_accounts_and_shrink_credit_normal_ones() -> None:
    assert balance_change(NormalBalance.DEBIT, 500) == 500
    assert balance_change(NormalBalance.DEBIT, -500) == -500
    assert balance_change(NormalBalance.CREDIT, 500) == -500
    assert balance_change(NormalBalance.CREDIT, -500) == 500


class TestValidateEntries:
    def test_accepts_balanced_entries(self) -> None:
        validate_entries([draft(uuid4(), 700), draft(uuid4(), -300), draft(uuid4(), -400)])

    def test_accepts_entries_balanced_separately_per_currency(self) -> None:
        validate_entries(
            [
                draft(uuid4(), 100, "USD"),
                draft(uuid4(), -100, "USD"),
                draft(uuid4(), 92, "EUR"),
                draft(uuid4(), -92, "EUR"),
            ]
        )

    @pytest.mark.parametrize("count", [0, 1])
    def test_needs_at_least_two_entries(self, count: int) -> None:
        with pytest.raises(InvalidTransaction, match="at least two entries"):
            validate_entries([draft(uuid4(), 100) for _ in range(count)])

    def test_rejects_zero_amounts(self) -> None:
        with pytest.raises(InvalidTransaction, match=r"entries\[1\]: amount must not be zero"):
            validate_entries([draft(uuid4(), 100), draft(uuid4(), 0), draft(uuid4(), -100)])

    def test_rejects_amounts_outside_the_json_safe_range(self) -> None:
        with pytest.raises(InvalidTransaction, match="amount must be between"):
            validate_entries(
                [draft(uuid4(), MAX_MAGNITUDE + 1), draft(uuid4(), -MAX_MAGNITUDE - 1)]
            )

    def test_rejects_the_same_account_twice(self) -> None:
        account_id = uuid4()
        with pytest.raises(InvalidTransaction, match="appears more than once"):
            validate_entries([draft(account_id, 100), draft(account_id, -50), draft(uuid4(), -50)])

    def test_rejects_unbalanced_entries_and_reports_the_imbalance(self) -> None:
        with pytest.raises(UnbalancedTransaction) as caught:
            validate_entries(
                [
                    draft(uuid4(), 100, "USD"),
                    draft(uuid4(), -90, "USD"),
                    draft(uuid4(), 5, "EUR"),
                    draft(uuid4(), -5, "EUR"),
                ]
            )
        assert caught.value.extensions == {"imbalances": {"USD": 10}}
        assert "USD +10" in caught.value.detail

    def test_currencies_never_offset_each_other(self) -> None:
        # +100 USD and -100 EUR sum to zero numerically but are not balanced.
        with pytest.raises(UnbalancedTransaction):
            validate_entries([draft(uuid4(), 100, "USD"), draft(uuid4(), -100, "EUR")])


def test_check_currencies_rejects_an_entry_in_a_foreign_currency() -> None:
    usd = account(currency="USD")
    eur = account(currency="EUR")
    accounts = {usd.id: usd, eur.id: eur}
    check_currencies([draft(usd.id, 100, "USD"), draft(eur.id, -100, "EUR")], accounts)
    with pytest.raises(InvalidTransaction, match="does not match account"):
        check_currencies([draft(usd.id, 100, "USD"), draft(eur.id, -100, "USD")], accounts)


class TestApplyEntries:
    def test_returns_new_balances_in_each_accounts_own_terms(self) -> None:
        cash = account(normal_balance=NormalBalance.DEBIT, balance=1_000)
        wallet = account(normal_balance=NormalBalance.CREDIT, balance=1_000)
        new = apply_entries(
            [draft(cash.id, 250), draft(wallet.id, -250)], {cash.id: cash, wallet.id: wallet}
        )
        # A debit grows the asset; the matching credit grows the liability.
        assert new == {cash.id: 1_250, wallet.id: 1_250}

    def test_refuses_to_overdraw_an_account_that_must_stay_non_negative(self) -> None:
        wallet = account(normal_balance=NormalBalance.CREDIT, balance=100)
        other = account(normal_balance=NormalBalance.CREDIT, balance=0)
        with pytest.raises(InsufficientFunds) as caught:
            apply_entries(
                [draft(wallet.id, 101), draft(other.id, -101)], {wallet.id: wallet, other.id: other}
            )
        assert caught.value.extensions == {
            "account_id": str(wallet.id),
            "balance": 100,
            "held_amount": 0,
            "available_balance": 100,
            "change": -101,
        }

    def test_allows_spending_down_to_exactly_zero(self) -> None:
        wallet = account(normal_balance=NormalBalance.CREDIT, balance=100)
        other = account(normal_balance=NormalBalance.CREDIT)
        new = apply_entries(
            [draft(wallet.id, 100), draft(other.id, -100)], {wallet.id: wallet, other.id: other}
        )
        assert new[wallet.id] == 0

    def test_allows_negative_balances_when_the_account_permits_them(self) -> None:
        funding = account(normal_balance=NormalBalance.CREDIT, allow_negative=True)
        wallet = account(normal_balance=NormalBalance.CREDIT)
        new = apply_entries(
            [draft(funding.id, 500), draft(wallet.id, -500)],
            {funding.id: funding, wallet.id: wallet},
        )
        assert new == {funding.id: -500, wallet.id: 500}

    def test_rejects_balances_outside_the_json_safe_range(self) -> None:
        big = account(balance=MAX_MAGNITUDE)
        funding = account(allow_negative=True)
        with pytest.raises(InvalidTransaction, match="supported range"):
            apply_entries(
                [draft(big.id, 1), draft(funding.id, -1)], {big.id: big, funding.id: funding}
            )


class TestTransferEntries:
    def test_between_debit_normal_accounts_credits_the_source(self) -> None:
        source = account(normal_balance=NormalBalance.DEBIT)
        destination = account(normal_balance=NormalBalance.DEBIT)
        assert transfer_entries(source, destination, 300, "USD") == [
            draft(source.id, -300),
            draft(destination.id, 300),
        ]

    def test_between_credit_normal_accounts_debits_the_source(self) -> None:
        source = account(normal_balance=NormalBalance.CREDIT)
        destination = account(normal_balance=NormalBalance.CREDIT)
        assert transfer_entries(source, destination, 300, "USD") == [
            draft(source.id, 300),
            draft(destination.id, -300),
        ]

    def test_moves_value_from_source_to_destination(self) -> None:
        source = account(normal_balance=NormalBalance.CREDIT, balance=1_000)
        destination = account(normal_balance=NormalBalance.CREDIT, balance=0)
        entries = transfer_entries(source, destination, 300, "USD")
        new = apply_entries(entries, {source.id: source, destination.id: destination})
        assert new == {source.id: 700, destination.id: 300}

    def test_requires_a_shared_normal_balance_side(self) -> None:
        with pytest.raises(InvalidTransfer, match="normal balance"):
            transfer_entries(
                account(normal_balance=NormalBalance.DEBIT),
                account(normal_balance=NormalBalance.CREDIT),
                100,
                "USD",
            )

    def test_requires_two_different_accounts(self) -> None:
        same = account()
        with pytest.raises(InvalidTransfer, match="must be different"):
            transfer_entries(same, same, 100, "USD")

    @pytest.mark.parametrize("amount", [0, -5])
    def test_requires_a_positive_amount(self, amount: int) -> None:
        with pytest.raises(InvalidTransfer, match="positive"):
            transfer_entries(account(), account(), amount, "USD")


def test_reversal_negates_every_entry() -> None:
    a, b = uuid4(), uuid4()
    original = [draft(a, 250), draft(b, -250)]
    assert reversal_entries(original) == [draft(a, -250), draft(b, 250)]


# Balanced postings: n-1 random non-zero legs plus one leg that balances them.
balanced_amounts = (
    st.lists(st.integers(min_value=-1_000_000, max_value=1_000_000).filter(bool), min_size=1)
    .map(lambda legs: [*legs, -sum(legs)])
    .filter(lambda legs: legs[-1] != 0)
)


@given(balanced_amounts)
def test_any_balanced_posting_validates_and_its_reversal_cancels_it(amounts: list[int]) -> None:
    accounts = {
        state.id: state for state in (account(allow_negative=True, balance=0) for _ in amounts)
    }
    entries = [
        draft(account_id, amount) for account_id, amount in zip(accounts, amounts, strict=True)
    ]
    validate_entries(entries)
    validate_entries(reversal_entries(entries))

    after = apply_entries(entries, accounts)
    moved = {
        account_id: AccountState(
            id=state.id,
            currency=state.currency,
            normal_balance=state.normal_balance,
            allow_negative=True,
            balance=after[account_id],
            version=1,
        )
        for account_id, state in accounts.items()
    }
    restored = apply_entries(reversal_entries(entries), moved)
    assert all(value == 0 for value in restored.values())


class TestApplyEntriesWithHolds:
    def test_postings_cannot_spend_held_funds(self) -> None:
        wallet = account(normal_balance=NormalBalance.CREDIT, balance=1_000, held=600)
        other = account(normal_balance=NormalBalance.CREDIT)
        accounts = {wallet.id: wallet, other.id: other}
        assert apply_entries(transfer_entries(wallet, other, 400, "USD"), accounts) == {
            wallet.id: 600,
            other.id: 400,
        }
        with pytest.raises(InsufficientFunds) as caught:
            apply_entries(transfer_entries(wallet, other, 401, "USD"), accounts)
        assert caught.value.extensions == {
            "account_id": str(wallet.id),
            "balance": 1_000,
            "held_amount": 600,
            "available_balance": 400,
            "change": -401,
        }
        assert "of which 600 is reserved by active holds" in caught.value.detail

    def test_money_coming_in_is_never_refused_because_of_holds(self) -> None:
        fully_held = account(normal_balance=NormalBalance.CREDIT, balance=500, held=500)
        funding = account(normal_balance=NormalBalance.CREDIT, allow_negative=True)
        new = apply_entries(
            transfer_entries(funding, fully_held, 1, "USD"),
            {fully_held.id: fully_held, funding.id: funding},
        )
        assert new[fully_held.id] == 501

    def test_the_available_balance_stays_in_the_json_safe_range(self) -> None:
        # An account that may go negative is not limited by its funds, so the
        # range check is what keeps balance - held_amount inside +/-MAX_MAGNITUDE.
        funding = account(normal_balance=NormalBalance.CREDIT, allow_negative=True, held=10)
        other = account(normal_balance=NormalBalance.CREDIT)
        accounts = {funding.id: funding, other.id: other}
        limit = MAX_MAGNITUDE - 10
        assert apply_entries(transfer_entries(funding, other, limit, "USD"), accounts) == {
            funding.id: -limit,
            other.id: limit,
        }
        with pytest.raises(InvalidTransaction, match="available balance would leave"):
            apply_entries(transfer_entries(funding, other, limit + 1, "USD"), accounts)


class TestHoldRules:
    def test_an_active_hold_expires_at_its_expires_at(self) -> None:
        assert hold_status(HoldStatus.ACTIVE, LATER, LATER - TICK) is HoldStatus.ACTIVE
        assert hold_status(HoldStatus.ACTIVE, LATER, LATER) is HoldStatus.EXPIRED
        assert hold_status(HoldStatus.ACTIVE, LATER, LATER + timedelta(days=9)) is (
            HoldStatus.EXPIRED
        )

    @pytest.mark.parametrize("final", [HoldStatus.CAPTURED, HoldStatus.VOIDED])
    def test_captured_and_voided_holds_never_expire(self, final: HoldStatus) -> None:
        assert hold_status(final, LATER, LATER + timedelta(days=9)) is final

    def test_the_state_machine_has_one_start_state_and_three_final_ones(self) -> None:
        assert HOLD_TRANSITIONS[HoldStatus.ACTIVE] == {
            HoldStatus.CAPTURED,
            HoldStatus.VOIDED,
            HoldStatus.EXPIRED,
        }
        for final in (HoldStatus.CAPTURED, HoldStatus.VOIDED, HoldStatus.EXPIRED):
            assert HOLD_TRANSITIONS[final] == frozenset()
        # Expiry is derived when a hold is read, so it is never a stored state.
        assert set(STORED_HOLD_STATUSES) == set(HoldStatus) - {HoldStatus.EXPIRED}

    def test_only_an_active_unexpired_hold_can_be_captured_or_voided(self) -> None:
        require_active(hold(), LATER - TICK)
        with pytest.raises(HoldExpired) as expired:
            require_active(hold(), LATER)
        assert expired.value.extensions["hold_status"] == "expired"
        for final in (HoldStatus.CAPTURED, HoldStatus.VOIDED):
            with pytest.raises(HoldNotActive) as closed:
                require_active(hold(final), NOW)
            assert closed.value.extensions["hold_status"] == final

    def test_a_capture_takes_the_whole_hold_by_default_and_never_more(self) -> None:
        authorized = hold(amount=1_000)
        assert capture_amount(authorized, None) == 1_000
        assert capture_amount(authorized, 1) == 1
        assert capture_amount(authorized, 1_000) == 1_000
        for too_much in (1_001, MAX_MAGNITUDE):
            with pytest.raises(InvalidHold, match="authorized only 1000"):
                capture_amount(authorized, too_much)
        with pytest.raises(InvalidHold, match="positive"):
            capture_amount(authorized, 0)

    def test_a_hold_needs_the_amount_to_be_available(self) -> None:
        source = account(normal_balance=NormalBalance.CREDIT, balance=1_000, held=600)
        destination = account(normal_balance=NormalBalance.CREDIT)
        check_hold_request(source, destination, 400, "USD", expires_at=LATER, now=NOW)
        with pytest.raises(InsufficientFunds) as caught:
            check_hold_request(source, destination, 401, "USD", expires_at=LATER, now=NOW)
        assert caught.value.extensions["available_balance"] == 400

    def test_an_account_that_may_go_negative_can_hold_beyond_its_balance(self) -> None:
        source = account(normal_balance=NormalBalance.CREDIT, allow_negative=True, held=5_000)
        destination = account(normal_balance=NormalBalance.CREDIT)
        check_hold_request(source, destination, 10_000, "USD", expires_at=LATER, now=NOW)

    def test_held_and_available_balances_stay_in_the_json_safe_range(self) -> None:
        destination = account(normal_balance=NormalBalance.CREDIT)

        def request(source: AccountState, amount: int) -> None:
            check_hold_request(source, destination, amount, "USD", expires_at=LATER, now=NOW)

        # held_amount may reach MAX_MAGNITUDE, never more.
        rich = account(
            normal_balance=NormalBalance.CREDIT,
            allow_negative=True,
            balance=MAX_MAGNITUDE,
            held=MAX_MAGNITUDE - 5,
        )
        request(rich, 5)
        with pytest.raises(InvalidHold, match="active holds") as caught:
            request(rich, 6)
        assert caught.value.extensions == {
            "account_id": str(rich.id),
            "held_amount": MAX_MAGNITUDE - 5,
            "requested_amount": 6,
        }
        # available_balance may reach -MAX_MAGNITUDE, never less.
        promised = account(normal_balance=NormalBalance.CREDIT, allow_negative=True, held=10)
        request(promised, MAX_MAGNITUDE - 10)
        with pytest.raises(InvalidTransaction, match="available balance would leave"):
            request(promised, MAX_MAGNITUDE - 9)

    def test_a_hold_follows_the_transfer_rules(self) -> None:
        wallet = account(normal_balance=NormalBalance.CREDIT, balance=1_000)
        with pytest.raises(InvalidTransfer, match="must be different"):
            check_hold_request(wallet, wallet, 1, "USD", expires_at=LATER, now=NOW)
        with pytest.raises(InvalidTransfer, match="normal balance"):
            check_hold_request(wallet, account(), 1, "USD", expires_at=LATER, now=NOW)
        euros = account(normal_balance=NormalBalance.CREDIT, currency="EUR")
        with pytest.raises(InvalidTransaction, match="does not match account"):
            check_hold_request(wallet, euros, 1, "USD", expires_at=LATER, now=NOW)

    @pytest.mark.parametrize("expires_at", [NOW, NOW - TICK])
    def test_a_hold_must_expire_in_the_future(self, expires_at: datetime) -> None:
        source = account(normal_balance=NormalBalance.CREDIT, balance=1_000)
        destination = account(normal_balance=NormalBalance.CREDIT)
        with pytest.raises(InvalidHold, match="must be later than"):
            check_hold_request(source, destination, 1, "USD", expires_at=expires_at, now=NOW)


@given(
    hold_amount=st.integers(min_value=1, max_value=10**9),
    other_holds=st.integers(min_value=0, max_value=10**9),
    slack=st.integers(min_value=0, max_value=10**9),
    data=st.data(),
)
def test_capturing_any_part_of_a_hold_fits_and_never_lowers_the_available_balance(
    hold_amount: int, other_holds: int, slack: int, data: st.DataObject
) -> None:
    # Before the capture the source's holds fit within its balance, as the service
    # guarantees. The capture posts `captured` and releases the rest of its hold,
    # so the funds check on the posting can never fail.
    balance = other_holds + hold_amount + slack
    captured = data.draw(st.integers(min_value=1, max_value=hold_amount))
    side = data.draw(st.sampled_from(NormalBalance))
    source = account(normal_balance=side, balance=balance, held=other_holds)
    destination = account(normal_balance=side)
    new = apply_entries(
        transfer_entries(source, destination, captured, "USD"),
        {source.id: source, destination.id: destination},
    )
    available_before = balance - other_holds - hold_amount
    available_after = new[source.id] - other_holds
    assert available_after == available_before + (hold_amount - captured) >= available_before >= 0
    assert new[destination.id] == captured
