from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr


BLOCKED_DECEASED_DOCTYPES = {
	"Appointment",
	"Vet Case Sheet",
	"Vet Visit",
	"Pet Boarding",
	"PetCareService",
	"Pet Procedure",
}


def apply_pet_visibility_filters(filters: dict | None, *, exclude_deceased: bool = True) -> dict:
	"""Add the deceased exclusion to a Pet LISTING query's filters. Returns a new dict.

	Deliberately narrower than `deceased_pet_context`, and the asymmetry is the point.
	The two have opposite failure directions:

	- A listing filter that is too broad HIDES A LIVING ANIMAL from the operator's picker
	  and stops work. So it keys only on `is_deceased`, a Check column - int NOT NULL
	  DEFAULT 0 - which cannot be null and therefore cannot drop a row by accident.
	  `status` and `pet_status` are Select columns, and `["!=", "Deceased"]` against a null
	  one evaluates to NULL in SQL and silently removes that pet from the list. They are
	  0% null today; that is not a guarantee worth betting the picker on.
	- A write guard that is too narrow ADMITS A DEAD ANIMAL. So `assert_pet_not_deceased`
	  keeps reading all three markers and fails closed on any of them.

	Narrow at the door, broad at the till. `setdefault`, so a caller that explicitly wants
	deceased pets - a mortality report - passes its own value and keeps it.
	"""
	filters = dict(filters or {})
	if exclude_deceased:
		filters.setdefault("is_deceased", 0)
	return filters


def deceased_pet_context(pet: str | None) -> dict | None:
	"""The death on file for this pet, or None if it is alive. THE definition of dead.

	Three columns can say it - `is_deceased`, `status`, `pet_status` - and any one of them
	is taken as authoritative rather than requiring agreement, because a half-written death
	must fail closed. On this site all three agree on all three deceased pets, but the rule
	is written for the day they do not.

	Returns the death record and date as well as the flag, so a refusal can name what it is
	refusing on instead of asserting death without evidence. `is_pet_deceased` is this
	function's boolean, so there is one definition and not two.
	"""
	if not pet or not frappe.db.exists("Pet", pet):
		return None
	row = frappe.db.get_value(
		"Pet",
		pet,
		["name", "pet_name", "is_deceased", "status", "pet_status", "death_date", "death_record"],
		as_dict=True,
	)
	if not row:
		return None
	if not (cint(row.get("is_deceased")) or row.get("status") == "Deceased" or row.get("pet_status") == "Deceased"):
		return None
	return row


def is_pet_deceased(pet: str | None) -> bool:
	return bool(deceased_pet_context(pet))


def _deceased_evidence(row: dict) -> str:
	"""'died 2026-07-19, death record PDR-2026-00011' - whatever of it is on file."""
	parts = []
	if row.get("death_date"):
		parts.append(_("died {0}").format(row.get("death_date")))
	if row.get("death_record"):
		parts.append(_("death record {0}").format(row.get("death_record")))
	if not parts:
		# Flagged deceased with nothing behind it. Say so rather than implying a record
		# exists - that is itself worth seeing, and it is what a data fix would start from.
		parts.append(_("no death record on file"))
	return ", ".join(parts)


def assert_pet_not_deceased(pet: str | None, *, action: str):
	"""Refuse an action on a dead animal, naming the pet and the death it is refusing on.

	`action` completes the sentence "cannot ..." and should be the operational verb the
	caller is performing - "be reserved into a room", "be checked in" - so the message says
	what was blocked rather than which function blocked it.
	"""
	if not pet or frappe.flags.allow_deceased_pet_override:
		return
	row = deceased_pet_context(pet)
	if not row:
		return
	label = cstr(row.get("pet_name")).strip()
	frappe.throw(
		_("Pet {0} is deceased ({1}) and cannot {2}.").format(
			frappe.bold(f"{label} ({row['name']})" if label else row["name"]),
			_deceased_evidence(row),
			action,
		)
	)


def validate_not_deceased(pet: str | None, *, doctype: str | None = None):
	if not pet or frappe.flags.allow_deceased_pet_override:
		return
	row = deceased_pet_context(pet)
	if row:
		label = cstr(row.get("pet_name")).strip()
		frappe.throw(
			_("Pet {0} is deceased ({1}). New {2} records are blocked.").format(
				frappe.bold(f"{label} ({row['name']})" if label else row["name"]),
				_deceased_evidence(row),
				frappe.bold(doctype or "clinical"),
			)
		)


def validate_document_not_deceased(doc, method=None):
	"""Block a new record naming a dead animal. Every pet on it, not just the first.

	`pets_from_doc` rather than `pet_from_doc`: a Pet Boarding used to hold exactly one pet
	and this checked exactly that field. It now holds up to seven, and checking only the
	nominal `pet` let a deceased animal onto a booking as occupant two - which is how
	PET-00191 (nezoko), dead since 2026-07-19, was boarded on 2026-08-17 and billed. The
	nominal pet on that booking was alive, so this guard passed it.

	Still `is_new()` only, deliberately. Re-validating on every save would refuse the death
	cascade's own writes - it marks the pet deceased and then saves the booking it is
	closing - and would strand any booking a pet died on. Entry is what this blocks; the
	API-layer guards in boarding.py cover the paths that ADD a pet to a booking that
	already exists, which are updates and never reach this hook.
	"""
	if doc.doctype not in BLOCKED_DECEASED_DOCTYPES or not doc.is_new():
		return
	for pet in pets_from_doc(doc):
		validate_not_deceased(pet, doctype=doc.doctype)


def pets_from_doc(doc) -> list[str]:
	"""Every pet a document puts at risk, in a stable order, de-duplicated.

	For a Pet Boarding that is the nominal `pet` AND every occupant row. For everything
	else it is the single pet `pet_from_doc` already knew about.
	"""
	pets = [pet_from_doc(doc)]
	if doc.doctype == "Pet Boarding":
		pets.extend(row.get("pet") for row in (doc.get("occupants") or []))
	seen, ordered = set(), []
	for pet in pets:
		pet = cstr(pet).strip()
		if pet and pet not in seen:
			seen.add(pet)
			ordered.append(pet)
	return ordered


def pet_from_doc(doc):
	if doc.doctype == "Appointment":
		return doc.get("custom_pet")
	if doc.doctype == "Vet Case Sheet":
		return doc.get("animal_patient")
	if doc.doctype == "Vet Visit":
		return doc.get("animal_patient") or frappe.db.get_value("Vet Case Sheet", doc.get("case_sheet"), "animal_patient")
	if doc.doctype == "Pet Boarding":
		return doc.get("pet")
	if doc.doctype == "PetCareService":
		return doc.get("pet_id")
	if doc.doctype == "Pet Procedure":
		return doc.get("pet")
	return None


def source_pet(source_doctype: str | None, source_name: str | None) -> str | None:
	source_doctype = cstr(source_doctype).strip()
	source_name = cstr(source_name).strip()
	if not source_doctype or not source_name:
		return None
	field_map = {
		"Vet Visit": "animal_patient",
		"Pet Procedure": "pet",
		"Pet Boarding": "pet",
		"PetCareService": "pet_id",
		"Appointment": "custom_pet",
		"Lab": "pet",
		"Imaging": "pet",
	}
	fieldname = field_map.get(source_doctype)
	if not fieldname:
		return None
	return frappe.db.get_value(source_doctype, source_name, fieldname)


def guardian_for_pet(pet: str | None) -> str | None:
	if not pet:
		return None
	return frappe.db.get_value("PetGuardian", {"pet_id": pet, "role": "primary_owner"}, "guardian_id") or frappe.db.get_value(
		"PetGuardian", {"pet_id": pet}, "guardian_id"
	)

