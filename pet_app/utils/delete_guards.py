"""Refuse deleting an Item or Medication that clinical records still point at.

Frappe's own link check (`check_if_doc_is_linked`) is skipped entirely by
`delete_doc(..., force=True)`, and it never sees `Vet Visit Medication Item.medication_item`,
which is a Data field rather than a Link. On 2026-07-14 a live-site cleanup script
force-deleted test Items and Medications that visits, care episodes, billable rows and the
dispense ledger still named. Every one of those parent documents then failed its next save
with `Could not find ...` - including an open care episode, which blocked Continue / New /
Close case for a live pet.

`on_trash` is the one hook `delete_doc` runs even with `force=True` (it runs before the force
check), so the refusal lives here. ERPNext's `Item.on_trash` runs first and deletes Bins,
Item Prices and variants; the throw below aborts the transaction, which rolls those back.

A referenced Item or Medication is retired by disabling it, never by deleting it: history
keeps its name, and nothing can prescribe or sell it.
"""

from __future__ import annotations

import frappe
from frappe import _

ITEM_REFERENCES = (
	("Vet Visit Medication Item", "medication_item"),
	("Pet Care Episode Medication", "medication_item"),
	("Pet Billable Item", "item_code"),
	("Medication", "linked_item"),
	("Medication Dispense Ledger", "medication_item"),
)

# The same exposure: the 2026-07-15 teardowns force-deleted Medications that
# Vet Visit Medication Item rows still name (VVT-2026-00161).
MEDICATION_REFERENCES = (
	("Vet Visit Medication Item", "medication"),
	("Pet Care Episode Medication", "medication"),
	("Medication Dispense Ledger", "medication"),
	("Preventive Care Record", "medication"),
	("Pet Boarding Medication Schedule", "medication"),
)


def refuse_referenced_item_delete(doc, method=None):
	_refuse_if_referenced(doc, ITEM_REFERENCES)


def refuse_referenced_medication_delete(doc, method=None):
	_refuse_if_referenced(doc, MEDICATION_REFERENCES)


def _refuse_if_referenced(doc, references):
	found = []
	for doctype, fieldname in references:
		if not frappe.db.table_exists(doctype) or not frappe.db.has_column(doctype, fieldname):
			continue
		count = frappe.db.count(doctype, {fieldname: doc.name})
		if not count:
			continue
		example_field = "parent" if frappe.get_meta(doctype).istable else "name"
		example = frappe.db.get_value(doctype, {fieldname: doc.name}, example_field)
		found.append(_("{0} {1} (e.g. {2})").format(count, _(doctype), example))

	if found:
		frappe.throw(
			_("{0} {1} cannot be deleted because it is still referenced by: {2}. Disable it instead.").format(
				_(doc.doctype), frappe.bold(doc.name), "; ".join(found)
			),
			frappe.LinkExistsError,
		)
