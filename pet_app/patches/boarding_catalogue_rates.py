"""Move boarding pricing onto the catalogue, changing no figure.

Six small data moves, all of which name what is already true rather than invent anything:

1. The two existing boarding rows get `animal_type = "All"` - which is what they already
   are, since one price currently serves every species.
2. They get `boarding_type` Travel / Treatment, the dimension the catalogue could not
   previously express.
3. `CareService-00016.item_code` is repointed from `Regular Bording` (sic) to
   `Travel Boarding`. Both are priced 15,000, so no figure moves - but the settings path
   bills `Travel Boarding` and the catalogue path would otherwise start printing the typo
   on invoices. Repointing keeps the customer-visible line identical.
4. `boarding_category` in settings is pointed at the category those rows live in, so the
   resolver has no category name compiled into it.
5. `max_pets_per_booking` is written explicitly. It reads as empty today and is only 1
   because of a fallback in code; a limit nobody can see in the form is not a limit the
   owner controls.
6. Room Stay billable rows created since the Stage 2 backfill are stamped with their pet.
   The backfill was complete; the write path re-opened the gap.

DELIBERATELY NOT DONE: no Cat or Dog rows are created. Those are prices, and prices are the
owner's to enter. With only the "All" rows present every pet resolves exactly as it does
today, so this patch is behaviour-neutral by construction - the dog under-billing corrects
itself when the owner adds a Dog row in Desk, visibly and on their timing.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr


TEMPLATE_DOCTYPE = "CareService template"

# (template, boarding_type, expected item_code). Matched by name because these two rows are
# the ones this site has; a site without them simply skips and the owner sets up its own.
BOARDING_ROWS = (
	("CareService-00016", "Travel", "Travel Boarding"),
	("CareService-00017", "Treatment", "Treatment Boarding"),
)

DEFAULT_MAX_PETS_PER_BOOKING = 1


def execute():
	_tag_boarding_templates()
	_set_boarding_category()
	_store_capacity()
	_stamp_room_stay_pets()
	frappe.db.commit()


def _tag_boarding_templates():
	if not frappe.db.has_column(TEMPLATE_DOCTYPE, "animal_type"):
		return
	for name, boarding_type, item_code in BOARDING_ROWS:
		if not frappe.db.exists(TEMPLATE_DOCTYPE, name):
			continue
		updates = {}
		current = frappe.db.get_value(
			TEMPLATE_DOCTYPE, name, ["animal_type", "boarding_type", "item_code"], as_dict=True
		)
		if not cstr(current.animal_type).strip():
			updates["animal_type"] = "All"
		if not cstr(current.boarding_type).strip():
			updates["boarding_type"] = boarding_type
		# Only repoint the known typo, and only onto an Item that exists. A template whose
		# item somebody has since corrected by hand is left alone.
		if cstr(current.item_code).strip() == "Regular Bording" and frappe.db.exists("Item", item_code):
			updates["item_code"] = item_code
		if updates:
			frappe.db.set_value(TEMPLATE_DOCTYPE, name, updates, update_modified=False)
			frappe.logger("pet_app.boarding").info(
				{"event": "BOARDING_TEMPLATE_TAGGED", "template": name, "updates": updates}
			)


def _set_boarding_category():
	"""Point settings at whichever category the boarding rows actually live in."""
	if cstr(frappe.db.get_single_value("Pet Boarding Settings", "boarding_category")).strip():
		return
	category = None
	for name, _boarding_type, _item in BOARDING_ROWS:
		category = frappe.db.get_value(TEMPLATE_DOCTYPE, name, "category_id")
		if category:
			break
	if not category:
		# Nothing to infer from. Left unset so the resolver's own message tells the owner
		# what to do, rather than this patch guessing a category.
		return
	frappe.db.set_single_value("Pet Boarding Settings", "boarding_category", category)


def _store_capacity():
	"""Write the limit down. It currently has no stored value at all."""
	current = cint(frappe.db.get_single_value("Pet Boarding Settings", "max_pets_per_booking"))
	if current > 0:
		return
	frappe.db.set_single_value("Pet Boarding Settings", "max_pets_per_booking", DEFAULT_MAX_PETS_PER_BOOKING)


def _stamp_room_stay_pets() -> int:
	"""Room Stay rows written since the Stage 2 backfill carry no pet. Give them one.

	Scoped to rows whose booking has exactly ONE occupant. With capacity at 1 that is every
	booking, but writing the condition rather than assuming it means this patch cannot
	misattribute a charge if it is ever re-run on a site where capacity has risen.
	"""
	if not frappe.db.has_column("Pet Billable Item", "pet"):
		return 0
	return frappe.db.sql(
		"""
		UPDATE `tabPet Billable Item` i
		JOIN `tabPet Boarding` b ON b.name = i.parent
		JOIN (
			SELECT parent, MIN(pet) AS pet, COUNT(*) AS n
			FROM `tabPet Boarding Occupant`
			WHERE parenttype = 'Pet Boarding'
			GROUP BY parent
		) o ON o.parent = b.name AND o.n = 1
		SET i.pet = o.pet
		WHERE i.parenttype = 'Pet Boarding' AND IFNULL(i.pet, '') = ''
		"""
	)
