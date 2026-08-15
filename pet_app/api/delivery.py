from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.api.response import fail, ok, standardize_response


@frappe.whitelist(methods=["POST"])
def assign_driver(driver=None, sales_order=None, sales_invoice=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		driver = driver or payload.get("driver")
		if not driver:
			return fail(_("Driver is required."), code="VALIDATION_ERROR")
		doc = frappe.get_doc(
			{
				"doctype": "Delivery Assignment",
				"driver": driver,
				"sales_order": sales_order or payload.get("sales_order"),
				"sales_invoice": sales_invoice or payload.get("sales_invoice"),
				"guardian": payload.get("guardian"),
				"customer": payload.get("customer"),
				"status": "Assigned",
				"delivery_address": payload.get("delivery_address"),
				"cash_to_collect": payload.get("cash_to_collect"),
				"assigned_at": now_datetime(),
			}
		)
		doc.insert(ignore_permissions=True)
		return ok({"assignment": _assignment_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_driver_tasks(driver=None, status=None):
	try:
		# Driver.custom_user does not exist and never did - this half of the expression
		# queried a missing column. The stock Driver.user field is the link, and
		# sync_driver_user is what populates it, so there is nothing to add: repointed
		# rather than creating a second field holding the same fact.
		driver = driver or frappe.db.get_value("Driver", {"user": frappe.session.user}, "name")
		filters = {"driver": driver} if driver else {}
		if status:
			filters["status"] = status
		else:
			filters["status"] = ["in", ["Assigned", "Picked Up"]]
		rows = frappe.get_all("Delivery Assignment", filters=filters, fields=["*"], order_by="assigned_at asc, creation asc", ignore_permissions=True)
		tasks = [dict(row) for row in rows]
		enrich_link_aliases(tasks, guardian_field="guardian", include_pet=False, include_doctor=False, include_provider=False)
		return ok({"tasks": tasks}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def mark_delivered(assignment=None, data=None, **kwargs):
	return _set_assignment_status(assignment, data, kwargs, "Delivered")


@frappe.whitelist(methods=["POST"])
@standardize_response
def mark_failed_delivery(assignment=None, reason=None, data=None, **kwargs):
	return _set_assignment_status(assignment, data, kwargs, "Failed", reason=reason)


@frappe.whitelist(methods=["POST"])
def upload_delivery_proof(assignment=None, file=None, proof_type="Photo", note=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		assignment = assignment or payload.get("assignment")
		if not assignment:
			return fail(_("Assignment is required."), code="VALIDATION_ERROR")
		doc = frappe.get_doc(
			{
				"doctype": "Delivery Proof",
				"assignment": assignment,
				"proof_type": proof_type or payload.get("proof_type") or "Photo",
				"file": file or payload.get("file"),
				"received_by": payload.get("received_by"),
				"captured_at": now_datetime(),
				"note": note or payload.get("note"),
			}
		).insert(ignore_permissions=True)
		return ok({"proof": {key: doc.get(key) for key in ("name", "assignment", "proof_type", "file", "received_by", "captured_at", "note")}})
	except Exception as exc:
		return _error_response(exc)


def _set_assignment_status(assignment, data, kwargs, status, reason=None):
	try:
		payload = _payload(data, kwargs)
		doc = frappe.get_doc("Delivery Assignment", assignment or payload.get("assignment"))
		doc.status = status
		if status == "Delivered":
			doc.delivered_at = now_datetime()
		if status == "Failed":
			doc.failure_reason = reason or payload.get("reason")
		doc.save(ignore_permissions=True)
		return ok({"assignment": _assignment_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


def _assignment_payload(doc) -> dict:
	return with_link_aliases(
		{key: doc.get(key) for key in ("name", "driver", "sales_order", "sales_invoice", "guardian", "customer", "status", "delivery_address", "cash_to_collect", "assigned_at", "delivered_at", "failure_reason")},
		guardian_field="guardian",
		include_pet=False,
		include_doctor=False,
		include_provider=False,
	)


def _payload(data, kwargs):
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
