# ledgerline design notes

These notes explain what the service guarantees, how the code enforces each
guarantee, and which alternatives were considered. Code references point at
`src/ledgerline/`.

## Contents

1. [Goals and non-goals](#1-goals-and-non-goals)
2. [Double-entry bookkeeping as implemented](#2-double-entry-bookkeeping-as-implemented)
3. [Money as integer minor units](#3-money-as-integer-minor-units)
4. [Immutability and reversals](#4-immutability-and-reversals)
5. [Storage model](#5-storage-model)
6. [The write path](#6-the-write-path)
7. [Concurrency control](#7-concurrency-control)
8. [Idempotency](#8-idempotency)
9. [Point-in-time balances and statements](#9-point-in-time-balances-and-statements)
10. [Reconciliation](#10-reconciliation)
11. [Errors, logging and authentication](#11-errors-logging-and-authentication)
12. [Testing strategy](#12-testing-strategy)
13. [Out of scope](#13-out-of-scope)
14. [Questions worth being ready for](#14-questions-worth-being-ready-for)

## 1. Goals and non-goals

The service records money movements so that:

- the books always balance (every movement has an equal and opposite side);
- history is append-only and auditable;
- a retried request never moves money twice;
- concurrent requests never overdraw an account or lose an update;
- every guarantee is checked by tests, and the cached state can be re-verified
  from the journal at any time (`ledgerline reconcile`).

It is deliberately small: one service, one database, synchronous request
handling. Payments rails, FX, multi-tenancy and horizontal scaling are out of
scope (section 13).

## 2. Double-entry bookkeeping as implemented

### Accounts and normal balances

Every account has a type, and the type decides which side makes it grow (its
*normal balance*):

| Type | Grows with | Example |
| --- | --- | --- |
| asset | debits | cash at the bank |
| expense | debits | card-processing fees |
| liability | credits | a customer's wallet: money the platform owes the customer |
| equity | credits | owner's capital |
| revenue | credits | fees earned |

`domain.default_normal_balance` encodes this table. A caller may override it
for contra accounts (accumulated depreciation is an asset account that grows
with credits).

### Signed entries

A journal transaction is a list of entries. Each entry has an account, a
currency and a signed integer amount: **positive for a debit, negative for a
credit**. With that convention, "total debits equal total credits" becomes
"the amounts sum to zero", checked per currency by `domain.validate_entries`.

Balances are reported in each account's own terms (`domain.balance_change`):

```text
debit-normal account:   balance = sum(amounts)
credit-normal account:  balance = -sum(amounts)
```

So a wallet holding $75.00 reports `7500` even though it was built from
credits, and "below zero" means the same thing for every account.

### Worked example (the README walkthrough)

| Transaction | Cash at bank (asset) | Alice wallet (liability) | Bob wallet (liability) |
| --- | --- | --- | --- |
| Deposit $100 | `+10000` debit, balance 10000 | `-10000` credit, balance 10000 | |
| Alice pays Bob $25 | | `+2500` debit, balance 7500 | `-2500` credit, balance 2500 |
| Reversal of the payment | | `-2500` credit, balance 10000 | `+2500` debit, balance 0 |

Each row sums to zero, so the whole journal does too (the *trial balance*).
The accounting equation holds at every step: assets (10000) equal liabilities
(Alice + Bob).

### Rules every transaction must satisfy

`domain.validate_entries` runs before the database is touched:

- at least two entries;
- no zero amounts, and every amount within +/-(2^53 - 1) (section 3);
- each account at most once, so every statement line has one well-defined
  running balance and no intermediate state can look like an overdraft;
- the amounts sum to zero **per currency**. A USD leg can never be balanced by
  a EUR leg, which is why `+100 USD, -100 EUR` is rejected.

After loading the accounts, `domain.check_currencies` requires each entry to be
in its account's currency, and `domain.apply_entries` computes each new balance
and raises `InsufficientFunds` if an account with `allow_negative: false`
would drop below zero.

### Transfers

`POST /v1/transfers` takes `from`, `to` and a positive amount. "From" loses
value and "to" gains it, which is only unambiguous when both accounts grow on
the same side: two wallets, or two bank accounts. For two credit-normal wallets
the source is debited and the destination credited; for two debit-normal
accounts it is the other way round (`domain.transfer_entries`). A deposit, by
contrast, makes *both* sides grow (debit cash, credit the wallet), so mixed
cases are rejected with `invalid-transfer` and must be posted as explicit
journal transactions, where the caller states the debits and credits.

### Multiple currencies

An account holds exactly one currency and the ledger never converts. A foreign
exchange is modelled as one transaction with a balanced USD pair and a
balanced EUR pair, routed through conversion accounts; the test suite includes
such a transaction.

## 3. Money as integer minor units

Amounts are integers in the currency's minor unit (cents for USD).

- **Not floats:** binary floating point cannot represent 0.10 exactly, and
  rounding errors accumulate. Strict validation (`schemas.SignedAmount`,
  `strict=True`) rejects `12.5` and also `"1250"` and `true`, so nothing is
  coerced silently.
- **Not decimals:** `Decimal` would work, but integers make the invariants
  exact equalities, map to `BIGINT` on both databases, and leave no rounding
  mode to argue about. Formatting for display is the client's job.
- **Bounded:** amounts and balances stay within +/-(2^53 - 1)
  (`domain.MAX_MAGNITUDE`). That is the largest integer a JavaScript client can
  parse from JSON without rounding, and it fits comfortably in a signed 64-bit
  column.

The minor-unit exponent (2 for USD, 0 for JPY) is not stored; the service never
needs it because it never converts or formats amounts.

## 4. Immutability and reversals

Posted transactions and entries are never updated or deleted; there is simply
no code path that does it. (In production I would also revoke `UPDATE` and
`DELETE` on those tables from the application's database role.)

A correction is a **reversal**: a new transaction whose entries are the
original's with the signs flipped (`domain.reversal_entries`), linked through
`transactions.reverses_id`. Consequences:

- The statement shows the original, the reversal and why (the reversal's
  description), which is what an auditor expects.
- `UNIQUE (reverses_id)` lets each transaction be reversed at most once; the
  service checks first so the client gets `409 already-reversed` instead of a
  constraint error.
- Reversing a reversal is refused (`422 reversal-not-allowed`). "Un-reversing"
  is a new business event and should be posted as one.
- A reversal is an ordinary posting, so it obeys the overdraft rule: if Alice
  has already spent a deposit, reversing the deposit fails with
  `insufficient-funds`. Recovering that money is a separate process.
- Point-in-time balances before the reversal still show the original effect
  (README step 8).

## 5. Storage model

Four tables, created by `migrations/versions/20261023_0001_create_ledger_tables.py`
and mirrored by `models.py` (a test checks they agree):

| Table | Key columns | Constraints |
| --- | --- | --- |
| `accounts` | `balance BIGINT`, `version INTEGER`, `allow_negative` | `CHECK (allow_negative OR balance >= 0)`, enum CHECKs, index on `(created_at, id)` |
| `transactions` | `posted_at`, `reverses_id` | FK `reverses_id -> transactions.id`, `UNIQUE (reverses_id)` |
| `entries` | `amount BIGINT`, `currency`, `balance_after` | `CHECK (amount <> 0)`, `UNIQUE (transaction_id, account_id)`, FKs, index on `(account_id, id)` |
| `idempotency_keys` | `(principal, key)` primary key, `fingerprint`, `response_body` | the primary key arbitrates races |

**Why cache a balance at all?** Reading `SUM(amount)` on every write is O(number
of entries) and still racy without locks. A stored balance makes reads O(1) and
gives every write one row to check and update. The cost is a denormalisation
that must never drift, so it changes only together with the entries, guarded
by the version column, and `ledgerline reconcile` re-derives it.

**Constraints are a backstop, not the main check.** The service validates
everything first so clients get precise errors. The constraints exist so that
even a bug in the service cannot store a zero amount, duplicate a leg, reverse
twice or overdraw an account; such a bug would surface as a 500 with a logged
`IntegrityError` instead of as corrupted books.

Timestamps use a small `UtcDateTime` type: PostgreSQL stores `timestamptz`,
SQLite stores naive text, and the type normalises both to timezone-aware UTC.

## 6. The write path

Every money-moving request runs `LedgerService._write`, one database
transaction per attempt:

```text
for attempt in 1..10:
    BEGIN
      record = SELECT * FROM idempotency_keys WHERE principal = ? AND key = ?
      if record: COMMIT and return the stored response (or 409 if the fingerprint differs)

      accounts = SELECT * FROM accounts WHERE id IN (...)
      check currencies, compute new balances, refuse overdrafts     -- domain.py
      posted_at = now()
      for each account, in sorted id order:
          UPDATE accounts SET balance = ?, version = version_read + 1
          WHERE id = ? AND version = version_read                  -- compare-and-swap
          if 0 rows changed: ROLLBACK, back off, retry
      INSERT INTO transactions ...; INSERT INTO entries ...
      INSERT INTO idempotency_keys (principal, key, fingerprint, response_body)
          if the primary key already exists: ROLLBACK, back off, retry (and replay)
    COMMIT
raise ConcurrentUpdate  (409, Retry-After: 1)
```

Business-rule failures (unknown account, unbalanced entries, insufficient
funds) raise immediately and roll back; only conflicts are retried.

## 7. Concurrency control

### The race

```text
T1: read Alice = 100           T2: read Alice = 100
T1: 100 - 80 >= 0, ok          T2: 100 - 70 >= 0, ok
T1: write Alice = 20           T2: write Alice = 30      <- T1's withdrawal is lost
```

Both requests saw enough money, both succeeded, $150 left a $100 account, and
the stored balance no longer matches the entries. Every approach below
prevents this by making the read-check-write sequence atomic per account.

### Options considered

1. **Pessimistic locking (`SELECT ... FOR UPDATE`).** Lock the account rows
   before reading them; a second writer waits until the first commits, then
   reads the new balance. No retries, and contention turns into queueing.
   Costs: lock waits hold connections, every writer pays for locking even when
   there is no conflict, lock order must be managed to avoid deadlocks, and
   SQLite has no row locks.
2. **Optimistic concurrency (chosen).** Read without locks, decide, then write
   with `UPDATE ... WHERE id = ? AND version = ?`. If someone else changed the
   row since the read, zero rows match, and the whole attempt rolls back and
   retries with fresh data. Costs: wasted work and retries under contention,
   and a request can give up (`409 concurrent-update`) on a very hot account.
3. **`SERIALIZABLE` isolation.** Let PostgreSQL detect conflicts and retry on
   serialization failures. Correct and general, but behaviour differs between
   databases and failures are reported less precisely.
4. **A single conditional update**
   (`UPDATE ... SET balance = balance - :x WHERE id = :id AND balance >= :x`).
   Atomic and lock-based in effect. It works well for one account, but a
   multi-leg posting also needs each account's exact new balance for
   `balance_after`, and all legs must succeed or fail together.

I chose optimistic versioning because:

- the decision (is this posting allowed, what are the new balances?) stays in
  plain Python (`domain.py`) that is unit-tested without a database, and the
  database only answers "is my read still current?";
- the same code runs on PostgreSQL and on SQLite, which has no `SELECT ... FOR UPDATE`;
- conflicts are explicit, so tests can force them (`test_concurrency.py`
  replaces the compare-and-swap with one that fails twice and checks that the
  retry succeeds, and one that always fails and checks that retries stop);
- typical ledger traffic touches many different accounts, where optimistic
  control rarely conflicts.

### How the compare-and-swap behaves on each database

**PostgreSQL (Read Committed, the default).** If T2's `UPDATE` targets a row
that T1 has updated but not committed, T2 waits. When T1 commits, PostgreSQL
re-evaluates T2's `WHERE` clause against the new row version; the version no
longer matches, so T2 updates zero rows and retries. If T1 rolls back instead,
T2's update proceeds. Correctness therefore does not depend on a stricter
isolation level.

**SQLite.** SQLite allows one writer at a time. `db.py` makes every transaction
start with `BEGIN IMMEDIATE`, so writers queue for the lock (up to a 30-second
busy timeout) instead of reading concurrently and then failing to upgrade to a
write lock with "database is locked". Writers are fully serialised, so the
compare-and-swap never fails on SQLite; the threaded tests still check that there
are no lost updates, and they exercise real conflicts when run on PostgreSQL
(locally through pgserver, and in CI).

### Deadlocks

Two transactions that update the same accounts in different orders can each
hold one row lock and wait for the other. `_post` always updates accounts in
sorted id order, so lock acquisition order is consistent and that cycle cannot
form.

### Retries

Up to 10 attempts, with exponential backoff and full jitter capped at 50 ms
between them (`service._backoff`), so requests that collided do not retry in
lockstep. After the last attempt the client gets `409 concurrent-update` with
`Retry-After: 1`; nothing was written, and retrying with the same
`Idempotency-Key` is safe.

### When I would switch approach

If a few accounts are extremely hot (a fee account, a settlement account),
optimistic retries waste work and can starve. Options then: row locks
(`SELECT ... FOR UPDATE`) for those writes, a per-account queue or single
writer, or splitting a hot account's balance across several sub-account rows
that are summed on read.

## 8. Idempotency

### Contract

- `Idempotency-Key` is required on money-moving `POST`s (transactions,
  transfers, reversals) and optional on `POST /v1/accounts`.
- Same key and same request: the original status and body are replayed, with
  `Idempotent-Replayed: true`.
- Same key and a different request: `409 idempotency-key-reused`.
- Concurrent duplicates: exactly one takes effect; the others replay it.

### Design

- **Scope.** Keys are stored per client: the primary key is
  `(principal, key)`, where `principal` is a fingerprint of the API key. Two
  clients choosing the same key cannot collide or read each other's responses.
- **Fingerprint.** SHA-256 of the method, the path and the *validated* body
  serialised as canonical JSON (sorted keys, no whitespace), so a retry with
  reordered fields still matches, while a different amount or a different
  endpoint does not.
- **Atomic with the effect.** The idempotency row is inserted in the same
  database transaction as the ledger changes. If the server crashes before the
  commit, nothing happened and a retry executes normally; if it crashes after
  the commit but before responding, the retry finds the row and replays. There
  is no window in which money moved but the key was not recorded.
- **Races.** Two identical requests can both find no record and both do the
  work. Only one can insert `(principal, key)`; the other gets a primary-key
  violation (on PostgreSQL it first waits for the winner's uncommitted
  insert), rolls back its own ledger changes, retries, and replays the
  winner's response. In practice the loser usually fails the balance
  compare-and-swap first, with the same outcome. `test_concurrency.py` forces
  this path deterministically.
- **Only successes are stored.** A request that fails validation or a business
  rule rolls back entirely, the key included, so the client can fix the
  problem and retry with the same key. Stripe instead stores the outcome of any
  request that started executing; storing failures makes "the outcome of this
  key" final but forces clients to mint a new key after, say, a top-up. For a
  ledger I preferred the simpler rule that only an effect is recorded.
- **Status code for mismatches.** The IETF Idempotency-Key draft suggests 422
  for a reused key with a different payload; this service returns 409 because
  the request conflicts with state the server already holds (the key is taken).
- **Retention.** Records are kept forever. A production system would expire
  them after a window (Stripe uses 24 hours) with a cleanup job.

## 9. Point-in-time balances and statements

`GET /v1/accounts/{id}/balance?as_of=T` sums the account's entries whose
transaction has `posted_at <= T`; history is rebuilt from the immutable
journal, not from the cached balance.

For that history to be consistent, `posted_at` must order transactions on the
same account the way they were applied. `_post` takes the timestamp *after*
reading the account rows. A successful attempt read the current version of
every account it touches (otherwise its compare-and-swap would fail), so any
earlier transaction on those accounts had committed before the read, and
therefore before this timestamp. Per account, `posted_at` order matches commit
order. This assumes a single server clock; with several application servers,
clock skew could reorder history (balances themselves stay correct).

Statements (`GET /v1/accounts/{id}/entries`) list entries in id order with
`balance_after`, the account's balance right after the entry, written in the
same transaction as the entry. The state-machine test re-derives every running
balance after each step.

Lists use **keyset pagination**: the opaque cursor encodes the last row's sort
key (`(created_at, id)` for accounts, entry id for statements), and the next
page starts strictly after it. Unlike `OFFSET`, this stays fast on large tables
and does not skip or repeat rows when new rows are appended. Its blind spot is a
row that commits later with a smaller key than a page the client has already
read.

## 10. Reconciliation

`ledgerline reconcile` (`reconcile.py`) recomputes the invariants from the
entries and exits 1 if any fails:

1. each account's stored balance equals the signed sum of its entries, in the
   account's own terms;
2. every transaction's entries sum to zero per currency;
3. every entry uses its account's currency;
4. the trial balance (all entries per currency) is zero.

Each check is a single SQL statement, so each sees a consistent snapshot even
while the API writes. Tests tamper with the database directly (a changed
balance, an unbalanced transaction, an entry in the wrong currency) and check
that each is reported.

## 11. Errors, logging and authentication

**Errors.** Each domain exception class carries its HTTP status, problem type
and title (`errors.py`), and one handler turns any of them into an RFC 9457
document (`api/problems.py`). Request validation errors, unknown routes and
unexpected crashes use the same format, so clients parse one error shape.
Validation errors report location, message and type but never echo the
rejected input. The catalogue is in [errors.md](errors.md).

**Logging.** One JSON object per line. A pure ASGI middleware
(`api/middleware.py`) assigns each request an ID (or keeps a well-formed
incoming `X-Request-ID`), stores it in a context variable, times the request
and logs one line with method, path, status, duration and client. Context
variables follow the request into the worker thread that runs the endpoint,
so any log call made while handling the request carries the ID. Unhandled
exceptions are logged with their traceback and returned as a 500 problem that
includes the request ID, without internal details.

**Authentication.** Static bearer keys from `LEDGERLINE_API_KEYS`. The presented
key and each configured key are hashed with SHA-256 and compared with
`hmac.compare_digest`, so timing reveals neither the key's contents nor its
length, and every configured key is checked so timing does not reveal which
one matched. With no keys configured every `/v1` request is rejected. Logs and
idempotency records identify clients by a 16-hex-digit fingerprint of their key.
This is enough for a single trusted caller; section 13 covers what real
deployments need.

The SHA-256 digests here are used for fixed-length comparison and stable
bearer-token fingerprints, **not password storage**. A fingerprint can permit
offline guessing of a weak human-chosen key if logs or idempotency records
are exposed. Use the CLI to generate a 32-byte random bearer token; do not
use the disposable example key outside local demos. Token entropy is an
operational requirement, not a validation rule. See [security review #4](https://github.com/ult666666/ledgerline/issues/4)
for the CodeQL findings and any future fingerprint design changes. Any
change to fingerprint derivation must preserve principal stability across
service restarts to avoid breaking durable idempotency.

## 12. Testing strategy

| Layer | File | What it shows |
| --- | --- | --- |
| Domain rules | `test_domain.py` | Each rule in isolation, plus a Hypothesis property: any balanced posting validates and its reversal cancels it |
| HTTP API | `test_api_*.py`, `test_idempotency.py`, `test_http.py` | Every endpoint, error type, header and status code |
| State machine | `test_stateful.py` | Random operation sequences keep the invariants (below) |
| Concurrency | `test_concurrency.py` | No lost updates or overdrafts under simultaneous writers; one effect per key; retries and their bound |
| Schema | `test_migrations.py`, `test_storage.py` | `alembic upgrade head` matches the models and the API runs on it; storage details |
| Operations | `test_reconcile.py`, `test_cli.py`, `test_observability.py` | Reconciliation catches tampering; CLI exit codes; log format |

The Hypothesis `RuleBasedStateMachine` keeps a model of every balance and
predicts each request's outcome: success, `insufficient-funds`,
`already-reversed`, `reversal-not-allowed`, a replay or
`idempotency-key-reused`. After every step it asserts that stored balances
equal the model (so replays and rejected requests changed nothing), that
`reconcile` passes (stored balance equals the sum of entries, transactions and
the trial balance are zero per currency), that no non-negative account is below
zero, and that every `balance_after` matches the running sum. When a sequence
fails, Hypothesis shrinks it to a minimal reproduction.

To check that the state machine has teeth, I temporarily broke the service
three ways (skipping one account's balance update, never storing idempotency
records, and writing the pre-posting balance into `balance_after`); the test
failed on each.

The whole suite runs on SQLite by default and on PostgreSQL when
`DATABASE_URL` points at a database whose name contains `test`.

## 13. Out of scope

- **FX conversion.** Rates, rounding policy, quote expiry and the accounts for
  FX gains and losses are a product in themselves. The ledger already supports
  the bookkeeping (one transaction balanced per currency through conversion
  accounts); the conversion logic would live in a separate service that posts
  to it.
- **Multi-tenant authorization.** Today every key can touch every account. A
  real ledger needs tenants (a `tenant_id` on every row, enforced in every
  query or with PostgreSQL row-level security), per-client scopes such as
  read-only or per-account-group access, and identity from OAuth 2.0/OIDC or
  mTLS rather than shared secrets.
- **Sharding.** One PostgreSQL primary goes a long way for a ledger. Beyond it,
  accounts could be partitioned by tenant; a transaction spanning shards then
  needs a coordination protocol (for example a two-phase pending/posted
  transfer model), which is a large step up in complexity.
- **Event streaming.** Downstream consumers (notifications, analytics) should
  learn about postings without dual writes. The usual fix is a transactional
  outbox: insert an event row in the same database transaction as the posting
  and publish it to a broker from a separate relay.
- Also not built: pending or authorised-but-unsettled funds, backdated
  entries, account closure, transaction search, rate limiting, metrics and
  tracing export.

## 14. Questions worth being ready for

**Why not store amounts as floats or decimals?** Floats cannot represent most
decimal fractions exactly. Decimals would work, but integers of minor units
make every invariant an exact integer equality and avoid rounding policy.

**Why keep a stored balance if it can be computed from entries?** O(1) reads and
one row to compare-and-swap on writes. The price is a cache that must never
drift, hence the version check, the single transaction and `reconcile`.

**Two requests withdraw from the same account at the same time. What happens?**
Both read version *n*. The first compare-and-swap moves the row to *n + 1*. On
PostgreSQL the second waits for the first to commit, re-checks its `WHERE`
clause, matches zero rows, rolls back and retries against the new balance, so
the overdraft rule is applied to fresh data. On SQLite the second simply waits
for the write lock.

**What if the server crashes after committing but before responding?** The
client retries with the same key; the idempotency row was committed with the
transfer, so it gets the original response, not a second transfer.

**Why is the key scoped per API client?** Otherwise two clients picking the same
key would collide, and one could receive the other's stored response.

**Why are only successful responses stored?** A failure leaves no trace in the
database, which keeps the write atomic and lets a client retry the same logical
request after fixing the cause. Section 8 covers the trade-off against Stripe's
behaviour.

**Why can a transfer not go from a cash account to a wallet?** Because "from"
and "to" are ambiguous across normal-balance sides: a deposit increases both
accounts. Those movements are explicit journal transactions.

**Why `BEGIN IMMEDIATE` on SQLite?** A deferred transaction reads under a
shared lock and then needs to upgrade to a write lock; two such transactions
deadlock, and SQLite reports that as "database is locked". Taking the write
lock up front makes writers queue instead.

**How would you scale it?** PostgreSQL first: connection pooling, read replicas
for statements and history, and partitioning the `entries` table by time. Then
hot-account mitigation (section 7) and a transactional outbox for consumers.
Sharding by tenant comes last, because cross-shard transfers need a two-phase
protocol.
