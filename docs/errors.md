# Error catalogue

Every error response is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
problem document served as `application/problem+json`. This one comes from the
README walkthrough:

```json
{
  "type": "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#insufficient-funds",
  "title": "Insufficient funds",
  "status": 422,
  "detail": "account 538b137e-b66e-403e-99e2-8f23682083c5 has a balance of 2500 and does not allow it to go below zero; the requested change was -100000",
  "account_id": "538b137e-b66e-403e-99e2-8f23682083c5",
  "balance": 2500,
  "change": -100000,
  "request_id": "64a03f8983dc4a3c87baf30e8c60cf44"
}
```

`type`, `title` and `status` are always present. `detail` explains this
occurrence. Some types add extension members (listed below), and every problem
carries the `request_id` that also appears in the `X-Request-ID` response header
and in the server's log line for the request. Clients should branch on `type`,
not on `detail` text.

Generic HTTP errors that are not specific to the ledger (unknown route, wrong
method) use `"type": "about:blank"` with the standard status phrase as `title`.

| Status | Type | Retry? |
| --- | --- | --- |
| 400 | [invalid-idempotency-key](#invalid-idempotency-key), [invalid-cursor](#invalid-cursor) | No, fix the request |
| 401 | [authentication-required](#authentication-required) | No, fix the credentials |
| 404 | [not-found](#not-found) | No |
| 409 | [already-reversed](#already-reversed), [idempotency-key-reused](#idempotency-key-reused) | No |
| 409 | [concurrent-update](#concurrent-update) | Yes, with the same Idempotency-Key |
| 422 | [validation-error](#validation-error), [invalid-transaction](#invalid-transaction), [unbalanced-transaction](#unbalanced-transaction), [invalid-transfer](#invalid-transfer), [insufficient-funds](#insufficient-funds), [reversal-not-allowed](#reversal-not-allowed) | Only after changing the request or the ledger state |
| 500 | `about:blank` | Report it with the `request_id` |
| 503 | [database-unavailable](#database-unavailable) | Yes, later |

## authentication-required

`401`. The `Authorization: Bearer <key>` header is missing, uses another
scheme, or the key is not one of `LEDGERLINE_API_KEYS`. The response includes
`WWW-Authenticate: Bearer`.

## invalid-idempotency-key

`400`. A money-moving `POST` (`/v1/transactions`, `/v1/transfers`,
`/v1/transactions/{id}/reverse`) was sent without an `Idempotency-Key` header,
or the key is not 1-255 visible ASCII characters.

## invalid-cursor

`400`. The `cursor` query parameter is not a `next_cursor` value returned by
the same list endpoint.

## not-found

`404`. The account or transaction does not exist. Extensions: `account_id` or
`transaction_id`. Unknown accounts referenced inside a request body are
reported the same way.

## validation-error

`422`. The body, path or query parameters do not match the schema: wrong
types (amounts must be JSON integers, not `12.5` or `"1250"`), unknown fields,
a currency that is not three upper-case letters, a timestamp without a
timezone, and so on. Extension: `errors`, a list of `{loc, msg, type}`.

## invalid-transaction

`422`. The entries break a posting rule: fewer than two entries, a zero
amount, the same account twice, an amount or resulting balance outside
+/-(2^53 - 1), or an entry whose currency differs from its account's.

## unbalanced-transaction

`422`. The entries do not sum to zero for every currency. Extension:
`imbalances`, e.g. `{"USD": 1}`.

## invalid-transfer

`422`. A transfer's accounts are the same, or they do not share a normal
balance side (for example an asset and a liability). Post an explicit journal
transaction to `/v1/transactions` instead.

## insufficient-funds

`422`. The posting would take an account with `allow_negative: false` below
zero. Nothing was written. Extensions: `account_id`, `balance` (before the
posting) and `change` (the requested change, in the account's own terms).

## reversal-not-allowed

`422`. The transaction is itself a reversal. To redo the original, post a new
transaction.

## already-reversed

`409`. The transaction already has a reversal. Extensions: `transaction_id`
and `reversed_by_transaction_id`.

## idempotency-key-reused

`409`. The `Idempotency-Key` was already used by this client for a different
request (another body, path or method). Use a new key for a new request.

## concurrent-update

`409`. The accounts kept changing under concurrent requests and the write gave
up after its bounded retries. Nothing was written. The response includes
`Retry-After: 1`; retrying with the same `Idempotency-Key` is safe.

## database-unavailable

`503`. Returned by `/readyz` when the database does not answer a trivial query.
