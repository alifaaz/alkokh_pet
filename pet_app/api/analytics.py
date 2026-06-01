from __future__ import annotations

import frappe
from frappe.utils import cstr, flt, getdate, nowdate

from pet_app.api.response import fail, ok, standardize_response
from pet_app.api.link_aliases import enrich_link_aliases


@frappe.whitelist()
def clinic_daily_report(date=None):
	try:
		target = getdate(date or nowdate())
		rows = frappe.get_all(
			"Vet Visit",
			filters={"visit_datetime": ["between", [f"{target} 00:00:00", f"{target} 23:59:59"]]},
			fields=["name", "status", "doctor", "animal_patient", "guardian", "billing_status", "total_billable_amount"],
			ignore_permissions=True,
		)
		visits = [dict(row) for row in rows]
		enrich_link_aliases(visits, pet_field="animal_patient", guardian_field="guardian", doctor_field="doctor", include_provider=False)
		return ok({"date": target, "visits": visits}, meta={"total": len(visits)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def doctor_performance(date_from=None, date_to=None):
	try:
		conditions, values = _date_conditions("visit_datetime", date_from, date_to)
		rows = frappe.db.sql(
			f"""
			select doctor, count(*) visits, sum(case when status='Completed' then 1 else 0 end) completed
			from `tabVet Visit`
			where docstatus < 2 {conditions}
			group by doctor
			order by visits desc
			""",
			values,
			as_dict=True,
		)
		doctors = [dict(row) for row in rows]
		enrich_link_aliases(doctors, doctor_field="doctor", include_pet=False, include_guardian=False, include_provider=False)
		return ok({"doctors": doctors}, meta={"total": len(doctors)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def diagnostics_turnaround(date_from=None, date_to=None):
	try:
		data = []
		for doctype in ("Lab", "Imaging"):
			if not frappe.db.has_column(doctype, "released_at"):
				continue
			conditions, values = _date_conditions("creation", date_from, date_to, alias="d")
			rows = frappe.db.sql(
				f"""
				select '{doctype}' doctype, count(*) total, avg(timestampdiff(minute, d.creation, d.released_at)) avg_minutes
				from `tab{doctype}` d
				where d.docstatus < 2 and d.released_at is not null {conditions}
				""",
				values,
				as_dict=True,
			)
			data.extend(dict(row) for row in rows)
		return ok({"diagnostics": data}, meta={"total": len(data)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def boarding_occupancy(date=None):
	try:
		target = getdate(date or nowdate())
		total_rooms = frappe.db.count("Service Room", {"status": "Active"}) if frappe.db.exists("DocType", "Service Room") else 0
		occupied = frappe.db.count("Pet Boarding", {"record_status": ["in", ["Reserved", "Checked In"]], "docstatus": ["<", 2]}) if frappe.db.exists("DocType", "Pet Boarding") else 0
		return ok({"date": target, "total_rooms": total_rooms, "occupied": occupied, "available": max(total_rooms - occupied, 0), "occupancy_rate": flt(occupied) / total_rooms * 100 if total_rooms else 0})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def guardian_retention(months=12):
	try:
		rows = frappe.db.sql(
			"""
			select guardian, count(*) visits, min(visit_datetime) first_visit, max(visit_datetime) last_visit
			from `tabVet Visit`
			where guardian is not null and docstatus < 2
			group by guardian
			having visits > 1
			order by visits desc
			""",
			as_dict=True,
		)
		guardians = [dict(row) for row in rows]
		enrich_link_aliases(guardians, guardian_field="guardian", include_pet=False, include_doctor=False, include_provider=False)
		return ok({"guardians": guardians}, meta={"total": len(guardians)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def revenue_by_branch(date_from=None, date_to=None):
	try:
		conditions, values = _date_conditions("posting_date", date_from, date_to, alias="si", date_only=True)
		branch_columns = []
		if frappe.db.has_column("Sales Invoice", "branch"):
			branch_columns.append("si.branch")
		if frappe.db.has_column("Sales Invoice", "custom_branch"):
			branch_columns.append("si.custom_branch")
		branch_expr = f"coalesce({', '.join(branch_columns)}, 'Default')" if branch_columns else "'Default'"
		rows = frappe.db.sql(
			f"""
			select {branch_expr} branch, sum(si.grand_total) revenue
			from `tabSales Invoice` si
			where si.docstatus = 1 {conditions}
			group by branch
			order by revenue desc
			""",
			values,
			as_dict=True,
		)
		return ok({"branches": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
@standardize_response
def pet_mortality_summary(date_from=None, date_to=None):
	return _mortality_group(None, date_from, date_to)


@frappe.whitelist()
@standardize_response
def mortality_by_reason(date_from=None, date_to=None):
	return _mortality_group("death_reason_category", date_from, date_to)


@frappe.whitelist()
@standardize_response
def mortality_by_source(date_from=None, date_to=None):
	return _mortality_group("source_doctype", date_from, date_to)


@frappe.whitelist()
def unexpected_death_review():
	try:
		rows = frappe.get_all(
			"Pet Death Record",
			filters={"was_unexpected": 1, "status": ["!=", "Cancelled"], "manager_review_status": ["in", ["Pending", "Not Required"]]},
			fields=["name", "pet", "guardian", "death_datetime", "death_reason_category", "source_doctype", "source_name", "manager_review_status"],
			order_by="death_datetime desc",
			ignore_permissions=True,
		)
		records = [dict(row) for row in rows]
		enrich_link_aliases(records, pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)
		return ok({"records": records}, meta={"total": len(records)})
	except Exception as exc:
		return _error_response(exc)


def _mortality_group(group_field=None, date_from=None, date_to=None):
	try:
		if not frappe.db.exists("DocType", "Pet Death Record"):
			return ok({"records": []}, meta={"total": 0})
		if group_field:
			conditions, values = _date_conditions("death_datetime", date_from, date_to)
			rows = frappe.db.sql(
				f"""
				select coalesce({group_field}, 'Unknown') label, count(*) total
				from `tabPet Death Record`
				where status != 'Cancelled' {conditions}
				group by label
				order by total desc
				""",
				values,
				as_dict=True,
			)
			return ok({"groups": [dict(row) for row in rows]}, meta={"total": len(rows)})
		conditions, values = _date_conditions("death_datetime", date_from, date_to)
		row = frappe.db.sql(
			f"""
			select count(*) total, sum(was_unexpected) unexpected, sum(requires_manager_review) manager_review_required
			from `tabPet Death Record`
			where status != 'Cancelled' {conditions}
			""",
			values,
			as_dict=True,
		)[0]
		return ok({"summary": dict(row)})
	except Exception as exc:
		return _error_response(exc)


def _date_conditions(fieldname, date_from=None, date_to=None, alias=None, date_only=False):
	prefix = f"{alias}." if alias else ""
	field = f"{prefix}{fieldname}"
	values = {}
	if date_from and date_to:
		values["date_from"] = getdate(date_from) if date_only else f"{getdate(date_from)} 00:00:00"
		values["date_to"] = getdate(date_to) if date_only else f"{getdate(date_to)} 23:59:59"
		return f" and {field} between %(date_from)s and %(date_to)s", values
	if date_from:
		values["date_from"] = getdate(date_from) if date_only else f"{getdate(date_from)} 00:00:00"
		return f" and {field} >= %(date_from)s", values
	if date_to:
		values["date_to"] = getdate(date_to) if date_only else f"{getdate(date_to)} 23:59:59"
		return f" and {field} <= %(date_to)s", values
	return "", values


def _error_response(exc):
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
