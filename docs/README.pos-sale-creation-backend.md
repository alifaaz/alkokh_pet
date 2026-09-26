# POS sale creation — backend contract

**Status:** implemented. The outage is fixed on the backend; the frontend must rewire
`CreatePosSale` to land it.

The guard stays closed. `sales_invoice_guard.before_insert` still refuses every direct
`frappe.client.insert` of a Sales Invoice, and that is deliberate — it is what stops the
"ten invoices in sixty-eight minutes" sprawl coming back. POS gets a named door instead.

## Why Option A was not possible

Option A was "exempt POS in the guard, key it on `pos_profile` and/or `is_pos`". Reading
the shipped client (recovered from the sourcemaps in `/home/alkokh/dist`), `CreatePosSale`
builds a **Due** sale as:

```ts
isPos:      !isDueSale        // false
posProfile: isDueSale ? undefined : payload.cashierProfileName   // undefined
paymentMode / cashAccount:   undefined
```

So a Due counter sale carries **no `is_pos` and no `pos_profile`**. There is no field for
a guard exemption to key on, and on the wire it is shape-identical to the direct-insert
path the guard exists to stop. A `pos_profile`-keyed exemption would have left every
credit sale broken while opening the guard for everything else.

---

## The method

```
pet_app.api.pos.create_pos_sale
```

Creates **one new Sales Invoice and submits it, in a single call and a single
transaction.** On any refusal the whole thing rolls back; there is never a stranded
draft. The old two-step insert-then-submit disappears.

### Request

Same vocabulary as `pet_app.api.pos.settle_open_invoice`, so the till has one payload
shape for both paths.

| Field | Required | Notes |
| --- | --- | --- |
| `customer` | ✅ | |
| `pos_profile` | ✅ | **Required on a Due sale too** — see below |
| `payment_status` | ✅ | `"Paid"` or `"Due"`. This, not `is_pos`, is the switch |
| `items` | ✅ | JSON array of `{item_code, item_name?, qty, rate, uom?, warehouse?}` |
| `company` | | defaults to Pet App Accounting Settings |
| `branch` | | usually omit — see Branch |
| `warehouse` | | defaults to the profile's warehouse |
| `paid_amount` | Paid only | what was **actually collected**; clamped to `[0, grand_total]` |
| `payment_mode` | | defaults to the profile's default Mode of Payment |
| `cash_account` | | defaults to the profile's `custom_cash_account` |
| `posting_date`, `due_date` | | default to today; `due_date` is floored at `posting_date` |
| `selling_price_list` | | defaults to the profile's |
| `discount_type` | | `"Percentage"` or `"Amount"`, applied on Grand Total |
| `discount_value` | | |
| `remarks` | | |
| `idempotency_key` | | strongly recommended — see Retries |

**Stop sending:** `is_pos`, `update_stock`, `currency`, `price_list_currency`, `amount`
per line, and the `branch` stamp from `applyBranchToDoc`. The server resolves all of
them. A per-line `amount` is ignored on purpose: a line is worth `qty × rate` as ERPNext
computes it, and a client-computed total must not decide what the customer is charged.

### `pos_profile` is now required on a Due sale

The client omits it today because ERPNext's POS fields do not apply to a `is_pos = 0`
invoice. But the till is still the till: the profile is where the branch, the warehouse,
the price list and the cashier attribution come from, and a Due sale that cannot say
which counter made it is not reportable. The server sets ERPNext's own `pos_profile`
field only for a Paid sale, and always stamps `custom_pos_profile` and
`custom_cashier_user`, so a Due sale stays attributable without pretending to be a POS
invoice.

### Response

Deliberately the same envelope `settle_open_invoice` returns, so one client handler
covers both:

```json
{
  "invoice": { ...full Sales Invoice doc... },
  "payment_entry_id": null,
  "receipt": { ...same shape as the settle receipt, one "counter" block... },
  "settled_existing_invoice": false,
  "created_invoice": "ACC-SINV-2026-01656",
  "item_count": 2,
  "replayed": false
}
```

### Retries and double-charging

Pass a stable `idempotency_key` per checkout attempt (a UUID minted when the cashier hits
Pay, reused across retries of that same sale). If a previous attempt already completed,
the call returns that invoice with `"replayed": true` instead of charging the customer
and moving the stock a second time. Without a key, a retry after a timeout **will**
create a second sale.

---

## Error codes

All arrive as `{"ok": false, "meta": {"code": ...}, "errors": [{"message": ...}]}`.
The `message` is written to be shown to a cashier as-is.

| Code | What the cashier should see / do |
| --- | --- |
| `CUSTOMER_REQUIRED`, `CUSTOMER_NOT_FOUND` | Pick a customer |
| `POS_PROFILE_REQUIRED`, `POS_PROFILE_NOT_FOUND`, `POS_PROFILE_DISABLED`, `POS_PROFILE_COMPANY_MISMATCH` | Till misconfigured — call a supervisor |
| `INVALID_PAYMENT_STATUS` | Client bug |
| `INVALID_ITEMS`, `NO_ITEMS` | Add something to the sale |
| `SALE_ITEM_INVALID`, `ITEM_NOT_FOUND` | Named line is bad; message names the item |
| `SALE_ITEM_WAREHOUSE_REQUIRED` | No warehouse for a stock item; till misconfigured |
| `INSUFFICIENT_STOCK` | **Expected in normal use.** Message names item, warehouse, qty left, qty asked. Show it and let them change the qty |
| `POS_PARTIAL_PAYMENT_NOT_ALLOWED` | This till requires payment in full; message carries both numbers |
| `DUE_SALE_CANNOT_CARRY_PAYMENT` | Client bug: money sent on a Due sale |
| `INVALID_DISCOUNT_TYPE` | Client bug |
| `PAYMENT_ACCOUNT_NOT_APPLIED` | Should never fire. Sale rolled back rather than credit the wrong drawer. Escalate |

`INSUFFICIENT_STOCK` is checked **before** anything is written, because Stock Settings has
`allow_negative_stock = 0` and ERPNext's own throw otherwise arrives as a stack trace
after the customer has already been asked to pay. ERPNext remains the authority; this is
a courtesy check, not a lock.

---

## Three things that changed behind the method

### 1. A Due sale can no longer land in a clinic draft

`invoice_reuse.get_or_create_open_invoice` keeps one open Draft per customer and appends
to it. `is_pos = 1` already opted out. A **Due** counter sale is `is_pos = 0`, so it
would have fallen into the reuse path and appended the counter's goods to a clinic draft
that nobody submits at the till — the goods sold, and never taken off the shelf.

`force_new=True` is now passed for both payment modes. The payment method is not what
decides whether a document is mergeable.

### 2. The Hotel till's cash now lands in the Hotel till

`SalesInvoice.before_save()` calls `set_account_for_mode_of_payment()`, which
unconditionally rewrites every payments row to the Mode of Payment's **company** default.
Both profiles default to `نقداً`, whose company default is `531101 — صندوق كاشير POS 1 -
المحل` (the **Store**). So the Hotel till's takings were being credited to the Store's
drawer, silently, and would reconcile to a plausible balance in the wrong branch.

Fixed by ordering: Frappe runs `before_save` only for `_action == "save"`, and the submit
transition runs `before_validate`/`validate`/`before_submit` instead. The till's account
is re-asserted between the last draft save and `submit()`, and a post-submit assertion
(`PAYMENT_ACCOUNT_NOT_APPLIED`) rolls the sale back rather than post to the wrong account
if a future version ever moves that hook. `settle_open_invoice` had the same defect and
got the same fix.

Verified end to end: a Hotel-till sale now produces `GL Entry` debit
`531102 — صندوق كاشير POS 2 - الفندق`.

### 3. Branch comes from the POS Profile

`POS Profile.branch` is now set (`hotel` / `main`) and is the source of truth. A till is
one counter in one clinic; the revenue and the stock movement belong to that clinic
regardless of which cashier is standing at it — the same reasoning `settle_open_invoice`
already applies when it refuses to move a clinic draft's branch to the cashier's.

Because the branch comes from a site setting rather than the request, it is written with
`BRANCH_AUTHORISED_FLAG`, so a cashier restricted to one clinic can cover a shift at the
other till. A branch supplied by the **client** is never authorised that way — it still
goes through `assert_can_write_to_branch`. **The client should stop sending `branch`
for Sales Invoice**; `applyBranchToDoc` is insert-only and no longer has an insert to
hook, and the server no longer needs it.

---

## Partial payments

Both POS Profiles had `allow_partial_payment = 0`, which contradicted the brief. On the
product owner's decision they are now **`1`** — part payment is real, and the remainder
becomes the customer's receivable.

Note ERPNext's own `validate_full_payment` only fires for `is_created_using_pos`, which
this flow deliberately does not set (it demands a POS Opening Entry, and `POS Settings`
would reject it). So the profile's setting is enforced by `create_pos_sale` or nowhere.
Flip a profile back to `0` and that till refuses part payments again, loudly.

## An invoice carrying prepayments is refused at the till

`settle_open_invoice` / `settle_open_invoices` refuse any draft whose `total_advance` is
above zero, with `INVOICE_CARRIES_ADVANCES`.

**Why refusing, and not just netting the amount.** The first attempt here made `_settle`
collect `grand_total - total_advance` and assumed ERPNext would reconcile the advances on
submit. It does not. ERPNext skips `update_against_document_in_jv()` outright when `is_pos`
is set:

```python
# erpnext/accounts/doctype/sales_invoice/sales_invoice.py
if cint(self.is_pos) != 1 and not self.is_return:
    self.update_against_document_in_jv()
```

So a POS invoice never turns its `advances` rows into Payment Entry References, and
`update_outstanding_amt` then rebuilds outstanding from GL entries that were never written.

Measured on the test site before the guard: a 200,000 stay carrying 120,000 of allocated
advances, settled at the till for the correct 80,000, **submitted with outstanding 120,000
and all three deposits still `unallocated`**. The customer had paid in full and still owed
on paper - charged once, owing twice. An assertion on `outstanding` alone does not catch
this; the Payment Entry References do.

**Where those invoices go instead.** Boarding is the only writer of `advances` in this app,
and `check_out_boarding` now submits its own invoice as a NON-POS document
(`healthcare/boarding.py::_submit_checkout_invoice`), which is what consumes the advances.
The balance is then collected by `record_boarding_payment`, which writes a Payment Entry
referencing that invoice. Nothing carrying advances should reach the till at all; a draft
that does is a check-out whose submit failed, and it is paid as an ordinary invoice.

### `amount_due` on the receipt

`pos.py::_amount_due(doc)` returns `(rounded_total or grand_total) - total_advance` and is
used for the settlement arithmetic and exported on the receipt payload:

| Field | Meaning |
| --- | --- |
| `grand_total` | what the work cost - unchanged meaning |
| `total_advance` | already paid before this moment |
| `amount_due` | **what the till should collect now** |

Both new fields are additive; no existing key changed. **Clients should show `amount_due`
as the amount to collect** - the old screen showed `grand_total`, and that is the number a
cashier typed on a stay that was already part-paid.

It **must** be read after `doc.save()`: `total_advance` is recomputed by
`calculate_taxes_and_totals` from the advances table and is stale before it.

`create_pos_sale` deliberately does not use it - that endpoint always builds a fresh
invoice (`force_new=True`), so no advance can exist on it.

---

## The manual invoice page

`/accounting/sales-invoices/create` — **keep it, do not hide it.** Route it at:

```
pet_app.api.sales.create_manual_sales_invoice
```

Creates a **Draft** and never submits, matching what the page does today. Gated on
`System Manager` / `Accounts Manager` / `Accounts User` / `Accounting` — deliberately
narrower than `accounting.cashier._is_accounting_user`, which also admits the `POS Page`
and `Cashiers Page` roles every till cashier holds. A cashier's authority is to ring up
what is in front of them, not to raise an arbitrary invoice against any customer.

Request: `customer`, `items[] = {item_code, qty, rate?, description?, warehouse?}`,
`company?`, `branch?`, `posting_date?`, `due_date?`, `selling_price_list?`, `warehouse?`,
`update_stock?`, `remarks?`, `allow_duplicate?`.

Response: `{sales_invoice, customer, branch, docstatus, grand_total}`.

Extra codes:

| Code | Meaning |
| --- | --- |
| `MANUAL_INVOICE_NOT_PERMITTED` | Not an accounting user. Hide the action for these users rather than letting them hit this |
| `CUSTOMER_HAS_OPEN_DRAFT` | The customer already holds an open draft. `details.open_invoice` names it |

`CUSTOMER_HAS_OPEN_DRAFT` is a refusal, not a warning, and it is the point of the door:
silently creating a second draft beside an existing one is exactly the sprawl the guard
exists to prevent, and only the accountant can say whether these charges belong on the
existing document. Offer them "open ACC-SINV-…" or "create a separate invoice anyway",
and send `allow_duplicate=1` for the second.
