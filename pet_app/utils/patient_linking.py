from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr


def get_or_create_patient_for_pet(pet_id: str, guardian_id: str | None = None) -> str | None:
	"""Create or return a Healthcare Patient for a Pet when Healthcare is installed.

	This module is intentionally defensive: pet approval should not fail on sites
	that do not have the Healthcare Patient DocType installed.
	"""
	if not pet_id:
		frappe.throw(_("Pet is required."))
	if not frappe.db.exists("Pet", pet_id):
		frappe.throw(_("Pet {0} does not exist.").format(frappe.bold(pet_id)))
	if not frappe.db.exists("DocType", "Patient"):
		return None

	patient = _find_existing_patient(pet_id, guardian_id)
	if patient:
		return patient

	pet = frappe.db.get_value(
		"Pet",
		pet_id,
		["name", "pet_name", "gender", "birth_date", "requested_by"],
		as_dict=True,
	)
	guardian_id = guardian_id or pet.get("requested_by")
	guardian = (
		frappe.db.get_value(
			"Guardian",
			guardian_id,
			["name", "full_name", "phone", "email_id", "customer_id"],
			as_dict=True,
		)
		if guardian_id and frappe.db.exists("Guardian", guardian_id)
		else frappe._dict()
	)

	patient_doc = frappe.new_doc("Patient")
	_set_if_field(patient_doc, "patient_name", pet.get("pet_name") or pet_id)
	_set_if_field(patient_doc, "sex", _normalize_patient_sex(pet.get("gender")))
	_set_if_field(patient_doc, "dob", pet.get("birth_date"))
	_set_if_field(patient_doc, "mobile", guardian.get("phone"))
	_set_if_field(patient_doc, "email", guardian.get("email_id"))
	_set_if_field(patient_doc, "customer", guardian.get("customer_id"))
	_set_if_field(patient_doc, "custom_pet", pet_id)
	_set_if_field(patient_doc, "custom_pet_id", pet_id)
	_set_if_field(patient_doc, "pet", pet_id)
	_set_if_field(patient_doc, "custom_guardian", guardian_id)
	_set_if_field(patient_doc, "custom_guardian_id", guardian_id)
	_set_if_field(patient_doc, "guardian", guardian_id)

	patient_doc.insert(ignore_permissions=True)
	return patient_doc.name


def _find_existing_patient(pet_id: str, guardian_id: str | None = None) -> str | None:
	meta = frappe.get_meta("Patient")
	for fieldname in ("custom_pet", "custom_pet_id", "pet"):
		if meta.has_field(fieldname):
			patient = frappe.db.get_value("Patient", {fieldname: pet_id}, "name")
			if patient:
				return patient

	pet_name = frappe.db.get_value("Pet", pet_id, "pet_name")
	if pet_name:
		patient = frappe.db.get_value("Patient", {"patient_name": pet_name}, "name")
		if patient:
			return patient

	if guardian_id:
		guardian = frappe.db.get_value("Guardian", guardian_id, ["phone", "customer_id"], as_dict=True)
		if guardian:
			for fieldname, value in (("mobile", guardian.phone), ("customer", guardian.customer_id)):
				if value and meta.has_field(fieldname):
					patient = frappe.db.get_value("Patient", {fieldname: value}, "name")
					if patient:
						return patient
	return None


def _set_if_field(doc, fieldname: str, value):
	if value is not None and doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _normalize_patient_sex(gender: str | None) -> str | None:
	gender = cstr(gender).strip()
	if gender in {"Male", "Female"}:
		return gender
	return None

