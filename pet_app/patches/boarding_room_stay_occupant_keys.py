"""Re-key room-stay charges that belong to a pet's second or later stay.

`linked_service_id` became per-occupant rather than per-pet. A pet's first stay keeps the
bare `boarding_room_stay:PET-00042` it already had - which is why 65 of the 66 room-stay
rows on this site need nothing done to them - but a second stay now takes
`boarding_room_stay:PET-00042#2`.

Without this, the one booking that already has a repeated pet would be misread by the new
pairing: its second occupant would look for `...#2`, find nothing, and
`_ensure_room_stay_billable_item` would add a THIRD charge beside the two that exist.

How rows are matched to stays
-----------------------------
Occupant rows and their room-stay rows are both walked in table order, per pet. That is
exactly the pairing the old code arrived at positionally, so this preserves the association
already on the record rather than inventing a new one - the point here is to write down the
link that was previously implicit, not to re-decide it.

Only rows that need a suffix are touched. A pet with one stay is not rewritten, and a row
whose key is already correct is skipped, so re-running finds nothing.

Deliberately NOT touched: amounts, quantities, rates, statuses. This changes an identifier
and nothing a guardian is charged. A booking with a repeated pet is billed for both stays -
a pet that left and came back stayed twice - and that decision lives in pricing, not here.
"""

from __future__ import annotations

import frappe

from pet_app.api.healthcare.boarding import _room_stay_service_id_for


def execute():
	if not frappe.db.table_exists("Pet Boarding Occupant"):
		return

	repeated = frappe.db.sql(
		"""
		SELECT parent, pet
		FROM `tabPet Boarding Occupant`
		WHERE parenttype = 'Pet Boarding' AND IFNULL(pet, '') != ''
		GROUP BY parent, pet
		HAVING COUNT(*) > 1
		""",
		as_dict=True,
	)
	if not repeated:
		return

	updated = 0
	for group in repeated:
		rows = frappe.get_all(
			"Pet Billable Item",
			filters={"parent": group.parent, "parenttype": "Pet Boarding", "pet": group.pet, "item_type": "Room Stay"},
			fields=["name", "linked_service_id"],
			order_by="idx asc",
		)
		for ordinal, row in enumerate(rows, start=1):
			expected = _room_stay_service_id_for(group.pet, ordinal)
			if (row.linked_service_id or "") == expected:
				continue
			frappe.db.set_value("Pet Billable Item", row.name, "linked_service_id", expected, update_modified=False)
			updated += 1

	frappe.logger("pet_app.boarding").info(
		{
			"event": "ROOM_STAY_OCCUPANT_KEYS_REKEYED",
			"bookings": sorted({g.parent for g in repeated}),
			"rows_updated": updated,
		}
	)
