# Cashier activity backend contract

Canonical backend contract for `/accounting/cashiers/dashboard`, verified on 2026-09-09.
The document cited by the original handoff was absent from this checkout; this contract
records the implemented API and the decisions needed to consume it accurately.

## Responses and access

Methods use the Frappe route `/api/method/pet_app.api.accounting.cashier.<method>`.
Frappe wraps the return value in `message`; the app returns
`{ "ok": true, "data": { ... }, "meta": {}, "errors": [] }`.
Read the payload from `message.data`. Errors have `ok: false` and details in `errors`.

Both report endpoints share the profile picker's scope: accounting access or POS Profile
read permission, otherwise an explicit POS Profile User assignment; POS Profile User
Permissions narrow the result. An explicitly requested profile uses the same authorization
gate. Profile names outside the requested, authorized scope are not exposed by sharing
warnings or expense attribution. Branch is a reporting filter; money remains company-wide,
as specified by the repository's branch-separation policy.

Dates are inclusive. Omitted dates default to one day (today); a reversed range is refused.
Optional `company` and `include_disabled=1` are supported. The picker restricts disabled
profile listing to readers with POS Profile read/accounting access. A specifically named,
authorized disabled profile remains available for historical reporting.

## `get_cashier_activity(from_date, to_date, profile=None)`

```json
{
  "cashiers": [{
    "profile_name": "Alkokh Vet Hotel - Cashier",
    "company": "Kokh-vet",
    "warehouse": "مخزن التينادور - K",
    "cash_account": "531102 - صندوق كاشير POS 2 - الفندق - K",
    "disabled": 0,
    "incoming": 1325000.0,
    "outgoing": 85000.0,
    "current_balance": 1240000.0,
    "expected_cash_on_hand": 1240000.0,
    "aggregation_basis": "cash_account",
    "is_settled": 0,
    "settled_at": null,
    "settlement_difference": null,
    "settlement_difference_type": null,
    "settlement": null,
    "settlement_count": 0,
    "settled_total": 0.0,
    "cash_account_warnings": []
  }],
  "from_date": "2026-09-01",
  "to_date": "2026-09-06",
  "total": 1
}
```

The numbers above illustrate the shape, not the site's actual balances.

| Field | Meaning |
| --- | --- |
| `incoming`, `outgoing` | All non-cancelled GL debits and credits to the cash account in the range, in company currency. |
| `current_balance` | Closing account balance through `to_date`, including activity before `from_date`. |
| `expected_cash_on_hand` | That same closing balance; it agrees with the settlement snapshot for `to_date`. Outgoing and treasury transfers are already deducted. |
| `settled_at`, `settlement_difference` | Posting date and difference of the latest submitted settlement **inside the range**, ordered by posting date, creation, then name. |
| `settlement_count`, `settled_total` | Count and sum of transfer amounts across all submitted settlements in the range. |
| `settlement` | Latest settlement record in the range, or null. |

The original prompt's example subtracted outgoing again from the closing balance.
This implementation avoids that double deduction. A settlement on an earlier day in a
multi-day range does not mean every day in that range is settled; use `settled_at`.
An unsettled difference is null, not zero. A zero difference is a recorded matched count.

The endpoint makes one HTTP call and batches profile metadata, payment defaults, ledger
aggregates, and settlement reads. It does not call detail or snapshot once per profile.
It returns range aggregates, not a daily time series.

`cash_account_warnings` may contain:

- `no_cash_account`: zero figures represent missing configuration.
- `shared_cash_account`: amounts describe the shared account. `other_profile_count`
  includes all other owners; `profiles` only includes owners inside the response scope.
  Do not add duplicate account balances across profile rows.
- `mode_of_payment_account_mismatch`: the company's cash payment default differs from
  the till account. Invoice paths that use that default need separate verification.

## `list_cashier_expenses(from_date, to_date, profile=None, branch=None)`

The payload has this shape (the nested `data` list preserves the existing list convention):

```json
{
  "data": [{
    "id": "Journal Entry:ACC-JV-2026-00004:expense-account:cash-account",
    "posting_date": "2026-09-03",
    "kind": "cash",
    "category": "clinic",
    "account": "expense-account",
    "amount": 10000.0,
    "voucher_type": "Journal Entry",
    "voucher_no": "ACC-JV-2026-00004",
    "source": "cashier_expense",
    "remarks": "Supplies",
    "exact": 1
  }],
  "total": 1,
  "totals": { "cash": 10000.0, "stock": 0.0 },
  "cash_total_complete": true,
  "unresolved_cash_vouchers": [],
  "from_date": "2026-09-03",
  "to_date": "2026-09-03",
  "profiles": ["Alkokh Vet Store - Main Cashier"]
}
```

Cash and stock have separate totals; there is no combined amount. Cash expenses are
already included in till outgoing and expected cash. Stock issues never debit the drawer.
Amounts are in company currency; filter `company` when reporting across companies with
different currencies.

### Cash: confirmed voucher amounts

The backend reads both sides of each candidate voucher, joins Account for `root_type`,
and nets debit/credit movements per account. It never parses the GL `against` string.
Rows are grouped by voucher, expense account, and funding cash account; IDs include the
cash account to remain unique when a voucher funds one expense from two tills.

- One net credit account: every net expense debit is attributable to that funder.
- Multiple net credit accounts, one net debit account: the till's net credit is exactly
  its contribution to that expense account.
- Multiple funders and multiple net debit accounts: category allocation is unknown.
  These vouchers are **not proportionally estimated** or included in `totals.cash`.
  They appear in `unresolved_cash_vouchers`, and `cash_total_complete` becomes false.

An unresolved record contains `id`, `posting_date`, `voucher_type`, `voucher_no`,
`cash_account`, `cash_outflow`, `branch`, and `reason: "ambiguous_expense_allocation"`.
`cash_outflow` is the actual drawer movement, **not an expense amount**. Show an incomplete
total with links to these vouchers for review; never add their outflows to expense totals.
This is an intentional correction to the prior workspace implementation's `exact: 0`
proportional allocation. Returned expense rows now all carry `exact: 1`.

`source` is `cashier_expense` for a Journal Entry stamped with `custom_pos_profile`, and
`ledger` otherwise. Stamped profiles outside the requested scope are excluded. Without
a stamp, attribution uses the cash account: `profile` is null when shared,
`shared_cash_account` is true, and `profile_candidates` contains only visible owners.

Additional fields include `category_label_en`, `category_label_ar`, `account_name`,
`cash_account`, `profile`, `profile_candidates`, `branch`, `cashier_user`, and `cost_center`.
An expense split across cost centers is aggregated with `cost_center: null`.

### Stock: submitted Material Issues

The query joins Stock Entry Detail to submitted Material Issue parents and filters dates
in SQL, without a 200-record cap. It groups child `amount` by voucher, `expense_account`,
and source warehouse. Categories resolve from the configured account mapping; an account
without a configured category returns null and keeps its account label. Unset/non-expense
accounts are retained with `root_type` so issued stock does not disappear silently.

Additional fields include `warehouse`, `company`, `root_type`, `stock_entry_type`,
`item_count`, `profile`, `profile_candidates`, and `shared_warehouse`.
Attribution is by warehouse. A warehouse shared by profiles cannot identify one cashier:
`profile` is null and `shared_warehouse` is true. Only visible profile names are returned.
Each stock group appears once per response even if several visible profiles share it.

An explicit `branch` filter applies to both kinds and excludes vouchers with a different
or missing branch, including voucher types without a branch field.

## Settlement snapshot and count

`get_cashier_settlement_snapshot` includes `is_settled`, `settled_at`,
`settlement_difference`, `settlement_difference_type`, `settlement`, `settlement_count`,
and `settled_total` for its exact `posting_date`, without a global history cap.
Draft and cancelled settlements are excluded. `last_settlement` remains the separate,
legacy last Payment Entry transfer, regardless of date.

`settle_cashier_to_treasury` exposes `counted_cash` and `transfer_amount` as named,
keyword-only parameters. Existing positional arguments and nested `data` remain supported.

```python
settle_cashier_to_treasury(
    pos_profile="Alkokh Vet Hotel - Cashier",
    posting_date="2026-09-09",
    counted_cash=880000,
    transfer_amount=800000,
)
```

`difference_amount = counted_cash - expected_cash` before the transfer. The endpoint
populates it and the settlement controller recomputes it during validation. No separate
count source is consulted. Omitting `counted_cash` retains the legacy fallback to transfer
amount; that is an assumed count. Send the actual physical count, including explicit zero,
to record a measured difference. The transfer amount need not equal the physical count.

## Verified site configuration: 2026-09-09

Read-only inspection of `frappe.localhost` found two enabled profiles with **distinct**
`custom_cash_account` values:

| Profile | Cash account prefix | Warehouse |
| --- | --- | --- |
| Alkokh Vet Store - Main Cashier | `531101` | المخزن الرئيسي - K |
| Alkokh Vet Hotel - Cashier | `531102` | مخزن التينادور - K |

The two tills do not share a cash account. Both use `نقداً`, whose company default is
Store's `531101`. However, `pet_app.api.pos` explicitly pins the selected payment account
after invoice save. All **89 submitted cash invoices stamped to Hotel** inspected today
use Hotel's `531102`. The earlier handoff's claim that Hotel sales necessarily post to
Store is therefore inaccurate for this path. Invoice paths using ERPNext defaults still
need care; the mismatch warning records the configuration risk, not a detected misposting.
No account mappings, submitted vouchers, or historical balances were changed here.

## Earlier handoffs

- `record_cashier_expense` already creates a submitted Journal Entry, validates the till
  account and expense account, and stamps the profile, category, cashier, and branch.
  Branch defaults from the category and goes through the existing branch write check.
- `pet_app.api.accounting.expenses.get_expense_categories` (also exported at
  `pet_app.api.accounting.get_expense_categories`) now includes `default_warehouse`.
  It uses the category branch, then an explicitly requested branch, through the existing
  `Branch.custom_wharehouse` mapping. With no branch/mapping it returns null. No additional
  category warehouse field or schema migration is required.
- `pet_app.api.accounting.payment_accounts.resolve_payment_accounts` already exists,
  with the same shortcut export at `pet_app.api.accounting.resolve_payment_accounts`.
- **Clinic draft stock is not one global yes/no answer.** Today 17 care services with
  submitted stock issues link to one draft with `update_stock=0`; 80 other drafts have
  `update_stock=1` and stock-item lines. Draft creation itself does not post stock, but
  a separate dispense/Material Issue may already have done so. Preserve the stored flag
  and use `pet_app.api.pos` settlement methods, which refuse counter stock on a non-stock
  draft with `STOCK_FLAG_CONFLICT`. Do not flip the flag or clone clinic lines into a
  second stock invoice. Legacy mixed visit bills can deliberately drop the header flag
  when one line was already issued; inspect source `stock_entry`/`stock_issued_qty` for
  those bills. This inspection does not certify every historical draft for stock relief.

## Validation

`pet_app/api/accounting/test_cashier_activity.py` exercises real Journal Entries and GL
movements, permission-scoped callers, settlements, and Stock Entry parent/child queries.
The history tests use more than 200 rows. Tests isolate their tills/accounts and roll back
all test data. The endpoints were also read against the configured site.

No metadata was changed for this work. Restart the web process when deploying Python
changes; a separate bench console process does not prove running workers have reloaded.
