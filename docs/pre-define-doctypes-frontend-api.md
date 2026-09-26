# Pre Define Item / Pre Define Stock Entry — Frontend API Reference

Field-level reference for the two staging doctypes covered narratively in
[`pre-define-staging-doctypes.md`](./pre-define-staging-doctypes.md) (read that one
first for *why* these doctypes exist and the migration business rules — this doc is
only "what JSON do I send and what do I get back").

**No custom create endpoint exists yet for either doctype.** Everything below uses
Frappe's built-in generic REST resource API, which already works today with zero code
changes on any doctype the caller has permission for. If a purpose-built bulk-create
endpoint is added later (tolerant of partial-batch failures, resolving `Pre Define
Item` by `item_code` instead of by its generated name), it will be documented
separately and this doc will link to it — until then, this is the real integration
surface.

## Auth

Same convention as every other API in this app (see
[`agent-handoff-doctypes-api-functionalities.md`](./agent-handoff-doctypes-api-functionalities.md)):

```http
Authorization: token <api_key>:<api_secret>
```

## Base path and doctype-name encoding

Frappe's generic REST resource is `/api/resource/<Doctype Name>`. Doctype names with
spaces must be URL-encoded (`%20` or `+`):

```
/api/resource/Pre%20Define%20Item
/api/resource/Pre%20Define%20Stock%20Entry
```

Standard verbs on that path:

| Verb | Path | Does |
|---|---|---|
| `POST` | `/api/resource/<Doctype>` | Create one document. Body is JSON, keys are fieldnames. |
| `GET` | `/api/resource/<Doctype>/<name>` | Fetch one document by its name. |
| `GET` | `/api/resource/<Doctype>?filters=[["field","=","value"]]` | List/filter. Add `&fields=["name","item_code"]` to shape the response. |
| `PUT` | `/api/resource/<Doctype>/<name>` | Update fields on an existing document. |
| `DELETE` | `/api/resource/<Doctype>/<name>` | Delete. Not expected to be used by this flow. |

## Who is allowed to call this

Both doctypes currently grant `create` only to **System Manager** and **Stock
Manager** (plus Administrator, who always has full access). No guardian- or
frontend-facing role holds `create` on either doctype today.

```
Pre Define Item:         System Manager, Stock Manager  → create/read/write/delete/import
Pre Define Stock Entry:  System Manager, Stock Manager  → create/read/write/delete  (no import)
```

**This is a real integration blocker, not a formality**: whatever account/API key the
frontend uses to call these endpoints must hold one of those two roles. If the
frontend is meant to act as an ordinary staff user rather than a privileged one, that
needs a decision — either grant one of these roles to the relevant role profile, or
add a narrower Custom DocPerm, before wiring this up for real.

---

## `Pre Define Item`

Naming: `PRE-ITM-#####` (auto-generated on insert — you cannot choose it).
`title_field`: `arabic_name`. Importable via Desk Data Import (`allow_import: 1`).

| Field | Label | Type | Link target / options | Required | Default | Read-only |
|---|---|---|---|---|---|---|
| `arabic_name` | Arabic Name (الاسم) | Data | | **yes** | | |
| `english_name` | English Name | Data | | | | |
| `item_code` | Item Code (الكود) | Data | | | | |
| `group_arabic` | Group (المجموعة) | Data | | | | |
| `category` | Category | Data | | | | |
| `parent_item_group` | Parent Item Group | Link | `Item Group` | | `Pet Supplies` | |
| `stock_uom` | Stock UOM | Link | `UOM` | **yes** | `Nos` | |
| `standard_rate` | Standard Selling Rate | Currency | | | | |
| `valuation_rate` | Valuation Rate | Currency | | | | |
| `barcode` | Barcode | Data | | | | |
| `disabled` | Disabled | Check (0/1) | | | `0` | |
| `description` | Description | Small Text | | | | |
| `status` | Status | Select | `Draft` / `Migrated` / `Error` | yes | `Draft` | **yes** — system-managed |
| `item` | Item | Link | `Item` | | | **yes** — filled by migration |
| `item_group` | Item Group | Link | `Item Group` | | | **yes** — filled by migration |
| `migration_error` | Migration Error | Small Text | | | | **yes** |

Only `arabic_name` is hard-required by the schema, but `PreDefineItem.validate()`
additionally requires at least one of `item_code`, `english_name`, or `arabic_name` to
be non-empty (it becomes the effective Item code, max 140 chars), and rejects a
duplicate `item_code` against any other Pre Define Item row — this check runs in
Python on every save, not as a DB constraint, so a `POST` that reuses a code fails with
a normal validation error naming the conflicting row, not a generic "duplicate entry".

Don't send `status`, `item`, `item_group`, `migration_error`, or `name` — they're
system-managed and a create request should omit them entirely.

**Create:**
```http
POST /api/resource/Pre%20Define%20Item
Authorization: token <api_key>:<api_secret>
Content-Type: application/json

{
  "arabic_name": "فيتامين للقطط",
  "english_name": "Cat Vitamin Drops",
  "item_code": "SKU-10234",
  "category": "Supplements",
  "stock_uom": "Nos",
  "standard_rate": 8500,
  "valuation_rate": 6000,
  "barcode": "6291041012345"
}
```

**Response (201):**
```json
{
  "data": {
    "name": "PRE-ITM-00512",
    "arabic_name": "فيتامين للقطط",
    "item_code": "SKU-10234",
    "status": "Draft",
    "item": null,
    "item_group": null,
    ...
  }
}
```

**Look up what you already created** (e.g. to get the name for a stock-entry row
later, keyed by the `item_code` you know):
```http
GET /api/resource/Pre%20Define%20Item?filters=[["item_code","=","SKU-10234"]]&fields=["name","item_code","status"]
```

---

## `Pre Define Stock Entry` (+ child `Pre Define Stock Entry Item`)

Naming: naming series `PRE-STE-.YYYY.-` (e.g. `PRE-STE-2026-00031`, auto-generated).
`title_field`: `stock_entry_type`. **Not importable via Desk Data Import** —
`allow_import` is unset and the permission rows don't grant `import`, unlike Pre
Define Item. The REST path below is the only bulk-friendly way in today.

| Field | Label | Type | Link target / options | Required | Default | Read-only |
|---|---|---|---|---|---|---|
| `naming_series` | Series | Select | `PRE-STE-.YYYY.-` | yes | `PRE-STE-.YYYY.-` | |
| `stock_entry_type` | Stock Entry Type | Link | `Stock Entry Type` | **yes** | | |
| `purpose` | Purpose | Select | (Stock Entry purpose list) | | | **yes** — fetched from `stock_entry_type.purpose` |
| `company` | Company | Link | `Company` | **yes** | | |
| `posting_date` | Posting Date | Date | | yes | Today | |
| `posting_time` | Posting Time | Time | | yes | Now | |
| `from_warehouse` | Default Source Warehouse | Link | `Warehouse` | | | |
| `to_warehouse` | Default Target Warehouse | Link | `Warehouse` | | | |
| `items` | Items | Table | `Pre Define Stock Entry Item` | **yes**, ≥1 row | | |
| `remarks` | Remarks | Text | | | | |
| `status` | Status | Select | `Draft` / `Migrated` / `Error` | yes | `Draft` | **yes** |
| `stock_entry` | Stock Entry | Link | `Stock Entry` | | | **yes** — filled by migration |
| `migration_error` | Migration Error | Small Text | | | | **yes** |

`stock_entry_type` must already exist **and** have a non-empty `purpose` on that
record — `validate()` throws hard if it doesn't. `company` self-defaults to the site's
global default company if you omit it, but only if it's genuinely absent from the
payload (an empty string is not treated the same as omitted in every path — send it
explicitly if you know it).

### Child table: `Pre Define Stock Entry Item`

Sent as a plain JSON array under the parent's `items` key — no separate endpoint, no
`doctype` key needed per row, Frappe infers the child doctype from the parent's Table
field.

| Field | Label | Type | Link target / options | Required | Default | Read-only |
|---|---|---|---|---|---|---|
| `pre_item` | Pre Define Item | Link | `Pre Define Item` | **yes** | | |
| `item_name` | Item Name | Data | | | | **yes** — fetched from `pre_item.arabic_name` |
| `pre_item_code` | Code | Data | | | | **yes** — fetched from `pre_item.item_code` |
| `item_code` | Item | Link | `Item` | | | **yes** — fetched from `pre_item.item` (blank until migrated) |
| `qty` | Qty | Float | | **yes** | | |
| `uom` | UOM | Link | `UOM` | yes | | fills from `pre_item.stock_uom` if left blank |
| `conversion_factor` | Conversion Factor | Float | | yes | `1` | |
| `s_warehouse` | Source Warehouse | Link | `Warehouse` | per purpose, see below | | |
| `t_warehouse` | Target Warehouse | Link | `Warehouse` | per purpose, see below | | |
| `basic_rate` | Basic Rate | Currency | (company currency) | | falls back to `pre_item.valuation_rate` at migration time | |
| `batch_no` | Batch No (text) | Data | free text, **not** a Batch link | | | |
| `expiry_date` | Expiry Date | Date | | | | |

**`pre_item` is the sharp edge**: it must be the *already-generated name* of a saved
Pre Define Item row (e.g. `"PRE-ITM-00512"`), not its `item_code`. There is no
server-side resolution today — create the Pre Define Item first, read back its `name`
(via the lookup call above, filtered by the `item_code` you already know), then use
that name here. Doing this for many rows means: one `POST` per Pre Define Item, then
one `GET` filtered by `item_code in [...]` to fetch all the generated names at once,
then the Stock Entry `POST` with those names filled in.

**Warehouse requirement depends on `purpose`** (same rule ERPNext's own Stock Entry
uses): Material Receipt needs a target only; Material Issue, Material Consumption for
Manufacture, Send to Subcontractor, and Subcontracting Delivery need a source only;
Material Transfer (including for Manufacture) needs both, and they must differ; every
other purpose needs at least one. A row's own `s_warehouse`/`t_warehouse` falls back
to the parent's `from_warehouse`/`to_warehouse` if left blank. Any warehouse used must
exist, be a non-group leaf, and belong to the entry's `company`.

**Create:**
```http
POST /api/resource/Pre%20Define%20Stock%20Entry
Authorization: token <api_key>:<api_secret>
Content-Type: application/json

{
  "stock_entry_type": "Material Receipt",
  "company": "Alkokh Veterinary Clinic",
  "to_warehouse": "Main Store - AVC",
  "items": [
    {
      "pre_item": "PRE-ITM-00512",
      "qty": 24,
      "basic_rate": 6000,
      "batch_no": "B-2026-014",
      "expiry_date": "2027-06-30"
    }
  ]
}
```

**Response (201):**
```json
{
  "data": {
    "name": "PRE-STE-2026-00031",
    "status": "Draft",
    "purpose": "Material Receipt",
    "stock_entry": null,
    "items": [
      {
        "pre_item": "PRE-ITM-00512",
        "item_name": "فيتامين للقطط",
        "pre_item_code": "SKU-10234",
        "item_code": null,
        "qty": 24,
        "uom": "Nos",
        "conversion_factor": 1
      }
    ],
    ...
  }
}
```

`item_code` on the child row stays `null` until the row's Pre Define Item is migrated
— it fills in automatically once that happens, no re-send needed.

---

## After creating: migrating into real records

Creating a staging row (above) never touches real ERPNext data — it only exists in
`Pre Define Item` / `Pre Define Stock Entry` until migrated. Migration is a separate,
already-existing whitelisted call, documented in full (with all its business rules —
Item Group reuse, warehouse validation, batch-as-remarks, etc.) in
[`pre-define-staging-doctypes.md`](./pre-define-staging-doctypes.md#bulk-migration):

```
POST /api/method/pet_app.pet_app.doctype.pre_define_item.pre_define_item.migrate_pre_define_items
POST /api/method/pet_app.pet_app.doctype.pre_define_stock_entry.pre_define_stock_entry.migrate_pre_define_stock_entries
```

Both take `names` (a JSON array of the staging doc names to migrate) and return
`{"migrated": [...], "failed": {name: message}}`. Batches over 50 names run in the
background and return `{"queued": true, "count": n}` instead.

## Status lifecycle (both doctypes)

```
Draft ──(migrate call succeeds)──▶ Migrated  (locked: further edits are refused)
Draft ──(migrate call fails)─────▶ Error     (migration_error set; any manual save resets to Draft)
```

## Uploading the actual spreadsheet data

This doc defines the API surface only. The Excel-sourced data itself still needs a
column → field mapping once the real file (not a screenshot) is available — that's a
separate step, either a one-time script that calls the `POST` endpoints above per row,
or a small upload screen in the frontend that does the same.
