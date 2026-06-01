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


def is_pet_deceased(pet: str | None) -> bool:
	if not pet or not frappe.db.exists("Pet", pet):
		return False
	row = frappe.db.get_value("Pet", pet, ["is_deceased", "status", "pet_status"], as_dict=True)
	return bool(row and (cint(row.get("is_deceased")) or row.get("status") == "Deceased" or row.get("pet_status") == "Deceased"))


def validate_not_deceased(pet: str | None, *, doctype: str | None = None):
	if not pet or frappe.flags.allow_deceased_pet_override:
		return
	if is_pet_deceased(pet):
		frappe.throw(
			_("Pet {0} is deceased. New {1} records are blocked.").format(
				frappe.bold(pet), frappe.bold(doctype or "clinical")
			)
		)


def validate_document_not_deceased(doc, method=None):
	if doc.doctype not in BLOCKED_DECEASED_DOCTYPES or not doc.is_new():
		return
	pet = pet_from_doc(doc)
	validate_not_deceased(pet, doctype=doc.doctype)


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

