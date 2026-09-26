# Item Barcode Generator — Backend Contract

Canonical contract. Implemented 2026-09-03. Generation only; label printing is a later,
separate change.

Assigns this company's own barcode to an ERPNext `Item` that has none. Builds on the
`Item.custom_barcode` Custom Field (unique, indexed, trimmed on save — see
`pet_app/utils/item_barcode.py` and `pet_app/patches/item_barcode_unique_index.py`).

## Format

`ALK-` followed by a six-digit zero-padded sequence number: `ALK-000001`, `ALK-000123`.
Past 999999 the number widens to seven digits; it never wraps.

Internal only. Deliberately **not** EAN-13 or any registered scheme. That decision is
settled.

## Two ways in, and which to use

| | Persists when | Use for |
| --- | --- | --- |
| Preview + `custom_generate_barcode` on save | the operator saves | any open form or edit dialog |
| `generate_item_barcodes` / `generate_missing_item_barcodes` | the call returns | bulk admin, no form open |

**A form must never call the writing endpoints.** They commit as soon as the request
returns, so a Generate button wired to one makes the *click* the act that assigns a
barcode. An operator who clicks, sees the field fill, then closes the dialog has already
changed the Item, and Cancel has lied to them. Use the preview instead and let the save
mint the value.

## Generating from a form

Two steps, and the second one is an ordinary save.

1. **Preview** — call `peek_next_item_barcode()` and put the returned value in the field,
   shown as provisional. Nothing is written and no number is reserved.
2. **Save** — send `custom_generate_barcode: 1` on the Item along with everything else.
   The `before_validate` hook mints the real barcode inside the save's own transaction
   and clears the flag.

The client should not send the previewed string back. The field is display-only until
the save, and the save's response carries the authoritative value. The forecast can
differ if another operator saves first; nothing is printed before the save, so nothing
is misled.

Because the counter advance rides in the save's transaction, a save that fails
validation for any reason rolls the number back with it and leaves no gap.

## Endpoints

All three are whitelisted, require `write` on `Item` at DocType level (Frappe's normal
`PermissionError` otherwise), and return the standard envelope.

### `pet_app.api.item_barcode.peek_next_item_barcode()`

GET or POST. Forecasts the next value. **Writes nothing.**

```
GET /api/method/pet_app.api.item_barcode.peek_next_item_barcode
```

```json
{
  "ok": true,
  "data": {"barcode": "ALK-000001", "is_preview": true},
  "meta": {
    "preview": true, "reserved": false,
    "generate_flag_field": "custom_generate_barcode",
    "series_key": "ALK-BARCODE-"
  },
  "errors": []
}
```

`meta.generate_flag_field` names the field to send on save, so a client reads it off the
API rather than hardcoding it.

### `pet_app.api.item_barcode.generate_item_barcodes(items)`

**POST only.** Writes at call time. A named list. `items` is a JSON array of Item names,
a Python list, or one Item name. Names are deduplicated, first occurrence sets the
order. Not split on commas.

```
POST /api/method/pet_app.api.item_barcode.generate_item_barcodes
{"items": ["Cbc", "Dog 1-5 kg - Dog 1 - 5", "no-such-item"]}
```

### `pet_app.api.item_barcode.generate_missing_item_barcodes(dry_run=0)`

**POST only**, including `dry_run=1`: Frappe gates HTTP methods per function, and this
one can write. Every Item the caller can read that currently has no barcode, in name
order.

`dry_run=1` returns the names and count and **writes nothing** — no barcode, no series
number consumed. Use it to see what a real run would touch.

```
POST /api/method/pet_app.api.item_barcode.generate_missing_item_barcodes
{"dry_run": 1}
```

## Response

```json
{
  "ok": true,
  "data": {
    "results": [
      {"item": "Cbc", "item_name": "CBC", "barcode": "ALK-000001",
       "generated": true, "status": "generated", "reason_code": null, "reason": null,
       "numbers_skipped": 0},
      {"item": "Dog 1-5 kg - Dog 1 - 5", "item_name": "...", "barcode": "5941234",
       "generated": false, "status": "skipped",
       "reason_code": "ALREADY_HAS_BARCODE",
       "reason": "Item already has barcode 5941234; left unchanged."},
      {"item": "no-such-item", "item_name": null, "barcode": null,
       "generated": false, "status": "skipped",
       "reason_code": "NOT_FOUND", "reason": "No Item named no-such-item."}
    ]
  },
  "meta": {
    "requested": 3, "generated": 1, "skipped": 2,
    "skipped_by_reason": {"ALREADY_HAS_BARCODE": 1, "NOT_FOUND": 1},
    "numbers_skipped_as_taken": 0,
    "series_key": "ALK-BARCODE-",
    "elapsed_ms": 42
  },
  "errors": []
}
```

Dry run: `data.items` is the name list, `data.results` is empty, and `meta` carries
`dry_run: true`, `missing`, `would_generate`.

### Per-item `reason_code`

| Code | Meaning | `barcode` in the row |
| --- | --- | --- |
| `ALREADY_HAS_BARCODE` | Item already carries a code. **Never overwritten.** | the existing code |
| `NOT_FOUND` | No Item by that name. | `null` |
| `NOT_PERMITTED` | Caller lacks document-level `write` on this Item (User Permission). | `null` |

### Whole-request `meta.code` on `ok: false`

| Code | Meaning | Written |
| --- | --- | --- |
| `BARCODE_FIELD_MISSING` | `Item.custom_barcode` is not on the site yet. Run `bench migrate`. | nothing |
| `ITEMS_REQUIRED` / `ITEMS_INVALID` | Empty or malformed `items`. | nothing |
| `SERIES_EXHAUSTED` | 1000 consecutive numbers were already in use. A human needs to look. | rolled back |
| `BARCODE_GENERATION_FAILED` | Unexpected error; traceback in Error Log. | rolled back |

One request is one transaction. Per-item skips do not abort it. Any whole-request
failure rolls back every write made in that request, so a row is never reported as
`generated` and then lost.

### Why the writing endpoints declare `methods=["POST"]`

Not tidiness. With the framework default of GET/POST/PUT/DELETE, a GET reached the
function body, generated, and returned `ok: true` with a real barcode. Frappe then
rolled the whole request back on the way out, because it commits only for unsafe
methods. The caller was told a number had been assigned that existed nowhere. The
declaration makes the framework refuse the GET before the body runs.

## Never overwrite

Enforced twice: the Item row is read with `SELECT ... FOR UPDATE` and skipped if the
field holds anything, and the write is `frappe.db.set_value` on that one column — not
`doc.save()`, which rewrites every column from memory and is the classic lost-update
shape when a desk user has the same form open. `modified` is bumped, so a desk save
started before the generation is refused by Frappe's timestamp check on submit.

## `Item.custom_generate_barcode`

A Check, shipped in `pet_app/fixtures/custom_field.json` and listed in the hooks
allow-list. It is a **request, not a state**: the hook clears it on every save, so the
column is 0 on every stored row.

| Property | Value | Why |
| --- | --- | --- |
| `depends_on` | `eval:!doc.custom_barcode` | hidden once a code exists, so it cannot be ticked pointlessly |
| `no_copy` | 1 | Duplicate must not carry a standing instruction to mint |
| `print_hide` | 1 | an input, never a fact about the item |
| `default` | none | unticked |

Behaviour in `pet_app.utils.item_barcode._apply_generate_request`:

- **Ticked, barcode empty** — takes the next free number, clears the flag.
- **Ticked, barcode already set** — keeps the existing value untouched, clears the flag,
  and tells the operator with a non-blocking alert. Never overwrite is the rule the
  whole feature is built on, and the one way to reach here is someone who typed a real
  code and left the box ticked.
- **Not ticked** — nothing happens.

Not guarded on `is_new()`: an existing Item that never got a code is the main case.
Every Item writer in the app runs the document lifecycle, so Desk, Data Import and all
ten of this app's Item writers reach it with no client changes.

## Collision strategy

Three layers, each independent of the others.

1. **A locked counter.** The sequence is one row in `tabSeries` — the same table
   Frappe's naming series use — keyed `ALK-BARCODE-`, advanced via
   `frappe.model.naming.getseries`, which reads the row with `SELECT ... FOR UPDATE`
   and then increments it. InnoDB holds that lock until the transaction commits, so a
   second operator generating at the same moment blocks on the row and receives the
   number after the first operator's last one. Two transactions cannot hold the same
   value. The row is seeded with `INSERT ... ON DUPLICATE KEY UPDATE` before first use
   so two first-ever callers serialise on it rather than racing an insert.
2. **A pre-check.** Each candidate is looked up against `Item.custom_barcode` before
   the write. A number an operator typed by hand (`ALK-000005`, say) is passed over and
   retired; it is never handed out later.
3. **The unique index.** If the index still refuses the write — a hand-typed code
   landed between the pre-check and the write — the statement is rolled back to a
   savepoint placed *after* the counter advance, and the next number is tried. The
   counter never moves backwards, so the same number is never offered twice.

### What happens when an Item is deleted

Nothing, to the counter. Frappe's `revert_series_if_last` decrements a naming series
when the document holding its *last* number is deleted, and it is keyed on the deleted
document's own name (`doc.meta.autoname` / `doc.naming_series`). `Item` is named
`field:item_code`, and the barcode is a field value, not a name, so a delete never
reaches this series. The number is retired and the sequence has a gap. That is the
intended behaviour: a code printed on a label must never resolve to a different item
later.

The private key `ALK-BARCODE-` (rather than `ALK-`) is what keeps this true if some
DocType is later given a naming series `ALK-.######`: that series would share, and on
delete revert, an `ALK-` counter. It cannot touch this one.

### Templates and variants

ERPNext copies template fields onto variants only when the field is mandatory or
listed in Item Variant Settings. `custom_barcode` is neither, so a template's barcode
stays on the template. Desk **Duplicate** does copy it (the field is not `no_copy`);
the duplicate then fails to save on the unique index until the code is cleared —
loud, not silent.

## Generic

One generator for every Item. No branching on item group, item type, template versus
variant, stock versus service. If a class of items should be excluded, exclude it in
the caller by naming a list.

## Tests

`pet_app/tests/test_item_barcode_generator.py`, in four classes:

| Class | Needs | Covers |
| --- | --- | --- |
| `TestBarcodeFormatting` | nothing | format, argument parsing, the POST-only declarations |
| `TestItemBarcodeGenerator` | `custom_barcode` | the writing endpoints, locking, collisions |
| `TestBarcodePeek` | `custom_barcode` | the preview reserves nothing and forecasts correctly |
| `TestGenerateOnSave` | `custom_generate_barcode` | the save mints, an abandoned save does not |

Each integration class skips itself with a message when its column is not on the site
yet, rather than failing on an absence that is expected before the migrate. Nothing
commits; the classes roll back, so a test run consumes no live numbers.

```
bench --site <site> run-tests --app pet_app --module pet_app.tests.test_item_barcode_generator
```
