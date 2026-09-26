# Stock Price

Review and apply many selling and buying prices in one submitted document.

Each row shows an item's current selling and buying price next to the new ones you want.
Saving fills an **Action** column per side, so what submit will do is visible before it
does it. Submit writes the prices and records what it replaced; cancel puts them back.

## What it does not do

Stock Price writes `Item Price` rows. It does **not** touch `Item.standard_rate`,
`Item.valuation_rate`, Stock Ledger Entries or Bins, so it cannot change what stock is
*worth* — only what it is bought and sold for on the price list. Valuation must come from
real purchase transactions; a tool that let a person type it would be a way to fake a
margin. This is the same policy `utils/item_price_import.py` states.

## Price lists

Fixed, not chosen per document:

| Side | Resolved by |
|---|---|
| Selling | `pet_app.utils.price_list.get_veterinary_selling_price_list()` |
| Buying | `pet_app.utils.price_list.get_buying_price_list()` |

Both are stored read-only on the document, so an old record still says which lists it
wrote to even if the site configuration later points elsewhere. Validate refuses a pair
whose currencies differ — one set of Currency fields cannot honestly show two.

`get_buying_price_list()` throws when the list is missing, disabled, or not a buying list.
Callers that must survive its absence use the `DEFAULT_BUYING_PRICE_LIST` constant
instead; `get_medications` does, because Standard Buying is enabled by
`enforce_iqd_defaults` but never created by `seed_initial_master_data`.

## Blank and 0 both mean "leave it alone"

A Currency field cannot express "empty" — Frappe stores `0` for both. A `0` is read as
*not entered*, never as *worth nothing*, because writing it would put the item on sale for
free. This is the reading `item_price_import` already applies to a blank cell in the price
sheet, and a second, contradictory convention in a second tool would be worse than the
ambiguity.

The ambiguity is answered by **visibility**, not by a checkbox: `selling_action` and
`buying_action` are server-filled on every save.

| Action | Meaning |
|---|---|
| `Skipped` | New price blank or 0 — this price is not touched |
| `Unchanged` | New price equals the current one, within `RATE_TOLERANCE` (0.001) |
| `Update` | An existing price row will be re-rated |
| `Create` | No price row exists for this item on this list; one will be made |

Setting a price to literally zero is not possible here, deliberately. It stays a one-off
on the Item Price form.

A document whose every row is `Skipped` or `Unchanged` saves fine as a draft but refuses
to submit — a submitted document that did nothing is a lie in the audit trail.

## The general row: one selector, shared by read and write

`pet_app/utils/item_price.py` holds the only implementation. An item can carry several
rows on one price list, so which one is "the price" is a choice — and it stays correct
only because the code that reads it and the code that writes it make the same one.

A row is a **general row** for `(item_code, price_list)` when it carries no `customer`,
`supplier` or `batch_no`, and is valid on the date asked about. The winner is:

1. latest `valid_from`
2. then no `packing_unit` over one with
3. then latest `modified`
4. then highest `name`

The ordering is **total**. `api/medication.py::_buying_price_row` orders on `valid_from`
and `modified` only, and 14 items on this site carry two rows on one list — so a tie is
reachable, and a tie means two identical calls can return different answers.

`packing_unit` is a *preference, not a filter*, because ERPNext does not filter on it:
`get_item_price` selects without looking at it and only afterwards checks that the desired
qty is a multiple of it. Treating it as a filter made two live-priced items read as
unpriced, which would have had this tool create a second row competing with the real one.

**An item can hold more than one legitimate price on one list.** The 14 two-row items here
are mostly per-UOM — a bag price and a kilo price. Every reader in this app already
collapses those to one, so this does too, but the resolved row carries `candidate_count`,
the child row sets `selling_multi_row` / `buying_multi_row`, and validate raises a message
naming the affected rows. The document edits one row and says which; the others are left
alone.

### Writes

**Update in place, changing `price_list_rate` and nothing else** — not the uom, not the
dates, not `selling`/`buying`/`currency` (read-only; ERPNext sets them from the Price List
in `update_price_list_details`). Two reasons:

- `ItemPrice.check_duplicates` matches the exact tuple `(item_code, price_list, uom,
  valid_from, valid_upto, customer, supplier, batch_no, packing_unit)` and excludes the
  row itself, so an unchanged tuple makes a duplicate error impossible. None of the
  uom-blanking workarounds elsewhere in this app are needed — and they cannot work here
  anyway, because **`Item Price.uom` is `reqd: 1`** on this site and a blank-uom row will
  not save at all.
- ERPNext applies **no date-overlap validation**, so a second row dated today is legal
  beside an undated one — and every selling reader in this app resolves with an
  *unordered* `frappe.db.get_value`, which would then return an arbitrary one of the two.
  Keeping the count at one makes ordered and unordered readers agree.

A row is **created** only when no general row exists at all, always in the item's
`stock_uom` — the one UOM ERPNext guarantees is in the item's UOM Conversion Detail, and
the shape the other 4,277 rows on this site already have. Validate pre-checks that
conversion row and throws on the draft rather than mid-submit.

Creating the first selling row for an item that had none changes that item's effective
price source away from the `Item.standard_rate` fallback used by `get_item_effective_rate`
and four other readers. Correct, but visible.

## Submit and cancel

Submit and cancel are `on_submit` / `on_cancel` on the controller, three lines each,
delegating to `pet_app/utils/stock_price.py`. No other controller in this app implements
those hooks, and that is usually right — but here the price writes must be **atomic with
the docstatus transition**. Behind a whitelisted method called after submit, a submitted
document could exist with no prices applied, and cancel would then "restore" prices that
were never set.

Permission is checked on **`Item Price`**, the doctype actually mutated — permission to
submit a Stock Price is not permission to rewrite the price list.

There are **no per-row savepoints** on submit. A submit is all-or-nothing: a failure
propagates, Frappe's rollback unwinds it, and neither the document nor any price moves.
Savepoints here would only buy the ability to swallow errors. Nothing commits — the submit
arrives as a POST, which Frappe commits on the way out.

### Cancel restores a snapshot, not history

Each row stores `previous_*_rate` (what was there) and `applied_*_rate` (what was
written). Cancel puts the first back; a row this document *created* is deleted, because
returning the item to *no price* is correct where writing 0 would leave it sellable for
nothing.

That is only safe while nothing else has moved the price since, so `before_cancel` refuses
when:

- **the rate drifted** — the row no longer holds what this document set. The message names
  the first blocking item and lists every one, set-vs-found.
- **a `Product` links to a row this document created** — `Product.item_price` is a Link, so
  deleting would raise `LinkExistsError` mid-cancel with an opaque message.

A row that was **deleted** since does *not* block: there is nothing to restore and nothing
to get wrong, and blocking would leave a document that can never be cancelled.

**Consequence, stated rather than discovered:** once a later document changes the same
item, the earlier one can no longer be cancelled. Silently stomping the later price would
be worse. Correct forward with a new document.

No snapshot field carries `allow_on_submit` — a hand-edited snapshot would defeat the
guard entirely.

## Get Items

`pet_app.pet_app.doctype.stock_price.stock_price.get_items` — read-only, returns the
standard envelope.

| Argument | Default | |
|---|---|---|
| `item_group` | — | with `include_child_groups` (default 1), via the nested-set `lft`/`rgt` descendants |
| `brand` | — | exact |
| `search` | — | `like` on `item_code` or `item_name` |
| `include_disabled` | 0 | |
| `unpriced_only` | 0 | items with no selling row |
| `limit` | 500 | capped at 2000 |

Templates (`has_variants = 1`) are always excluded — ERPNext refuses an Item Price on one.

Prices for the whole batch are resolved in **one** query. The medication list endpoint
resolves them one item at a time; that is the thing not to copy here.

`current_selling_rate` and `current_buying_rate` come back as **`null`, never 0**, when no
row exists. An item with no price is not an item priced at nothing.

The form only appends items not already in the table, so anything already typed is kept.

## Permissions

`System Manager` (full) and `Stock Manager` (everything but delete). Both need `submit` and
`cancel`.

The real gate is the `Item Price` DocPerm checked at submit and cancel. `Stock Manager`
holds `write` and `create` there but **not `delete`** — so undoing a `Create` is gated on
`create`, the permission that authorised the insert, not on `delete`. This is not an
arbitrary delete: it is one named row, recorded in a submitted document. Whoever may
create the row may un-create it.

Adding any other role to Stock Price means also giving it `Item Price` write.

## Known divergence to clean up separately

Five other Item Price upserts remain in this app: `medication.upsert_item_price`,
`api/medication._apply_buying_price`, `care_service_billing_option.upsert_item_price`,
`variants.set_variant_price`, `item_price_import._apply`. **Three write `uom = None` or omit
`uom`, which cannot save against a `reqd: 1` field** — they are broken on create today.
Converging them onto `utils/item_price.py` would fix that, but it is a bug fix with its own
testing, not something to smuggle into a new feature.

`item_price_import._apply` keeps its own, stricter selector on purpose: it matches every
row for the pair regardless of validity dates so `len(existing) > 1` surfaces as `conflict`
in its CSV. An unattended bulk import must refuse an ambiguous item; a UI tool should pick
one and show which. Two implementations by design, not seven by accident.
