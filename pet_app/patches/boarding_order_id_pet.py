"""Rewrite boarding-sourced order_ids into the per-pet format.

`order_id` on a boarding-sourced order used to be `{boarding}-{kind}-{care_service}`,
which names no pet. It is now `{boarding}-{kind}-{care_service}-{pet}`, because without
the pet two animals in one booking ordering the same service collide - see
_boarding_order_id in api/healthcare/boarding.py.

Rewriting the existing rows rather than teaching the readers two formats. There are five
of them on the site this was written against, and a matcher that understands two shapes
is a matcher somebody has to keep understanding.

Both sides are updated. The order carries `order_id` and so does the Pet Billable Item
row raised alongside it; leaving those disagreeing would break the last-resort branch of
visit_billing._find_billable_row, which matches on order_id + item_code + item_type when
linked_service_id has not already matched.

Idempotent: an id that already carries its pet is skipped, and so is one whose shape is
not the format this patch replaces.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr


# doctype -> (fieldname holding the pet, order kind as it appears in the id)
ORDER_SOURCES = {
	"Lab": ("pet", "lab"),
	"Imaging": ("pet", "radiology"),
	"PetCareService": ("pet_id", "service"),
}


def execute():
	for doctype, (pet_field, kind) in ORDER_SOURCES.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		if not frappe.db.has_column(doctype, "order_id"):
			continue
		_backfill(doctype, pet_field, kind)
	frappe.db.commit()


def _backfill(doctype: str, pet_field: str, kind: str):
	rows = frappe.get_all(
		doctype,
		filters={"source_doctype": "Pet Boarding", "source_name": ["is", "set"]},
		fields=["name", "order_id", "source_name", pet_field],
	)
	for row in rows:
		pet = cstr(row.get(pet_field)).strip()
		current = cstr(row.get("order_id")).strip()
		if not pet or not current:
			# Nothing to build an id from. Left alone deliberately: an order with no pet
			# is a data problem this patch would only paper over.
			continue

		expected = f"{row.source_name}-{kind}-{_care_service_of(current, row.source_name, kind)}-{pet}"
		if current == expected or current.endswith(f"-{pet}"):
			continue

		frappe.db.set_value(doctype, row.name, "order_id", expected, update_modified=False)
		_realign_billable_row(row.source_name, current, expected, doctype, row.name)
		frappe.logger("pet_app.boarding").info(
			{
				"event": "BOARDING_ORDER_ID_BACKFILLED",
				"doctype": doctype,
				"order": row.name,
				"pet": pet,
				"from": current,
				"to": expected,
			}
		)


def _care_service_of(current_order_id: str, boarding: str, kind: str) -> str:
	"""The care-service segment of the old id, recovered by stripping the known prefix.

	Taken from the id rather than re-read from the order, so a template that has since
	been renamed or repointed cannot change an id that is only being reshaped.
	"""
	prefix = f"{boarding}-{kind}-"
	if current_order_id.startswith(prefix):
		return current_order_id[len(prefix) :]
	return current_order_id


def _realign_billable_row(boarding: str, old_order_id: str, new_order_id: str, linked_doctype: str, linked_name: str):
	"""Keep the boarding's billable row's order_id in step with its order."""
	if not frappe.db.exists("Pet Boarding", boarding):
		return
	for name in frappe.get_all(
		"Pet Billable Item",
		filters={
			"parenttype": "Pet Boarding",
			"parent": boarding,
			"order_id": old_order_id,
			"linked_doctype": linked_doctype,
			"linked_name": linked_name,
		},
		pluck="name",
	):
		frappe.db.set_value("Pet Billable Item", name, "order_id", new_order_id, update_modified=False)
