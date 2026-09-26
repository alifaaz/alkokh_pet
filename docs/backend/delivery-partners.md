# Delivery Partners — backend contract

> **RECONSTRUCTED, PENDING REVIEW.** The original `docs/backend/delivery-partners.md` was
> not in the repository when this backend was built, and no frontend source referencing
> delivery partners was available on the build host either. Every *behavioural* rule below
> came from the implementation brief and is authoritative. Every *identifier* — field
> names, argument names, response keys — was reconstructed to match this app's existing
> conventions and **must be checked against what the merged frontend actually sends and
> reads**. Where the two disagree, the frontend wins and this document plus the code
> should be corrected together. The one identifier stated in the brief itself, and so not
> a guess, is `custom_partner_commission_rate`.

Delivery partners are the aggregator apps — Talabat, Toters and similar — that list our
products, charge the customer at their end, and transfer the month's takings minus their
commission. 100,000 of sales at 25% arrives as 75,000.

## 0. The one rule that cannot bend

**A partner order must never count as cash in the till.**

`expected_cash_on_hand` is derived from the cashier's cash account ledger
(`POS Profile.custom_cash_account`), and a till is resolved purely by that account. If a
partner sale touched it, the cashier would be told to hand over money a delivery app is
holding, and the drawer would come up short by the value of every app order that shift —
found by whoever counts it, hours later, with nothing to point at.

So a partner order is booked as a **`Due` sale**: `is_pos = 0`, no payment row, an
ordinary receivable against the partner's own Customer. The money is collected later, from
the partner, not from anyone standing at the counter.

**Do not "improve" this into a Mode of Payment per partner.** Considered and rejected.
`set_account_for_mode_of_payment` has already been caught rewriting one till's takings
into another till's drawer — which is why `create_pos_sale` re-asserts the profile's cash
account between the last save and the submit — and a partner Mode of Payment walks
straight into that. It also gives no per-invoice settled state, so reconciliation degrades
to "the account balance looks about right".

## 1. `Delivery Partner`

Module `Pet App`. Named `field:partner_name`, so the record's primary key *is* its trading
name and that is what a Sales Invoice's partner link stores.

| Field | Type | Notes |
| --- | --- | --- |
| `partner_name` | Data | Required, unique. The primary key. |
| `is_active` | Check | Default 1. Off ⇒ the till refuses new orders (`PARTNER_INACTIVE`). Existing orders and settlements are unaffected — a switched-off partner is still settleable, because last month's takings still have to be collected. |
| `commission_type` | Select | `Percentage` / `Amount`, default `Percentage`. Which of the two figures below this partner is actually charged on. Partners that predate the field are `Percentage`, which is what they were. |
| `commission_rate` | Percent | Required **when the type is `Percentage`**. Must be `> 0` and `< 100`; refused server-side, the client is not the authority. Zeroed when the type is `Amount`. |
| `commission_amount` | Currency | Required **when the type is `Amount`**: the flat fee kept from *each* order, whatever it is worth. Must be `> 0`. Zeroed when the type is `Percentage`. |
| `partner_name_ar` | Data | Trading name in Arabic, for the till and printed receipts. |
| `logo` | Attach Image | **Not mandatory.** See *Logo upload*. |
| `brand_color` | Color | Used by the POS partner tile so a cashier picks the right app by sight. |
| `settlement_cycle` | Select | `Weekly` / `Biweekly` / `Monthly`, **with a leading blank option**. Reporting metadata only — nothing computes a settlement period from it. |
| `customer` | Link → Customer | **Backend-owned, read-only.** Created on insert. |
| `commission_expense_account` | Link → Account | Where commission is booked as an expense at settlement. Must not be a till cash account. |
| `contact_person`, `contact_number` | Data | Optional. |
| `notes` | Small Text | Optional. |

These field names are **no longer reconstructed**: they were recovered from the live
site's Error Log, where the merged frontend's own list request records the exact set it
asks for. Frappe fails an entire list query when one requested field is missing, so the
partner list page 500s until every one of them exists — which is precisely how they came
to be recorded.

Permissions: System Manager, Accounts Manager and Sales Manager get full rights; Accounts
User gets read/write; Sales User gets read (the till needs to list partners).

### `customer` — owned by the backend, never sent by a client

Created automatically on insert as a `Company`-type Customer named after the partner.
`customer_type` is Company deliberately: `validate_customer_identity_projection` refuses a
new Individual Customer with no Guardian behind it, and an aggregator is a company anyway.

Every partner order is billed to it, so **repointing it at a Customer that already carries
non-partner history is refused**. Not "any history" — the partner's own orders are exactly
what is expected to be there. What is refused is adopting a Customer billed for something
else (a walk-in, a clinic visit, another partner), because from then on two populations
share one receivable and the statement can never be reconciled. A wrong partner→customer
link surfaces nowhere until a statement fails to reconcile a month later, by which time
the invoices are submitted.

### Logo upload

`logo` **must not be mandatory on insert.** The form uses the app's shared image uploader,
and Frappe refuses a file naming a doctype with no docname — so the record has to exist
before its image can be uploaded at all. The sequence is:

1. Client saves the partner (no logo).
2. Client uploads with `doctype=Delivery Partner`, `docname=<name>`, `is_private=0`.
3. Client PUTs `{ "logo": "<file_url>" }` itself.

**Public, not private.** A private logo 403s in the till: the browser does not send the
auth header on an image request.

## 2. Sales Invoice custom fields

Seven — six from `pet_app.patches.delivery_partner_schema` and
`custom_partner_commission_type` from `pet_app.patches.delivery_partner_commission_type` —
all read-only, all null on
an ordinary sale. **Patch-owned** — they must not be added to the Custom Field fixture
allow-list in `hooks.py`; `tests/test_fixture_allowlist.py` fails on a field owned by both.

| Field | Type | Notes |
| --- | --- | --- |
| `custom_delivery_partner` | Link → Delivery Partner | Its presence is what makes an invoice a partner sale. |
| `custom_partner_order_ref` | Data | The aggregator's own order number. |
| `custom_partner_customer_name` | Data | Who ordered it in the app. The invoice is billed to the partner, so without this the end customer's name appears nowhere. |
| `custom_partner_commission_type` | Select | **A snapshot taken at sale time** (`pet_app.patches.delivery_partner_commission_type`): which of the two figures below this order was charged on. Empty on orders that predate flat-fee partners — those are all `Percentage`. |
| `custom_partner_commission_rate` | Percent | **A snapshot taken at sale time.** `0` on a flat-fee order. |
| `custom_partner_commission_amount` | Currency | `grand_total × rate ÷ 100` on a percentage order, the partner's flat fee on an `Amount` one. Computed at sale time. |
| `custom_partner_settlement` | Link → Settlement | `allow_on_submit`. Empty means unsettled — **this field is the settled state**, so there is one place to look and no flag that can disagree with it. |

`custom_partner_commission_rate` is a **snapshot**. Never read the live rate at settlement:
the first time a partner renegotiates, every past month would silently restate. The same
goes for `custom_partner_commission_type`, which decides *which* stored figure means
anything — reading the live type would restate every past order the first time a partner
moves from a percentage to a flat fee.

These fields are what unblock the frontend's orders tab, and **their absence is already
handled**. Frappe fails an entire list query when one requested field does not exist, so
the frontend deliberately keeps them out of its shared Sales Invoice field list and asks
for them only in its own query, retrying without them. Nothing breaks while they are
missing and nothing needs redeploying when they arrive.

## 3. `pet_app.api.pos.create_pos_sale` — five optional arguments

`delivery_partner`, `partner_order_ref`, `partner_customer_name`, `partner_commission_rate`,
`partner_commission_amount`.

The frontend omits the whole group on an ordinary sale, so **a counter sale's request body
is unchanged from today** and never reaches any of the partner logic.

`partner_commission_rate` and `partner_commission_amount` are optional even on a partner
sale: omitted, the partner's current figure is used. Whichever is used is snapshotted onto
the invoice.

**The type is the partner's, never the caller's.** A till that could choose it could bill a
flat-fee partner a percentage, and the settlement would then read whichever figure that
sale happened to store. So `partner_commission_rate` is *ignored* — not refused — for an
`Amount` partner: the POS bundle sends the partner's rate on every sale, and a flat-fee
partner's rate is `0`, which would otherwise make every one of their orders unsellable.

Refusals, **in this order** — the client renders the first code it receives as the
persistent on-screen block beside Process Transaction, not a toast that vanishes:

| Code | When |
| --- | --- |
| `PARTNER_NOT_FOUND` | no such partner *(added; not in the original table, but unavoidable)* |
| `PARTNER_INACTIVE` | the partner is switched off |
| `PARTNER_SALE_MUST_BE_DUE` | a partner sale arrived carrying a payment (`payment_status = "Paid"`, or any `paid_amount`) |
| `PARTNER_CUSTOMER_MISMATCH` | `customer` is not the partner's own customer |
| `PARTNER_ORDER_REF_REQUIRED` | no partner order number |
| `PARTNER_INVALID_COMMISSION_RATE` | `Percentage` partner, resolved rate outside `0 < r < 100` *(added)* |
| `PARTNER_INVALID_COMMISSION_AMOUNT` | `Amount` partner, resolved flat fee not `> 0` |
| `PARTNER_COMMISSION_EXCEEDS_TOTAL` | the flat fee is not less than the order's total — the flat-fee counterpart of refusing a rate of 100 or more. Raised late, once the total is known. |

Every message is written for a cashier mid-sale: it says what to do, not what is
structurally wrong.

The receipt payload gains two keys, both `null` on an ordinary sale:

- `partner_label` — the partner's display name
- `partner_order_ref`

**Without them a Due partner sale prints as a plain unpaid invoice**, and whoever collects
it may ask the customer for money the app already took. Both are read off the *document*
rather than the request, so a replayed (idempotent retry) sale prints correctly too.

`branch` continues to be stamped server-side, unchanged by this work.

## 4. `Delivery Partner Settlement`

Named `DPS-.YYYY.-`. Not submittable: it is written, with its Payment Entry, in one
transaction by `create_settlement`.

| Field | Type | Notes |
| --- | --- | --- |
| `partner` | Link → Delivery Partner | Required. **`partner`, not `delivery_partner`** — the client filters on this name. |
| `partner_name` | Data | `fetch_from` the partner; denormalised so a list renders without a fetch per row. |
| `customer` | Link → Customer | Snapshot of the partner's customer. |
| `posting_date` | Date | Required. |
| `from_date`, `to_date` | Date | Reporting metadata only — the covered invoices are the rows in the table, never a date range re-evaluated later. |
| `statement_reference` | Data | The partner's own statement or transfer number. |
| `gross_amount` | Currency | Read-only, derived. **Built from each invoice's OUTSTANDING, not its grand total.** |
| `commission_rate` | Percent | Read-only, derived: this settlement's commission over its own gross. Not the partner's live rate and not any invoice snapshot — it is what the settlement actually worked out at, to set beside the statement in a dispute. |
| `commission_amount` | Currency | Required, **editable** — see below. |
| `adjustment_amount` | Currency | May be negative. |
| `adjustment_account` | Link → Account | Where the adjustment posts. **Separate from the commission account on purpose:** a clawback is not commission, and merging them makes the commission figure useless for reporting. Falls back to the commission account when blank. |
| `received_amount` | Currency | Required. What actually landed in the bank. |
| `received_in` | Link → Account | Required. Must not be a till cash account. |
| `payment_entry` | Link → Payment Entry | Read-only. |
| `invoice_count` | Int | Read-only, stored, so a list does not fetch every child table. |
| `invoices` | Table → `Delivery Partner Settlement Invoice` | Read-only. |
| `idempotency_key` | Data | Read-only. |
| `remarks` | Small Text | |

These names, like §1's, were **recovered from the live site's Error Log** rather than
reconstructed. The settlements tab showed nothing for every partner because the doctype
called its link `delivery_partner` while the client filters on `partner` — and Frappe fails
an entire list query on one unknown field, so every settlement was invisible and the tab
read as "this partner has none". `pet_app.patches.delivery_partner_settlement_field_alignment`
renames the column in place, preserving settlements already banked.

Child table `Delivery Partner Settlement Invoice`: `sales_invoice`, `posting_date`,
`partner_order_ref`, `gross_amount` (that invoice's outstanding), `commission_amount`.

Permissions: System Manager and Accounts Manager full; Accounts User create/read/write;
Sales Manager and Sales User read.

**`gross_amount` is built from OUTSTANDING.** A partly paid invoice must contribute only
what it still owes, or the settlement claims to collect money already collected and the
tie-out is wrong by exactly that difference.

**A flat fee is not pro-rated.** A percentage row is `outstanding × snapshotted rate`; an
`Amount` row is the snapshotted fee itself, capped at what the invoice still owes — a flat
fee does not scale down with a part payment, but it can never exceed the receivable it is
clearing, or the settlement could not tie out.

**`commission_amount` is editable, deliberately.** The stored rate seeds it, but the
document being reconciled is the partner's own statement. When they disagree, the
statement wins.

## 5. `pet_app.api.delivery_partners.create_settlement`

```
create_settlement(partner, invoices=None, from_date=None, to_date=None,
                  received_amount=None, commission_amount=None, adjustment_amount=None,
                  received_in=None, adjustment_account=None, statement_reference=None,
                  posting_date=None, remarks=None, idempotency_key=None, probe=0)
```

`invoices` is a list of Sales Invoice names (or of `{invoice: ...}` rows). Omitted, it
defaults to every unsettled, submitted, still-outstanding invoice for that partner in the
date range. Omitted money figures are derived: `commission_amount` from the snapshotted
per-invoice figures (rate or flat fee, per that invoice's snapshotted type),
`received_amount` from `gross − commission − adjustment`.

**One transaction: the settlement, the Payment Entry and the invoice stamps, or nothing.**
Split into separate saves, a failure between them either credits the partner's account
with no document explaining it, or marks invoices settled against money that never
arrived. Both are found by whoever reconciles the month, long after the evidence is gone.

The Payment Entry is a **Receive** against the partner's Customer for `received_amount`,
with the covered invoices as reference rows allocated at their full outstanding, and the
commission and adjustment as **deductions** (two rows, so they stay distinguishable in the
ledger), so the invoices close in full:

```
Dr Bank / Cash          75,000     (received_amount)
Dr Commission Expense   25,000     (commission_amount)
    Cr Partner Receivable      100,000     (gross_amount)
```

ERPNext's own `validate_difference_amount` then re-proves the arithmetic independently:
allocated − received − deductions must be zero or the Payment Entry refuses to submit.

Refusals:

| Code | When |
| --- | --- |
| `SETTLEMENT_NOT_PERMITTED` | caller may not create settlements / submit Payment Entries |
| `PARTNER_REQUIRED`, `PARTNER_NOT_FOUND`, `PARTNER_CUSTOMER_MISSING` | partner resolution |
| `NO_INVOICES_TO_SETTLE` | nothing outstanding in the period |
| `INVOICE_NOT_FOUND`, `INVOICE_NOT_SUBMITTED`, `INVOICE_NOT_FOR_PARTNER`, `INVOICE_NOT_OUTSTANDING` | per-invoice checks |
| `INVOICE_ALREADY_SETTLED` | names the invoice **and** the settlement holding it |
| `SETTLEMENT_DOES_NOT_TIE_OUT` | `received + commission + adjustment != gross` — **names the gap** |
| `SETTLEMENT_RECEIVED_IN_REQUIRED` | no bank account given |
| `SETTLEMENT_COMMISSION_ACCOUNT_MISSING` | commission to book, but no `commission_expense_account` on the partner |
| `SETTLEMENT_ADJUSTMENT_ACCOUNT_MISSING` | an adjustment to book, but no account for it |
| `SETTLEMENT_ACCOUNT_IS_A_TILL` | `received_in` is a cashier's cash account |
| `SETTLEMENT_COMMISSION_ACCOUNT_IS_A_TILL` | the commission account is a cashier's cash account |
| `SETTLEMENT_ADJUSTMENT_ACCOUNT_IS_A_TILL` | the adjustment account is a cashier's cash account |

**Nothing here may credit a till.** If `received_in` resolved to a cashier's cash account,
that money would enter a drawer nobody physically received it in. Both accounts are
checked against every `POS Profile.custom_cash_account` on the site.

`idempotency_key` makes a retry after a timeout return the original settlement with
`replayed: true`. Without it, a retry pays the partner's account down twice.

**Probe support:** `probe=1` returns `{available, code?, reason?}` immediately without
touching a document; the client hides the Record settlement button until it answers. If
probe support were skipped the client would treat an argument-shaped `TypeError` as
"deployed" and proceed, so it still works — probe just avoids one confusing failure.

Response:

```json
{
  "settlement": "DPS-2026-0001", "partner": "Talabat", "partner_name": "Talabat",
  "customer": "Talabat", "payment_entry": "PE-0001", "posting_date": "2026-09-30",
  "gross_amount": 100000, "commission_amount": 25000, "adjustment_amount": 0,
  "received_amount": 75000, "received_in": "Bank - X",
  "invoice_count": 2,
  "invoices": [{"sales_invoice": "...", "posting_date": "...",
                "partner_order_ref": "...", "gross_amount": 0, "commission_amount": 0}],
  "replayed": false
}
```

All of it inside the app's standard envelope (`{ok, data, meta, errors}`).

## 6. `get_partner_summary` — optional

```
get_partner_summary(partner, from_date=None, to_date=None)
```

Returns `partner`, `partner_label`, `customer`, `is_active`, `commission_type`,
`commission_rate`, `partner_commission_amount` (the partner's flat fee per order — *not*
`commission_amount`, which is this period's total), `from_date`, `to_date`, `order_count`, `gross_amount`, `commission_amount`, `net_amount`,
`outstanding_amount`, `unsettled_count`, `unsettled_amount`, `settled_count`, and
`last_settlement` (or `null`).

The client already computes every one of these by summing its own order list and falls
back to that automatically. What the fallback cannot do is see past the page it fetched —
it asks for 500 rows, and a busy partner eventually exceeds that, at which point every
figure on the detail page silently understates. This aggregates in SQL, so page size stops
mattering.

## 7a. "Bill the App Customer" (`is_inside`)

A partner flag, default off. **Off, nothing in this document changes.** On, a partner order
is billed to the **real customer the cashier picks** instead of the partner's own Customer —
while the partner still collects the money, keeps its commission and settles later.

| Where | What |
| --- | --- |
| `Delivery Partner.is_inside` | Check, label "Bill the App Customer". |
| `Sales Invoice.custom_partner_is_inside` | Read-only snapshot at sale time (`pet_app.patches.delivery_partner_inside`). The guard below reads *this*, never the live flag. |
| `Delivery Partner Settlement.journal_entry` | Link, set **instead of** `payment_entry` when a settlement covers orders billed to app customers. |

**At the till** (`create_pos_sale`): both `PARTNER_CUSTOMER_MISMATCH` checks are replaced by
`PARTNER_INSIDE_NEEDS_REAL_CUSTOMER`, which refuses a customer that is any partner's billing
Customer or the POS Profile's walk-in default — the debt has to sit on a person. Everything
else is unchanged: `Due` only, order reference required, commission stamped.

**At settlement**: a Payment Entry has one party, and ERPNext will not let it clear an invoice
billed to anyone else. So the posting document is chosen from the invoices' **actual**
customers — all on the partner's own Customer ⇒ the Payment Entry of §5, unchanged; any on
somebody else ⇒ one Journal Entry:

```
Dr Bank / Cash            received_amount
Dr Commission Expense     commission_amount
Dr Adjustment             adjustment_amount   (Cr if negative)
    Cr <debit_to>, party = <customer>, ref = Sales Invoice      one row per invoice
```

Each credit references its invoice, so the invoice closes and the Payment Ledger Entry that
customer balances read is written. Tie-out, idempotency, till-account refusals and the
one-transaction rollback are the same code as the Payment Entry path.

**The double-collection guard** (`refuse_partner_collected_invoices`): until the partner
settles, an inside order is an ordinary-looking debt on the customer's account, and the
customer must not be asked for it — the partner already took it. Refused with
`PARTNER_INVOICE_COLLECTED_BY_PARTNER` by `accounting.cashier.create_payment_entry_with_cashier_context`
and by `driver_orders._receive`. Only unsettled `custom_partner_is_inside = 1` invoices are
caught; ordinary partner orders are left exactly as they were.

> **Known effect:** the order shows as debt on that customer's balance and statement until
> the partner settles. That is what billing the customer while the partner holds the money
> means; the guard stops the till acting on it.

## 7. Permissions and release

Role permissions ship in the two doctype JSONs (§1, §4).

Separately, and as a **frontend release step**: the two page keys
`page.delivery_partners.list` and `page.delivery_partners.detail` must be synced and
granted from `/permission` before anyone but a full-access user can open the module.

## 8. Deployment

```
bench --site <site> migrate
sudo supervisorctl restart frappe-bench-web:frappe-bench-frappe-web
```

`kill -HUP` does not reload code here — gunicorn runs with `--preload`. Note also that
`bench console` imports fresh source from disk, so it cannot prove the running workers
reloaded; only an HTTP request through the workers can.
