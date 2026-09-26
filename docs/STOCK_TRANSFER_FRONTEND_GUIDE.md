# Stock Transfers — frontend implementation guide

Backend implementation: `pet_app`. Canonical wire contract: [STOCK_TRANSFER_CONTRACT.md](STOCK_TRANSFER_CONTRACT.md), including the setup endpoints in section 8. This guide is a consumer implementation checklist; the backend contract remains authoritative.

## What to build

Build these views under the existing warehouse workspace:

| View | Suggested route | Purpose |
|---|---|---|
| Transfer list | `/warehouse/stock-transfers` | Search, warehouse/status filters, pipeline counts, pagination |
| Create transfer | `/warehouse/stock-transfers/new` | Source/target, requester, date, priority, items and UOM |
| Transfer detail | `/warehouse/stock-transfers/:id` | Preparation, dispatch, shipment, arrival, receipts, loss and history |
| Company settings | `/warehouse/stock-transfers/settings` | Configure warehouses, expense account, cost center, frontend URL and activation |

Register `settings` and `new` before the dynamic `:id` route. These routes share the existing `page.warehouse.stock_transfers` visibility key and `module.warehouse`. Settings controls also require the native `Stock Transfer Settings` permissions. Page visibility alone never authorizes an operation.

Use **Stock Transfer Order** as the DocType name. `Stock Transfer`, `Stock Transfer Orders`, and client-created Stock Entry transitions are not supported.

## API handling

Use the application's authenticated session and existing CSRF handling. GET endpoints read; POST endpoints accept JSON. Frappe wraps the service envelope in `message`:

```json
{"message":{"ok":true,"data":{},"meta":{},"errors":[]}}
```

Use the existing interceptor if it already unwraps `message.data`. Do not unwrap twice. A response with `ok:false` is a refusal, including when its HTTP status is 200. Show its readable `errors` and use `meta.code` for behavior. Never replace failed reads with an empty successful list.

## Replace the generic unavailable banner

On entering the workspace:

1. Read `pet_app.api.stock_transfer.get_capabilities`.
2. Read `pet_app.api.stock_transfer_settings.get_status?company=<encoded-company>` for the selected company.
3. Keep historical list/detail reads available while actions are disabled.
4. If `status.enabled=false`, show the company's readiness issues. When `ready=true` but `enabled=false`, display “Stock transfers are switched off for this company.”
5. If `can_configure=true`, show **Configure stock transfers**, linking to the settings view. Otherwise show **Ask a system administrator to complete stock transfer setup**.
6. Show a mutation only when it appears in capability `actions`; on an existing order additionally require detail `allowed_actions` to contain it. Company `status.enabled` is also required. In a multi-company deployment, one enabled company does not enable the others.

The user-facing banner should explain the missing field, not say that backend code must be implemented. Network and permission failures need their own error states.

## Settings view

Setup API namespace: `/api/method/pet_app.api.stock_transfer_settings`.

### Load

Call `get_setup` without arguments to get the company choices. A sole permitted company is automatically selected. If `company:null`, let the user select a company and call `get_setup?company=<id>`.

The response contains:

- `company`, `companies: [{id,label}]`
- `settings`: the complete form snapshot, or null until a company is selected
- `version`: opaque string, or null when the company has no settings record
- `permissions: {read,write}`
- `readiness: {ready,enabled,issues:[{field,code,message}]}`
- `options: {warehouses,expense_accounts,cost_centers}`; each choice is `{id,label}`

Use exactly those permission-filtered choices. Empty options are real empty choices, not a reason to fetch every warehouse/account using generic unrestricted queries. Display required fields and validation messages beside their controls.

### Form fields

| Field | UI | Rules |
|---|---|---|
| `transit_warehouse` | Single warehouse selector | Enabled company leaf; cannot also be quarantine |
| `quarantine_warehouses` | Multiple warehouse selector | At least one to activate; unique IDs |
| `loss_expense_account` | Expense account selector | Enabled company expense ledger |
| `cost_center` | Cost center selector | Enabled company leaf |
| `public_app_url` | URL input | Public frontend base, HTTP(S), no credentials/query; `/#/` supported |
| `reservation_tests_passed` | Administrator confirmation checkbox | Explain that ticking it attests to completed reservation/ledger tests; it does not run tests |
| `enabled` | Switch | Activation requires all prerequisites and the confirmation above |

Suggested labels: “Transit warehouse”, “Damaged goods warehouse(s)”, “Stock-loss expense account”, “Cost center”, “Public application URL”, “Reservation and ledger tests verified”, “Enable stock transfers”. Add Arabic translations and RTL layout using the app's existing i18n system.

Support **Save draft** with `enabled:false` and **Save settings** with the requested activation value. A draft may have missing fields, but supplied selections must be valid. Do not silently set `reservation_tests_passed` when a user clicks Enable. The administrator must explicitly confirm the test result.

### Save

Call POST `save_setup` with `company`, the last `version` as `expected_version`, and the complete payload. Send JSON booleans, string IDs/nulls, and an array of quarantine warehouse IDs. Do not send company, document name, modified, child-row metadata, or options inside `payload`.

```ts
const body = {
  company: selectedCompany,
  expected_version: loaded.version, // null only before the first save
  payload: {
    enabled: form.enabled,
    reservation_tests_passed: form.reservation_tests_passed,
    transit_warehouse: form.transit_warehouse || null,
    quarantine_warehouses: [...form.quarantine_warehouses],
    loss_expense_account: form.loss_expense_account || null,
    cost_center: form.cost_center || null,
    public_app_url: form.public_app_url.trim(),
  },
};
```

The successful result includes `settings`, the new `version`, `permissions`, and `readiness`. Replace the saved snapshot and refetch workflow capabilities and company status. The save result does not contain selector `options`; retain the loaded options or reload `get_setup`.

On `VERSION_CONFLICT`, reload settings and let the administrator review the newer values. On a timeout, reload and compare persisted values with the submitted snapshot before saving again. Settings saves do not post stock and do not use the workflow idempotency-key protocol. Workflow mutations still require it.

The setup API selects existing warehouses/accounts. Warehouse creation can use the application's existing authorized warehouse-management UI. No new account or warehouse is created implicitly by `get_setup` or `save_setup`.

## Workflow views

The complete argument/response specifications and examples are in sections 2–4 of the canonical contract. The following summarizes the UI responsibilities.

### List

- GET `stock_transfer.list_transfers` with `search`, `status`, `warehouse`, `offset`, `limit` (20 default, max 100).
- Show `counts.Ordered`, `Preparing`, `Transferring`, `Received`. Counts exclude the selected status filter but respect search and warehouse scope.
- `total` respects all filters. Reset offset when filters change; do not derive counts from the page of items.
- Preserve cancelled records through the status filter even though there is no cancelled pipeline tile.

### Create and edit

- Use capability regular warehouse choices; transit/quarantine are excluded.
- Use capability `requesters`. Do not invent requester IDs or a manager-role check.
- `stock_transfer.search_items` requires the selected source warehouse. Keep `current_stock` and `reserved_stock` null when unknown; display “Unavailable”, not zero.
- Show `current_stock` in the picker itself, while the operator is still choosing. A candidate with `current_stock: 0` cannot be transferred at all, and catching it here is far cheaper than catching it at Ready. This site carries many catalogue items that have never been stocked, so this is common, not exotic.
- Do not block a quantity above `current_stock`. Ordering ahead of an incoming delivery is legitimate; the server never checks stock at create time. Warn on the line and let the order save.
- Use the existing `GetItemUomOptions` integration. Keep transaction UOM and authoritative conversion factor together.
- Generate stable UUID `line_id` values for new lines. Preserve IDs when editing; replacing an item requires a new line ID.
- Enable editing only when `update_transfer` is allowed.

### Preparation

- Start with `start_preparation`.
- Load allocation options with `get_allocations(transfer_id,line_id,phase:"prepare")`.
- Send `save_preparation` as a complete snapshot of every line. It replaces reservations; it does not increment them.
- Keep item quantities in transaction UOM and allocation quantities in stock UOM. Untracked items send empty allocation arrays.
- Allow partial saves with `ready:false`; Ready requires all approved demand prepared and allocated.
- Show each line's `issues` as a problem column, on every view that lists lines — not only during preparation. `issues` is returned by `get_transfer` and by **every** mutation including `create_transfer`, so a bad line is visible the moment the order is created.
- A line carrying any code other than `NOT_PREPARED` cannot reach Ready. Name the line and the reason; never leave Ready disabled with no explanation — that is precisely how `ST-2026-892CBAF517E54CF7` burned eighteen hours and was cancelled with one bad line out of 108.
- `message` is server-rendered and already readable in context (`"Needs 768 Gram, 0 free in المخزن الرئيسي - K."`). Display it as given; do not re-derive it from quantities on the client, or the column will drift out of agreement with the Ready button.
- `NOT_PREPARED` is ordinary work in progress, not a fault. The server only emits it while the order is `Preparing`, so it never appears on a new order; style it differently from the blocking codes.
- A permitted manager can use `approve_reduction` with a reason and complete approved-quantity snapshot. After reduction, readiness must be confirmed again.

#### Removing an item from the order

How this works depends on the status, and the frontend must offer both:

| Status | Call | Effect |
| --- | --- | --- |
| `Ordered` | `update_transfer` with that line omitted | Real removal. Preserve `line_id` on every line that stays |
| `Preparing` | `approve_reduction` with `approved_qty: 0` for that line | Row remains for audit; render it as removed |

There is no removal after dispatch. On the `Preparing` path four server rules apply, so surface them rather than letting the call fail:

- It needs the manager `submit` permission — check `allowed_actions`, not a role name.
- A non-empty `reason` is required.
- The payload is a complete snapshot of every line, and at least one line must actually change.
- Total approved demand must stay positive, so the **last** remaining line cannot be zeroed. Offer `cancel_transfer` instead.

`approve_reduction` also sets `has_exceptions` permanently and clears `ready`, so readiness has to be confirmed again afterwards.

### Dispatch and shipment

- Collect the full shipment snapshot: driver name/contact, vehicle, package count/null, seal, departure instant, ETA/null.
- Driver name and timezone-qualified departure are mandatory. Packages must be a nonnegative integer; ETA cannot precede departure.
- `dispatch_transfer` posts stock into transit and returns linked stock documents. Never separately submit a generic Stock Entry.
- `update_shipment` keeps the original departure time; disable that field after dispatch.
- `record_arrival` takes `{}`. Actor and actual arrival time come from the server.

### Receipt, damage and loss

- Load shipment allocation options with phase `receive`; do not query unrelated transit stock.
- Receipts are deltas. Send only touched lines with accepted/damaged quantities and their separate allocations.
- Damage requires notes and an authorized configured quarantine warehouse.
- Aggregate good/damaged allocation use must fit the same shipment allocation's outstanding quantity.
- Manager `resolve_exception` settles loss deltas and requires a reason. Never send an expense-account override.
- Trust the returned status. Received includes accepted, quarantined, and lost quantities; retain exception badges.
- Cancellation takes a reason and is available only before dispatch when allowed by the server.

### Retry and reconciliation behavior

For every workflow mutation, freeze `{transfer_id,expected_version,idempotency_key,payload}` before sending. Create omits transfer ID/version. Use a new key for a new intent; keep the same complete frozen request for an uncertain retry.

Persist unresolved requests per user and transfer in sessionStorage. A transport timeout or malformed success is uncertain: keep the key and block competing changes until explicit retry resolves it. `IDEMPOTENCY_CONFLICT` requires reconciliation; never automatically clear the key and repost. A definitive `ok:false` refusal has no committed workflow effects.

After successful mutations, replace the entire detail state with returned `data`, including version, quantities, history, linked documents and allowed actions. Do not patch quantities optimistically.

### History and labels

Render server history oldest first and linked Stock Entries read-only. Provide authenticated navigation/printing through existing shared helpers. Do not expose generic edit, cancel, amend, or delete controls for workflow-owned Stock Entries.

Build QR links from capability `public_app_url`, not the API origin or `window.location` inside Electron:

```ts
const url = `${publicAppUrl.replace(/\/$/, '')}/warehouse/stock-transfers/${encodeURIComponent(id)}`;
// https://clinic.example/#/ => https://clinic.example/#/warehouse/stock-transfers/ST-...
```

A QR code is a link, never an authorization token. Normal login and record permissions still apply.

## Acceptance checklist

- Missing configuration shows actionable fields and the correct administrator CTA.
- Stock staff can read status but cannot read/edit the settings form.
- A configuration draft stays disabled; valid setup can be enabled and immediately refreshes capability gating.
- Stale settings saves cannot overwrite another administrator's changes.
- Standard, partial, damaged, and loss-settled transfers reconcile against server quantities and stock links.
- Repeated submits/timeouts reuse the original key and never duplicate dispatch/receipt documents.
- Source-only and target-only users see only their allowed steps; access revocation takes effect on the next request.
- English/Arabic desktop and phone layouts, keyboard focus, long names, printing, and authenticated web/Electron deep links work.

All backend additions live in `apps/pet_app`. No Frappe or ERPNext core source changes are required.
