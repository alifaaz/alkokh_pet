# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import date_diff, getdate, now_datetime

from pet_app.api.permissions import require_doctype_permission, require_restriction_value
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian
from pet_app.utils.medical_profile import set_visit_case_choice, update_profile_for_case_sheet, update_profile_for_visit
from pet_app.utils.practitioner import get_practitioner_for_user
from pet_app.workflows import clinical_state
from pet_app.api.response import standardize_response


class VetCaseSheet(Document):
	def validate(self):
		self._set_defaults()
		self._pull_appointment_values()
		self._populate_pet_snapshot()
		self._populate_customer_from_guardian()
		self._validate_guardian_pet_link()
		clinical_state.validate_document_transition(self)
		self._validate_conditional_fields()
		self._validate_status_consistency()

	def on_update(self):
		self._sync_status_with_visit()
		update_profile_for_case_sheet(self)

	def _set_defaults(self):
		if not self.case_sheet_date:
			self.case_sheet_date = now_datetime()

		if not self.priority:
			self.priority = "Normal"

		if not self.status:
			self.status = "Draft"

	def _pull_appointment_values(self):
		if not self.meta.has_field("appointment") or not self.appointment:
			return
		if not frappe.db.exists("Appointment", self.appointment):
			frappe.throw(_("Appointment {0} was not found.").format(frappe.bold(self.appointment)))

		appointment = frappe.get_doc("Appointment", self.appointment)
		if appointment.meta.has_field("custom_pet") and appointment.get("custom_pet"):
			if self.animal_patient and self.animal_patient != appointment.get("custom_pet"):
				frappe.throw(_("Case Sheet pet must match the linked Appointment pet."))
			self.animal_patient = appointment.get("custom_pet")
		if appointment.meta.has_field("custom_guardian") and appointment.get("custom_guardian"):
			if self.guardian and self.guardian != appointment.get("custom_guardian"):
				frappe.throw(_("Case Sheet guardian must match the linked Appointment guardian."))
			self.guardian = appointment.get("custom_guardian")
		if appointment.meta.has_field("custom_customer") and appointment.get("custom_customer") and not self.customer:
			self.customer = appointment.get("custom_customer")

	def _populate_pet_snapshot(self):
		if not self.animal_patient:
			return

		pet = frappe.db.get_value(
			"Pet",
			self.animal_patient,
			["animal_species", "animal_type", "breed", "gender", "weight", "birth_date"],
			as_dict=True,
		)
		if not pet:
			return

		self.species = pet.animal_type or pet.animal_species or self.species
		self.breed = pet.breed or ""
		self.sex = pet.gender or "Unknown"
		self.age_text = _format_pet_age(pet.birth_date)

		if pet.weight and not self.weight:
			self.weight = pet.weight

		if not self.guardian:
			guardian = _get_primary_guardian_for_pet(self.animal_patient)
			if guardian:
				self.guardian = guardian.name
				if not self.phone_number and guardian.get("phone"):
					self.phone_number = guardian.phone

	def _populate_customer_from_guardian(self):
		if not self.guardian:
			return

		guardian = frappe.db.get_value(
			"Guardian",
			self.guardian,
			["name", "customer_id", "phone"],
			as_dict=True,
		)
		if not guardian:
			return

		self.customer = guardian.customer_id
		if not self.phone_number:
			self.phone_number = guardian.phone or _get_customer_phone(self.customer) or self.phone_number

	def _validate_conditional_fields(self):
		if self.chief_complaint == "Other" and not self.complaint_other:
			frappe.throw(_("Complaint Details is required when Chief Complaint is Other."))

		if self.chronic_disease and not self.chronic_disease_details:
			frappe.throw(_("Chronic Disease Details is required when Chronic Disease is checked."))

		if self.currently_on_medication and not self.medication_details:
			frappe.throw(_("Medication Details is required when Currently on Medication is checked."))

		if self.previous_vet_visit and not self.previous_vet_details:
			frappe.throw(_("Previous Vet Details is required when Previous Vet Visit is checked."))

		if self.feeding_type == "Other" and not self.feeding_type_other:
			frappe.throw(_("Feeding Type Other is required when Feeding Type is Other."))

	def _validate_status_consistency(self):
		if self.vet_visit and self.status in {"Draft", "Waiting Practitioner"}:
			self.status = "In Consultation"

	def _validate_guardian_pet_link(self):
		if self.guardian and self.animal_patient and not frappe.db.exists(
			"PetGuardian", {"guardian_id": self.guardian, "pet_id": self.animal_patient}
		):
			frappe.throw(
				_("Pet {0} is not linked to Guardian {1}.").format(
					frappe.bold(self.animal_patient), frappe.bold(self.guardian)
				)
			)

	def _sync_status_with_visit(self):
		if not self.vet_visit or not frappe.db.exists("Vet Visit", self.vet_visit):
			return

		visit_status = frappe.db.get_value("Vet Visit", self.vet_visit, "status")
		if visit_status == "Completed":
			target_status = "Closed"
		elif visit_status == "Cancelled":
			target_status = "Converted to Visit"
		else:
			target_status = "In Consultation"

		if self.status != target_status:
			self.db_set("status", target_status, update_modified=False)


def build_case_summary(case_sheet) -> str:
	positive_symptoms = []
	symptom_map = {
		"has_vomiting": _("Vomiting"),
		"has_diarrhea": _("Diarrhea"),
		"has_cough": _("Cough"),
		"has_sneezing": _("Sneezing"),
		"has_loss_of_appetite": _("Loss of Appetite"),
		"has_lethargy": _("Lethargy"),
		"has_itching": _("Itching"),
		"has_wound": _("Wound"),
		"has_limping": _("Limping"),
		"has_breathing_issue": _("Breathing Issue"),
		"has_eye_discharge": _("Eye Discharge"),
		"has_ear_discharge": _("Ear Discharge"),
		"has_fever_flag": _("Fever Flag"),
	}

	for fieldname, label in symptom_map.items():
		if _value(case_sheet, fieldname):
			positive_symptoms.append(label)

	parts = []
	chief_complaint = _value(case_sheet, "chief_complaint")
	if chief_complaint:
		parts.append(_("Chief Complaint: {0}").format(chief_complaint))

	complaint_other = _value(case_sheet, "complaint_other")
	if complaint_other:
		parts.append(_("Complaint Details: {0}").format(complaint_other))

	symptom_duration = _value(case_sheet, "symptom_duration")
	if symptom_duration:
		parts.append(_("Duration: {0}").format(symptom_duration))

	if positive_symptoms:
		parts.append(_("Positive Symptoms: {0}").format(", ".join(positive_symptoms)))

	intake_notes = _value(case_sheet, "intake_notes")
	if intake_notes:
		parts.append(_("Intake Notes: {0}").format(intake_notes.strip()))

	return "\n".join(parts)


@frappe.whitelist()
@standardize_response
def get_pet_context(pet_name: str) -> dict:
	if not pet_name:
		return {}
	if not frappe.has_permission("Pet", doc=pet_name, ptype="read"):
		raise frappe.PermissionError(_("Not permitted to access Pet context."))

	pet = frappe.db.get_value(
		"Pet",
		pet_name,
		["animal_species", "animal_type", "breed", "gender", "weight", "birth_date"],
		as_dict=True,
	)
	if not pet:
		return {}

	guardian = _get_primary_guardian_for_pet(pet_name)
	customer = guardian.customer_id if guardian else None
	phone_number = (guardian.phone if guardian else None) or _get_customer_phone(customer)

	return {
		"guardian": guardian.name if guardian else None,
		"customer": customer,
		"phone_number": phone_number,
		"species": pet.animal_type or pet.animal_species,
		"breed": pet.breed,
		"sex": pet.gender or "Unknown",
		"weight": pet.weight,
		"age_text": _format_pet_age(pet.birth_date),
	}


@frappe.whitelist()
@standardize_response
def start_visit(case_sheet_name: str, doctor_case_choice: str | None = None, care_episode: str | None = None, case_choice_note: str | None = None) -> dict:
	if not case_sheet_name:
		frappe.throw(_("Case Sheet is required."))

	if frappe.session.user != "Administrator":
		frappe.only_for(("Healthcare Practitioner", "Doctor", "System Manager", "Healthcare"))
	require_doctype_permission("Vet Visit", "create")

	doctor = _get_session_doctor()
	if not doctor:
		frappe.throw(_("Start Visit requires a user linked to a Healthcare Practitioner."))
	require_restriction_value("practitioner", doctor)

	case_sheet = frappe.get_doc("Vet Case Sheet", case_sheet_name)
	case_sheet.check_permission("read")
	if not case_sheet.customer and case_sheet.guardian:
		guardian = frappe.db.get_value(
			"Guardian",
			case_sheet.guardian,
			["name", "customer_id", "phone"],
			as_dict=True,
		)
		if guardian:
			case_sheet.customer = get_or_create_customer_from_guardian(guardian)
			case_sheet.save(ignore_permissions=True)

	existing_visit = frappe.db.get_value(
		"Vet Visit",
		{"case_sheet": case_sheet.name},
		["name", "status"],
		as_dict=True,
	)
	if existing_visit:
		if case_sheet.vet_visit != existing_visit.name:
			frappe.db.set_value("Vet Case Sheet", case_sheet.name, "vet_visit", existing_visit.name, update_modified=False)
		if doctor_case_choice:
			visit = frappe.get_doc("Vet Visit", existing_visit.name)
			set_visit_case_choice(visit, doctor_case_choice, episode=care_episode, note=case_choice_note)
		return {
			"name": existing_visit.name,
			"doctype": "Vet Visit",
			"status": existing_visit.status,
			"idempotent": True,
		}

	visit = get_mapped_doc(
		"Vet Case Sheet",
		case_sheet.name,
		{
			"Vet Case Sheet": {
				"doctype": "Vet Visit",
				"field_map": {
					"name": "case_sheet",
					"appointment": "appointment",
					"customer": "customer",
					"animal_patient": "animal_patient",
					"weight": "weight",
				},
			}
		},
		postprocess=_set_visit_defaults,
	)
	visit.insert()
	if doctor_case_choice:
		set_visit_case_choice(visit, doctor_case_choice, episode=care_episode, note=case_choice_note)
	else:
		update_profile_for_visit(visit, clinical_status="Waiting Doctor")
	visit.add_comment("Comment", _("Vet Visit created from Case Sheet by {0}.").format(frappe.session.user))

	frappe.db.set_value(
		"Vet Case Sheet",
		case_sheet.name,
		{
			"vet_visit": visit.name,
			"status": "In Consultation",
		},
		update_modified=False,
	)

	return {
		"name": visit.name,
		"doctype": visit.doctype,
	}


def _set_visit_defaults(source, target):
	target.visit_datetime = now_datetime()
	target.status = "In Progress"
	target.visit_type = _get_visit_type_from_case_sheet(source)
	target.case_summary = build_case_summary(source)
	target.weight = target.weight or source.weight

	doctor = _get_session_doctor()
	if doctor:
		target.doctor = doctor


def _get_visit_type_from_case_sheet(case_sheet) -> str:
	if _value(case_sheet, "chief_complaint") == "Follow-up":
		return "Follow-up"

	if _value(case_sheet, "priority") == "Emergency":
		return "Emergency"

	if _value(case_sheet, "chief_complaint") == "Vaccination":
		return "Vaccination"

	return "Consultation"


def _get_session_doctor() -> str | None:
	return get_practitioner_for_user(frappe.session.user)


def _get_primary_guardian_for_pet(pet_name: str):
	guardian_id = frappe.db.get_value("PetGuardian", {"pet_id": pet_name, "role": "primary_owner"}, "guardian_id")
	if not guardian_id:
		guardian_id = frappe.db.get_value("PetGuardian", {"pet_id": pet_name}, "guardian_id")

	if not guardian_id:
		return None

	return frappe.db.get_value("Guardian", guardian_id, ["name", "customer_id", "phone"], as_dict=True)


def _get_customer_phone(customer_name: str | None) -> str | None:
	if not customer_name or not frappe.db.exists("Customer", customer_name):
		return None

	meta = frappe.get_meta("Customer")
	candidate_fields = ["mobile_no", "mobile", "phone", "primary_phone"]
	fields = [fieldname for fieldname in candidate_fields if meta.has_field(fieldname)]
	if not fields:
		return None

	customer_values = frappe.db.get_value("Customer", customer_name, fields, as_dict=True) or {}
	for fieldname in fields:
		if customer_values.get(fieldname):
			return customer_values.get(fieldname)

	return None


def _format_pet_age(birth_date) -> str:
	if not birth_date:
		return ""

	birth_date = getdate(birth_date)
	today = getdate()
	if birth_date > today:
		return ""

	total_days = date_diff(today, birth_date)
	years = total_days // 365
	months = (total_days % 365) // 30

	if years and months:
		return _("{0}y {1}m").format(years, months)
	if years:
		return _("{0}y").format(years)
	if months:
		return _("{0}m").format(months)
	return _("{0}d").format(total_days)


def _value(doc_or_dict, fieldname: str):
	if isinstance(doc_or_dict, dict):
		return doc_or_dict.get(fieldname)
	return getattr(doc_or_dict, fieldname, None)
