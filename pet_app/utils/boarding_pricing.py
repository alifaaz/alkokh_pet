"""Where a boarding rate comes from: the CareService catalogue, and nothing else.

Boarding used to price from Pet Boarding Settings -> Item -> Item Price, which meant a new
species was a deploy. It now resolves from catalogue rows, so adding bird boarding is a
row in Desk.

Resolution, in one sentence: among enabled rows in the boarding category, take the one
matching this pet's `animal_type` and this stay's travel/medical type; if the species has
no row of its own, take the "All" row for that type.

Two dimensions, because one is not enough:

- `animal_type` mirrors Pet.animal_type. The catalogue's older `animal_species` is
  taxonomic class - cat and dog are both Mammal - so it cannot express "cats 15,000, dogs
  25,000", which is the whole point of the move.
- `boarding_type` separates travel from medical. Without it the resolver would have to
  recognise rows by their service name, and a name in code is the same deploy-to-change
  trap in a different coat.

The species axis falls back to "All"; the travel/medical axis does NOT. A stay is one or
the other, and quietly charging the travel rate for a medical stay because no medical row
exists would be worse than refusing.

Nothing here decides what happens when a price changes mid-stay. Staff adjust billable
rows by hand - deleting the auto-added row and entering their own qty and rate - so a rate
already on a row is left alone by the caller, and this module is only ever asked for the
rate of a row that does not have one yet.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt


CATEGORY_SETTING = "boarding_category"
FALLBACK_ANIMAL_TYPE = "All"


def get_boarding_category() -> str:
	"""The catalogue category holding boarding rates. A setting, never a constant."""
	category = cstr(frappe.db.get_single_value("Pet Boarding Settings", CATEGORY_SETTING)).strip()
	if not category:
		frappe.throw(
			_(
				"Boarding Category is not set in Pet Boarding Settings. "
				"Point it at the CareService category holding your boarding rates."
			)
		)
	return category


def resolve_boarding_rate(pet: str, boarding_type: str, *, category: str | None = None) -> frappe._dict:
	"""The catalogue row that prices this pet's stay.

	Returns item_code, rate, the template it came from, and whether the species matched
	exactly or fell through to "All" - the caller may want to say which on screen.
	"""
	boarding_type = cstr(boarding_type).strip()
	if not boarding_type:
		frappe.throw(_("Boarding type is required to resolve a rate."))

	category = category or get_boarding_category()
	animal_type = _animal_type_of(pet)

	row = _match(category, boarding_type, animal_type) if animal_type else None
	matched_on = animal_type
	if not row:
		row = _match(category, boarding_type, FALLBACK_ANIMAL_TYPE)
		matched_on = FALLBACK_ANIMAL_TYPE
	if not row:
		_throw_unresolved(category, boarding_type, animal_type)

	item_code = cstr(row.item_code).strip()
	if not item_code:
		frappe.throw(
			_("Boarding rate {0} has no Item Code. Set one on the CareService template.").format(
				frappe.bold(row.service_name or row.name)
			)
		)
	rate = flt(row.default_price)
	if rate <= 0:
		frappe.throw(
			_("Boarding rate {0} has no price. Set a Default Price on the CareService template.").format(
				frappe.bold(row.service_name or row.name)
			)
		)

	return frappe._dict(
		template=row.name,
		service_name=row.service_name,
		item_code=item_code,
		rate=rate,
		animal_type=matched_on,
		boarding_type=boarding_type,
		exact_species_match=matched_on != FALLBACK_ANIMAL_TYPE,
	)


def _animal_type_of(pet: str) -> str | None:
	pet = cstr(pet).strip()
	if not pet:
		return None
	return cstr(frappe.db.get_value("Pet", pet, "animal_type")).strip() or None


def _match(category: str, boarding_type: str, animal_type: str):
	"""The one enabled row for this category, type and species. Ambiguity is refused.

	An earlier version took the first of several by name, which is how four boarding rows
	saved as animal_type "All" - two of them meaning Dog - silently priced every dog at the
	cat rate. Deterministic is not the same as correct: two rows claiming the same key at
	different prices is a catalogue contradiction, and picking one hides it forever.
	"""
	rows = frappe.get_all(
		"CareService template",
		filters={
			"category_id": category,
			"disabled": 0,
			"boarding_type": boarding_type,
			"animal_type": animal_type,
		},
		fields=["name", "service_name", "item_code", "default_price"],
		order_by="name asc",
	)
	if len(rows) > 1:
		frappe.throw(
			_(
				"{0} boarding rates all claim Animal Type {1}: {2}. "
				"Set a distinct Animal Type on each, so one row answers for one species."
			).format(
				len(rows),
				frappe.bold(animal_type),
				frappe.bold(", ".join(f"{r.name} ({r.service_name})" for r in rows)),
			)
		)
	return rows[0] if rows else None


def _throw_unresolved(category: str, boarding_type: str, animal_type: str | None):
	"""Actionable, because the desk sees this and the desk cannot read code.

	The owner keeps a horse and three rabbits. If the "All" row is ever removed this is
	what stands between them and a blocked check-out, so it says exactly which row to add.
	"""
	frappe.throw(
		_(
			"No boarding rate found for {0} {1} boarding. "
			"Add a CareService template under category {2} with Boarding Type {1} and "
			"Animal Type {0}, or an Animal Type of {3} to cover every species."
		).format(
			frappe.bold(animal_type or _("this species")),
			frappe.bold(boarding_type),
			frappe.bold(category),
			frappe.bold(FALLBACK_ANIMAL_TYPE),
		)
	)


def unresolved_pets(pets_and_types) -> list[dict]:
	"""Which of these (pet, boarding_type) pairs the catalogue cannot price.

	Read-only and non-throwing, for a pre-flight check before the reconciliation lands and
	for a scheduled audit afterwards. Empty is the only acceptable result.
	"""
	category = cstr(frappe.db.get_single_value("Pet Boarding Settings", CATEGORY_SETTING)).strip()
	if not category:
		return [{"pet": None, "boarding_type": None, "reason": "Boarding Category is not set"}]

	missing = []
	for pet, boarding_type in pets_and_types:
		try:
			resolve_boarding_rate(pet, boarding_type, category=category)
		except Exception as exc:
			missing.append({"pet": pet, "boarding_type": boarding_type, "reason": cstr(exc)})
	return missing
