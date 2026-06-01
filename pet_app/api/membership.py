from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import add_months, cstr, getdate, nowdate

from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.response import fail, ok


@frappe.whitelist()
def list_plans(active=1):
	try:
		filters = {"active": 1} if int(active or 0) else {}
		rows = frappe.get_all(
			"Pet Membership Plan",
			filters=filters,
			fields=["name", "plan_name", "monthly_fee", "annual_fee", "discount_percent", "points_multiplier", "description", "active"],
			order_by="monthly_fee asc, plan_name asc",
			ignore_permissions=True,
		)
		return ok({"plans": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def subscribe_guardian(guardian=None, plan=None, start_date=None, months=12, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		guardian = guardian or payload.get("guardian")
		plan = plan or payload.get("plan")
		if not guardian or not plan:
			return fail(_("Guardian and Plan are required."), code="VALIDATION_ERROR")
		existing = frappe.db.get_value("Pet Membership Subscription", {"guardian": guardian, "status": "Active"}, "name")
		if existing:
			doc = frappe.get_doc("Pet Membership Subscription", existing)
			doc.plan = plan
		else:
			doc = frappe.new_doc("Pet Membership Subscription")
			doc.guardian = guardian
			doc.plan = plan
		doc.status = "Active"
		doc.start_date = getdate(start_date or payload.get("start_date") or nowdate())
		doc.end_date = add_months(doc.start_date, int(months or payload.get("months") or 12))
		doc.note = payload.get("note")
		doc.save(ignore_permissions=True)
		return ok({"subscription": _subscription_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_subscription_status(guardian=None):
	try:
		if not guardian:
			guardian = frappe.db.get_value("Guardian", {"user_id": frappe.session.user}, "name")
		if not guardian:
			return fail(_("Guardian is required."), code="VALIDATION_ERROR")
		name = frappe.db.get_value("Pet Membership Subscription", {"guardian": guardian, "status": "Active"}, "name")
		if not name:
			return ok({"subscription": {}, "active": False})
		doc = frappe.get_doc("Pet Membership Subscription", name)
		return ok({"subscription": _subscription_payload(doc), "active": True})
	except Exception as exc:
		return _error_response(exc)


def active_subscription_for_guardian(guardian):
	name = frappe.db.get_value("Pet Membership Subscription", {"guardian": guardian, "status": "Active"}, "name")
	return frappe.get_doc("Pet Membership Subscription", name) if name else None


def plan_discount_percent(guardian):
	subscription = active_subscription_for_guardian(guardian)
	if not subscription:
		return 0
	return frappe.db.get_value("Pet Membership Plan", subscription.plan, "discount_percent") or 0


def _subscription_payload(doc) -> dict:
	plan = frappe.db.get_value("Pet Membership Plan", doc.plan, ["plan_name", "discount_percent", "points_multiplier"], as_dict=True) or {}
	payload = {
		"name": doc.name,
		"guardian": doc.guardian,
		"plan": doc.plan,
		"plan_name": plan.get("plan_name"),
		"discount_percent": plan.get("discount_percent"),
		"points_multiplier": plan.get("points_multiplier"),
		"status": doc.status,
		"start_date": doc.start_date,
		"end_date": doc.end_date,
	}
	return with_link_aliases(payload, guardian_field="guardian", include_pet=False, include_doctor=False, include_provider=False)


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
