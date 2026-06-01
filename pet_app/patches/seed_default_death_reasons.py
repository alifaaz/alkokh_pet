from __future__ import annotations

import frappe


DEFAULT_DEATH_REASONS = [
	{
		"reason_name": "Cardiac Arrest",
		"reason_code": "CARDIAC_ARREST",
		"category": "Disease / Illness",
		"active": 1,
		"guardian_visible_label": "Passed away due to a critical medical condition",
		"sort_order": 10,
	},
	{
		"reason_name": "Respiratory Failure",
		"reason_code": "RESP_FAILURE",
		"category": "Disease / Illness",
		"active": 1,
		"guardian_visible_label": "Passed away due to respiratory failure",
		"sort_order": 20,
	},
	{
		"reason_name": "Sepsis",
		"reason_code": "SEPSIS",
		"category": "Disease / Illness",
		"active": 1,
		"guardian_visible_label": "Passed away after severe illness",
		"sort_order": 30,
	},
	{
		"reason_name": "Severe Trauma",
		"reason_code": "SEVERE_TRAUMA",
		"category": "Accident / Trauma",
		"active": 1,
		"guardian_visible_label": "Passed away due to severe trauma",
		"sort_order": 40,
	},
	{
		"reason_name": "Surgical Complication",
		"reason_code": "SURG_COMP",
		"category": "Procedure Complication",
		"active": 1,
		"requires_manager_review": 1,
		"requires_incident_report": 1,
		"guardian_visible_label": "Passed away following a procedure complication",
		"sort_order": 50,
	},
	{
		"reason_name": "Anesthesia Reaction",
		"reason_code": "ANESTH_REACTION",
		"category": "Anesthesia Complication",
		"active": 1,
		"requires_manager_review": 1,
		"requires_incident_report": 1,
		"guardian_visible_label": "Passed away following an anesthesia-related complication",
		"sort_order": 60,
	},
	{
		"reason_name": "Advanced Age",
		"reason_code": "ADVANCED_AGE",
		"category": "Natural Death",
		"active": 1,
		"guardian_visible_label": "Passed away naturally",
		"sort_order": 70,
	},
	{
		"reason_name": "Euthanasia - Medical Recommendation",
		"reason_code": "EUTH_MEDICAL",
		"category": "Euthanasia",
		"active": 1,
		"requires_doctor_confirmation": 1,
		"guardian_visible_label": "Passed away after medically recommended euthanasia",
		"sort_order": 80,
	},
	{
		"reason_name": "Euthanasia - Guardian Request",
		"reason_code": "EUTH_GUARDIAN",
		"category": "Euthanasia",
		"active": 1,
		"requires_doctor_confirmation": 1,
		"guardian_visible_label": "Passed away after guardian-requested euthanasia",
		"sort_order": 90,
	},
	{
		"reason_name": "Found Dead During Boarding",
		"reason_code": "FOUND_BOARDING",
		"category": "Boarding Incident",
		"active": 1,
		"requires_manager_review": 1,
		"requires_incident_report": 1,
		"guardian_visible_label": "Passed away during boarding",
		"sort_order": 100,
	},
	{
		"reason_name": "Dead On Arrival",
		"reason_code": "DOA",
		"category": "Emergency / Critical Case",
		"active": 1,
		"guardian_visible_label": "Arrived deceased",
		"sort_order": 110,
	},
	{
		"reason_name": "Unknown Cause",
		"reason_code": "UNKNOWN",
		"category": "Unknown / Found Dead",
		"active": 1,
		"requires_manager_review": 1,
		"guardian_visible_label": "Passed away from an unknown cause",
		"sort_order": 120,
	},
	{
		"reason_name": "External Death Reported By Guardian",
		"reason_code": "EXTERNAL_GUARDIAN",
		"category": "External / Reported By Guardian",
		"active": 1,
		"guardian_visible_label": "Death reported by guardian",
		"sort_order": 130,
	},
]


def execute():
	if not should_seed_default_death_reasons():
		return
	seed_default_death_reasons()


def should_seed_default_death_reasons() -> bool:
	return bool(frappe.conf.get("pet_app_seed_default_death_reasons"))


def seed_default_death_reasons():
	if not frappe.db.exists("DocType", "Pet Death Reason"):
		return

	for row in DEFAULT_DEATH_REASONS:
		docname = frappe.db.exists("Pet Death Reason", row["reason_name"])
		if not docname and row.get("reason_code"):
			docname = frappe.db.get_value("Pet Death Reason", {"reason_code": row["reason_code"]}, "name")

		if docname:
			doc = frappe.get_doc("Pet Death Reason", docname)
		else:
			doc = frappe.new_doc("Pet Death Reason")
			doc.reason_name = row["reason_name"]

		for fieldname, value in row.items():
			if doc.meta.has_field(fieldname):
				doc.set(fieldname, value)
		doc.save(ignore_permissions=True)
