from __future__ import annotations

import frappe
from frappe.utils import cint, cstr, flt, getdate, nowdate

from pet_app.api.response import fail, ok


@frappe.whitelist()
def evaluate_visit(visit):
	try:
		doc = frappe.get_doc("Vet Visit", visit)
		alerts = []
		alerts.extend(_vital_alerts(doc))
		alerts.extend(_medication_alerts(doc))
		alerts.extend(_vaccination_alerts(doc))
		for alert in alerts:
			_create_alert_log(doc, alert)
		return ok({"alerts": alerts}, meta={"total": len(alerts)})
	except Exception as exc:
		return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)


def _vital_alerts(visit):
	pet = frappe.db.get_value("Pet", visit.animal_patient, ["animal_type", "animal_species"], as_dict=True) or {}
	species = pet.get("animal_type") or pet.get("animal_species")
	ranges = frappe.get_all("Species Vital Range", filters={"active": 1}, fields=["name", "species", "vital_field", "min_value", "max_value", "severity"], ignore_permissions=True)
	alerts = []
	for row in ranges:
		if row.species and species and row.species != species:
			continue
		value = visit.get(row.vital_field)
		if value in (None, ""):
			continue
		if (row.min_value not in (None, "") and flt(value) < flt(row.min_value)) or (row.max_value not in (None, "") and flt(value) > flt(row.max_value)):
			alerts.append({"alert_type": "Abnormal Vital", "severity": row.severity, "message": f"{row.vital_field} is outside expected range", "rule": row.name})
	return alerts


def _medication_alerts(visit):
	alerts = []
	seen = set()
	for row in visit.get("prescribed_medications") or []:
		med = row.medication or row.medication_item
		if med in seen:
			alerts.append({"alert_type": "Duplicate Medication", "severity": "Warning", "message": f"Duplicate medication: {med}"})
		seen.add(med)
	if visit.get("prescribed_medications") and not visit.weight:
		alerts.append({"alert_type": "Missing Weight", "severity": "Warning", "message": "Weight is required before medication dosing."})
	profile = frappe.db.get_value("Pet Medical Profile", {"pet": visit.animal_patient}, ["allergies"], as_dict=True) if frappe.db.exists("DocType", "Pet Medical Profile") else None
	if profile and profile.allergies:
		for row in visit.get("prescribed_medications") or []:
			if cstr(row.medication or row.medication_item).lower() in cstr(profile.allergies).lower():
				alerts.append({"alert_type": "Allergy", "severity": "Critical", "message": f"Medication may conflict with allergy: {row.medication or row.medication_item}"})
	return alerts


def _vaccination_alerts(visit):
	if not frappe.db.exists("DocType", "Preventive Care Record"):
		return []
	# The `is set` guard is here for robustness, not because this call is broken: measured,
	# `frappe.db.exists` does NOT match a NULL date against `<`, while `frappe.get_all` and
	# `frappe.get_list` DO (they render the comparison through `ifnull(...)`, turning NULL into
	# a very old date). That difference is undocumented, so relying on it would make this
	# correct by accident - and it would break silently the day this is rewritten as a
	# get_all. A dose with no next due date has no recurrence and cannot be overdue.
	overdue = frappe.get_all(
		"Preventive Care Record",
		filters=[
			["pet", "=", visit.animal_patient],
			["kind", "=", "Vaccination"],
			["next_due_date", "is", "set"],
			["next_due_date", "<", getdate(nowdate())],
			["reminder_status", "!=", "Cancelled"],
			# A cancelled dose was never given, so it cannot be overdue for a next one.
			# The doctype this replaces had no status and so could not make the distinction.
			["status", "!=", "Cancelled"],
		],
		limit=1,
		ignore_permissions=True,
	)
	return [{"alert_type": "Vaccination Overdue", "severity": "Info", "message": "Vaccination is overdue."}] if overdue else []


def _create_alert_log(visit, alert):
	if not frappe.db.exists("DocType", "Clinical Alert Log"):
		return
	frappe.get_doc({"doctype": "Clinical Alert Log", "visit": visit.name, "pet": visit.animal_patient, "alert_rule": alert.get("rule"), "alert_type": alert.get("alert_type"), "severity": alert.get("severity"), "message": alert.get("message"), "status": "Open"}).insert(ignore_permissions=True)
