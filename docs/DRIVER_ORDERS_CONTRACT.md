# POS orders, driver deliveries and van sales

Backend authority: `pet_app.api.driver_orders`. All amounts use the POS Profile's
company currency (IQD on the current site). The cashier operates this workflow.
Driver-app sale entry and route planning are outside this release.

## Business rules

- **Booked fee (patch `driver_fee_accounting`, once the fee accounts are set in
  Pet App Accounting Settings).** Goods 50,000 plus a 5,000 fee: the invoice is 55,000,
  with the fee as an Actual "Delivery Fee" charge row to the fee income account
  (708500). On earning, a Journal Entry (`custom_driver_journal_kind = Fee`) posts
  Dr fee expense (624200) / Cr the driver's own fee account ("Driver Fees HR-DRI-…",
  a liability under "Driver Fees Payable"). A COD driver collects the full 55,000 into his
  cash account, and settlement nets his cash against the fees owed to him.
  `Sales Invoice.custom_driver_fee_booked` holds the fee, and `custom_direct_delivery_fee` is 0.
  A return never credits the fee back. The snapshot adds `fee_account`,
  `fees_owed_to_driver` and `fees_paid_to_driver`.
- **Off-books fee (the old rule, while the fee accounts are unset).** Goods priced at 50,000
  plus a 5,000 direct delivery fee means the customer pays 55,000, the driver keeps 5,000
  and the shop receives 50,000. The direct fee never enters the shop's Sales Invoice,
  Payment Entry, revenue or driver cash balance.
- An order earns its agreed fee once, on its first successful delivery. Later delivery
  of the remainder earns no second fee. A complete refusal earns nothing. Each general
  van sale has its own fee. Customer/driver fee refunds are outside the shop's ledger.
- Dispatch moves stock, not money. Delivery creates an invoice. Collection records
  money actually received. Handover records money physically received by the cashier.
- A completed delivery can have an unpaid customer invoice. Customer debt is never
  presented as cash held by the driver.

## Setup and rollout

Schema patch: `pet_app.patches.driver_orders_schema`, registered in `patches.txt` and
`INSTALL_SCHEMA_PATCHES`. The patch owns its new fields; do not export them as fixtures.
Existing `Sales Order.custom_delivery_fee` remains fixture-owned and becomes editable
after submit only through the controlled assignment path.

The patch adds these settings to **Pet App Accounting Settings**:

| Field | Purpose |
|---|---|
| `custom_enable_driver_orders` | Disabled by default; enables the APIs |
| `custom_driver_warehouse_parent` | Company warehouse group containing driver warehouses |
| `custom_driver_cash_parent` | Company Asset account group for driver cash accounts |

On the current site, migration uses the existing empty transit warehouse and the
531000 cash group. It converts the transit warehouse to a group only if it has no
Stock Ledger history or nonzero stock. The existing driver's invalid Income account
is repointed only if it has no GL history. A driver/account with posted history needs
explicit review; migration refuses to silently change its meaning.

Each driver needs an enabled, distinct warehouse and Asset/Cash account in the
company currency. `setup_driver` provisions missing warehouse/account configuration
under the configured groups. Existing valid accounts are retained. When the warehouse
parent is configured, the existing Driver creation flow automatically creates a driver
warehouse and validates the supplied cash account. A driver with stock or cash cannot
change custody configuration; deactivate historical drivers
instead of deleting them.

The patch disables the conflicting `orders` Workflow, and its fixture also ships
disabled. The ten historical orders remain untouched: their custom status is
Cancelled while their ERPNext docstatus remains submitted.

Deployment order:

1. Review and deploy the changes, migrate a staging site, and run the tests below.
2. Migrate the intended production site during the normal deployment window. Review
   other pending patches in the working tree before running a full `bench migrate`.
3. Restart web and worker processes so hooks and the Sales Invoice extension reload.
4. Configure driver warehouses/accounts, POS assignments and native permissions.
5. Enable `custom_enable_driver_orders` after staging passes and the POS integration
   is ready. A code-only deployment does not enable the feature.

No new business DocType is introduced. Submitted Stock Entries and their child rows
are the stock custody ledger; Sales Invoices and Payment Entries own accounting.
Durable Comment records hold operation keys, request fingerprints and responses.
They cannot be edited/deleted through ordinary document operations, and are not
subject to Integration Request log expiry.

### Current site configuration (2026-09-09)

The driver schema patch is applied on `frappe.localhost`, the web and queue workers
have restarted, and `custom_enable_driver_orders` is enabled. An authenticated live
settlement request succeeded with zero cash owed and no loaded stock.

| Setting | Configured value |
|---|---|
| Driver | اوراس — `HR-DRI-2026-00021` |
| Driver warehouse | `Driver HR-DRI-2026-00021 - K` |
| Warehouse parent | `مخزون في الطريق - K` |
| Driver Cash account | `Driver Cash HR-DRI-2026-00021 - K` (Asset / Cash, IQD) |
| Cash account parent | `531000 - الصندوق الرئيسي - K` |

The old Income account had no GL history; only the Driver's link was replaced.
The conflicting Workflow is disabled and all ten historical orders are preserved.
The default direct delivery fee is still zero pending the owner's chosen amount;
send the agreed fee explicitly on each order in the meantime.

Cashier rollout still requires identifying the acting user, confirming their POS
Profile assignment, and granting the required native stock/driver permissions and
warehouse scope. Backend enablement does not install the four frontend POS views.

## Authorization and transactions

- Mutations are POST requests and require `idempotency_key` (1–140 characters).
  Mint one UUID per user action and reuse it with the **same payload** after a timeout.
- Keys are scoped to company and authenticated user. A changed payload returns
  `IDEMPOTENCY_CONFLICT`; a successful retry returns `replayed: true` and the original
  result. Read detail/settlement again for current balances after replay.
- Company-row locking serializes driver operations. ERPNext/Bin checks enforce stock
  availability. Failure rolls back the complete request, including documents and audit.
  Snapshot conflicts retry the full transaction up to three times; `DRIVER_BUSY_RETRY`
  asks the caller to retry with the same key if contention persists.
- **Orders are not tied to a cashier till.** `pos_profile` is optional on every read and
  order action (`list_pos_orders`, `get_order_detail`, `get_driver_settlement_snapshot`,
  `assign_driver_to_order`, `prepare_order`, `dispatch_order`, `record_delivery_result`,
  `close_order_remainder`, `record_customer_payment`, `return_sold_goods`,
  `refund_customer`, `setup_driver`). `load_van` / `return_unsold_stock` take
  `pos_profile` **or** `branch` (default: the user's branch). Without a till the gate is
  native doctype permission plus Branch scope: reads use the user's Branch User
  Permissions, writes must target a branch the user belongs to. The origin POS Profile
  still supplies currency, taxes and payment defaults. Sales Order `custom_pos_profile`,
  `custom_fulfillment_warehouse` and `custom_driver_cash_account` ignore User Permissions
  (patch `driver_orders_ignore_till_permissions`), so a till-restricted user sees every
  order in their branch. `get_order_detail` needs only Sales Order read; `invoices` and
  `settlement` are filled only for users who may read Sales Invoice, Payment Entry and
  Stock Entry (`settlement_visible`).
- Still till-bound: `create_pos_order`, `create_van_sale`, and any payment with
  `received_by: "shop"` (`DRIVER_TILL_REQUIRED` without one). Payment Entries posted
  without a till carry no `custom_pos_profile`, so they never count in a till summary.
- When `pos_profile` is passed, the previous rules apply unchanged: the caller must be a
  POS operator, have access to the selected POS Profile, branch
  and origin warehouse, and have native permissions for the documents used. Operations
  creating documents require their create/submit permissions; order actions also need
  Sales Order write; setup needs Driver write, Warehouse create and Account create.
  Payment operators also need Account read for ERPNext's account validation.
- Driver and source-document access is checked. Detail/settlement readers also need
  Sales Invoice, Payment Entry, Sales Order and Stock Entry read as applicable.
- Cash collection at the shop and handover require the acting user's assigned POS
  Profile (`applicable_for_users`). A supplied profile cannot redirect another user's till.
- No blanket Warehouse User Permissions are granted. Configure required document read
  access for staff using the delivery workflow; the APIs still enforce origin/profile scope.
- Raw edits, cancellation and deletion of managed stock/accounting documents are refused.
  Use explicit goods returns, refunds or order closure. Direct driver stock/cash postings
  through other document APIs are blocked while this workflow owns those accounts.

Every API uses the existing envelope:

```json
{"ok": true, "data": {"documents": [{"doctype": "Sales Invoice", "name": "..."}],
 "operation": "...", "replayed": false}, "meta": {}, "errors": []}
```

Failure: `ok: false`, `meta.code`, and `errors[].message`. Display the message to the
cashier. Never interpret a failed response as a partially successful sale.

## API reference

Prefix every method below with `pet_app.api.driver_orders.`.
All mutations additionally require `idempotency_key`.

| Method | Required business inputs | Optional inputs |
|---|---|---|
| `setup_driver` | `driver`, `pos_profile` | — |
| `create_pos_order` | `customer`, `pos_profile`, `items` | `driver`, `payment_arrangement`, `delivery_fee`, `delivery_date`, `delivery_latitude`, `delivery_longitude`, `discount_type`, `discount_value` |
| `assign_driver_to_order` | `order`, `driver`, `pos_profile` | `delivery_fee` |
| `prepare_order` | `order`, `pos_profile` | — |
| `dispatch_order` | `order`, `pos_profile` | `items` |
| `load_van` | `driver`, `pos_profile`, `items` | — |
| `record_delivery_result` | `order`, `pos_profile`, `accepted_items` | `remainder`, `payment` |
| `create_van_sale` | `driver`, `customer`, `pos_profile`, `items` | `delivery_fee`, `payment` |
| `return_unsold_stock` | `driver`, `pos_profile`, `items` | — |
| `close_order_remainder` | `order`, `pos_profile` | — |
| `record_customer_payment` | `reference_doctype`, `reference_name`, `pos_profile`, `payment` | — |
| `receive_driver_cash` | `driver`, `pos_profile`, `amount` | `cash_account` from the driver's custody history |
| `return_sold_goods` | `invoice`, `pos_profile`, `items` | — |
| `refund_customer` | `pos_profile`, `amount`, `payment`; exactly one of `return_invoice` / `advance_payment` | — |

Read methods:

- `list_pos_orders(pos_profile, state=None, driver=None, limit_start=0, page_length=30)`;
  maximum page size 100; returns `orders`.
- `get_order_detail(order, pos_profile)`; returns the Sales Order, invoice names and
  the driver's settlement when assigned.
- `get_driver_settlement_snapshot(driver, pos_profile)`; returns cash accounts/balances,
  available load rows, open orders, payments, unpaid invoices, customer receivables and
  credits, direct fees earned, today's driver cash receipts and handovers.

Stock/orders/invoice lists follow the selected branch. Driver cash balances and payment
history are company-wide because cash can be handed over at either authorized till.

### Order entry and dispatch

```json
{
  "customer": "CUSTOMER-ID",
  "pos_profile": "Alkokh Vet Store - Main Cashier",
  "driver": "HR-DRI-2026-00021",
  "items": [{"item_code": "ITEM-ID", "qty": 5, "rate": 10000, "uom": "Nos"}],
  "payment_arrangement": "Cash on Delivery",
  "delivery_fee": 5000,
  "idempotency_key": "a-unique-order-key"
}
```

Arrangements: `Cash on Delivery` (default), `Prepaid`, `On Account`, `Card on Delivery`,
`Wallet on Delivery`. Arrangement expresses intent; payment documents record actual
receipts. The cashier may change the actual collection method without inventing cash.

`create_pos_order` returns `order`, `items[].order_item`, goods total, direct fee and
customer total. Item rates are supplied by the cashier/POS; line amount, company,
currency and warehouse are calculated on the backend. Supported items are enabled
stock items, including concrete variants. Services and variant templates are refused.

Call `prepare_order`, then `dispatch_order`. Omitted dispatch `items` means all
unfulfilled quantities not already held by the driver. Explicit dispatch rows use
`{"order_item": "SO-CHILD-ROW", "qty": 2}` in the order row's UOM.

Driver and fee become frozen at first dispatch. Never change the Driver master to
redirect a shipment. The original branch, warehouse and cash account remain attached
to each submitted load and invoice, including later returns.

### Delivery results and general van sales

```json
{
  "order": "SAL-ORD-...",
  "pos_profile": "Alkokh Vet Store - Main Cashier",
  "accepted_items": [{"order_item": "SO-CHILD-ROW", "qty": 2}],
  "remainder": "keep_open",
  "payment": {"amount": 20000, "received_by": "driver", "mode_of_payment": "نقداً"},
  "idempotency_key": "a-unique-delivery-result-key"
}
```

`accepted_items: []` records a refusal. `remainder` is `keep_open` (default) or `close`.
Closing an unfulfilled remainder closes its ERPNext order commitment but leaves stock
with the driver until physical receipt. Never send payment on a refusal.

The authoritative operational field is `custom_delivery_state`: Draft, Preparing,
Out for Delivery, Partially Delivered, Returned, Completed, Cancelled. The legacy
`custom_order_status` mirrors compatible values; partial delivery maps to Out for
Delivery there. Use the new field in the POS. Use invoice outstanding amounts for debt,
not the old `custom_payment_status` field.

Order invoices preserve original prices, UOM conversions, discounts and tax rates.
Fixed header discounts and Actual taxes are prorated over accepted quantities.
The guarded invoice factory creates a new non-POS Sales Invoice for each delivery
result, using native `sales_order` / `so_detail` references.

For general van stock, call `load_van` with `{item_code, qty, uom?}` rows. Then call
`create_van_sale` with `{item_code, qty, rate, uom?}` rows. General sales can only use
unassigned loads from that branch; stock reserved for an order cannot be consumed.
Eligible load rows are consumed oldest first. A single invoice must use one warehouse
and cash-account snapshot.

### Customer payments and handovers

Payment object fields:

| Field | Meaning |
|---|---|
| `amount` | Actual goods payment; positive and no more than the goods balance |
| `received_by` | `driver`, `shop`, or `bank` |
| `mode_of_payment` | Existing Mode of Payment name; defaults from profile |
| `external_reference` | Required for bank/card/wallet receipts and refunds |

`driver` requires Cash and uses the document's driver cash snapshot. `shop` requires
Cash and uses the assigned cashier's till. `bank` requires a Bank-type Mode of Payment
with a configured company account. No arbitrary account override is accepted for
customer payments. The direct fee is excluded from `amount`.

An omitted `payment` creates an unpaid invoice. Partial receipts obey the selected
POS Profile's `allow_partial_payment`. Record later payments against the Sales Invoice.
Prepayments are recorded against an unbilled Sales Order and allocated to its invoices.

`receive_driver_cash` receives an explicit amount into the acting cashier's till.
Receiving less than the driver's balance leaves the remainder owed. It neither pays
customer invoices again nor deducts a driver fee.

### Goods returns and refunds

- Unsold physical receipt: `return_unsold_stock` rows are
  `{load_row, stock_qty}`; quantities are **stock UOM**, not sales UOM. Return to the
  original branch warehouse. For damaged stock, use the normal ERPNext transfer process
  after receiving it into the branch.
- Post-sale return: `return_sold_goods` rows are `{invoice_item, qty}` in the original
  invoice row's UOM. It creates a linked return invoice and receives goods into the
  original driver warehouse. A later `return_unsold_stock` records branch receipt.
- Refund against a return: `refund_customer(return_invoice=..., amount=..., payment=...)`.
  Refund cannot exceed the outstanding credit; the payment object selects the actual
  cash/bank source. Shop does not refund the driver's directly collected fee.
- Unused prepayment refund: close the order remainder, then use
  `refund_customer(advance_payment=..., amount=..., payment=...)`. The service releases
  only that receipt's unused order allocation, keeps invoice allocations intact,
  submits a Pay entry and reconciles it to the released receipt credit. Partial refunds
  and subsequent refunds are bounded by remaining Payment Ledger credit.

Stock-changing rows can include `batch_no` or newline-separated `serial_no`. ERPNext
validates and creates serial/batch bundles. For a serial/batch sale spanning multiple
loads, specify `load_row` and record each load separately. Do not send client-created
bundle IDs or client-computed conversion factors.

## POS screens

1. **Orders:** customer, items, goods price, driver, direct fee, customer total and payment
   arrangement. Provide Prepare and Dispatch actions. Present native permission errors.
2. **Dispatch / van loading:** separate customer-order allocations from general stock.
   Display quantities and UOM. Show available stock from the backend.
3. **Delivery results:** accepted quantities, actual goods payment, keep-open/close choice
   and original item-row IDs. Refused goods remain visibly with the driver.
4. **Driver settlement:** goods cash held, cash handed over, cash still owed, unsold
   stock, unresolved orders and customer debt as separate figures. Fee information is
   separate and must never reduce `cash_owed`.

Refresh detail/settlement after each successful action. `documents` supplies standard
ERPNext document links for audit/drill-down. Do not insert or submit business documents
directly from the frontend.

## Compatibility and errors

Legacy `pet_app.api.order.assign_driver_to_order` forwards managed orders when given
`pos_profile` and `idempotency_key`. Legacy `transition_order` forwards Preparing and
Cancelled for managed orders; other actions return `DRIVER_ACTION_REQUIRED` because
they need quantities or actual payment details. Old unmarked orders cannot restart the
Material Issue / cash-at-dispatch flow; create a new POS driver order. Existing ordinary
POS and clinical billing APIs retain their behavior.

Important codes: `DRIVER_ORDERS_DISABLED`, `IDEMPOTENCY_KEY_REQUIRED`,
`IDEMPOTENCY_CONFLICT`, `DRIVER_BRANCH_CONFIGURATION`, `DRIVER_BRANCH_MISMATCH`,
`DRIVER_CASH_ACCOUNT_INVALID`, `DRIVER_ORDER_CLOSED`, `INSUFFICIENT_STOCK`,
`DRIVER_STOCK_ALLOCATED`, `GOODS_PAYMENT_EXCEEDS_BALANCE`, `DRIVER_DOCUMENT_LOCKED`,
`LEGACY_DELIVERY_REQUIRES_POS_ORDER`, `DRIVER_STOCK_LEDGER_MISMATCH`,
`DRIVER_PAYMENT_LEDGER_MISMATCH`. Normal Frappe validation and permission errors also
use the standard envelope. `DRIVER_ORDER_INVALID` describes other actionable refusals.

## Verification

`pet_app.tests.test_driver_orders` runs against an isolated `test_driver_orders_*`
database with this site's configuration cloned and the schema patch applied. The
test module refuses production. It exercises actual Stock Ledger, GL and payment
allocation behavior; test transactions roll back.

Run existing `test_pos_sale_creation`, `test_branch_warehouse`,
`test_fixture_allowlist` and `test_install_schema_patches` on staging as regressions.

`pet_app.tests.driver_orders_concurrency.run()` is a separate staging-only probe.
It commits uniquely named fixtures and uses two independent database connections to
verify duplicate dispatch and competing delivery submissions. Its records remain in
the isolated test database for inspection. Never point this probe at production.

## One-step driver settlement (Driver Settlement, `DRS-.YYYY.-`)

    collected_amount == handed_over_amount + fee_amount + adjustment_amount

- `collected_amount`: cash the driver took on the covered orders (receipts into the
  order's driver-cash snapshot minus refunds he paid out of it, across the order, its
  invoices and returns). Always recomputed server-side.
- Only **Completed** orders delivered on/after `custom_driver_settlement_start` (set
  to the migration date) are offered. Older cash keeps using `receive_driver_cash` (Handover).
- Reads (`pos_profile` optional; without one the scope is the user's branches):
  `get_driver_period_summary`, `list_driver_settlement_orders` and `list_driver_settlements`
  (`driver` optional, for the frontend probe). Before migration they return
  `DRIVER_SETTLEMENT_NOT_INSTALLED`.
- `create_driver_settlement` needs `pos_profile` = the acting cashier's own till and
  Payment Entry create/submit (the Handover gate). It posts **one** Journal Entry
  (`custom_driver_journal_kind = Settlement`): Cr driver cash (collected), Dr driver fee
  account (fees), Dr shortage 658000 / Cr overage 758000 (adjustment), and Dr/Cr till cash
  (handed over; negative = the till pays the driver). It stamps
  `Sales Order.custom_driver_settlement`. Refusals: `SETTLEMENT_DOES_NOT_TIE_OUT`
  (the equation, a client collected figure that differs, fees above what is owed, or a
  missing adjustment reason), `ORDER_ALREADY_SETTLED`, and cash already handed over
  (collected above the driver-cash balance). `payment_entry` is always null.

## Driver self-screen (`pet_app.api.driver_self`)

Identity: `Driver.user == session user` (Active). There are no `driver` / `pos_profile` args.
`DRIVER_NOT_LINKED` otherwise. The Driver role gets `page.drivers.me` (patch
`driver_self_screen`) and holds no document permissions. The methods act for the driver
only on orders assigned to him.

- `get_my_profile`, `list_my_orders` (adds `address`, `phone` (only for open orders and
  when `custom_driver_sees_customer_phone` is set), `payment_arrangement`, `goods_total`,
  `delivery_fee` and `customer_total`), and `get_my_balance`
  (`net = fees_owed_to_driver - cash_held_for_shop`).
- `scan_deliver(order, idempotency_key)`: `ORDER_NOT_YOURS`, `DRIVER_ORDER_CLOSED`,
  `ORDER_NOT_OUT_FOR_DELIVERY`. Accepts every remaining line and closes the order. On Cash
  on Delivery it takes the invoice's full outstanding (goods + booked fee − advances) into
  the driver's cash, and the client sends no amount.

### Driver screen additions (2026-09-19)

- `list_my_orders(group, limit_start, page_length)`: `group` ∈ `on_road`
  (Out for Delivery + Partially Delivered), `returned`, `delivered` (Completed +
  Partially Delivered) or `all`. It returns `{orders, total, counts}`. `counts` comes on every page.
  Rows add `delivery_attempts [{reason, note, at}]`, `refusal_reason`, `refusal_note` and
  `driver_note`. `get_my_order(order)` returns the same row, or `ORDER_NOT_YOURS`.
- `scan_deliver(order, note?)` stores `note` as `Sales Order.custom_driver_note`.
- `report_refusal(order, reason, note?)`: `reason` ∈ refused, changed_mind, damaged or
  wrong_order. This is `record_delivery_result(accepted_items: [])` with the remainder kept open:
  no invoice, payment or fee, and the state becomes **Returned**. The goods stay with the
  driver until `return_unsold_stock`. The reason and note are stored in `custom_refusal_reason` /
  `custom_refusal_note`.
- `report_attempt(order, reason, note?)`: `reason` ∈ no_answer, not_home or wrong_address.
  Nothing moves. A row is added to the order's `custom_delivery_attempts` (child table
  Driver Delivery Attempt).
- `list_pos_orders` rows now also carry `refusal_reason`, `refusal_note`, `driver_note` and
  `delivery_attempts`, for the POS "On the road" card.

### Linking a driver to an existing User

- `create_driver(user=…)` links an existing User instead of creating one. Driver
  `user` can be changed by `PUT /api/resource/Driver/<name>`.
- The User must exist, be enabled, not be Administrator/Guest or hold System Manager,
  and not be linked to another driver (`DRIVER_USER_ALREADY_LINKED` names that driver).
- Users get their roles from Role Profiles on this site, so linking adds the driver
  profile (`custom_driver_role_profile`, default "السائق") and never touches the password,
  names or username.
- On re-link, the previous login loses the driver profile unless another driver uses it.
  A login this module created is also disabled; a linked staff account stays enabled.
- Deleting a driver no longer deletes or disables a linked staff account, only its
  driver access.

### Order actions

- `update_pos_order(order, customer, pos_profile, items, payment_arrangement, delivery_fee,
  delivery_date, discount_type, discount_value, …)`. It works only while the order is Draft or Preparing
  and nothing has been dispatched; otherwise it returns `DRIVER_ACTION_REQUIRED`. It is also refused while a prepayment
  sits on the order. A submitted Sales Order cannot change its customer or lines in
  place, so the order is **cancelled and amended**: `data.order` is the new name (e.g.
  `SAL-ORD-2026-00016-1`) and `data.replaced_order` is the old one. The driver is kept.
- `cancel_order(order, pos_profile?, idempotency_key)` works atomically in any state except Cancelled:
  1. It closes the remainder.
  2. It issues a credit note for every unreturned line of every invoice from the order.
  3. It sends every unit on the order's load rows back to the branch warehouse.
  4. It sets the state to Cancelled.

  It returns every created document plus `refund_due` / `refund_required`: credit-note
  balance plus any unused advance. The refund itself stays manual (`refund_customer`). An earned
  driver fee is not reversed, so a paid 20,000 + 5,000 order shows `refund_due` 20,000.
- **Answers to the runner's questions.**
  - The legacy `transition_order(order, "Cancelled")` forwards to
    `close_order_remainder`. That accepts every state except Completed/Cancelled and
    moves no stock.
  - `get_order_detail` now returns `load_rows` for the order (`load_row`, `item_code`,
    `loaded_qty`, `sold_qty`, `returned_qty`, `available_qty` …).
  - `return_sold_goods` puts goods back into the **driver's** warehouse and onto their
    **original load rows**, which carry the order (`custom_sales_order`). So their
    `available_qty` rises again, and `return_unsold_stock` on those rows takes them to the branch.

## No till required anywhere (2026-09-19, §7)

`pos_profile` is optional on every `driver_orders` read and mutation. When it is sent,
the previous till rules apply unchanged. Without it:

- **Scope.**
  - Order actions use the order's own branch.
  - `create_pos_order`, `create_van_sale`, `load_van` and `return_unsold_stock` take
    `branch`, defaulting to the user's branch. A user who covers several branches must
    send one (`DRIVER_BRANCH_REQUIRED`).
  - Reads use the user's permitted branches.
  - The only gate is native doctype permission plus branch scope; POS Profile assignment
    is not checked.
- **Defaults a till used to supply.**
  - Warehouse: the branch's warehouse.
  - Price list: Selling Settings' default.
  - Currency: the company currency.
  - Taxes: the company's default Sales Taxes template.
  - Partial payments: allowed.
  - Mode of payment: the one sent, else the site default cash mode.
- **Where money the shop receives lands** (`received_by: "shop"`, `receive_driver_cash`,
  and the handed-over line of `create_driver_settlement`):
  - With a till: the acting cashier's **till drawer**, when the mode is Cash (or none
    was sent). A non-cash mode posts to that mode's account.
  - Without a till: the chosen **Mode of Payment's company account** (e.g. Cash → `Cash - K`).

  *This differs from the ask on purpose.* With a till and a Cash mode, the ask would have
  posted to the mode account, so cash physically in a till would miss that till's count.
- **Driver cash.** `received_by: "driver"` always uses the driver's cash account.
  `received_by: "shop"` now also accepts non-cash modes, which require `external_reference`.
- **Handover.** `receive_driver_cash(driver, amount, mode_of_payment?, pos_profile?, branch?)`.
  It refuses to send cash into an **active** driver's own cash account.
- **Settlement.** `create_driver_settlement` requires `mode_of_payment`
  (`PAYMENT_MODE_REQUIRED`), and `pos_profile` is optional. Without a till, the settlement
  takes the orders' own branch, and all orders must share it.
- **Settleable orders** are Completed, Returned and Cancelled orders that were invoiced
  and delivered on or after the start date. Returned and Cancelled are included so cash
  the driver kept from a returned order, at least the fee, can still be settled. A refused
  order has no invoice and is never offered.
- The capability probe `list_pos_orders?page_length=1` without `pos_profile` answers normally.

## Driver menu (§8)

- `add_note(order, note)`: any state, his own order. Sets `driver_note`, replacing the last one.
- `report_return(order, reason, note?, refund_cash)`
  - `reason` is one of `damaged`, `wrong_order`, `changed_mind` or `other`.
  - The order must be Completed or Partially Delivered.
  - It issues a credit note for every sold line; the goods go back into the driver's warehouse.
  - The state becomes `Returned`.
  - It stores `return_reason` and `return_note` (patch `driver_return_fields`).
  - With `refund_cash=1` on a Cash-on-Delivery order, the driver pays the credit notes back
    from his cash account, so `cash_held_for_shop` drops.
  - **The delivery fee stands:** the trip was made, and a return never credits the fee,
    so the refund covers the goods only.
  - Other payment arrangements are refunded at the shop (`DRIVER_ORDER_INVALID` for
    `refund_cash`).
- `list_my_orders` / `get_my_order` rows now carry:
  - `phone` on every row: the order's contact mobile, else the customer's `mobile_no`.
    It is controlled by `custom_driver_sees_customer_phone`, on by default.
  - `delivery_latitude` and `delivery_longitude`.
  - `return_reason` and `return_note`.
- `list_pos_orders` rows also carry `return_reason` and `return_note`.

## Orders and settlement are not linked to any till (2026-09-19, supersedes the till rules above)

**Orders**
- Driver orders are seen and worked on using the user's **normal doctype permissions**:
  Sales Order and related doctype perms plus their User Permissions (e.g. Branch), as
  `get_list` / `has_permission` apply them.
- `pos_profile` never scopes, filters, authorizes or stamps an order, even when it is sent.
  `list_pos_orders` returns the same rows with or without it. `DRIVER_BRANCH_MISMATCH` and
  the POS-operator / till-assignment checks are gone.
- New orders, van sales and loads use the `branch` sent, else the user's branch. Only as a
  convenience, they fall back to the branch of a till the client has selected, then the
  site's only branch. Defaults: Selling Settings price list, company currency, company
  default taxes. Nothing gets `custom_pos_profile`.
- Patch `driver_orders_unlink_tills` clears the old till stamp from existing driver Sales
  Orders, Sales Invoices and Stock Entries.

**Money**
- `pos_profile` only names **the till the money goes to**. That can be **any enabled till**
  of the company; there is no assignment check.
  - A Cash mode lands in that till's cash account.
  - A non-cash mode lands in the mode's account.
  - The Payment Entry is stamped with that till only when money actually went through it.
- Without a till, money goes to the chosen Mode of Payment's company account.
- This applies to `received_by: "shop"` payments, refunds and `receive_driver_cash`.
- `create_driver_settlement` **requires** `pos_profile` (any enabled till) and
  `mode_of_payment`.
  - The orders only need to be the driver's and readable by the user, from any branch.
  - The settlement's branch is the till's branch.
- On existing driver Payment Entries, the patch keeps the till stamp only where the money
  went through that till's cash account.

## Split collection at the door and order money status (2026-09-21)

### `scan_deliver(order, note?, collection?, idempotency_key)`
- The customer may pay part, say "Due", or pay by card. The driver sends
  `collection = {cash_amount, card_amount, card_mode, card_reference?}`:
  - `cash_amount` goes into the driver's cash account. Only this counts toward his settlement.
  - `card_amount` goes into the `card_mode`'s company account (for example `بطاقة بنكية` →
    511500), never the driver's hand. `card_reference` is optional; blank means the order name.
  - Whatever is left stays **Due** on the invoice (customer receivable, not driver cash).
  - `{cash_amount: 0, card_amount: 0}` = all Due. Works for any payment arrangement.
- Omitting `collection` keeps the old behaviour: Cash on Delivery takes the full balance in cash.
- Response adds `collection: {cash, card, due}` and `outstanding_amount`.
- Refusals (`ok:false`, nothing posted): negative amounts, a card amount without a valid
  `card_mode` (`DRIVER_ORDER_INVALID`), cash + card above the invoice balance
  (`GOODS_PAYMENT_EXCEEDS_BALANCE`).
- `get_my_profile` returns `card_modes`: the modes the picker may offer.
- `report_return(refund_cash=1)` refunds from the driver's cash only up to the cash he took on that
  order. The card or Due part stays on the credit note for the shop to refund.

### Money status on every order row
`list_pos_orders`, `list_my_orders` and `get_my_order` rows, and `get_order_detail.money_status`, carry:

| Field | Meaning |
|---|---|
| `settlement_status` | `settled` (covered by a Driver Settlement), `pending` (delivered and invoiced on/after the settlement start, not settled yet), `not_applicable` (not delivered, refused, or from the Handover era) |
| `is_settled` / `driver_settlement` | bool / the `DRS-…` name |
| `payment_status` | `Paid`, `Partly Paid`, `Unpaid`, `Refund Due`, or `null` before an invoice |
| `invoiced_amount`, `outstanding_amount` | net across the order's invoices and returns |
| `cash_collected` | cash into the driver's hand (receipts − his refunds) |
| `other_collected` | card, bank, wallet, or paid at the shop |

The two sides are independent: a Due order can be `settled` (the driver handed over his cash and
fee) while the customer is still `Unpaid`.

`list_pos_orders(settled=1)` returns orders a settlement covered; `settled=0` returns invoiced,
settleable orders not yet covered (check each row's `settlement_status` for `pending`).

## Driver notifications (2026-09-21)

`utils.driver_orders.notify_driver` puts an Alert Notification Log on `Driver.user`. The bell
picks it up, and `notifications.push` mirrors it to OneSignal push. `link` is
`<push_frontend_base_url>/drivers/me`. It runs inside the operation, so a failed action
notifies nobody and a replay never notifies twice. A notification error never fails the action.

| Event | Method | `document_type` |
|---|---|---|
| Assigned | `create_pos_order` with a driver, `assign_driver_to_order` | Sales Order |
| Out for Delivery | `dispatch_order` (when the state changes to Out for Delivery) | Sales Order |
| Edited | `update_pos_order` (names old → new) | Sales Order (new name) |
| Cancelled | `cancel_order`; `close_order_remainder` when it ends Cancelled | Sales Order |
| Settled | `create_driver_settlement` (orders, handed over, fees, adjustment) | Driver Settlement |

Frontend: the bell routes `Sales Order` to `/order/details/…`, which a driver cannot open.
For driver users, open `link` (`/drivers/me`) instead.

## Driver screen: order lines and customer search (2026-09-22)

Both are reads. No schema change.

### `get_my_order(order)` → `data.order.items`
The Sales Order's own lines in `idx` order, in every delivery state, with the same
`ORDER_NOT_YOURS` check. An order with no lines returns `items: []`. `list_my_orders` rows do not carry `items`.

| Field | Meaning |
|---|---|
| `item_code`, `item_name`, `uom` | As on the order line |
| `qty` | Ordered, in `uom`. It never changes after delivery. |
| `rate`, `amount` | The line's price after any line discount, and `qty × rate`. A discount on the whole order is not spread over the lines, so on those orders the lines add up to more than `goods_total`. |
| `delivered_qty` | Delivered, net of returns: a Returned order reads 0 |

### `list_my_orders(group, limit_start, page_length, search?)`
- `search` matches, as a case-insensitive substring, any of: the order `name`, `customer`,
  `customer_name`, and the row's `phone`. The phone is matched only when the driver may see
  it (`custom_driver_sees_customer_phone`).
- Both sides are folded first: `أ إ آ ٱ` → `ا`, `ى` → `ي`, `ة` → `ه`; tatweel,
  diacritics and invisible direction marks (LRM/RLM) are dropped; Arabic-Indic digits
  become 0–9; runs of spaces become one. A client that checks the rows itself must fold the same way,
  or matches such as `احمد` → `أحمد` will look wrong to it.
- `group`, `state`, paging (max 100), `total` and `counts` all apply to the matches.
  `counts` is per group of matches.
- A blank `search` (or one that folds to nothing) is the unchanged query.
