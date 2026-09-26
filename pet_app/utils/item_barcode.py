from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

FIELDNAME = "custom_barcode"

# The Check that asks this save to mint a barcode. It is a request, never a stored
# state: the branch below always clears it, so the column is 0 on every saved row.
GENERATE_FLAG_FIELDNAME = "custom_generate_barcode"


def before_validate_item_barcode(doc, method=None):
	"""Trim Item.custom_barcode, and store "no barcode" as NULL rather than ''.

	Wired as a doc_event rather than living in a controller, because Item belongs to
	ERPNext and this app has no class to put a validate() on. That difference matters:
	the Product trim could sit in Product.validate() and be certain of covering every
	writer, whereas here the hook is the only seam, so it has to be the doc_event and
	not a helper the app's own Item writers call individually. There are ten such
	writers (care_service_billing_option, medication, careservice_template,
	procedure_template, variants x3, product x2, care_service) plus the desk form,
	ERPNext's own item creation and Data Import - every one of them runs the document
	lifecycle, so every one of them reaches this.

	The single path that would NOT reach it is a direct `frappe.db.set_value`, which
	skips the lifecycle entirely. There is one in this app, the projection fallback at
	api/product.py:360, and it writes a fixed payload of item_name / item_group / brand
	/ description / image - no barcode - so it cannot store an untrimmed code. Any new
	`db.set_value` that starts writing this field would bypass the trim, and would have
	to normalise for itself.

	Both halves matter under the UNIQUE index on this column:

	- Trimming, because a scan gun appends a terminator (CR/LF, sometimes a tab) and an
	  operator pasting a code brings spaces with it. "5941 " and "5941" are one barcode
	  to the person holding the gun but two distinct keys to the index, so an untrimmed
	  write both escapes the uniqueness rule and stores a code an exact-match lookup can
	  never find.
	- Nulling the blank, because MariaDB counts NULL as always-distinct but treats ''
	  as an ordinary value: the second item saved with an empty barcode would collide
	  with the first, which is the opposite of "optional". Frappe does this much itself
	  for unique fields in `BaseDocument.get_valid_dict`, but only for a value that is
	  ALREADY blank - it does not trim, so "   " and "ABC " still need handling here.

	before_validate, not validate: normalisation has to land before anything reads the
	value, including ERPNext's own Item.validate and the framework's duplicate check.

	This is also where a requested barcode is minted - see `_apply_generate_request`.
	"""
	# meta.has_field, so a site that has not yet synced the fixture (the field is
	# created by sync_fixtures, which runs at the END of a migrate) saves normally
	# instead of erroring on a missing attribute.
	if not doc.meta.has_field(FIELDNAME):
		return

	barcode = cstr(doc.get(FIELDNAME)).strip()
	doc.set(FIELDNAME, barcode or None)

	_apply_generate_request(doc)


def _apply_generate_request(doc):
	"""Mint a barcode when this save asked for one, so the SAVE is what persists it.

	The alternative - a Generate button that calls the write endpoint - makes the button
	click the persisting act. An operator who clicks it, sees the field fill, then closes
	the dialog without saving has already changed the Item, and "Cancel" has lied to
	them. Carrying the request as a field means the whole thing lands in the save's own
	transaction: no save, no barcode, and no number consumed.

	Two branches, and the second one matters as much as the first:

	- Flag set and the field empty: take the next free number. The counter advance and
	  the Item write are now in the SAME transaction as the rest of the save, so a save
	  that fails validation downstream rolls the counter back with it and leaves no gap
	  in the sequence. That is strictly better than the endpoint, which commits its
	  number the moment the request returns.
	- Flag set and the field already filled: the typed value wins, untouched, and the
	  operator is told. Never overwrite is the rule the whole feature is built on, and
	  the one plausible way to reach here is an operator who typed a real code AND left
	  the box ticked - exactly the person who must not silently lose what they typed.

	The flag is cleared either way, so it never persists as a stored 1 that would look
	like a standing instruction to a later reader.

	Deliberately NOT guarded on `is_new()`: an existing Item that never got a code is
	the main case, not the exception.
	"""
	if not doc.meta.has_field(GENERATE_FLAG_FIELDNAME):
		# The migrate that ships the Check has not run yet. Saves work normally.
		return
	if not doc.get(GENERATE_FLAG_FIELDNAME):
		return

	doc.set(GENERATE_FLAG_FIELDNAME, 0)

	existing = cstr(doc.get(FIELDNAME)).strip()
	if existing:
		# alert, not a modal: a Data Import of several hundred rows must not stack up
		# blocking dialogs, and this is information rather than a question.
		frappe.msgprint(
			_("Item already has barcode {0}. It was kept and no new barcode was generated.").format(
				existing
			),
			indicator="orange",
			alert=True,
		)
		return

	# Imported here, not at module scope: item_barcode_generator imports FIELDNAME from
	# this module, so a module-level import back would be circular.
	from pet_app.utils.item_barcode_generator import next_free_barcode

	barcode, _skipped = next_free_barcode()
	doc.set(FIELDNAME, barcode)
