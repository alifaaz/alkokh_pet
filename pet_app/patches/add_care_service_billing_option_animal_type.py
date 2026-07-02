from __future__ import annotations

import frappe
from frappe.utils import cstr


DOCTYPE = "Care Service Billing Option"
FIELDNAME = "animal_type"

ANIMAL_TYPE_KEYWORDS = (
	("dog", "Dog"),
	("cat", "Cat"),
	("bird", "Bird"),
	("fish", "Fish"),
	("reptile", "Reptile"),
	("horse", "Horse"),
	("rabbit", "Rabbit"),
)

SPECIES_DEFAULTS = {
	"Bird": "Bird",
	"Fish": "Fish",
	"Reptile": "Reptile",
}


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	frappe.reload_doc("pet_app", "doctype", "care_service_billing_option", force=True)
	if not frappe.db.has_column(DOCTYPE, FIELDNAME):
		return

	rows = frappe.get_all(
		DOCTYPE,
		fields=["name", "option_label", "size_weight_label", "animal_species", FIELDNAME],
		limit_page_length=0,
	)
	for row in rows:
		if cstr(row.get(FIELDNAME)).strip():
			continue

		animal_type = guess_animal_type(row)
		frappe.db.set_value(DOCTYPE, row.name, FIELDNAME, animal_type, update_modified=False)


def guess_animal_type(row) -> str:
	text = " ".join(
		cstr(row.get(field)).strip().lower()
		for field in ("option_label", "size_weight_label")
		if row.get(field)
	)
	for keyword, animal_type in ANIMAL_TYPE_KEYWORDS:
		if keyword in text:
			return animal_type

	return SPECIES_DEFAULTS.get(cstr(row.get("animal_species")).strip()) or "Other"
