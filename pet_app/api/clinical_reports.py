from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate, nowdate

from pet_app.api.response import fail, ok
from pet_app.api.link_aliases import enrich_link_aliases


@frappe.whitelist()
def clinical_daily_summary(date=None):
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
			select doctor, count(*) as visits, sum(case when status = 'Completed' then 1 else 0 end) as completed
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
			conditions, values = _date_conditions("creation", date_from, date_to, table_alias="d")
			rows = frappe.db.sql(
				f"""
				select
					'{doctype}' as doctype,
					count(*) as total,
					avg(timestampdiff(minute, d.creation, d.released_at)) as avg_minutes
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
def unbilled_visits(date_from=None, date_to=None):
	try:
		filters = {"docstatus": ["<", 2], "billed": 0}
		if date_from and date_to:
			filters["visit_datetime"] = ["between", [f"{getdate(date_from)} 00:00:00", f"{getdate(date_to)} 23:59:59"]]
		rows = frappe.get_all(
			"Vet Visit",
			filters=filters,
			fields=["name", "visit_datetime", "doctor", "animal_patient", "guardian", "status", "total_billable_amount"],
			order_by="visit_datetime desc",
			ignore_permissions=True,
		)
		visits = [dict(row) for row in rows]
		enrich_link_aliases(visits, pet_field="animal_patient", guardian_field="guardian", doctor_field="doctor", include_provider=False)
		return ok({"visits": visits}, meta={"total": len(visits), "amount": sum(flt(row.total_billable_amount) for row in rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def follow_up_due(date=None):
	try:
		target = getdate(date or nowdate())
		rows = frappe.get_all(
			"Vet Visit",
			filters={"follow_up_required": 1, "follow_up_date": ["<=", target], "follow_up_status": ["in", ["Requested", "Scheduled", "Contacted", "Missed"]]},
			fields=["name", "follow_up_date", "follow_up_status", "animal_patient", "guardian", "doctor", "follow_up_reason"],
			order_by="follow_up_date asc",
			ignore_permissions=True,
		)
		follow_ups = [dict(row) for row in rows]
		enrich_link_aliases(follow_ups, pet_field="animal_patient", guardian_field="guardian", doctor_field="doctor", include_provider=False)
		return ok({"follow_ups": follow_ups}, meta={"total": len(follow_ups)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def top_diagnoses(date_from=None, date_to=None, limit=20):
	try:
		limit = max(min(int(limit or 20), 100), 1)
		conditions, values = _date_conditions("v.visit_datetime", date_from, date_to)
		values["limit"] = limit
		rows = frappe.db.sql(
			f"""
			select t.diagnosis_label as diagnosis, count(*) as total
			from (
				select coalesce(dis.disease_name, d.disease, v.illness, 'Unspecified') as diagnosis_label
				from `tabVet Visit` v
				left join `tabVisit Diagnosis` d on d.parent = v.name and d.parenttype = 'Vet Visit'
				left join `tabDisease` dis on dis.name = d.disease
				where v.docstatus < 2 {conditions}
			) t
			group by t.diagnosis_label
			order by total desc
			limit %(limit)s
			""",
			values,
			as_dict=True,
		)
		return ok({"diagnoses": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def medication_usage(date_from=None, date_to=None, limit=50):
	try:
		limit = max(min(int(limit or 50), 200), 1)
		conditions, values = _date_conditions("v.visit_datetime", date_from, date_to)
		values["limit"] = limit
		rows = frappe.db.sql(
			f"""
			select
				coalesce(m.medication, m.medication_item) as medication,
				sum(coalesce(m.qty, 0)) as prescribed_qty,
				sum(coalesce(m.dispensed_qty, 0)) as dispensed_qty,
				count(*) as rows_count
			from `tabVet Visit Medication Item` m
			inner join `tabVet Visit` v on v.name = m.parent
			where v.docstatus < 2 {conditions}
			group by medication
			order by dispensed_qty desc, prescribed_qty desc
			limit %(limit)s
			""",
			values,
			as_dict=True,
		)
		return ok({"medications": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


def _date_conditions(fieldname, date_from=None, date_to=None, table_alias=None):
	prefix = f"{table_alias}." if table_alias and "." not in fieldname else ""
	field = f"{prefix}{fieldname}"
	values = {}
	conditions = ""
	if date_from and date_to:
		conditions = f" and {field} between %(date_from)s and %(date_to)s"
		values["date_from"] = f"{getdate(date_from)} 00:00:00"
		values["date_to"] = f"{getdate(date_to)} 23:59:59"
	elif date_from:
		conditions = f" and {field} >= %(date_from)s"
		values["date_from"] = f"{getdate(date_from)} 00:00:00"
	elif date_to:
		conditions = f" and {field} <= %(date_to)s"
		values["date_to"] = f"{getdate(date_to)} 23:59:59"
	return conditions, values


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return fail(_("Not permitted"), code="PERMISSION_ERROR")
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
