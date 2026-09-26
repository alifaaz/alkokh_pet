# Pre Define Item & Pre Define Stock Entry (staging doctypes)

Two temporary doctypes in `pet_app` (module *Pet App*) that let items and stock movements be captured **before** the real `Item` / `Item Group` masters exist. Each row is later migrated, one click, into the real ERPNext tables and then locked.

| Staging doctype | Migrates to | Trigger |
|---|---|---|
| Pre Define Item | Item Group (created if missing) + Item | form button **Migrate to Item**, list action **Migrate selected** |
| Pre Define Stock Entry (+ child Pre Define Stock Entry Item) | **draft** Stock Entry (never auto-submitted) | form button **Migrate to Stock Entry**, list action **Migrate selected** |

Both have a `status` (Draft / Migrated / Error), a read-only link to the created document, and a `migration_error` text shown when status is Error. Any manual save of an Error row resets it to Draft. A Migrated row refuses further edits.

## Intake sheet → Pre Define Item

Import the Excel sheet through **Data Import → Pre Define Item** with this column mapping:

| Sheet column | Field | Notes |
|---|---|---|
| الاسم | `arabic_name` | required |
| الكود | `item_code` | becomes the Item name; unique among non-empty codes |
| المجموعة | `group_arabic` | Arabic name of the Item Group |
| english name | `english_name` | becomes `Item.item_name` (falls back to Arabic) |
| category | `category` | Item Group name (English) |

## Pre Define Item

- Naming `PRE-ITM-#####`, title = Arabic name, importable, tracks changes.
- Roles: System Manager, Stock Manager.

| Field | Type | Required | Default | Purpose |
|---|---|---|---|---|
| arabic_name | Data | yes | | الاسم → `Item.arabic_name` (custom field) |
| english_name | Data | | | → `Item.item_name` |
| item_code | Data | | | الكود → `Item.item_code`. If blank: English name, then Arabic name |
| group_arabic | Data | | | المجموعة → `Item Group.arabic_name` |
| category | Data | | | → `Item.item_group` (created as a leaf if missing) |
| parent_item_group | Link Item Group | | Pet Supplies | parent used only when the category has to be created |
| stock_uom | Link UOM | yes | Nos | → `Item.stock_uom` |
| standard_rate | Currency | | | → `Item.standard_rate` |
| valuation_rate | Currency | | | → `Item.valuation_rate`, and fallback row rate in stock entries |
| barcode | Data | | | → `Item.custom_barcode` (unique; only written when non-empty) |
| disabled | Check | | 0 | → `Item.disabled` |
| description | Small Text | | | → `Item.description` |
| status / item / item_group / migration_error | read-only | | Draft | migration bookkeeping |

Migration rules:
1. Item Group = `category` (or `group_arabic` when category is blank). If it exists and is a leaf it is reused (Arabic name filled in if empty). If it is a **group node** (e.g. "Pharmacy") the row errors: pick a leaf name or change `parent_item_group`. Otherwise a leaf is created under `parent_item_group`.
2. Item = effective code. If an Item with that name already exists it is **linked**, not duplicated (its Arabic name is filled in if empty). Otherwise the Item is created as a stock, sales and purchase item.

## Pre Define Stock Entry

Mirror of Stock Entry with the same fieldnames, but rows link to Pre Define Item.

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| naming_series | Select | yes | PRE-STE-.YYYY.- | |
| stock_entry_type | Link Stock Entry Type | yes | | e.g. Material Receipt |
| purpose | Select (read-only) | | | fetched from the type |
| company | Link Company | yes | user default | |
| posting_date / posting_time | Date / Time | yes | Today / Now | copied to the real entry with `set_posting_time` |
| from_warehouse / to_warehouse | Link Warehouse | | | defaults for rows without their own |
| items | Table Pre Define Stock Entry Item | yes | | |
| remarks | Text | | | |
| status / stock_entry / migration_error | read-only | | Draft | migration bookkeeping |

Child row **Pre Define Stock Entry Item**:

| Field | Type | Required | Notes |
|---|---|---|---|
| pre_item | Link Pre Define Item | yes | |
| item_name, pre_item_code, item_code | read-only | | fetched from the pre item (`item_code` fills once migrated) |
| qty | Float | yes | > 0 |
| uom | Link UOM | yes | defaults to the pre item's stock UOM |
| conversion_factor | Float | yes | default 1 |
| s_warehouse / t_warehouse | Link Warehouse | per purpose | leaf warehouse of the company |
| basic_rate | Currency | | falls back to the pre item's valuation rate |
| batch_no, expiry_date | Data / Date | | carried into the real entry's remarks as text only |

Warehouse rules per purpose (same as ERPNext): Material Receipt needs a target; Material Issue, Material Consumption for Manufacture, Send to Subcontractor and Subcontracting Delivery need a source; Material Transfer (incl. for Manufacture) needs both and they must differ; other purposes need at least one.

Migration: every row's Pre Define Item that is not yet migrated is migrated first (a failing item fails the whole entry). A **draft** Stock Entry (`MAT-STE-.YYYY.-`) is inserted with the parent fields and rows copied by name. Review and submit it in ERPNext.

## Bulk migration

`pet_app.utils.staging_migration.migrate_each(doctype, names)` runs each document in its own savepoint: failures are written to that row's `migration_error` (plus an Error Log) and the others continue. Batches over 50 documents run in the background worker.

Server endpoints used by the buttons:
- `pet_app.pet_app.doctype.pre_define_item.pre_define_item.migrate_pre_define_items(names)`
- `pet_app.pet_app.doctype.pre_define_stock_entry.pre_define_stock_entry.migrate_pre_define_stock_entries(names)`

## Related schema

- Custom Field `Item-arabic_name` (Data, after `item_name`) is fixture-owned in `hooks.py`, mirroring `Item Group-arabic_name`.
