from __future__ import annotations

import frappe


PRACTITIONER_DOCTYPE = "Healthcare Practitioner"
DOCTOR_DOCTYPE = "Doctor"

LINK_FIELDS = (
	("Vet Visit", "doctor"),
	("Visit Consult Request", "requested_doctor"),
	("PetCareService", "doctor"),
	("Lab", "doctor"),
	("Imaging", "doctor"),
	("Pet Procedure", "doctor"),
	("Pet Consent Form", "doctor"),
	("Pet Vaccination Record", "doctor"),
	("Pet Deworming Record", "doctor"),
	("Vet Visit Addendum", "doctor"),
	("Pet Queue Ticket", "doctor"),
	("Appointment", "custom_doctor"),
	("Doctor Availability", "doctor"),
	("Practitioner Availability", "practitioner"),
)


def execute():
	ensure_roles()
	migrate_doctor_records()
	migrate_doctype_metadata()
	migrate_case_sheet_status()
	migrate_practitioner_availability()
	frappe.clear_cache()


def ensure_roles():
	for role in ("Healthcare Practitioner", "Healthcare", "Doctor"):
		if frappe.db.exists("Role", role):
			continue
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)


def migrate_doctor_records():
	if not frappe.db.exists("DocType", DOCTOR_DOCTYPE) or not frappe.db.exists("DocType", PRACTITIONER_DOCTYPE):
		return

	rows = frappe.get_all(
		DOCTOR_DOCTYPE,
		fields=["name", "doctor_name", "phone", "user", "specialization", "photo", "disabled"],
		ignore_permissions=True,
	)
	mapping = {}
	for row in rows:
		target_name = _resolve_practitioner_target(row)
		mapping[row.name] = target_name
		_sync_practitioner_from_doctor(row, target_name)

	_update_link_values(mapping)


def _resolve_practitioner_target(row) -> str:
	if frappe.db.exists(PRACTITIONER_DOCTYPE, row.name):
		return row.name
	if row.user:
		existing = frappe.db.get_value(PRACTITIONER_DOCTYPE, {"user_id": row.user}, "name")
		if existing:
			return existing
	if row.phone:
		existing = frappe.db.get_value(PRACTITIONER_DOCTYPE, {"phone": row.phone}, "name")
		if existing:
			return existing
	return row.name


def _sync_practitioner_from_doctor(row, target_name: str):
	payload = {
		"practitioner_name": row.doctor_name,
		"practitioner_type": "Doctor",
		"phone": row.phone,
		"user_id": row.user,
		"specialization": row.specialization,
		"photo": row.photo,
		"disabled": row.disabled,
	}
	if frappe.db.exists(PRACTITIONER_DOCTYPE, target_name):
		doc = frappe.get_doc(PRACTITIONER_DOCTYPE, target_name)
		for fieldname, value in payload.items():
			if value is not None:
				doc.set(fieldname, value)
		doc.save(ignore_permissions=True)
		return

	payload.update({"doctype": PRACTITIONER_DOCTYPE, "name": target_name})
	doc = frappe.get_doc(payload)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)


def _update_link_values(mapping: dict[str, str]):
	for old_name, new_name in mapping.items():
		if not old_name or not new_name or old_name == new_name:
			continue
		for doctype, fieldname in LINK_FIELDS:
			if not _table_has_column(doctype, fieldname):
				continue
			frappe.db.sql(f"update `tab{doctype}` set `{fieldname}`=%s where `{fieldname}`=%s", (new_name, old_name))


def migrate_doctype_metadata():
	for table in ("DocField", "Custom Field"):
		if not frappe.db.exists("DocType", table):
			continue
		for row in frappe.get_all(
			table,
			filters={"fieldtype": "Link", "options": DOCTOR_DOCTYPE},
			fields=["name", "label"],
			ignore_permissions=True,
		):
			updates = {"options": PRACTITIONER_DOCTYPE}
			if row.label in {"Doctor", "doctor"}:
				updates["label"] = "Healthcare Practitioner"
			elif row.label == "Requested Doctor":
				updates["label"] = "Requested Healthcare Practitioner"
			frappe.db.set_value(table, row.name, updates, update_modified=False)

	_label_updates = {
		"Doctor Notes": "Practitioner Notes",
		"Doctor Reviewed": "Practitioner Reviewed",
		"Doctor Reviewed At": "Practitioner Reviewed At",
	}
	for old_label, new_label in _label_updates.items():
		for table in ("DocField", "Custom Field"):
			for row in frappe.get_all(table, filters={"label": old_label}, pluck="name", ignore_permissions=True):
				frappe.db.set_value(table, row, "label", new_label, update_modified=False)

	_replace_docfield_options("Vet Case Sheet", "status", "Waiting Doctor", "Waiting Practitioner")


def _replace_docfield_options(parent: str, fieldname: str, old: str, new: str):
	for table in ("DocField", "Custom Field"):
		parent_field = "parent" if table == "DocField" else "dt"
		for row in frappe.get_all(
			table,
			filters={parent_field: parent, "fieldname": fieldname},
			fields=["name", "options"],
			ignore_permissions=True,
		):
			if old in (row.options or ""):
				frappe.db.set_value(table, row.name, "options", row.options.replace(old, new), update_modified=False)


def migrate_case_sheet_status():
	if _table_has_column("Vet Case Sheet", "status"):
		frappe.db.sql("update `tabVet Case Sheet` set status='Waiting Practitioner' where status='Waiting Doctor'")


def migrate_practitioner_availability():
	if not frappe.db.exists("DocType", "Doctor Availability") or not frappe.db.exists("DocType", "Practitioner Availability"):
		return
	rows = frappe.get_all(
		"Doctor Availability",
		fields=["name", "doctor", "day_of_week", "date", "start_time", "end_time", "slot_minutes", "active"],
		ignore_permissions=True,
	)
	for row in rows:
		target_name = row.name
		if frappe.db.exists("Practitioner Availability", target_name):
			doc = frappe.get_doc("Practitioner Availability", target_name)
		else:
			doc = frappe.new_doc("Practitioner Availability")
			doc.name = target_name
		doc.practitioner = row.doctor
		doc.day_of_week = row.day_of_week
		doc.date = row.date
		doc.start_time = row.start_time
		doc.end_time = row.end_time
		doc.slot_minutes = row.slot_minutes
		doc.active = row.active
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)


def _table_has_column(doctype: str, fieldname: str) -> bool:
	return frappe.db.exists("DocType", doctype) and frappe.db.has_column(doctype, fieldname)
