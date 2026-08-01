# Medication Billing Contract

This document is the current backend contract for Visit V11 medication prescribing, dispensing, billing, and invoice stock timing in `pet_app`.

Generated from the current codebase on 2026-07-09. It is descriptive, not aspirational.

## 1. Prescribed Medication Save

### Storage model

Prescribed medications are child rows on `Vet Visit.prescribed_medications` using the child DocType `Vet Visit Medication Item`. The DocType is marked as a child table with `istable: 1` in `apps/pet_app/pet_app/pet_app/doctype/vet_visit_medication_item/vet_visit_medication_item.json:241`.

The backend syncs prescription rows during `VetVisit.validate()`: it calls `_audit_prescribed_medication_changes()` and `_sync_prescribed_medications_billables()` before amount recalculation (`apps/pet_app/pet_app/pet_app/doctype/vet_visit/vet_visit.py:79-98`).

### Save path

The current frontend save path is raw Frappe document save:

1. Fetch raw `Vet Visit` document.
2. Replace or append rows in `prescribed_medications`.
3. Save the whole `Vet Visit` through `frappe.client.save`.

There is no dedicated backend prescribe endpoint in the current backend. Backend behavior is driven by `VetVisit.validate()`.

### Fields on `Vet Visit Medication Item`

| Field | Type | Required | Owner | Contract |
| --- | --- | --- | --- | --- |
| `medication` | Link -> `Medication` | No | Frontend-sent | Medication master link. Used by invoice warehouse/UOM fallback when present. Field defined at `vet_visit_medication_item.json:114-119`. |
| `dose_option` | Link -> `Medication Dose Option` | No | Frontend-sent when using dose options | Optional selected dose option. When present, backend validates it, defaults blank `qty` to `1`, requires `qty >= 1`, treats `qty` as the dose count, and copies `dispense_uom`, `stock_uom`, and `conversion_factor` onto the prescription row before billing sync (`vet_visit.py:370-428`). |
| `medication_item` | Data, options `Item`, read-only | Yes | Frontend-sent or fetched from `medication.linked_item` | Required item code for billing. Field is required at `vet_visit_medication_item.json:50-59`. Billing sync skips rows without it and throws if the Item does not exist (`vet_visit.py:454-467`). |
| `qty` | Float | Yes | Frontend-sent | Prescribed billable quantity. For dose-option rows, this is the number of dose administrations and must be `>= 1`. Required at `vet_visit_medication_item.json:60-66`. Billing sync requires `qty > 0` (`vet_visit.py:474-478`). |
| `dispense_uom` | Link -> `UOM` | No | Optional frontend / dispense metadata | Used by invoice item context if present; otherwise Medication `default_dispense_uom` or Item stock UOM is used (`vet_visit.py:934-949`). Field defined at `vet_visit_medication_item.json:67-72`. |
| `stock_uom` | Link -> `UOM`, read-only | No | Optional/backend | Used by invoice item context if present; otherwise Item `stock_uom` is used (`vet_visit.py:934-951`). Field defined at `vet_visit_medication_item.json:73-80`. |
| `conversion_factor` | Float, default `1` | No | Optional frontend / dispense metadata | Used by Sales Invoice item if > 0; otherwise Medication default conversion factor; if UOM equals stock UOM, defaults to 1 (`vet_visit.py:941-953`). Field defined at `vet_visit_medication_item.json:81-85`. |
| `rate` | Currency | No | Frontend-sent | Doctor override is honored. Billing sync uses `row.rate` when non-empty; otherwise it falls back to item pricing (`vet_visit.py:475-483`). Field defined at `vet_visit_medication_item.json:86-91`. |
| `amount` | Currency, read-only | No | Backend-computed | Backend sets `amount = qty * rate` during billing sync (`vet_visit.py:480-483`). Field defined at `vet_visit_medication_item.json:92-98`. |
| `dosage` | Data | No | Frontend-sent | Clinical note only; included in medication note text. Field defined at `vet_visit_medication_item.json:91-96`. |
| `frequency` | Data | No | Frontend-sent | Clinical note only; included in medication note text. Field defined at `vet_visit_medication_item.json:97-102`. |
| `duration_days` | Int | No | Frontend-sent | Clinical note only; included in medication note text. Field defined at `vet_visit_medication_item.json:103-108`. |
| `instructions` | Small Text | No | Frontend-sent | Clinical note only; included in medication note text. Field defined at `vet_visit_medication_item.json:109-113`. |
| `warehouse` | Link -> `Warehouse` | No | Optional frontend/backend | Not required from frontend. Invoice builder resolves warehouse from row, then Medication default, then Stock Settings (`vet_visit.py:910-932`). Field defined at `vet_visit_medication_item.json:125-131`. |
| `dispense_status` | Select | No | Backend/default; frontend may send initial value | Default is `Prescribed`. Options are listed below. Field defined at `vet_visit_medication_item.json:141-148`. |
| `dispensed_qty` | Float | No | Backend dispense | Updated by `dispense_visit_medication()` (`pharmacy.py:106-109`). Field defined at `vet_visit_medication_item.json:140-146`. |
| `return_qty` | Float | No | Backend return | Updated by `return_dispensed_medication()` (`pharmacy.py:148-156`). Field defined at `vet_visit_medication_item.json:147-152`. |
| `dispensed_by` | Link -> `User`, read-only | No | Backend dispense | Set to current session user on dispense (`pharmacy.py:106-108`). Field defined at `vet_visit_medication_item.json:157-163`. |
| `dispensed_at` | Datetime, read-only | No | Backend dispense | Set on dispense (`pharmacy.py:101-109`). Field defined at `vet_visit_medication_item.json:164-169`. |
| `returned_by` | Link -> `User`, read-only | No | Backend return | Set on return (`pharmacy.py:148-156`). Field defined at `vet_visit_medication_item.json:170-176`. |
| `returned_at` | Datetime, read-only | No | Backend return | Set on return (`pharmacy.py:148-156`). Field defined at `vet_visit_medication_item.json:177-182`. |
| `batch_no` | Link -> `Batch` | No | Optional frontend / dispense metadata | Passed to Sales Invoice item when present (`vet_visit.py:954-958`). Field defined at `vet_visit_medication_item.json:183-188`. |
| `expiry_date` | Date | No | Optional frontend / dispense metadata | Stored on row and dispense ledger, but not used by invoice builder. Field defined at `vet_visit_medication_item.json:190-193`; ledger copy at `pharmacy.py:216-217`. |
| `quantity_modified_by` | Link -> `User`, read-only | No | Backend audit | Set when `qty` changes (`vet_visit.py:428-451`). Field defined at `vet_visit_medication_item.json:199-205`. |
| `quantity_modified_at` | Datetime, read-only | No | Backend audit | Set when `qty` changes (`vet_visit.py:428-451`). Field defined at `vet_visit_medication_item.json:206-211`. |
| `rate_modified_by` | Link -> `User`, read-only | No | Backend audit | Set when `rate` changes (`vet_visit.py:428-454`). Field defined at `vet_visit_medication_item.json:212-218`. |
| `rate_modified_at` | Datetime, read-only | No | Backend audit | Set when `rate` changes (`vet_visit.py:428-454`). Field defined at `vet_visit_medication_item.json:219-224`. |
| `sales_invoice_item` | Data, read-only | No | Reserved/backend | Field exists, but current invoice creation code does not write it. Field defined at `vet_visit_medication_item.json:225-230`. |

### Frontend minimum payload

For a new medication row, the frontend must send:

```json
{
  "medication_item": "Amoxicillin",
  "qty": 1
}
```

Recommended payload:

```json
{
  "medication": "Amoxicillin",
  "medication_item": "Amoxicillin",
  "qty": 1,
  "rate": 10000,
  "dosage": "...",
  "frequency": "...",
  "duration_days": 5,
  "instructions": "..."
}
```

Dose-option payload, when the selected Medication has active dose options:

```json
{
  "medication": "Test Ketamine",
  "medication_item": "Test Ketamine",
  "dose_option": "child-row-name",
  "qty": 3,
  "rate": 5000,
  "dosage": "0.2 mL dose",
  "frequency": "Once",
  "instructions": "..."
}
```

When `dose_option` is present, the frontend should not send custom `dispense_uom`, `stock_uom`, or `conversion_factor`. The backend copies those from the selected dose option and linked Item (`vet_visit.py:370-428`). Blank `qty` defaults to `1`; nonblank `qty` must be `>= 1` and represents the dose count. Billing remains `qty × rate`; stock deduction is `qty × Medication Dose Option.qty`.

`warehouse` is not required from the frontend. After the 2026-07-08 backend fix, invoice creation resolves warehouse in this order: row `warehouse`, Medication `default_warehouse`, Stock Settings `default_warehouse` (`vet_visit.py:910-932`).

For editing an existing medication row, the frontend must include the existing child row `name`. If the existing row name is omitted, the raw save path creates a new child row; billing sync will then create a new `Pet Billable Item` and cancel the old linked billable because billables are keyed by `medication::{row.name}` (`vet_visit.py:485-510`).

## 2. Medication Master Defaults

The Medication master is the `Medication` DocType. Relevant fields:

| Field | Type | Purpose | Evidence |
| --- | --- | --- | --- |
| `medication_name` | Data, required | Medication master name/autoname | `medication.json:4`, `medication.json:33-40` |
| `linked_item` | Link -> `Item` | ERPNext item used for billing/stock | `medication.json:56-62` |
| `item_group` | Link -> `Item Group` | Item grouping | `medication.json:63-69` |
| `default_warehouse` | Link -> `Warehouse` | Invoice fallback warehouse | `medication.json:70-75`, used by `vet_visit.py:910-932` |
| `default_price` | Currency | Prescribe-rate default; backend can fall back to item pricing if row rate empty | `medication.json:76-80`, row-rate fallback at `vet_visit.py:475-483` |
| `dosage_form_or_unit` | Link -> `UOM` | Mirror of linked Item `stock_uom`; kept for compatibility/display, not a separate stock-unit input | `medication.json:86-92`, mirrored from `Item.stock_uom` by `medication.py` |
| `default_dispense_uom` | Link -> `UOM` | Optional backend invoice UOM fallback for non-dose medication rows; not required from the form | `medication.json:93-98`, used by `vet_visit.py:934-949` |
| `default_conversion_factor` | Float | Optional backend conversion factor fallback paired with `default_dispense_uom` for non-dose medication rows | `medication.json:99-104`, used by `vet_visit.py:941-953` |
| `dose_options` | Table -> `Medication Dose Option` | Optional fixed-price dose options with direct numeric stock deduction | `medication.json:109-119`, validated by `medication.py` without creating UOMs or Item UOM conversions |
| `strength` | Data | Master/profile display | `medication.json:120-124` |
| `default_dosage` | Data | Prescribe dialog default | `medication.json:125-129` |
| `default_frequency` | Data | Prescribe dialog default | `medication.json:130-134` |
| `default_duration_days` | Int | Prescribe dialog default | `medication.json:135-139` |
| `default_instructions` | Small Text | Prescribe dialog default | `medication.json:140-144` |
| `disabled` | Check | Hide/disable master item | `medication.json:150-156` |
| `reference_count`, `given_count`, `total_dispensed_amount` | Int/Int/Float read-only counters | Master usage counters | `medication.json:162-181` |

## Price List Contract

The veterinary billing price list is `Standard Selling` by default and is resolved in one place: `pet_app.utils.price_list.get_veterinary_selling_price_list()`. The resolver can be configured with `pet_app_veterinary_selling_price_list` in Frappe site config, but every medication, care-service, and visit-invoice call site must read through that resolver.

The price list has only two jobs:

1. Provide the fallback medication rate when a prescription row has no doctor-entered `row.rate`.
2. Provide Sales Invoice currency plumbing through the invoice header `selling_price_list`.

The price list must never override a doctor-entered medication row rate. Billing sync uses `row.rate` when it is non-empty and only falls back to Item Price when the row rate is blank. The invoice builder passes the already-resolved row rate into each Sales Invoice item, and the invoice sets `ignore_pricing_rule = 1`. ERPNext's `force_item_fields` does not include `rate`, so `set_missing_values()` may fill item metadata but does not force a price-list rate over a provided row rate.

Medication master defaults also write Item Prices to the resolved veterinary selling price list. In the default site configuration that means Medication `default_price` creates or updates a selling Item Price on `Standard Selling`.

## 3. Dose Options / Fixed Price With Variable Stock Deduction

Dose options are optional child rows on the `Medication` DocType using child DocType `Medication Dose Option`. A medication with no active dose options behaves exactly as before.

The current implementation is number-based: dose options do not create UOMs and do not add Item UOM Conversion rows. Each option stores the stock deduction quantity directly in the linked Item stock UOM.

### Medication Dose Option fields

| Field | Type | Required | Owner | Contract |
| --- | --- | --- | --- | --- |
| `label` | Data | No | Master data user | Doctor-facing label, e.g. `0.5 mL` or Arabic label. Optional; UI may derive a display label from `qty` and the Item stock UOM. |
| `qty` | Float | Yes | Master data user | Stock deduction quantity in the medication Item stock UOM. For a half-mL dose on an Item stocked in `ml`, set `qty = 0.5`. |
| `disabled` | Check | No | Master data user | Disabled options cannot be selected for prescribing. |

Removed from the active contract: `uom` and `conversion_factor` on `Medication Dose Option`. Dose options no longer use distinct UOMs such as `0.2 mL Dose`.

### Medication save validation

On Medication save, `_sync_item_defaults()` validates dose options without creating UOMs or Item UOM Conversion rows. For each active option, backend:

1. validates `qty > 0`;
2. rejects fractional deductions if the Item stock UOM is marked whole-number-only;
3. resolves an existing placeholder invoice UOM different from the Item stock UOM. The resolver prefers `Nos` when the stock UOM is not `Nos`; when stock UOM is `Nos`, it picks the first existing alternative from `ml`, `Unit`, `Bottle`, `Tablet`, then falls back to any existing UOM different from the stock UOM. No UOM is created.

The linked ERPNext Item `stock_uom` is the single source of truth for the medication stock unit. Medication `dosage_form_or_unit` mirrors `Item.stock_uom` on save and should not be treated as a separate frontend input.

The existing non-dose `default_dispense_uom` / `default_conversion_factor` path remains separate and unchanged. These fields are optional backend fallbacks; when absent, invoice UOM falls back to the medication row UOM or linked Item stock UOM.

### Prescribe mapping

The UI should show a dose-option picker only when `Medication.dose_options` contains at least one row where `disabled` is false. When selected, send only the selected `dose_option` plus normal clinical fields and `rate`; backend fills the invoice UOM fields.

When `dose_option` is present, `Vet Visit.validate()` applies the dose-count shape on the medication row. `dispense_uom` is an existing placeholder UOM resolved by the backend and guaranteed to differ from `stock_uom` so ERPNext does not reset `conversion_factor` to `1`. `qty` is the number of dose administrations, while `conversion_factor` is the per-dose stock deduction:

```json
{
  "qty": "<dose count, defaults to 1>",
  "dispense_uom": "<existing placeholder UOM != Item.stock_uom>",
  "stock_uom": "<Item.stock_uom>",
  "conversion_factor": "<Medication Dose Option.qty>"
}
```

Example for an Item stocked in `ml` with dose option `qty = 0.5`, prescription `qty = 3`, and rate `5000`:

```json
{
  "qty": 3,
  "rate": 5000,
  "amount": 15000,
  "dispense_uom": "Nos",
  "stock_uom": "ml",
  "conversion_factor": 0.5
}
```

ERPNext computes Sales Invoice stock quantity as `qty * conversion_factor`, so cashier submit would deduct `3 * 0.5 = 1.5 ml` while the invoice amount is `3 * 5000 = 15000`. If the Item stock UOM is configured as whole-number-only, ERPNext will still reject fractional `stock_qty`; that is native ERPNext UOM validation, not placeholder-UOM behavior.

### UOM picker rule

Dose options no longer create or require distinct UOMs. Generic UOM pickers do not need to filter dose UOMs for the current number-based dose-option flow.

## 4. Dispense / Return

### Dispense method

Method: `pet_app.api.pharmacy.dispense_visit_medication`.

Defined as whitelisted POST at `apps/pet_app/pet_app/api/pharmacy.py:76-77`.

Canonical frontend payload:

```json
{
  "visit": "VVT-...",
  "row_name": "child-row-name",
  "qty": 0.2
}
```

Accepted aliases/legacy inputs: `medication_row`, top-level `warehouse`, and `data`/kwargs payload (`pharmacy.py:77-82`). `warehouse` is optional and not required (`pharmacy.py:93`). If supplied, it is validated and stored on the medication row (`pharmacy.py:93-105`, `pharmacy.py:173-180`).

Current backend also accepts and persists optional metadata from payload: `uom`/`dispense_uom`, `stock_uom`, `conversion_factor`, `batch_no`, and `expiry_date` (`pharmacy.py:190-203`). These are not required for the simplified frontend dispense action.

What dispense writes:

- Validates visit + row (`pharmacy.py:80-89`).
- Rejects final statuses `Cancelled` and `Returned` (`pharmacy.py:14-15`, `pharmacy.py:90-91`).
- Defaults qty to remaining prescribed qty when qty is missing or non-positive (`pharmacy.py:94-97`).
- Prevents `dispensed_qty > qty` (`pharmacy.py:98-99`).
- Optionally stores warehouse and metadata (`pharmacy.py:101-105`, `pharmacy.py:190-203`).
- Increments `dispensed_qty`, sets `dispensed_by`, `dispensed_at`, and status `Dispensed` or `Partially Dispensed` (`pharmacy.py:106-110`).
- Saves the Vet Visit with `ignore_billing_lock` (`pharmacy.py:111-112`).
- Inserts a custom `Medication Dispense Ledger` row (`pharmacy.py:113-123`, `pharmacy.py:206-225`).

Dispense does not create an ERPNext Stock Entry, does not submit a Stock Entry, and does not post Stock Ledger Entries. It is workflow/status plus custom ledger only.

### Return method

Method: `pet_app.api.pharmacy.return_dispensed_medication`.

Defined as whitelisted POST at `apps/pet_app/pet_app/api/pharmacy.py:129-130`.

Canonical frontend payload:

```json
{
  "visit": "VVT-...",
  "row_name": "child-row-name",
  "qty": 0.2
}
```

What return writes:

- Validates visit + row (`pharmacy.py:132-142`).
- Requires positive return qty (`pharmacy.py:143-144`).
- Prevents return qty from exceeding dispensed qty (`pharmacy.py:145-146`).
- Increments `return_qty`, sets `returned_by`, `returned_at`, and status `Returned` or `Partially Dispensed` (`pharmacy.py:148-154`).
- Saves the Vet Visit with `ignore_billing_lock` (`pharmacy.py:155-156`).
- Inserts a custom `Medication Dispense Ledger` row (`pharmacy.py:157-167`, `pharmacy.py:206-225`).

Return does not restore ERPNext stock. ERPNext stock has not moved at dispense time.

## 5. Visit Close -> Draft Sales Invoice

### Endpoint

Use workspace action:

```json
{
  "source_type": "Vet Visit",
  "name": "VVT-...",
  "action": "complete_case",
  "payload": { ...clinical completion payload... }
}
```

Backend endpoint: `pet_app.api.workspace.perform_action`. It dispatches `action == "complete_case"` to `_complete_case()` (`workspace.py:244-292`).

### Strict atomic flow

`_complete_case()` creates a database savepoint, runs `_complete_case_atomic()`, rolls back to the savepoint on any exception, and releases the savepoint on success (`workspace.py:1582-1591`).

`_complete_case_atomic()` does the following:

1. Loads the visit and exits early if already Completed (`workspace.py:1594-1597`).
2. Saves clinical note payload if present (`workspace.py:1598-1600`; field mapping at `workspace.py:1465-1498`).
3. Saves diagnoses payload if present (`workspace.py:1601-1602`; diagnosis save at `workspace.py:1501-1529`).
4. Reloads the visit and validates `complete_case` transition (`workspace.py:1603-1604`).
5. Copies primary diagnosis / instructions into required fields if possible (`workspace.py:1605-1610`).
6. Validates completion requirements (`workspace.py:1611`, requirements at `workspace.py:1628-1632` and following lines).
7. Calls `_create_sales_invoice_for_visit()` before status transition (`workspace.py:1613`).
8. Throws if no draft invoice object was returned (`workspace.py:1614-1616`).
9. Only after invoice creation, transitions status to `Completed` (`workspace.py:1621`).
10. Marks visit/billables invoiced (`workspace.py:1622`, implementation at `vet_visit.py:844-853`).
11. Runs completed-visit sync, queue sync, and comment creation (`workspace.py:1623-1625`).

If any step fails, the savepoint rollback keeps the visit open and removes writes made after the savepoint.

### Invoice creation

Public API: `pet_app.pet_app.doctype.vet_visit.vet_visit.create_sales_invoice(visit_name)` is whitelisted and response-wrapped (`vet_visit.py:758-775`).

Internal strict helper: `_create_sales_invoice_for_visit(visit_name)` raises normally and is used by visit completion (`vet_visit.py:778-841`, called by `workspace.py:1613`).

The Sales Invoice is created as Draft:

- Backend builds `frappe.get_doc({... "doctype": "Sales Invoice", ...})` (`vet_visit.py:804-813`).
- Backend calls `sales_invoice.insert()` (`vet_visit.py:823`).
- There is no `sales_invoice.submit()` in this path.

The invoice sets `update_stock` based on invoice items containing warehouse:

```python
updates_stock = any(item.get("warehouse") for item in items)
"update_stock": 1 if updates_stock else 0
```

Evidence: `vet_visit.py:801-811`.

For stock medications, `_get_stock_invoice_context()` now resolves a warehouse or throws before invoice insert (`vet_visit.py:900-958`). Therefore medication invoices with stock items should have `update_stock = 1` once preconditions pass.

### Invoice preconditions and errors

Explicit invoice creation remains strict: the invoice endpoint still raises the errors below. `complete_case` now preflights invoice items first; when there are no billable rows, no active billable rows, or validated active rows total zero, it skips Sales Invoice creation and completes the visit with `sales_invoice = None`, `billed = 0`, and zero total. Active malformed billable rows still fail completion and roll back the clinical save.

| Condition | Error |
| --- | --- |
| Missing visit name | `Vet Visit is required.` (`vet_visit.py:721-722`) |
| Visit not found while locking | `Vet Visit {visit} was not found.` (`vet_visit.py:965-966`) |
| Existing linked invoice | `Vet Visit {visit} is already billed with Sales Invoice {invoice}.` (`vet_visit.py:727-732`, lock check also at `vet_visit.py:967-972`) |
| Missing customer | `Customer is required before invoicing this visit.` (`vet_visit.py:734-735`) |
| Pending Lab in strict mode | `Complete all Lab records before creating the invoice.` (`vet_visit.py:930` and following strict checks) |
| Missing billable child table rows | Direct invoice creation: `Add at least one billable item before invoicing.` Completion: skip invoice and complete without billing. |
| Missing billable item code | `Billable item row {idx} is missing Item Code.` (`vet_visit.py:807-808`) |
| Billable qty <= 0 | `Billable item row {idx} must have Qty greater than zero.` (`vet_visit.py:810-813`) |
| Billable rate < 0 | `Billable item row {idx} must not have a negative Rate.` (`vet_visit.py:811-815`) |
| No active billable items | Direct invoice creation: `Add at least one active billable item before invoicing.` Completion: skip invoice and complete without billing. |
| Total <= 0 | Direct invoice creation: `Total billable amount must be greater than zero before invoicing.` Completion: skip invoice after active rows pass item, quantity, rate, and stock warehouse validation. |
| Stock item warehouse cannot resolve | `Warehouse is required to invoice medication {medication}. Set a Warehouse on the prescription row, Medication Default Warehouse, or Stock Settings Default Warehouse.` (`vet_visit.py:910-932`) |
| Missing configured veterinary selling price list | `Price List Standard Selling is required for veterinary invoices.` |

## 6. Stock Timing

Stock does not move at prescribe time.

Stock does not move at dispense time. Dispense only updates medication workflow fields and a custom `Medication Dispense Ledger` (`pharmacy.py:101-123`, `pharmacy.py:206-225`).

Stock does not move at visit close because the Sales Invoice is inserted as Draft only (`vet_visit.py:823`).

ERPNext stock movement occurs later when the cashier submits the Draft Sales Invoice, provided `update_stock = 1` and invoice item warehouse/UOM data are valid. The visit invoice builder sets `update_stock = 1` when any invoice item has a warehouse (`vet_visit.py:801-811`).

## 7. Warehouse / UOM / Batch Invoice Mapping

For each active billable row, invoice item base fields are:

```python
item_code = row.item_code
qty = row.qty
rate = row.rate
amount = qty * rate
description = row.item_name or row.item_code
```

Evidence: `vet_visit.py:863-889`.

For stock items only, `_get_stock_invoice_context()` adds:

| Sales Invoice Item field | Source |
| --- | --- |
| `warehouse` | medication row `warehouse`, else Medication `default_warehouse`, else Stock Settings `default_warehouse` (`vet_visit.py:910-932`) |
| `uom` | medication row `dispense_uom`, else Medication `default_dispense_uom`, else Item `stock_uom` (`vet_visit.py:934-949`) |
| `stock_uom` | medication row `stock_uom`, else Item `stock_uom` (`vet_visit.py:934-951`) |
| `conversion_factor` | medication row `conversion_factor`, else Medication `default_conversion_factor`, else `1` when UOM equals stock UOM (`vet_visit.py:941-953`) |
| `batch_no` | medication row `batch_no`; also sets `use_serial_batch_fields = 1` (`vet_visit.py:954-958`) |

Medication defaults are loaded by `Medication` name first, then by `Medication.linked_item == medication_item` (`vet_visit.py:960-981`). This is important because frontend payloads may include only `medication_item`.

## 7. `dispense_status` Values

The authoritative Select options are:

```text
Prescribed
Pending Dispense
Dispensed
Partially Dispensed
Cancelled
Returned
```

Evidence: `vet_visit_medication_item.json:141-148`.

Pharmacy code treats pending statuses as `"", "Prescribed", "Pending Dispense", "Partially Dispensed"` and final statuses as `Cancelled`, `Returned` (`pharmacy.py:14-15`).

## 9. Billing Sync

Billing sync runs on `VetVisit.validate()` (`vet_visit.py:79-98`).

For each non-cancelled prescribed medication row with `medication_item`, `_sync_prescribed_medications_billables()`:

1. Loads the linked ERPNext `Item` (`vet_visit.py:454-467`).
2. Validates qty (`vet_visit.py:469-473`).
3. Uses `row.rate` if present; otherwise falls back to item pricing (`vet_visit.py:475`).
4. Sets `row.rate` and `row.amount` (`vet_visit.py:480-483`).
5. Builds `linked_service_id = medication::{row.name}` (`vet_visit.py:485`).
6. Upserts a `Pet Billable Item` with item type `Medication`, qty, rate, and note (`vet_visit.py:487-496`).
7. Cancels old medication billables whose `linked_service_id` no longer maps to an active medication row (`vet_visit.py:498-510`).

`upsert_visit_billable_item()` updates an existing row when it finds the same linked service identity; otherwise it appends a new billable row (`utils/visit_billing.py:37-109`).

Therefore:

- Rate override is honored when `row.rate` is sent.
- Editing an existing medication must send the existing child row `name`.
- Omitting `name` on edit creates a new medication child row, which creates a new billable and may cancel the old billable.

## 10. Field-Name Gotchas

- The medication child DocType is `Vet Visit Medication Item`, not `Medication Item` (`vet_visit_medication_item.json:246`).
- The child table field on `Vet Visit` is `prescribed_medications`; billing sync reads `self.prescribed_medications` (`vet_visit.py:454`).
- Required prescription fields are `medication_item` and `qty` at the child DocType level (`vet_visit_medication_item.json:50-66`).
- `warehouse` is not required from the frontend for prescribing; backend resolves it during invoice creation (`vet_visit.py:910-932`).
- `Pet Billable Item` has no `warehouse`, `batch_no`, `uom`, or `conversion_factor` fields. Its fields are item/name/type/qty/rate/amount/status/note/link fields only (`pet_billable_item.json:8-20`, `pet_billable_item.json:22-99`). Warehouse/UOM/batch are added only when building Sales Invoice items (`vet_visit.py:900-958`).
- `sales_invoice_item` exists on the medication child row, but current invoice code does not populate it (`vet_visit_medication_item.json:225-230`).
- `dispense_visit_medication` currently still accepts optional warehouse/UOM/batch metadata and persists it if supplied (`pharmacy.py:93-105`, `pharmacy.py:190-203`). The simplified frontend should send only `{visit, row_name, qty}` unless it intentionally wants to store that metadata.

## 11. Verification Notes From 2026-07-08

For `VVT-2026-00089`, the medication row had `warehouse = None`, `medication = Amoxicillin`, `medication_item = Amoxicillin`, qty `1`, rate `10000`.

After the warehouse fallback fix, `get_billable_invoice_items()` resolved the Sales Invoice item in memory as:

```json
{
  "item_code": "Amoxicillin",
  "qty": 1.0,
  "rate": 10000.0,
  "amount": 10000.0,
  "description": "Amoxicillin",
  "warehouse": "رفوف الصيدلية - K",
  "uom": "Tablets / Capsules",
  "stock_uom": "Tablets / Capsules",
  "conversion_factor": 1.0
}
```

Historical note: a prior `complete_case` attempt failed on a missing hardcoded nonstandard Price List. The current contract resolves the veterinary selling price list centrally and defaults it to `Standard Selling`. Strict rollback behavior remains the same for missing billing prerequisites.
