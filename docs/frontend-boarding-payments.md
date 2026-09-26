# Boarding payments — frontend test guide

New backend endpoint plus two changed responses. Nothing on the frontend calls any of it
yet, so today the feature is invisible in the app.

**Before testing:** the backend needs `bench restart`. Without it you are hitting the old
code and `record_boarding_payment` returns 404.

---

## 1. What changed and why you care

A guardian used to be able to pay **once** — the deposit at check-in. A second or third
payment could not be recorded at all: the deposit path silently returned the *existing*
Payment Entry, so the money vanished from the stay. Staff worked around it with hand-made
Payment Entries that carried no link to the booking, so check-out never saw them. The stay
was invoiced in full, the till asked for the whole amount **again**, and the money already
taken sat on the customer as invisible credit.

Three screens are affected:

| Screen | What to add |
| --- | --- |
| Boarding room / booking card | **"Add payment"** button + a payments list |
| Check-out result | show `invoice_submitted`, `invoice_outstanding` |
| POS settle | show **`amount_due`**, not `grand_total` |

---

## 2. `record_boarding_payment`

```
POST /api/method/pet_app.api.healthcare.boarding.record_boarding_payment
```

| key | type | required | notes |
| --- | --- | --- | --- |
| `boarding_id` | string | yes | `boarding` also accepted |
| `amount` | number | yes | must be > 0 |
| `note` | string | no | becomes a comment on the Payment Entry |

> ### ⛔ Only a cashier may take boarding money
>
> The caller must hold a **POS Profile**. Anyone else is refused with *"… holds no cashier
> till, so boarding money cannot be received"*, and **nothing is posted**.
>
> This matters because ending a stay and taking money are different jobs. `Doctor` is in
> `BOARDING_WRITE_ROLES`, so a doctor can check a stay out — but if they could also collect
> its balance, the cash would sit in their hand while the ledger booked it into the
> cashier's drawer, and the cashier would count short at settlement for money they never
> touched.
>
> **Show the "Add payment" button only to users who hold a till.** For everyone else, show
> the amount owed and "send the guardian to a cashier". Today only `farah@app.com` and
> `maher@petapp.com` hold one.

**One endpoint, two behaviours** — the backend decides, you do not send a mode:

* **Before check-out** — no invoice exists yet, so the money is held as an advance on the
  customer. `total_paid` and `balance` move.
* **After check-out** — the invoice exists and is posted, so the payment is applied
  straight to it and `invoice_outstanding` drops.

### Response

```jsonc
{
  "ok": true,
  "data": {
    "success": true,
    "boarding_id": "BRD-00210",
    "payment_entry": "ACC-PAY-2026-00079",
    "amount": 35000,
    "account": "531102 - صندوق كاشير POS 2 - الفندق - K",   // which drawer took it
    "pos_profile": "Alkokh Vet Hotel - Cashier",            // null = unattributed, see below
    "total_paid": 85000,        // everything paid on this stay, to date
    "total_cost": 200000,
    "balance": 115000,          // total_cost - total_paid
    "sales_invoice": null,      // set only after check-out
    "invoice_outstanding": 0,   // meaningful only after check-out
    "payments": [ /* see below */ ],
    "boarding": { /* the full serialised stay, same shape as check_in_boarding */ }
  },
  "meta": {},
  "errors": []
}
```

`payments[]` is the full history for this stay — use it for the list under the button:

```jsonc
{
  "name": "ACC-PAY-2026-00089",
  "posting_date": "2026-09-03",
  "paid_amount": 50000.0,
  "unallocated_amount": 0.0,
  "paid_to": "531102 - صندوق كاشير POS 2 - الفندق - K",
  "mode_of_payment": "Cash",
  "owner": "Administrator",
  "remarks": "Boarding payment for Pet Boarding BRD-00210."
}
```

### Which number to show where

* **While the stay is open** → `total_paid` ("مدفوع") and `balance` ("متبقي").
* **After check-out** → `invoice_outstanding` is the truth. `balance` agrees with it, but
  the invoice is the accounting document, so prefer it.

### Errors

Every refusal comes back as `ok: false` with `meta.code = "ValidationError"` and a
ready-to-display message in `errors[0].message`.

> ⚠️ **The message contains HTML** (`<strong>…</strong>`). Render it as HTML or strip the
> tags — do not print it raw.

| Case | Message |
| --- | --- |
| **Caller holds no POS Profile** | **{user} holds no cashier till, so boarding money cannot be received. Send the guardian to a cashier…** |
| `amount` ≤ 0 | A payment amount greater than zero is required. |
| Cancelled stay | Cancelled boarding records cannot take a payment. |
| More than the bill | Only X is outstanding on Sales Invoice **…**. Do not collect more than the bill. |
| Invoice already paid | Sales Invoice **…** is already settled in full. |
| Invoice not submitted | Sales Invoice **…** is not submitted, so a payment cannot be applied to it. |

---

## 3. Changed: `check_out_boarding`

Same call, four new keys:

| key | meaning |
| --- | --- |
| `invoice_submitted` | `true` — the invoice is posted. It is now **always** posted at check-out. |
| `invoice_outstanding` | what the guardian still owes. `0` means fully paid. |
| `total_paid` | everything paid during the stay |
| `payments[]` | the same history array |
| **`warnings[]`** | **structured warnings — see below. Empty when nothing is owed.** |

### `warnings[]` — check-out is never blocked, it warns

An unpaid balance does **not** stop check-out: the animal is going home either way, and
holding the stay open for an accounting reason keeps a kennel occupied and the record wrong.
Instead the response carries:

```jsonc
{
  "code": "BOARDING_BALANCE_DUE",
  "severity": "warning",
  "amount": 100000,
  "sales_invoice": "ACC-SINV-2026-01979",
  "collect_via": "pet_app.api.healthcare.boarding.record_boarding_payment",
  "message": "100,000 is still owed on Sales Invoice … A cashier collects it with a boarding payment."
}
```

**Show this on the check-out result screen.** Whoever ends the stay is often not a cashier,
so without it the guardian walks out and nobody at the counter knows there is money to
collect. Render `warnings[]` generically — more codes will be added.

**The invoice is no longer a draft**, and it must **not** be sent to the POS settle screen.
If `invoice_outstanding > 0`, collect it with `record_boarding_payment`, not with
`settle_open_invoice`.

## 4. Changed: POS settle payload

Two additive keys on the receipt payload (per invoice and on the merged total):

| key | meaning |
| --- | --- |
| `grand_total` | what the work cost — unchanged |
| `total_advance` | already paid before now |
| **`amount_due`** | **what the till should actually collect** |

**Show `amount_due` as the amount to collect.** The old screen showed `grand_total`, which
is how a cashier collected 175,000 from a customer who owed 105,000.

`settle_open_invoice` now **refuses** any invoice carrying prepayments, with
`meta.code = "INVOICE_CARRIES_ADVANCES"`. If you see that code, the invoice belongs to a
boarding stay — route the user to the boarding payment flow instead.

---

## 5. Test script

Test against **`driver-orders.test.localhost`**, not production. It is a copy of the real
data with the same rooms, guardians and tills.

### A — the main case (instalments)

1. Reserve a hotel room for a **dog**, back-date check-in **7 days ago**.
2. Check in with a deposit of **50,000**.
3. Add a payment of **35,000**. → `total_paid: 85000`, `balance: 115000`, and a **new**
   `payment_entry` id (not the deposit's — that was the whole bug).
4. Add another **35,000**. → `total_paid: 120000`, `balance: 80000`.
5. Check out. → `invoice_submitted: true`, `invoice_outstanding: 80000`.
   The stay is 8 nights × 25,000 = **200,000**.
6. Add a payment of **80,000**. → `invoice_outstanding: 0`.

**Pass condition:** the guardian's balance is **zero** — not in credit, not in debt. Check
with `pet_app.api.accounting.balance.get_customer_balance`: `receivable`, `credit` and
`net_balance` should all net out, and no payment should have `unallocated_amount > 0`.

### B — refusals (each must show a readable message, not a crash)

| Do this | Expect |
| --- | --- |
| Amount `0` or `-5000` | refused |
| Pay 50,000 more than `invoice_outstanding` | refused, message names the real figure |
| Pay again after `invoice_outstanding: 0` | refused, "already settled in full" |
| Add a payment on a cancelled stay | refused |

### C — edges

| Case | Expect |
| --- | --- |
| Check out with **no payments at all** | invoice posted, `invoice_outstanding` = full value. The guardian genuinely owes it — this is correct, not a bug. |
| Deposit **larger** than the whole stay | `invoice_outstanding: 0`, remainder stays as customer credit |
| Two stays for the same guardian | **two separate invoices**. If both land on one invoice, that is a regression — report it. |

### D — the doctor (verified end-to-end)

The case the cash rule exists for. Sign in as a doctor holding **no** POS Profile
(`sara@gmail.com` on the test site):

| Step | Expect |
| --- | --- |
| Check in **with** a deposit | **Refused** — *"…holds no cashier till"*. The stay stays `Reserved`, no deposit, no Payment Entry. The operator simply checks in again without one. |
| Check in **without** a deposit | **Works.** The refusal is about the cash, not the doctor. |
| Check out with a balance owed | **Passes.** Invoice posted, `invoice_outstanding` > 0, and `warnings[]` carries `BOARDING_BALANCE_DUE`. |
| Collect the balance | **Refused.** The invoice stays outstanding. |
| Cashier collects it | **Works** — into the cashier's own till, `invoice_outstanding` → 0. |

The important one is row 1: a guard that half-checked-in the animal would be worse than no
guard. Verify the stay is still `Reserved` afterwards.

### E — the drawer

Check `account` and `pos_profile` on every response.

* A cashier with a POS Profile → **their own** till, and `pos_profile` names it.
* A user with **no** POS Profile → the stay's branch till (hotel → `531102`).
* `pos_profile: null` means the money went to the general treasury and will appear in **no**
  till settlement. Rare, and worth flagging on screen if you see it.

---

## 7. Fixed: the customer balance screen

`get_customer_balance` used to read `GL Entry`, which is the one table ERPNext does **not**
update when a prepayment is reconciled. A fully-settled invoice therefore showed up as an
open debt, and the same money was counted a second time as credit. One customer was shown:

```
Debts            30,000     <- owed nothing
Account        -350,000
2 open invoices             <- both paid in full
380,000 on account          <- 30,000 of it double-counted
```

It now reads `Payment Ledger Entry`, where ERPNext actually records which invoice a payment
settled. No ledger entry changed — only the reporting. On production this corrected three
customers (125,000 of debt that did not exist, four "open" invoices that were paid) and left
the other 76 untouched.

**Nothing to change on the frontend.** Same endpoint, same fields, same shapes — the numbers
are simply right now. Worth re-checking any screen that showed a phantom debt.

## 8. Known gap

The POS settle screen still does not show the customer's existing credit. The data is
available from `get_customer_balance` (`credit`, `net_balance`) but it is a **separate**
call — the settle payload does not include it. Wiring it in is not done.
