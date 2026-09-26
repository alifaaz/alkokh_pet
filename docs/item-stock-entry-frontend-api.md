# Item & Stock Entry — Frontend JSON Contract

Every payload and response shape the frontend needs to create, read, update and submit
**Item** and **Stock Entry** on this site.

Companion document: [`warehouse-stock-frontend-readme.md`](warehouse-stock-frontend-readme.md)
covers warehouses, `Bin` stock reads and the end-to-end receipt/issue/transfer *flows*.
This document is the **field-level JSON reference** for the two doctypes themselves.

No custom backend API is required for either doctype. Both are standard Frappe REST
resources. The only custom endpoints are the barcode generator (section 4.6).

---

## 0. Transport

```http
POST   {{base_url}}/api/resource/{DocType}          create
GET    {{base_url}}/api/resource/{DocType}          list
GET    {{base_url}}/api/resource/{DocType}/{name}   read one
PUT    {{base_url}}/api/resource/{DocType}/{name}   update
DELETE {{base_url}}/api/resource/{DocType}/{name}   delete
```

Headers on every request:

```http
Content-Type: application/json
Accept: application/json
Authorization: token {{api_key}}:{{api_secret}}
```

Session-cookie auth works identically — same endpoints, send the session cookie instead.

Every successful `/api/resource` call wraps the document in `data`:

```json
{ "data": { "...the document..." } }
```

List calls return an array:

```json
{ "data": [ { "...row..." }, { "...row..." } ] }
```

Errors come back as an HTTP 4xx/5xx with:

```json
{
  "exception": "frappe.exceptions.ValidationError: Row 1: Qty is mandatory",
  "exc_type": "ValidationError",
  "_server_messages": "[\"{\\\"message\\\": \\\"Row 1: Qty is mandatory\\\"}\"]"
}
```

Read the human-readable text out of `_server_messages` (it is a JSON string containing a
JSON array of JSON strings — parse it twice). Never show `exception` raw to an operator.

---

## 1. Site constants (this deployment)

Do **not** hardcode these — read them from the API — but these are the current values, so
the frontend can be built and tested against real names.

| Thing | Value |
|---|---|
| Company | `Kokh-vet` (abbr `K`) |
| Currency | `IQD` |
| Default warehouse | `المخزن الرئيسي - K` |
| Valuation method | `FIFO` |
| Negative stock | **not allowed** (`allow_negative_stock = 0`) |
| Item naming | **by Item Code** — `name` is whatever you send as `item_code` |
| Branches | `main`, `hotel` |

Leaf warehouses (usable for stock operations):

```json
[
  "المخزن الرئيسي - K",
  "رفوف الصيدلية - K",
  "ثلاجة اللقاحات - K",
  "مخزن التينادور - K",
  "مخزن المرتجعات - K",
  "مخزون في الطريق - K",
  "Stores - K"
]
```

Price lists:

```json
[
  { "name": "Standard Buying",  "buying": 1, "selling": 0 },
  { "name": "Standard Selling", "buying": 0, "selling": 1 },
  { "name": "Clinic",           "buying": 0, "selling": 1 }
]
```

Warehouse names contain Arabic text and spaces. **Always URL-encode** them in query
strings and in `/api/resource/Warehouse/{name}` paths.

---

## 2. Document state (`docstatus`)

| `docstatus` | Meaning | Item | Stock Entry |
|---|---|---|---|
| `0` | Draft | Items always stay 0 | Editable, **no stock movement yet** |
| `1` | Submitted | n/a | Immutable, **stock has moved** |
| `2` | Cancelled | n/a | Reversed |

`Item` is not a submittable doctype — it has no draft/submit cycle. `Stock Entry` is:
creating it changes nothing until you submit it.

---

## 3. Item — field reference

`Item` autonames from `item_code`, so **`name === item_code`**. Sending
`"item_code": "Cefovet 500"` produces a document at
`/api/resource/Item/Cefovet%20500`.

### 3.1 Required on create

| Field | Type | Notes |
|---|---|---|
| `item_code` | Data | Becomes `name`. Must be unique. |
| `item_group` | Link → Item Group | Must be a **leaf** group (`is_group = 0`). |
| `stock_uom` | Link → UOM | The base unit. Cannot be changed once stock exists. |

### 3.2 Commonly used optional fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `item_name` | Data | = `item_code` | Display name. |
| `description` | Text Editor | — | HTML allowed. |
| `disabled` | Check | `0` | Use this instead of deleting. |
| `is_stock_item` | Check | `1` | `0` = service item, **cannot** appear on a Stock Entry. |
| `is_sales_item` | Check | `1` | |
| `is_purchase_item` | Check | `1` | |
| `is_fixed_asset` | Check | `0` | |
| `brand` | Link → Brand | — | |
| `image` | Attach Image | — | File URL, e.g. `/files/x.png`. |
| `opening_stock` | Float | — | **Create only.** Ignored on update. |
| `valuation_rate` | Currency | — | Seed rate; needed with `opening_stock`. |
| `standard_rate` | Currency | — | Creates an Item Price on the default price list. |
| `has_batch_no` | Check | `0` | Locks after transactions exist. |
| `create_new_batch` | Check | `0` | Only meaningful with `has_batch_no`. |
| `batch_number_series` | Data | — | e.g. `BATCH-.####`. |
| `has_expiry_date` | Check | `0` | Requires `has_batch_no`. |
| `shelf_life_in_days` | Int | — | |
| `has_serial_no` | Check | `0` | Locks after transactions exist. |
| `serial_no_series` | Data | — | e.g. `SN-.#####`. |
| `end_of_life` | Date | `2099-12-31` | |
| `warranty_period` | Data | — | In days, as a string. |
| `weight_per_unit` / `weight_uom` | Float / Link | — | |
| `min_order_qty`, `safety_stock`, `lead_time_days` | Float/Float/Int | — | |
| `purchase_uom`, `sales_uom` | Link → UOM | — | Need a matching `uoms` row. |
| `country_of_origin` | Link → Country | — | |
| `valuation_method` | Select | site default (FIFO) | `""`, `FIFO`, `Moving Average`, `LIFO`. |

**Read-only — never send these:** `last_purchase_rate`, `default_bom`,
`total_projected_qty`, `variant_of`, `default_item_manufacturer`,
`default_manufacturer_part_no`.

### 3.3 pet_app custom fields on Item

| Field | Type | Notes |
|---|---|---|
| `arabic_name` | Data | Arabic display name. Free text. |
| `custom_barcode` | Data | **Unique across all Items.** Server trims whitespace and stores blank as `null`. |
| `custom_generate_barcode` | Check | **A request, not a state.** Set to `1` on a save to mint a barcode; the server clears it back to `0` every time. Always reads back as `0`. |
| `custom_store_published` | Check | Visible in the storefront. |

Because `custom_barcode` is trimmed server-side, a scan-gun terminator (`CR`/`LF`/tab) or
a pasted trailing space is harmless — but the frontend should still trim before an
exact-match lookup, since the stored value has no padding.

### 3.4 Child tables on Item

**`barcodes`** — child doctype `Item Barcode` (separate from `custom_barcode`; this is
ERPNext's own multi-barcode table):

```json
{ "barcode": "6221031492013", "barcode_type": "EAN-13", "uom": "Nos" }
```

`barcode_type` options: `EAN`, `UPC-A`, `CODE-39`, `EAN-13`, `EAN-8`, `GS1`, `GTIN`,
`GTIN-14`, `ISBN`, `ISBN-10`, `ISBN-13`, `ISSN`, `JAN`, `PZN`, `UPC`. Leave it empty to
skip check-digit validation.

**`uoms`** — child doctype `UOM Conversion Detail`. The `stock_uom` row is added
automatically with factor `1`:

```json
{ "uom": "Box", "conversion_factor": 10 }
```

**`item_defaults`** — child doctype `Item Default`, one row per company:

```json
{
  "company": "Kokh-vet",
  "default_warehouse": "رفوف الصيدلية - K",
  "default_price_list": "Clinic",
  "income_account": null,
  "expense_account": null,
  "buying_cost_center": null,
  "selling_cost_center": null,
  "default_supplier": null
}
```

**`reorder_levels`** — child doctype `Item Reorder`:

```json
{
  "warehouse": "رفوف الصيدلية - K",
  "warehouse_reorder_level": 20,
  "warehouse_reorder_qty": 100,
  "material_request_type": "Purchase"
}
```

`material_request_type` options: `Purchase`, `Transfer`, `Material Issue`, `Manufacture`.

---

## 4. Item — requests

### 4.1 Create, minimal

```http
POST {{base_url}}/api/resource/Item
```

```json
{
  "item_code": "Cefovet 500",
  "item_name": "Cefovet 500 mg injection",
  "item_group": "Antibiotics",
  "stock_uom": "Vial"
}
```

That is genuinely all that is required. Everything else has a server default.

### 4.2 Create, full clinic/pharmacy item

```json
{
  "item_code": "Cefovet 500",
  "item_name": "Cefovet 500 mg injection",
  "arabic_name": "سيفوفيت ٥٠٠ ملغم حقن",
  "item_group": "Antibiotics",
  "stock_uom": "Vial",
  "description": "<p>Cefovet 500 mg powder for injection, per vial.</p>",
  "is_stock_item": 1,
  "is_sales_item": 1,
  "is_purchase_item": 1,
  "disabled": 0,
  "custom_store_published": 0,
  "custom_generate_barcode": 1,
  "has_batch_no": 1,
  "create_new_batch": 1,
  "batch_number_series": "CEF-.#####",
  "has_expiry_date": 1,
  "shelf_life_in_days": 730,
  "has_serial_no": 0,
  "min_order_qty": 10,
  "safety_stock": 20,
  "lead_time_days": 14,
  "weight_per_unit": 0.02,
  "weight_uom": "Kg",
  "country_of_origin": "Iraq",
  "uoms": [
    { "uom": "Vial", "conversion_factor": 1 },
    { "uom": "Box", "conversion_factor": 10 }
  ],
  "item_defaults": [
    {
      "company": "Kokh-vet",
      "default_warehouse": "رفوف الصيدلية - K",
      "default_price_list": "Clinic"
    }
  ],
  "reorder_levels": [
    {
      "warehouse": "رفوف الصيدلية - K",
      "warehouse_reorder_level": 20,
      "warehouse_reorder_qty": 100,
      "material_request_type": "Purchase"
    }
  ]
}
```

**Response** (abridged — the real response contains every field of the doctype):

```json
{
  "data": {
    "name": "Cefovet 500",
    "owner": "operator@kokh.vet",
    "creation": "2026-09-04 11:20:41.882713",
    "modified": "2026-09-04 11:20:41.882713",
    "modified_by": "operator@kokh.vet",
    "docstatus": 0,
    "idx": 0,
    "item_code": "Cefovet 500",
    "item_name": "Cefovet 500 mg injection",
    "arabic_name": "سيفوفيت ٥٠٠ ملغم حقن",
    "item_group": "Antibiotics",
    "stock_uom": "Vial",
    "disabled": 0,
    "is_stock_item": 1,
    "has_batch_no": 1,
    "create_new_batch": 1,
    "has_expiry_date": 1,
    "has_serial_no": 0,
    "custom_barcode": "K-000418",
    "custom_generate_barcode": 0,
    "custom_store_published": 0,
    "valuation_method": "",
    "end_of_life": "2099-12-31",
    "uoms": [
      { "name": "a1b2c3d4e5", "parent": "Cefovet 500", "parenttype": "Item",
        "parentfield": "uoms", "idx": 1, "uom": "Vial", "conversion_factor": 1 },
      { "name": "f6g7h8i9j0", "parent": "Cefovet 500", "parenttype": "Item",
        "parentfield": "uoms", "idx": 2, "uom": "Box", "conversion_factor": 10 }
    ],
    "item_defaults": [
      { "name": "k1l2m3n4o5", "parent": "Cefovet 500", "parenttype": "Item",
        "parentfield": "item_defaults", "idx": 1, "company": "Kokh-vet",
        "default_warehouse": "رفوف الصيدلية - K", "default_price_list": "Clinic" }
    ],
    "barcodes": [],
    "reorder_levels": [
      { "name": "p6q7r8s9t0", "parent": "Cefovet 500", "parenttype": "Item",
        "parentfield": "reorder_levels", "idx": 1,
        "warehouse": "رفوف الصيدلية - K", "warehouse_reorder_level": 20,
        "warehouse_reorder_qty": 100, "material_request_type": "Purchase" }
    ],
    "doctype": "Item"
  }
}
```

Note `custom_generate_barcode` came back `0` with `custom_barcode` filled in — that is the
flag doing its job (section 4.6).

### 4.3 List items

```http
GET {{base_url}}/api/resource/Item
      ?fields=["name","item_name","arabic_name","item_group","stock_uom","custom_barcode","image","disabled","has_batch_no"]
      &filters=[["disabled","=",0],["is_stock_item","=",1]]
      &order_by=modified desc
      &limit_page_length=20
      &limit_start=0
```

Search by name or Arabic name (`or_filters`):

```http
GET {{base_url}}/api/resource/Item
      ?fields=["name","item_name","arabic_name","item_group","stock_uom"]
      &or_filters=[["item_name","like","%سيفو%"],["arabic_name","like","%سيفو%"],["name","like","%سيفو%"]]
      &limit_page_length=20
      &limit_start=0
```

Lookup by barcode — exact match, expect 0 or 1 row (the column is unique):

```http
GET {{base_url}}/api/resource/Item?fields=["name","item_name","stock_uom"]&filters=[["custom_barcode","=","K-000418"]]
```

Total count for a pager:

```http
GET {{base_url}}/api/method/frappe.client.get_count?doctype=Item&filters=[["disabled","=",0]]
```

```json
{ "message": 374 }
```

**Always paginate.** There are 374 items on this site today and the list grows.
`limit_page_length=0` (fetch-everything) is forbidden in the UI.

### 4.4 Update an item

```http
PUT {{base_url}}/api/resource/Item/Cefovet%20500
```

Send only what changed:

```json
{
  "item_name": "Cefovet 500 mg injection (vial)",
  "arabic_name": "سيفوفيت ٥٠٠ ملغم حقن",
  "safety_stock": 30
}
```

**Child tables are replace-all, not merge.** Sending `uoms` replaces the whole table, so
read the current rows, modify the array, and send it back in full — including each
existing row's `name` so the server updates rather than recreates it:

```json
{
  "uoms": [
    { "name": "a1b2c3d4e5", "uom": "Vial", "conversion_factor": 1 },
    { "name": "f6g7h8i9j0", "uom": "Box", "conversion_factor": 12 },
    { "uom": "Carton", "conversion_factor": 120 }
  ]
}
```

Rows omitted from the array are deleted. Rows without `name` are inserted.

### 4.5 Disable, don't delete

```http
PUT {{base_url}}/api/resource/Item/Cefovet%20500
```

```json
{ "disabled": 1 }
```

`DELETE` succeeds only for an item with no stock ledger entries, no prices and no links.
Anything that has ever moved will fail with a `LinkExistsError`. The UI should offer
**Disable**, and treat delete as an admin-only action for a mistyped item created minutes
ago.

### 4.6 Barcode generation (custom endpoints)

Two-step, and the split matters: a dialog that is opened and closed must not burn a
number.

**Step 1 — preview (writes nothing):**

```http
GET {{base_url}}/api/method/pet_app.api.item_barcode.peek_next_item_barcode
```

```json
{
  "message": {
    "ok": true,
    "data": { "barcode": "K-000419", "is_preview": true },
    "meta": {
      "preview": true,
      "reserved": false,
      "generate_flag_field": "custom_generate_barcode",
      "series_key": "..."
    }
  }
}
```

The value is **provisional**. Show it greyed/italic with "will be assigned on save". Two
operators peeking at once see the same number; the first to save gets it.

**Step 2 — mint it, by saving the Item with the flag set:**

```json
{ "custom_generate_barcode": 1 }
```

The saved document's `custom_barcode` is the authority — read it from the PUT/POST
response, not from the preview. If the item already has a barcode the flag is a no-op and
the server returns a message saying so.

**Bulk assignment (POST only — a GET is rejected before it runs):**

```http
POST {{base_url}}/api/method/pet_app.api.item_barcode.generate_item_barcodes
```

```json
{ "items": ["Cefovet 500", "Omeprazole", "Oral spray"] }
```

Per-item conditions (already has one / not found / not permitted) come back as skipped
rows and do not abort the batch. A structural failure rolls the entire request back.

### 4.7 Item price

Prices live in a separate doctype — `Item.standard_rate` only seeds one on create.

```http
POST {{base_url}}/api/resource/Item Price
```

```json
{
  "item_code": "Cefovet 500",
  "price_list": "Clinic",
  "uom": "Vial",
  "price_list_rate": 7500,
  "currency": "IQD",
  "selling": 1,
  "buying": 0,
  "valid_from": "2026-09-04"
}
```

Read the current selling price:

```http
GET {{base_url}}/api/resource/Item Price
      ?fields=["name","price_list","uom","price_list_rate","currency","valid_from","valid_upto"]
      &filters=[["item_code","=","Cefovet 500"],["selling","=",1]]
      &order_by=valid_from desc
```

`Item Price` is hash-named, so keep `name` from the list response when you need to `PUT`
an update.

---

## 5. Stock Entry — field reference

This is the only doctype that moves stock. `Bin` is read-only; never write to it.

### 5.1 Header — required

| Field | Type | Notes |
|---|---|---|
| `stock_entry_type` | Link → Stock Entry Type | Drives everything. See 5.2. |
| `company` | Link → Company | `Kokh-vet`. |
| `items` | Table → Stock Entry Detail | At least one row. |

`naming_series` is required by the schema but defaults to `MAT-STE-.YYYY.-` — **do not
send it**. `purpose` is read-only and fetched from `stock_entry_type`; **do not send it**
either.

### 5.2 `stock_entry_type` values on this site

| Type | Needs `from_warehouse` | Needs `to_warehouse` |
|---|---|---|
| `Material Receipt` | no | **yes** (or per-row `t_warehouse`) |
| `Material Issue` | **yes** (or per-row `s_warehouse`) | no |
| `Material Transfer` | **yes** | **yes** |
| `Manufacture` | — | — |
| `Repack` | — | — |
| `Disassemble` | — | — |
| `Send to Subcontractor` | — | — |
| `Material Transfer for Manufacture` | — | — |
| `Material Consumption for Manufacture` | — | — |
| `Receive from Customer` | — | — |
| `Return Raw Material to Customer` | — | — |
| `Subcontracting Delivery` | — | — |
| `Subcontracting Return` | — | — |

The clinic UI only needs the first three. The rest exist because ERPNext ships them —
filter the picker to the three unless a manufacturing screen is being built.

Read the list live rather than hardcoding:

```http
GET {{base_url}}/api/resource/Stock Entry Type?fields=["name","purpose"]&filters=[["name","in",["Material Receipt","Material Issue","Material Transfer"]]]
```

### 5.3 Header — optional

| Field | Type | Notes |
|---|---|---|
| `posting_date` | Date | Defaults to today. Needs `set_posting_time: 1` to stick. |
| `posting_time` | Time | Same. |
| `set_posting_time` | Check | **Send `1` whenever you send a date/time**, otherwise the server overwrites both with "now". |
| `from_warehouse` | Link → Warehouse | Header default for every row's `s_warehouse`. |
| `to_warehouse` | Link → Warehouse | Header default for every row's `t_warehouse`. |
| `project` | Link → Project | |
| `remarks` | Text | Free text; show it in the UI, operators use it as the reason. |
| `is_opening` | Select | `No` / `Yes`. |
| `supplier` | Link → Supplier | Receipts only. |
| `apply_putaway_rule` | Check | Leave `0` unless putaway rules are configured. |
| `add_to_transit` | Check | Fetched from the type; leave it alone. |

### 5.4 Header — pet_app custom fields

| Field | Type | Notes |
|---|---|---|
| `branch` | Link → Branch | `main` or `hotel`. **Not auto-stamped** — Stock Entry is deliberately outside the branch-scoping set, so if you want it recorded, the frontend must send it. |
| `cashier` | Link → Employee | |
| `department` | Link → Department | |
| `location` | Link → Location | |
| `custom_sales_order` | Link → Sales Order | |

All five are plain optional links with no server-side default.

### 5.5 Row fields (`items[]`, child doctype `Stock Entry Detail`)

Minimum per row:

| Field | Type | Notes |
|---|---|---|
| `item_code` | Link → Item | Must be `is_stock_item = 1`, else "X is not a stock Item". |
| `qty` | Float | Must be **> 0**. Direction comes from the warehouses, never from a negative qty. |
| `s_warehouse` | Link → Warehouse | Source. Required for Issue/Transfer if not set on the header. |
| `t_warehouse` | Link → Warehouse | Target. Required for Receipt/Transfer if not set on the header. |

`uom`, `conversion_factor` and `stock_uom` are marked mandatory in the schema but the
server fills them from the Item during validation when you leave them out — along with
`description`, `expense_account`, `cost_center` and `barcode`. **Send `item_code`, `qty`
and the warehouses; let the server do the rest.** Only send `uom` when the operator picked
a non-stock unit, and then send `conversion_factor` with it.

Other row fields worth sending:

| Field | Type | Notes |
|---|---|---|
| `basic_rate` | Currency | Valuation rate. Required on a **receipt** for an item with no prior valuation. |
| `set_basic_rate_manually` | Check | Send `1` alongside a hand-typed `basic_rate` on a transfer. |
| `allow_zero_valuation_rate` | Check | Escape hatch for a zero-value receipt (samples, donations). |
| `batch_no` | Link → Batch | With `use_serial_batch_fields: 1`. |
| `serial_no` | Text | Newline-separated. With `use_serial_batch_fields: 1`. |
| `use_serial_batch_fields` | Check | **Send `1`** to use the plain `batch_no`/`serial_no` fields instead of building a Serial and Batch Bundle. |
| `expense_account` | Link → Account | Override; normally server-filled. |
| `cost_center` | Link → Cost Center | Override; normally server-filled. |
| `project` | Link → Project | |

**Read-only in the response — never send:** `basic_amount`, `amount`, `valuation_rate`,
`transfer_qty`, `actual_qty`, `additional_cost`, `stock_uom`, `item_name`,
`transferred_qty`, `putaway_rule`.

---

## 6. Stock Entry — requests

### 6.1 Material Receipt (add stock)

```http
POST {{base_url}}/api/resource/Stock Entry
```

```json
{
  "stock_entry_type": "Material Receipt",
  "company": "Kokh-vet",
  "to_warehouse": "رفوف الصيدلية - K",
  "posting_date": "2026-09-04",
  "posting_time": "11:30:00",
  "set_posting_time": 1,
  "branch": "main",
  "remarks": "Purchase delivery, invoice 4471",
  "items": [
    {
      "item_code": "Cefovet 500",
      "qty": 50,
      "t_warehouse": "رفوف الصيدلية - K",
      "basic_rate": 5200,
      "use_serial_batch_fields": 1,
      "batch_no": "CEF-00007"
    },
    {
      "item_code": "Omeprazole",
      "qty": 20,
      "t_warehouse": "رفوف الصيدلية - K",
      "basic_rate": 3100
    }
  ]
}
```

`basic_rate` is what makes a receipt succeed. An item with no valuation history and no
`basic_rate` throws — see 7.3.

### 6.2 Material Issue (remove stock)

```json
{
  "stock_entry_type": "Material Issue",
  "company": "Kokh-vet",
  "from_warehouse": "رفوف الصيدلية - K",
  "set_posting_time": 0,
  "branch": "main",
  "remarks": "Expired stock written off",
  "items": [
    {
      "item_code": "Cefovet 500",
      "qty": 3,
      "s_warehouse": "رفوف الصيدلية - K",
      "use_serial_batch_fields": 1,
      "batch_no": "CEF-00007"
    }
  ]
}
```

No `basic_rate` on an issue — the server values the outgoing stock by FIFO.

### 6.3 Material Transfer (move stock)

```json
{
  "stock_entry_type": "Material Transfer",
  "company": "Kokh-vet",
  "from_warehouse": "المخزن الرئيسي - K",
  "to_warehouse": "ثلاجة اللقاحات - K",
  "set_posting_time": 0,
  "branch": "main",
  "remarks": "Vaccine restock to fridge",
  "items": [
    {
      "item_code": "Micoshield",
      "qty": 10,
      "s_warehouse": "المخزن الرئيسي - K",
      "t_warehouse": "ثلاجة اللقاحات - K"
    }
  ]
}
```

`s_warehouse` and `t_warehouse` must differ on every row.

### 6.4 Create response (draft)

```json
{
  "data": {
    "name": "MAT-STE-2026-00027",
    "owner": "operator@kokh.vet",
    "creation": "2026-09-04 11:30:12.114508",
    "modified": "2026-09-04 11:30:12.114508",
    "docstatus": 0,
    "naming_series": "MAT-STE-.YYYY.-",
    "stock_entry_type": "Material Receipt",
    "purpose": "Material Receipt",
    "company": "Kokh-vet",
    "posting_date": "2026-09-04",
    "posting_time": "11:30:00",
    "set_posting_time": 1,
    "to_warehouse": "رفوف الصيدلية - K",
    "from_warehouse": null,
    "branch": "main",
    "cashier": null,
    "department": null,
    "location": null,
    "custom_sales_order": null,
    "remarks": "Purchase delivery, invoice 4471",
    "total_incoming_value": 322000,
    "total_outgoing_value": 0,
    "value_difference": 322000,
    "total_amount": 322000,
    "total_additional_costs": 0,
    "per_transferred": 0,
    "items": [
      {
        "name": "u1v2w3x4y5",
        "parent": "MAT-STE-2026-00027",
        "parenttype": "Stock Entry",
        "parentfield": "items",
        "idx": 1,
        "item_code": "Cefovet 500",
        "item_name": "Cefovet 500 mg injection",
        "item_group": "Antibiotics",
        "s_warehouse": null,
        "t_warehouse": "رفوف الصيدلية - K",
        "qty": 50,
        "uom": "Vial",
        "stock_uom": "Vial",
        "conversion_factor": 1,
        "transfer_qty": 50,
        "basic_rate": 5200,
        "basic_amount": 260000,
        "additional_cost": 0,
        "amount": 260000,
        "valuation_rate": 5200,
        "actual_qty": 0,
        "batch_no": "CEF-00007",
        "serial_no": null,
        "use_serial_batch_fields": 1,
        "serial_and_batch_bundle": null,
        "expense_account": "603790 - تسويات المخزون / فروقات الجرد - K",
        "cost_center": "الإدارة - K",
        "allow_zero_valuation_rate": 0,
        "docstatus": 0
      }
    ],
    "doctype": "Stock Entry"
  }
}
```

**Stock has not moved yet.** `docstatus` is `0` and `actual_qty` is still the pre-entry
balance.

### 6.5 Update a draft

```http
PUT {{base_url}}/api/resource/Stock Entry/MAT-STE-2026-00027
```

```json
{
  "remarks": "Purchase delivery, invoice 4471 (corrected)",
  "items": [
    { "name": "u1v2w3x4y5", "item_code": "Cefovet 500", "qty": 48,
      "t_warehouse": "رفوف الصيدلية - K", "basic_rate": 5200 }
  ]
}
```

Same replace-all rule as Item child tables: send every row you want to keep, with its
`name`.

Drafts only. A `PUT` on a submitted entry fails with `UpdateAfterSubmitError`.

### 6.6 Submit — this is what moves stock

```http
PUT {{base_url}}/api/resource/Stock Entry/MAT-STE-2026-00027
```

```json
{ "docstatus": 1 }
```

Equivalent, and preferable because it runs the full submit lifecycle explicitly:

```http
POST {{base_url}}/api/method/frappe.client.submit
```

```json
{
  "doc": {
    "doctype": "Stock Entry",
    "name": "MAT-STE-2026-00027"
  }
}
```

After a successful submit, `docstatus` is `1` and `Bin.actual_qty` for that item+warehouse
has changed. **Re-read `Bin` after submit — never optimistically update the stock number
in the frontend from the qty you sent.**

```http
GET {{base_url}}/api/resource/Bin?fields=["item_code","warehouse","actual_qty","stock_value","valuation_rate"]&filters=[["item_code","=","Cefovet 500"],["warehouse","=","رفوف الصيدلية - K"]]
```

### 6.7 Cancel

```json
{ "docstatus": 2 }
```

Cancelling writes the reversing ledger entries. A cancelled entry cannot be re-submitted —
use **Amend** (a new draft with `amended_from` set to the cancelled name):

```json
{
  "stock_entry_type": "Material Receipt",
  "company": "Kokh-vet",
  "amended_from": "MAT-STE-2026-00027",
  "to_warehouse": "رفوف الصيدلية - K",
  "items": [ { "item_code": "Cefovet 500", "qty": 48, "t_warehouse": "رفوف الصيدلية - K", "basic_rate": 5200 } ]
}
```

### 6.8 List stock entries

```http
GET {{base_url}}/api/resource/Stock Entry
      ?fields=["name","stock_entry_type","posting_date","posting_time","from_warehouse","to_warehouse","branch","total_amount","docstatus","remarks"]
      &filters=[["docstatus","=",1],["posting_date",">=","2026-09-01"]]
      &order_by=posting_date desc,posting_time desc
      &limit_page_length=20
      &limit_start=0
```

Filter by branch, warehouse or type by adding clauses to `filters`. To list the *rows*
(item-level movement) instead of the entries, query the child table directly:

```http
GET {{base_url}}/api/resource/Stock Entry Detail
      ?parent=Stock Entry
      &fields=["parent","item_code","qty","uom","s_warehouse","t_warehouse","basic_rate","amount"]
      &filters=[["item_code","=","Cefovet 500"],["docstatus","=",1]]
      &order_by=creation desc
      &limit_page_length=20
```

The `parent=Stock Entry` query parameter is mandatory when querying a child doctype.

---

## 7. Validation rules and errors

### 7.1 Direction is set by warehouses, never by sign

```text
t_warehouse only  → stock in
s_warehouse only  → stock out
both              → transfer
```

A negative `qty` is rejected: *"Row 1: The item X, quantity must be positive number"*.

### 7.2 Insufficient stock

```json
{ "exc_type": "NegativeStockError" }
```

Message: *"Negative stock error: Not enough stock for item X in warehouse Y"*.

Negative stock is disabled on this site, so **check `Bin.actual_qty` before enabling the
submit button** on an issue or a transfer, and show the available quantity next to the
input.

### 7.3 No valuation rate

Message: *"Valuation Rate required for Item {0} at row {1}"*.

Happens on a receipt for an item that has never been valued. Fix, in order of preference:

1. Send `basic_rate` on the row (correct answer for a purchase).
2. Send `allow_zero_valuation_rate: 1` (samples, donations, zero-cost stock).
3. Set `valuation_rate` on the Item first.

### 7.4 Group warehouse selected

Message: *"Group node warehouses are not allowed to select for transactions"*.

Only offer `is_group = 0` warehouses in a picker:

```http
GET {{base_url}}/api/resource/Warehouse?fields=["name","warehouse_name","company"]&filters=[["is_group","=",0],["disabled","=",0],["company","=","Kokh-vet"]]&limit_page_length=0
```

### 7.5 Non-stock item

Message: *"X is not a stock Item"*. Filter item pickers on Stock Entry screens with
`["is_stock_item","=",1]`.

### 7.6 Duplicate barcode

`custom_barcode` carries a UNIQUE index. A collision surfaces as a
`frappe.exceptions.UniqueValidationError` / `DuplicateEntryError`. Surface it as
*"That barcode already belongs to another item"* and offer to open the owning item —
look it up with the exact-match query in 4.3.

### 7.7 Backdating

`posting_date` in the past without `set_posting_time: 1` is silently overwritten with the
current timestamp. If the operator picked a date, always send `set_posting_time: 1`.

---

## 8. What the frontend must not do

```text
- Never write to Bin. It is a read-only projection of the stock ledger.
- Never write Stock Ledger Entry or GL Entry.
- Never send a negative qty to reverse a movement — cancel, or post the opposite entry.
- Never send `purpose` on a Stock Entry — it is fetched from stock_entry_type.
- Never send read-only fields (amount, valuation_rate, transfer_qty, actual_qty, ...).
- Never PATCH a child table partially — the array you send replaces the whole table.
- Never DELETE an Item that has ever moved. Set `disabled: 1`.
- Never trust a peeked barcode as final — read `custom_barcode` from the save response.
- Never optimistically update a stock figure. Re-read Bin after every submit.
- Never load a full list without limit_page_length / limit_start.
```

---

## 9. Roles

A user needs one of these to write. The frontend should hide, not just disable, actions
the session's roles do not permit.

| Doctype | Create/write roles (subset) |
|---|---|
| `Item` | `Item Manager`, `Items Create`, `Items Update`, `Stock Manager`, `Stock User`, `Warehouse Admin` |
| `Stock Entry` | `Stock Entries Create`, `Stock Entries Update`, `Stock Manager`, `Stock User`, `Warehouse Admin`, `Manufacturing User` |

Read-only roles exist alongside them (`Items Read`, `Stock Entries Read`,
`Warehouse Read`, `Stock Ledger Read`) — those sessions get a list-and-view UI with no
create button.

---

## 10. Frontend checklist

```text
[ ] Item create sends only item_code + item_group + stock_uom as required fields.
[ ] Item pickers on stock screens filter is_stock_item = 1 and disabled = 0.
[ ] Warehouse pickers filter is_group = 0 and URL-encode the Arabic names.
[ ] Barcode: peek to preview, save with custom_generate_barcode = 1 to mint.
[ ] Barcode lookup is an exact match on the trimmed value.
[ ] Child tables are sent whole, with each kept row's `name`.
[ ] Stock Entry is created as a draft, reviewed, then submitted as a separate call.
[ ] Receipts carry basic_rate (or allow_zero_valuation_rate).
[ ] Issues/transfers check Bin.actual_qty before enabling submit.
[ ] set_posting_time = 1 whenever a date or time is sent.
[ ] branch is sent explicitly — nothing stamps it on Stock Entry.
[ ] Bin is re-read after every submit.
[ ] Errors are read out of _server_messages, not `exception`.
[ ] Every list is paginated.
```
