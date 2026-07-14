from __future__ import annotations

import frappe
from frappe.utils import add_days, cstr, flt, nowdate

from pet_app.api.response import fail, ok


@frappe.whitelist()
def get_low_stock_alerts(warehouse=None):
	try:
		rules = frappe.get_all(
			"Inventory Alert Rule",
			filters={"alert_type": "Low Stock", "active": 1},
			fields=["name", "item_code", "item_group", "warehouse", "min_qty"],
			ignore_permissions=True,
		)
		alerts = []
		for rule in rules:
			target_warehouse = warehouse or rule.warehouse
			items = [rule.item_code] if rule.item_code else _items_for_group(rule.item_group)
			for item_code in items:
				qty = _stock_qty(item_code, target_warehouse)
				if qty <= flt(rule.min_qty):
					alerts.append({"rule": rule.name, "item_code": item_code, "warehouse": target_warehouse, "current_qty": qty, "min_qty": rule.min_qty})
		return ok({"alerts": alerts}, meta={"total": len(alerts)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_expiry_alerts(days=30, warehouse=None):
	try:
		if not frappe.db.exists("DocType", "Batch"):
			return ok({"alerts": []}, meta={"total": 0})
		filters = {"expiry_date": ["between", [nowdate(), add_days(nowdate(), int(days or 30))]]}
		rows = frappe.get_all("Batch", filters=filters, fields=["name", "item", "expiry_date", "batch_qty"], ignore_permissions=True)
		alerts = [{"batch": row.name, "item_code": row.item, "expiry_date": row.expiry_date, "qty": row.get("batch_qty")} for row in rows]
		return ok({"alerts": alerts}, meta={"total": len(alerts)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_reorder_suggestions(warehouse=None):
	try:
		low_stock = get_low_stock_alerts(warehouse=warehouse)
		if not low_stock.get("ok"):
			return low_stock
		suggestions = []
		for alert in low_stock["data"]["alerts"]:
			suggested_qty = max(flt(alert["min_qty"]) * 2 - flt(alert["current_qty"]), 0)
			suggestions.append({**alert, "suggested_qty": suggested_qty})
		return ok({"suggestions": suggestions}, meta={"total": len(suggestions)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_medication_usage_forecast(days=30):
	try:
		rows = frappe.db.sql(
			"""
			select coalesce(m.medication_item, med.linked_item) item_code, sum(case when coalesce(m.dispensed_qty, 0) > 0 then m.dispensed_qty else coalesce(m.qty, 0) end) qty
			from `tabVet Visit Medication Item` m
			left join `tabMedication` med on med.name = m.medication
			inner join `tabVet Visit` v on v.name = m.parent
			where v.visit_datetime >= date_sub(now(), interval %(days)s day)
			group by item_code
			order by qty desc
			""",
			{"days": int(days or 30)},
			as_dict=True,
		)
		forecast = [{"item_code": row.item_code, "period_qty": flt(row.qty), "daily_avg": flt(row.qty) / int(days or 30)} for row in rows if row.item_code]
		return ok({"forecast": forecast}, meta={"total": len(forecast)})
	except Exception as exc:
		return _error_response(exc)


def _items_for_group(item_group):
	filters = {"disabled": 0}
	if item_group:
		filters["item_group"] = item_group
	return frappe.get_all("Item", filters=filters, pluck="name", ignore_permissions=True)


def _stock_qty(item_code, warehouse=None):
	filters = {"item_code": item_code}
	if warehouse:
		filters["warehouse"] = warehouse
	rows = frappe.get_all("Bin", filters=filters, fields=["actual_qty"], ignore_permissions=True)
	return sum(flt(row.actual_qty) for row in rows)


def _error_response(exc):
	return fail(cstr(exc), code=getattr(exc, "exc_type", None) or exc.__class__.__name__)
