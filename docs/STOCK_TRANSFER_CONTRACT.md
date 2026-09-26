# Stock Transfer workflow — backend contract v1

Status: **implemented; configured and enabled for Kokh-vet on `frappe.localhost`; frontend setup-screen implementation and UI acceptance pending**.

This file is the canonical backend contract. `pet_app.api.stock_transfer` implements its methods. The supplied frontend handoff is the basis for the JSON shapes below; the frontend source and rendered UI are not part of this checkout. The isolated test commands and implementation decisions are recorded in section 6. The initial implementation was tested only on the isolated site. The subsequent targeted schema installation on `frappe.localhost` is recorded in section 7; the later configuration and activation are recorded in section 8.

## 1. Scope and supplied frontend evidence

| Capability | Supplied frontend evidence | Backend contract |
| --- | --- | --- |
| Stock documents | `src/api/stockEntryApi.ts`: Stock Entry CRUD, draft save and submit; Material Transfer already supported | Preserve existing behavior. Generate linked stock documents atomically from the new workflow |
| Lifecycle | Stock Entry `docstatus` maps to Draft / Submitted / Cancelled | Add `Stock Transfer Order` with Ordered / Preparing / Transferring / Received / Cancelled, independently of stock document status |
| Warehouses and items | Warehouse/Bin reads, authenticated images, item UOM lookup | Permission-scoped eligible warehouse/item methods and reliable nullable stock snapshots |
| Preparation/reservations | No transfer reservation contract found | Persist preparation, enforce reservations across every stock-consuming operation |
| Dispatch/receipt | No staged transfer contract found | Source → transit at dispatch; transit → destination/quarantine at receipt |
| Discrepancies | No transfer discrepancy contract found | Manager-approved reduction and loss settlement; append-only audit and stock links |
| Batch/serial | Existing editor is read-only pending bundle semantics | Backend-owned allocations and bundles. ERPNext Serial and Batch Bundles are created by this backend; allocation IDs come from the methods below |
| Labels/shipment | QR/barcode and printing primitives exist | Store shipment details; provide public application base URL for authenticated detail links |

The consuming frontend reports `src/type/stockTransfer.ts` as its TypeScript contract; backend wire behavior is owned here. Snake-case JSON below is intentional. No generic resource writes or client-simulated stock transitions are used by the new module.

## 2. Model, units and transitions

Create a dedicated `Stock Transfer Order` plus child lines, reservations, immutable shipment/receipt allocation records, and append-only events. One order has one dispatch and any number of partial receipts. No cross-company transfers or automatic migration of historical Material Transfers.

Persist original `requested_qty`, manager-adjustable `approved_qty`, `prepared_qty`, `dispatched_qty`, cumulative `accepted_qty`, `damaged_qty`, `lost_qty`, and derived `outstanding_qty` for every stable `line_id`. All these quantities are in that line's **transaction UOM**. `conversion_factor` converts to `stock_uom`; allocation `stock_qty` and stock snapshots are always in **stock UOM**. Server validates the conversion against current item configuration; do not trust a client factor. Preserve batch and serial identity throughout all movements. Enforce item precision and whole-number serial quantities.

`outstanding_qty = dispatched_qty - accepted_qty - damaged_qty - lost_qty`.

| Operation | State before → after | Stock effect |
| --- | --- | --- |
| Create | none → Ordered | None; approved initially equals requested |
| Update | Ordered → Ordered | None; editable only before preparation |
| Start preparation | Ordered → Preparing | None |
| Save preparation | Preparing → Preparing | Atomically reserve the complete new picked snapshot, release reduced reservations; never increment from the previous snapshot |
| Approve reduction | Preparing → Preparing | Manager reduces approved quantities; release excess reservations, trim allocations/prepared quantities to fit, set `ready=false`, retain original request and reason |
| Dispatch | Preparing, ready → Transferring | Submit one Material Transfer source → configured transit warehouse using prepared allocations; consume reservations in the same transaction |
| Update shipment | Transferring → Transferring | None; preserve recorded departure time |
| Record arrival | Transferring → Transferring | None; set authenticated arrival actor/server time once |
| Receive | Transferring → Transferring or Received | Submit accepted quantities transit → target and damaged quantities transit → quarantine; remaining quantities stay in transit |
| Resolve loss | Transferring → Transferring or Received | Manager posts Material Issue from transit to configured loss expense account |
| Cancel | Ordered/Preparing → Cancelled | Release reservations; require a reason |

Received means all dispatched quantities are accounted for, including manager-approved losses and quarantined damage. It does **not** imply every item arrived undamaged. Retain `has_exceptions=true` for reductions, damage, or loss. Set `received_at`/`received_by` to the actor/time of final settlement; individual receipt actors/timestamps remain in history. Never set Received while any outstanding quantity remains. Disallow zero-total orders/dispatches, negative quantities, over-receipts, oversettlement, same source/target, disabled/group warehouses, and warehouses outside the company/scope.

Preparation can be saved partially. `ready=true` requires every approved quantity to be prepared and allocated. A manager may reduce an order to the picked quantities, but remaining demand requires a separately created transfer; do not create it automatically. A post-dispatch return/reversal workflow is outside v1; cancellation is refused after dispatch.

Reservations must participate in all consumption paths (sales/POS, medication, Material Issues, other transfers). Lock stock/reservation rows in consistent order and revalidate availability at dispatch. Two transfer requests cannot reserve or dispatch the same serial or last available stock. An informational Bin `reserved_qty` alone is not proof that this requirement is implemented.

## 3. API contract

Namespace: `/api/method/pet_app.api.stock_transfer.<method>`.

Use GET for reads and POST JSON for mutations, authenticated using the shared application session. All replies use the standard response envelope:

```json
{"message":{"ok":true,"data":{},"meta":{},"errors":[]}}
```

For errors, return `ok:false`, a readable product-neutral message in `errors`, and a stable code in `meta.code`. The shared interceptor rejects errors and unwraps successful envelopes. Never return a successful empty object after a mutation.

### Reads

| Method | Arguments | `data` |
| --- | --- | --- |
| `get_capabilities` | none | `TransferCapabilities` below |
| `list_transfers` | `search`, `status` (empty or one of five states), `warehouse` (source OR target), `offset`, `limit` (UI 20; max 100) | `{items: TransferSummary[], total, counts: {Ordered,Preparing,Transferring,Received}}` |
| `get_transfer` | `transfer_id` | Full `TransferDetail` |
| `search_items` | `source_warehouse`, `search`, `limit` (UI 30) | `{items: TransferItemOption[]}` |
| `get_allocations` | `transfer_id`, `line_id`, `phase: "prepare" or "receive"` | `{items: TransferAllocation[]}` |

List search covers ID and notes. Apply authorization before pagination/counting. `total` respects all filters. The four pipeline counts respect search and warehouse but **exclude the selected status filter**, describing the complete matching pipeline; cancelled rows have no main pipeline tile. Order by modified descending then ID; never count only the current page.

```json
{
  "enabled": true,
  "contract_version": 1,
  "actions": ["create_transfer","update_transfer","start_preparation","save_preparation","approve_reduction","dispatch_transfer","update_shipment","record_arrival","receive_transfer","resolve_exception","cancel_transfer"],
  "current_user": {"id":"staff@example.com","label":"Warehouse staff"},
  "requesters": [{"id":"staff@example.com","label":"Warehouse staff"}],
  "warehouses": [
    {"id":"Main - KV","label":"Main warehouse","company":"Kokh-vet"},
    {"id":"Clinic - KV","label":"Clinic warehouse","company":"Kokh-vet"}
  ],
  "quarantine_warehouses": [{"id":"Quarantine - KV","label":"Quarantine","company":"Kokh-vet"}],
  "public_app_url":"https://inventory.example.com/"
}
```

Capabilities `actions` includes only operations this user can potentially perform, not just deployed method names. Detail `allowed_actions` further narrows by document, warehouse scope and current state. Omitted permissions never grant access. Include `current_user` in `requesters`; add other requesters only if the user may request on their behalf. Warehouse choices are eligible regular warehouses; do not expose transit/quarantine as regular source/target choices. Restrict quarantine choices to authorized configured warehouses. `enabled=false` disables mutation UI without hiding readable historical transfers. Any absent action also disables that action.

`public_app_url` is the frontend base URL, HTTP(S), with no credentials or query. A hash-router base ending `/#/` is supported. It must never be a backend URL or a `file:` Electron URL. QR encodes `<base>/warehouse/stock-transfers/<encoded-id>`. Scanning requires normal authentication and record authorization; a label does not grant access.

An item option contains `{id,label,image,stock_uom,current_stock,reserved_stock,has_batch,has_serial}`. Return actual available stock snapshots or `null` when unknown; never substitute zero for an unavailable query. Eligible items are enabled stock items permitted in the selected warehouse. Existing shared `GetItemUomOptions` loads valid units/conversions; authoritative mutation validation still belongs here.

Allocation options contain `{id,label,batch_no,serial_no,expiry_date,available_stock_qty}`. IDs are opaque server-issued references. Preparation availability includes this order's current reservations so saved allocations remain selectable. Receive options represent this shipment's outstanding allocations, not unrelated stock sharing the transit warehouse. Server checks aggregate allocations across good, damaged and loss quantities; selecting the same allocation in two categories must not exceed its outstanding stock quantity. Non-batch/non-serial items can use empty allocation arrays. Require exact allocation totals for managed items; expiry is read-only.

### Mutation envelope and bodies

Every mutation accepts:

```json
{
  "transfer_id":"ST-2026-00028",
  "expected_version":"opaque-revision-7",
  "idempotency_key":"client-generated-uuid",
  "payload":{}
}
```

Create omits `transfer_id` and `expected_version`. Every other action requires both. Scope keys to authenticated user + method; check replay **before** rejecting a now-stale expected version. Same key and payload returns the original full result without reposting; different payload returns `IDEMPOTENCY_CONFLICT`. Maintain durable idempotency records, including original result and affected documents. `expected_version` is an opaque monotonic revision, advanced for every successful change.

| Method | `payload` |
| --- | --- |
| `create_transfer`, `update_transfer` | Order example below |
| `start_preparation`, `record_arrival` | `{}` |
| `save_preparation` | `{ready:boolean,items:[{line_id,prepared_qty,allocations:[{allocation_id,stock_qty}]}]}`; complete line snapshot |
| `approve_reduction` | `{reason,items:[{line_id,approved_qty}]}`; complete line snapshot |
| `dispatch_transfer`, `update_shipment` | `{driver_name,driver_contact,vehicle,packages:number or null,seal,departure_time,expected_arrival}`; full shipment snapshot |
| `receive_transfer` | Receipt example below; quantities are **deltas**, omit untouched lines |
| `resolve_exception` | `{reason,items:[{line_id,lost_qty,allocations:[{allocation_id,stock_qty}]}]}`; loss deltas, omit untouched lines |
| `cancel_transfer` | `{reason}` |

```json
{
  "source_warehouse":"Main - KV",
  "target_warehouse":"Clinic - KV",
  "requester":"staff@example.com",
  "requested_date":"2026-09-09",
  "priority":"Normal",
  "notes":"Monthly clinic supplies",
  "items":[{"line_id":"client-line-uuid","item_code":"SYRINGE","uom":"Box","conversion_factor":12,"requested_qty":2}]
}
```

Persist client-generated line IDs for new rows, preserve IDs on edits, and reject duplicate or foreign line IDs. Requested quantities can be edited only while Ordered, so a line is removed by omitting it from `update_transfer` while Ordered, and thereafter only by `approve_reduction` to `approved_qty: 0`, which keeps the row for audit. Total approved demand must stay positive, so the last remaining line cannot be zeroed; cancel the order instead. Do not drop unmodelled stock document fields or rewrite UOM when creating underlying documents.

Example partial receipt for 24 dispatched stock units (2 Box):

```json
{
  "quarantine_warehouse":"Quarantine - KV",
  "items":[{
    "line_id":"client-line-uuid",
    "accepted_qty":1,
    "damaged_qty":0.5,
    "notes":"Six syringes damaged in transit",
    "accepted_allocations":[{"allocation_id":"shipment-allocation-1","stock_qty":12}],
    "damaged_allocations":[{"allocation_id":"shipment-allocation-1","stock_qty":6}]
  }]
}
```

This leaves 6 stock units (0.5 Box) in transit. If item UOM precision disallows fractional boxes, reject and have the operator use an allowed UOM from order creation; do not silently round. Damaged goods require notes and a permitted quarantine warehouse. Loss settlement requires a manager reason and the configured expense account, not a client-supplied account.

Shipment driver name and departure time are required for dispatch. Contact, vehicle and seal are optional strings; packages is a nonnegative integer or null; ETA is optional but cannot precede departure. Times are ISO-8601 instants with offset/`Z`. Server supplies all audit/receipt/arrival times and actors. `update_shipment` must preserve dispatch departure time. No driver-app integration or driver master creation is implied by these manual fields.

### Full detail response

Every mutation and `get_transfer` returns `TransferDetail` directly as `data`:

- Identity: `id`, `version`, `status`, `company`, source/target, requester, requested date, priority, notes, `item_count`, `has_exceptions`, `ready`, `allowed_actions`.
- `items`: all order fields and cumulative quantities above, `item_name`, `image`, `stock_uom`, nullable `current_stock`/`reserved_stock`, `requires_allocation`, saved preparation `allocations`, readable `allocation_details`, and `issues`.
- `issues`: `[{code,message}]` per line, empty when the line is fine. Each entry mirrors one refusal the preparation path would raise, so a line with no blocking issue is one Ready accepts: `INSUFFICIENT_STOCK` (the order's demand for that item exceeds free source stock), `ITEM_UNAVAILABLE`, `UOM_INVALID`, `INVALID_ALLOCATION`, and the non-blocking `NOT_PREPARED`, which is emitted only while the order is `Preparing` — unfinished picking is not a finding before picking starts or after dispatch. Stock issues are measured against **approved** demand, not what is prepared so far, because approved demand is what Ready requires. Omit stock-derived issues when `current_stock` is null: out of scope is unknown, not short.
- `shipment`: complete shipment object or null; `arrived_at`, `received_at`, `received_by`: nullable.
- `documents`: `[{id,kind}]`, where kind is `dispatch|receipt|damage|loss`, and ID links to a Stock Entry.
- `history`: oldest-first `[{id,action,actor,timestamp,note}]`, action one of the mutation names. Include quantity/allocation changes in readable notes and retain structured server audit data for reconciliation.

Use empty arrays, not missing arrays. Stock quantities must be numbers; unknown stock snapshots are null. Do not omit required cumulative quantities. A malformed successful mutation response is treated as uncertain by the frontend; it retains the original key for retry.

## 4. Atomicity, permissions, errors and setup

One mutation transaction covers workflow, reservations, batch bundles, stock documents, ledger effects, audit and idempotency outcome. Roll back all of them on a definitive refusal. Do not split receipt accepted/damaged posting into independently successful transactions. Use consistent locking across order and stock rows. Double dispatch/receipt/loss must never post twice, including transport retry or multiple tabs.

Reject manual mutation, submit/cancel, delete or amendment of **workflow-owned** stock documents through existing generic APIs; direct readers and print remain allowed. A frontend hiding buttons is not a security control. Keep unrelated existing Stock Entries fully compatible.

Suggested roles: stock staff create/read; source-authorized staff prepare/dispatch; target-authorized staff receive; managers approve reductions, settle losses, or cancel. Enforce at the API for every request and return `allowed_actions`; do not trust role names or requester IDs from the browser. Register `Stock Transfer Order` in the existing access snapshot/backend permission registry so the new frontend page key `page.warehouse.stock_transfers` works with authoritative permissions.

Refused **mutations** are recorded to the Error Log against the order (`reference_doctype`/`reference_name`), carrying action, code, message, actor, expected version, idempotency key and a truncated payload. A refusal returns `ok:false` rather than raising, so nothing else records it and a stuck operator otherwise leaves no trace. Reads are excluded as chatty. This satisfies the “monitor refused transitions” requirement in section 5.

Refusals name every affected line or item at once, not just the first: a shortage across a long order must not have to be discovered one item per attempt.

Stable errors: `STOCK_TRANSFER_DISABLED`, `PERMISSION_DENIED`, `NOT_FOUND`, `INVALID_STATE`, `VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `VALIDATION_ERROR`, `INSUFFICIENT_STOCK`, `RESERVATION_CONFLICT`, `INVALID_ALLOCATION`, `OVER_RECEIPT`, `WAREHOUSE_MISMATCH`, `TRANSIT_NOT_CONFIGURED`, `QUARANTINE_NOT_CONFIGURED`, `LOSS_ACCOUNT_NOT_CONFIGURED`. Supply readable messages naming affected lines/sections. Refusals must guarantee no effects committed. A timeout or malformed result is not a definitive refusal.

The frontend stores the frozen unresolved request and key in sessionStorage scoped by user and transfer. It disables other changes until explicit retry resolves it. A conflict locks the action for administrator reconciliation; never advise blindly clearing keys and retrying financial/stock effects. Backend idempotency and document state checks remain essential across browser/session loss.

Setup prerequisites: a transit warehouse per company, authorized quarantine warehouses, a stock loss expense account, valuation/batch settings, default posting policies, and a public frontend URL. Fail closed on missing posting prerequisites; never substitute a regular destination for transit or silently book damage into saleable stock. Reservation enforcement must be ready before reporting the feature enabled.

## 5. Acceptance scenarios and rollout

1. Create → prepare/reserve → ready → dispatch → arrival → exact receipt: source decreases at dispatch, transit increases then clears, destination increases only at receipt; four stages and linked documents agree.
2. Partial preparation blocks ready/dispatch; manager reduction preserves requested quantities, trims/releases allocations, requires renewed readiness; one shipment only.
3. Concurrent preparation of the last stock unit or serial: one reservation wins. Concurrent POS/material consumption respects held transfer reservations. Cancellation releases reservations.
4. Multiple receipts: 10 dispatched, 6 accepted first, then 3 accepted + 1 damaged; destination +9, quarantine +1, transit zero, Received with exceptions. Never post the first receipt again.
5. 10 dispatched, 8 accepted, 2 outstanding: manager loss settlement posts exactly 2 from transit; Received with exceptions. A normal staff user cannot settle loss.
6. Reject receipt/loss above outstanding, negative/NaN quantities, foreign line/allocation IDs, duplicate serials, aggregate allocation reuse across accepted/damaged, same/cross-company or unauthorized warehouses.
7. UOM 2 Box × 12 = 24 stock units; batch/serial allocations reconcile through dispatch, receipt, quarantine and loss. Validate serial counts/precision and preserve valuation.
8. Replay every mutation after a timeout: same result and stock document IDs. Same key/different payload fails without effects. Concurrent stale revision fails without partial writes.
9. Reject edits/cancellation after dispatch and generic writes against generated stock documents; ordinary Stock Entry functions remain unchanged for unrelated documents.
10. Revoke user access between view and submit: backend denies action. Counts/search/detail do not leak unauthorized records.
11. Disable/miss capabilities: no frontend mutations or fake success; readable historical list/detail may remain available. Errors and retries are distinct from empty lists.
12. Verify desktop and phone layouts in English and Arabic, keyboard access, long names, printed QR/barcode and authenticated deep links on web/Electron-compatible public URL.

Deploy backend migrations/settings/permissions and exercise these scenarios in a non-production company before enabling capabilities. Release frontend with actions gated; enable only after reconciliation and reservation tests pass. Monitor refused transitions, idempotency conflicts, negative transit balances, stuck reservations and transfers with unresolved transit stock. This change does not deploy the backend or the frontend to production.


## 6. Implementation, setup and verification

### Backend files and storage

- API: `pet_app/api/stock_transfer.py` (five GET methods and eleven POST methods).
- Service: `pet_app/stock_transfer/` (validation, Frappe permissions, allocation provenance, ERPNext posting, reservations, transaction boundary).
- Schema: `Stock Transfer Order`, `Stock Transfer Line`, `Stock Transfer Reservation`, `Stock Transfer Allocation`, `Stock Transfer Event`, `Stock Transfer Request`, `Stock Transfer Settings`, `Stock Transfer Quarantine`.
- Migration: `pet_app.patches.stock_transfer_schema`, also registered in `INSTALL_SCHEMA_PATCHES` for fresh installs. It owns the two `Stock Entry.custom_stock_transfer_*` custom fields; they are deliberately not fixture-owned.

`Stock Transfer Request` uses the SHA-256 of user + method + idempotency key as its primary key. Its fingerprint covers the complete frozen request, including transfer ID and expected version. Successful full detail and stock document IDs are stored in the same transaction. Replays return that original result after rechecking current read authorization. Keep the entire unresolved request unchanged on retry.

The order's `revision` increments on every successful command. The wire version combines order identity and revision; clients must treat it as opaque. `line_id` has a unique index across orders. Held serial numbers also have a unique index across all warehouses and orders. Orders remain at Frappe `docstatus=0`; the dedicated `status` carries this workflow. The DocType is marked submittable solely to make native Submit/Cancel permissions available for managerial commands; direct save/submit/cancel/delete is refused by its controller.

Immutable allocation records retain dispatch stock quantity and identity. Each settlement allocation references its original dispatch allocation, including untracked items whose clients send empty allocation arrays. Outstanding transit reservations protect shipment stock from unrelated consumption. Preparation reservations are complete replacements. The event ledger stores readable notes plus structured before/after snapshots and request payloads.

### Native authorization

No second permission platform was added. Default DocPerm grants Stock User read/create/write and Stock Manager read/create/write/submit/cancel. The server evaluates the current user's actual DocPerm/Custom DocPerm, Warehouse User Permissions, and Item permissions on each request.

- Create uses Create; updates and operational steps use Write.
- Reduction and loss require Submit; cancellation requires Cancel.
- Stock-posting commands additionally require Stock Entry Create and Submit.
- Source scope controls preparation, dispatch, shipment updates, reductions, and cancellation. Target scope controls arrival, receipts, and loss settlement.
- Read access requires either endpoint warehouse within scope. Generic list/detail readers have the same warehouse restrictions through Frappe query and document permission hooks. Source snapshots are null for target-only readers.
- On-behalf requesting requires managerial Submit plus access to the selected User. Ordinary staff can request only as themselves.

The `page.warehouse.stock_transfers` row is added only when absent in `Pet App Access Settings`. It controls route/sidebar visibility; the dynamic native DocType snapshot and API checks remain authoritative. Existing support-managed role and page assignments are preserved.

### Reservation enforcement and operational limits

A database `FOR UPDATE` lock on the `Stock Transfer Order` DocType row serializes stock writers and workflow commands across the site. Stock controllers, stock ledger entries, bundles, and native stock reservations acquire this lock. This deliberately favors correctness over throughput; benchmark contention before enabling on a busy site. No Redis lock or expiring lease is used.

The Stock Ledger Entry submission guard checks remaining physical stock, held serials, and batch quantities using current locking reads, including bundle and legacy batch ledger records. It also protects holds while transfers are disabled. Native Stock Reservation Entry submissions cannot reserve quantities already held by a transfer. Normal unrelated Stock Entries retain their existing behavior when no held stock is affected.

Cancellation and Stock Reconciliation affecting an item/warehouse with outstanding transfer reservations are refused until those reservations are released or settled. This conservative policy avoids cancellation/reposting and backdated reconciliation invalidating held stock. ERPNext valuation and accounting validation remain active. Deadlock/snapshot failures in transfer methods receive a full transaction rollback and `RESERVATION_CONFLICT`; unexpected server/transport failures remain failures and must use the original retry key.

Allocation stock quantities use precision 9. Transaction quantities use ERPNext Stock Entry Detail quantity precision and the selected UOM's whole-number rule; serial stock quantities must be whole. Decimal UOM multiplication avoids rejecting valid quantities because of floating-point multiplication artifacts. One underlying stock row per workflow line/category carries the original UOM and a backend-created bundle, even when the line contains many serials or batches.

### Site setup and activation

1. Deploy the code and run `bench --site <staging-site> migrate`. On a fresh app installation the schema patch runs through `after_install` as well. Schema installation does not create company settings, historical transfers, warehouses, accounts, or role-profile assignments.
2. Create one `Stock Transfer Settings` record per participating company. Configure `transit_warehouse`, child `quarantine_warehouses`, `loss_expense_account`, `cost_center`, and `public_app_url`. Warehouses must be distinct enabled leaves in the company; account and cost center must be enabled company ledgers/leaves. The public URL must point to the separately deployed frontend application. Do not use the backend host or an Electron file URL.
3. Configure and review native permissions and the page's visibility roles. Review ERPNext stock valuation, item UOMs, batches, serials, fiscal periods, warehouse accounts, and posting defaults. The workflow never substitutes a saleable warehouse for transit or quarantine.
4. Run staging ledger/concurrency tests and perform frontend acceptance. Only then mark `reservation_tests_passed` and `enabled`. Both flags default to zero and both are required by the API. They represent an administrator's deployment attestation; setting a flag does not run tests.
5. Run the capability and authenticated workflow calls through actual HTTP workers after your normal deployment/restart process. A bench process imports current source independently and is not proof that preloaded web workers have reloaded.

No production activation or frontend deployment is included in this change. The frontend's English/Arabic desktop/mobile layouts, keyboard behavior, printing, scanning, and authenticated web/Electron deep links require verification in the frontend deployment.

### Reproducible backend tests

The real-ledger suite refuses to run outside a database whose name begins `test_driver_orders_`. The existing isolated `driver-orders.test.localhost` site was used. It is not the operational site.

```bash
bench --site driver-orders.test.localhost execute pet_app.patches.stock_transfer_schema.execute
bench --site driver-orders.test.localhost execute pet_app.tests.test_stock_transfer.run
bench --site driver-orders.test.localhost execute pet_app.tests.stock_transfer_concurrency.run
```

The unit/integration suite rolls back its fixtures. The explicit two-connection concurrency probe commits isolated fixtures to exercise real contention; it retains test ledger history, cancels unused demand, and removes its temporary settings record. Do not redirect that probe to an operational database.

Coverage includes exact and partial receipts, damage/loss accounting, UOM preservation, batch/serial bundles, aggregate allocation reuse rejection, preparation snapshot replacement, managerial reductions, refusal rollback after an accepted posting but before damage posting, retries for every mutation, stale versions, generic stock-document guards, filtered pipeline counts, target-only staff, permission revocation, and competing stock consumers. The concurrency probe checks competing last-stock reservations, duplicate dispatch replay, competing receipts, and reservation versus ordinary Material Issue consumption.

Monitor `Stock Transfer Reservation` against open orders, dispatch allocations minus settlement allocations, and transit Bin balances; investigate any discrepancy before further stock actions. Events and original idempotent outcomes are append-only: do not clear request records or manufacture new retry keys to bypass a conflict.

Verification on 2026-09-09: 14 workflow/rule tests passed, 7 schema/fixture checks passed, and all four two-connection concurrency probes passed. Python compilation, Ruff checks, and diff whitespace checks passed. Production and frontend verification remain pending.


## 7. Missing DocType repair on frappe.localhost — 2026-09-09

The frontend-facing site did not have `Stock Transfer Order`; the initial schema patch had run only on the isolated test site. After a successful database backup, `pet_app.patches.stock_transfer_schema.execute` was applied specifically to `frappe.localhost`. No broad migration, core source changes, or stock-action activation were performed.

Authenticated HTTP checks against the running web server returned 200 for `DocType/Stock Transfer Order`, `pet_app.api.stock_transfer.get_capabilities`, and `pet_app.api.stock_transfer.list_transfers`. Capabilities returned `ok=true, enabled=false`; the list returned `ok=true` and zero records. The temporary verification session was removed after checking. The canonical DocType name is **Stock Transfer Order**, not Stock Transfer.

The company posting settings and normal frontend workflow verification are still required before activation. Earlier staging-only deployment statements above describe the initial implementation; this section records the later schema repair.


## 8. Company setup API and activation — 2026-09-09

This section supersedes earlier deployment-pending statements for the backend on `frappe.localhost`. Frontend implementation instructions are in [STOCK_TRANSFER_FRONTEND_GUIDE.md](STOCK_TRANSFER_FRONTEND_GUIDE.md); this file owns the API contract.

Namespace: `/api/method/pet_app.api.stock_transfer_settings`. All endpoints use the standard envelope. GET reads, POST JSON saves, normal session authentication and CSRF rules apply.

| Method | Arguments | Authorization | Success data |
|---|---|---|---|
| `get_status` (GET) | `company` required | Stock Transfer Order Read + company access | `{company,configured,ready,enabled,issues,can_configure}` |
| `get_setup` (GET) | `company` optional | Stock Transfer Settings Read + company access | `{company,companies,settings,version,permissions,readiness,options}` |
| `save_setup` (POST) | `company,expected_version,payload` | Native settings Create or Write, settings Read, and scoped company/warehouse/account/cost-center access | `{company,settings,version,permissions,readiness}` |

`get_status` is available to stock staff without exposing configured account IDs. Each issue is `{field,code,message}`. `ready` means configuration and test attestation are complete. `enabled` additionally requires the enabled switch. A complete but switched-off configuration has `ready:true,enabled:false,issues:[]`. `configured` reports whether a settings record exists; `can_configure` requires settings Read plus the applicable Create/Write permission.

`get_setup` returns permission-filtered `companies:[{id,label}]`; it selects the sole available company if no company is supplied. Otherwise an unselected response has `company:null,settings:null,version:null,readiness:null`, `permissions:{read:true,write:false}`, and empty option arrays. For a selected company, `settings` has `company` plus all fields below. `options` contains `warehouses`, `expense_accounts`, and `cost_centers`, each an array of `{id,label}`. Only enabled, company-scoped leaves appear; expense accounts must have root type Expense. `permissions` contains boolean `read` and `write`.

Save body, as a complete snapshot:

```json
{
  "company": "Kokh-vet",
  "expected_version": "opaque value returned by get_setup",
  "payload": {
    "enabled": true,
    "reservation_tests_passed": true,
    "transit_warehouse": "مخزون التحويلات قيد النقل - K",
    "quarantine_warehouses": ["حجر مخزون التحويلات - K"],
    "loss_expense_account": "603790 - تسويات المخزون / فروقات الجرد - K",
    "cost_center": "الإدارة - K",
    "public_app_url": "https://clinic.kokh-vet.com/"
  }
}
```

On first creation `expected_version` is null. Thereafter pass the returned version unchanged. A stale value returns `VERSION_CONFLICT` without overwriting settings. Save requires exactly the seven documented payload fields; booleans must be JSON booleans, warehouse/account/cost-center fields are IDs or null, and quarantine is an array of unique IDs. Unknown fields are rejected. The company of an existing record cannot be changed.

Incomplete drafts are permitted only with `enabled:false`; supplied values must still be valid. Enabling requires transit, at least one distinct quarantine warehouse, expense account, cost center, valid frontend URL, and test attestation. A warehouse used as an endpoint of an open order cannot newly become transit or quarantine. Both API and native Desk saves enforce configuration validation. Runtime activation checks use the same validation rules. Existing stored transfer transit snapshots are retained.

Errors use existing stable codes: `PERMISSION_DENIED`, `VALIDATION_ERROR`, `WAREHOUSE_MISMATCH`, `TRANSIT_NOT_CONFIGURED`, `QUARANTINE_NOT_CONFIGURED`, `LOSS_ACCOUNT_NOT_CONFIGURED`, `STOCK_TRANSFER_DISABLED`, `VERSION_CONFLICT`. Failed saves return the first readable error; successful draft reads/saves return all outstanding readiness issues with field identifiers.

Settings saves do not post stock and have no workflow idempotency key. After a timeout, reload and compare settings before submitting another save. Workflow stock mutations retain their durable idempotency protocol from section 3. Reads and saves never automatically create warehouses or accounts.

### Current configuration

The explicit administrative helper `pet_app.setup.stock_transfer.configure_company` created dedicated company warehouse leaves and reused the Company's stock adjustment account and cost center. It does not run on installation or ordinary reads; repeated provisioning returns existing settings without overwriting administrator choices.

- Transit: `مخزون التحويلات قيد النقل - K`, under `مخزون في الطريق - K`.
- Quarantine: `حجر مخزون التحويلات - K`, under `مخازن Kokh-vet - K`.
- Loss account: `603790 - تسويات المخزون / فروقات الجرد - K` (existing Company stock adjustment default).
- Cost center: `الإدارة - K` (existing Company default).
- Public application URL: `https://clinic.kokh-vet.com/` (existing configured frontend URL).
- `enabled=true`, `reservation_tests_passed=true` following the isolated backend test suite and concurrent ledger probes.

No production stock or accounting entries were posted by provisioning. No Frappe or ERPNext core source files were changed.

Verification: 17 rule/workflow/setup tests passed; all four two-connection concurrency probes passed. Authenticated requests through the running web server returned 200 with `enabled:true` for status, setup, and workflow capabilities. Administrator capabilities returned all 11 actions. A stale POST settings save returned `ok:false,meta.code=VERSION_CONFLICT`. Temporary verification sessions were removed afterward. Frontend role-specific controls, layouts, translations, labels, and deep links still require UI acceptance.
