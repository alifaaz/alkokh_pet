from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cstr, flt, now_datetime

from pet_app.api.link_aliases import with_link_aliases
from pet_app.api.response import fail, ok


@frappe.whitelist()
def get_guardian_points(guardian=None):
	try:
		guardian = guardian or frappe.db.get_value("Guardian", {"user_id": frappe.session.user}, "name")
		if not guardian:
			return fail(_("Guardian is required."), code="VALIDATION_ERROR")
		points = _points_balance(guardian)
		return ok(with_link_aliases({"guardian": guardian, "points": points}, guardian_field="guardian", include_pet=False, include_doctor=False, include_provider=False))
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def apply_points_to_invoice(guardian=None, invoice=None, points=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		guardian = guardian or payload.get("guardian")
		invoice = invoice or payload.get("invoice")
		points = flt(points if points is not None else payload.get("points"))
		if not guardian or not invoice or points <= 0:
			return fail(_("Guardian, invoice, and points are required."), code="VALIDATION_ERROR")
		balance = _points_balance(guardian)
		if points > balance:
			return fail(_("Not enough loyalty points."), code="VALIDATION_ERROR", details={"available_points": balance})
		value = points * _amount_per_point()
		frappe.get_doc(
			{
				"doctype": "Pet Loyalty Ledger",
				"guardian": guardian,
				"entry_type": "Redeem",
				"points": -points,
				"reference_doctype": "Sales Invoice",
				"reference_name": invoice,
				"posting_datetime": now_datetime(),
				"note": f"Redeemed against {invoice}; value {value}",
			}
		).insert(ignore_permissions=True)
		return ok(with_link_aliases({"guardian": guardian, "invoice": invoice, "points_applied": points, "discount_amount": value, "remaining_points": balance - points}, guardian_field="guardian", include_pet=False, include_doctor=False, include_provider=False))
	except Exception as exc:
		return _error_response(exc)


def award_points(guardian, points, reference_doctype=None, reference_name=None, note=None):
	if not guardian or not points:
		return
	frappe.get_doc(
		{
			"doctype": "Pet Loyalty Ledger",
			"guardian": guardian,
			"entry_type": "Earn",
			"points": flt(points),
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"posting_datetime": now_datetime(),
			"note": note,
		}
	).insert(ignore_permissions=True)


def _points_balance(guardian):
	rows = frappe.get_all("Pet Loyalty Ledger", filters={"guardian": guardian}, fields=["points"], ignore_permissions=True)
	return sum(flt(row.points) for row in rows)


def _amount_per_point():
	value = frappe.db.get_value("Pet Loyalty Rule", {"rule_type": "Redeem Value", "active": 1}, "amount_per_point")
	return flt(value or 0.01)


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
