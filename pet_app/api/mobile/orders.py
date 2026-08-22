from __future__ import annotations

import functools
import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from pet_app.api import order as order_api
from pet_app.api.mobile.addresses import assert_customer_address
from pet_app.api.mobile.response import error, ok
from pet_app.utils.guardian_customer import get_guardian_by_user


ORDER_NOT_FOUND = "order.not_found"
ORDER_NOT_CANCELLABLE = "order.not_cancellable"
ORDER_REQUEST_INVALID = "order.request_invalid"
ORDER_PERMISSION_DENIED = "auth.wrong_credentials"
PAYMENT_UNSUPPORTED = "payment.unsupported"
CASH_PAYMENT_METHOD = "Cash on Delivery"

ORDER_STATUS_STEPS = ("Draft", "Preparing", "Out for Delivery", "Cash Collected", "Completed")
ORDER_TERMINAL_STATUSES = ("Returned", "Cancelled")
ORDER_STATUS_FIELDS = (
	"custom_order_status",
	"custom_payment_method",
	"custom_payment_status",
	"custom_driver",
)


class MobileOrderError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _mobile_order_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except MobileOrderError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except frappe.PermissionError as exc:
			return error(ORDER_PERMISSION_DENIED, cstr(exc) or _("Not permitted"), 401)
		except Exception as exc:
			code = _map_order_error(exc)
			status = 401 if code == ORDER_PERMISSION_DENIED else 400
			return error(code, cstr(exc), status)

	return wrapper


def _map_order_error(exc) -> str:
	message = cstr(exc).lower()
	if "not permitted" in message or "not authorized" in message or "guardian context" in message:
		return ORDER_PERMISSION_DENIED
	if "cannot change status" in message or "not cancellable" in message:
		return ORDER_NOT_CANCELLABLE
	if "does not exist" in message or "not found" in message:
		return ORDER_NOT_FOUND
	return getattr(exc, "code", None) or exc.__class__.__name__


def _current_guardian() -> dict:
	if frappe.session.user == "Guest":
		raise MobileOrderError(ORDER_PERMISSION_DENIED, _("Authentication required."), 401)

	guardian = get_guardian_by_user(frappe.session.user)
	if not guardian:
		raise MobileOrderError(
			ORDER_PERMISSION_DENIED,
			_("No Guardian is linked to the current user."),
			401,
		)
	return guardian


def _guardian_customer(guardian: dict) -> str:
	customer = guardian.get("customer_id")
	if not customer and guardian.get("name"):
		customer = frappe.db.get_value("Guardian", guardian.get("name"), "customer_id")
	if not customer:
		raise MobileOrderError(
			ORDER_PERMISSION_DENIED,
			_("No Customer is linked to the current Guardian."),
			401,
		)
	return customer


def _coerce_items(items):
	if isinstance(items, str):
		items = json.loads(items)
	if not isinstance(items, list):
		raise MobileOrderError(ORDER_REQUEST_INVALID, _("Items payload must be a list."))
	if not items:
		raise MobileOrderError(ORDER_REQUEST_INVALID, _("Cart is empty."))
	return items


def _normalize_payment_method(payment_method=None) -> str:
	value = cstr(payment_method or CASH_PAYMENT_METHOD).strip()
	key = value.lower().replace("-", " ").replace("_", " ")
	if key in {"", "cash", "cod", "cash on delivery"}:
		return CASH_PAYMENT_METHOD
	raise MobileOrderError(
		PAYMENT_UNSUPPORTED,
		_("Only Cash on Delivery is currently supported for mobile orders."),
	)


def _item_price(item_code: str):
	return frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": "Standard Selling"},
		"price_list_rate",
	)


def _cart_quote(items, coupon_code=None) -> dict:
	items = _coerce_items(items)
	quote_items = []
	issues = []
	subtotal = 0.0

	for item in items:
		item_code = cstr(item.get("item_code")).strip()
		qty = flt(item.get("qty"))
		if not item_code or not frappe.db.exists("Item", item_code):
			issues.append(
				{
					"code": "cart.variant_unavailable",
					"message": _("Item {0} is unavailable.").format(item_code or _("Unknown")),
					"item_code": item_code,
				}
			)
			continue
		if qty <= 0:
			issues.append(
				{
					"code": ORDER_REQUEST_INVALID,
					"message": _("Quantity must be greater than zero."),
					"item_code": item_code,
				}
			)
			continue

		# Same resolver place_order uses. Anything else and the quote would advertise stock
		# from one warehouse while the order checked another.
		stock_qty = flt(
			frappe.db.get_value(
				"Bin",
				{"item_code": item_code, "warehouse": order_api._resolve_item_warehouse(item_code)},
				"actual_qty",
			)
		)
		rate = flt(_item_price(item_code))
		if rate <= 0:
			issues.append(
				{
					"code": "cart.variant_unavailable",
					"message": _("Price is not set for item {0}.").format(item_code),
					"item_code": item_code,
				}
			)
			continue

		if stock_qty <= 0:
			issues.append(
				{
					"code": "cart.out_of_stock",
					"message": _("Item {0} is out of stock.").format(item_code),
					"item_code": item_code,
					"available_qty": stock_qty,
				}
			)
		elif stock_qty < qty:
			issues.append(
				{
					"code": "cart.stock_limit_reached",
					"message": _("Only {0} units are available for item {1}.").format(stock_qty, item_code),
					"item_code": item_code,
					"available_qty": stock_qty,
				}
			)

		client_rate = item.get("rate") if item.get("rate") is not None else item.get("expected_rate")
		if client_rate is not None and flt(client_rate) != rate:
			issues.append(
				{
					"code": "cart.price_changed",
					"message": _("Price changed for item {0}.").format(item_code),
					"item_code": item_code,
					"old_rate": flt(client_rate),
					"new_rate": rate,
				}
			)

		amount = flt(qty * rate)
		subtotal += amount
		item_row = frappe.db.get_value(
			"Item",
			item_code,
			["item_name", "description", "image", "item_group", "brand"],
			as_dict=True,
		) or {}
		quote_items.append(
			{
				"item_code": item_code,
				"item_name": item_row.get("item_name") or item_code,
				"description": item_row.get("description"),
				"image": item_row.get("image"),
				"qty": qty,
				"available_qty": stock_qty,
				"rate": rate,
				"amount": amount,
				"net_amount": amount,
				"item_group": item_row.get("item_group"),
				"brand": item_row.get("brand"),
			}
		)

	# ERPNext's engine is authoritative (Phase 3). The cart is priced by building the same
	# Sales Order place_order will build and reading the numbers off it, so the quote and
	# the eventual order cannot disagree. The manual engine that used to live here is
	# retired; its remains are removed in Phase 4.
	discount_amount = 0.0
	discount_breakdown = []
	grand_total = subtotal
	normalized_coupon_code = cstr(coupon_code).strip().upper() if coupon_code else None

	priced, pricing_issues = order_api.preview_order_pricing(
		[{"item_code": i["item_code"], "qty": i["qty"]} for i in quote_items],
		_guardian_customer(_current_guardian()),
		coupon_code=normalized_coupon_code,
	)
	issues.extend(pricing_issues or [])

	if priced:
		subtotal = flt(priced.get("subtotal"))
		discount_amount = flt(priced.get("discount_amount"))
		discount_breakdown = priced.get("discount_breakdown") or []
		grand_total = flt(priced.get("grand_total"))

	return {
		"payment_method": CASH_PAYMENT_METHOD,
		"currency": "IQD",
		"items": quote_items,
		"issues": issues,
		"can_place_order": not issues,
		"subtotal": subtotal,
		"delivery_fee": 0,
		"discount_amount": discount_amount,
		"discount_breakdown": discount_breakdown,
		"coupon_code": normalized_coupon_code,
		"grand_total": grand_total,
	}


def _unwrap_api_result(result):
	if isinstance(result, dict) and result.get("ok") is False:
		errors = result.get("errors") or []
		message = errors[0].get("message") if errors else _("Request failed.")
		code = (result.get("meta") or {}).get("code") or _map_order_error(Exception(message))
		if code == "PERMISSION_ERROR":
			code = ORDER_PERMISSION_DENIED
		status = 401 if code == ORDER_PERMISSION_DENIED else 400
		raise MobileOrderError(code, message, status)

	if isinstance(result, dict) and result.get("ok") is True:
		data = result.get("data") or {}
		if isinstance(data, dict) and data.get("success") is False:
			raise MobileOrderError("promo.invalid", data.get("message") or _("Invalid promo code."))
		return data

	if isinstance(result, dict) and result.get("success") is False:
		raise MobileOrderError("promo.invalid", result.get("message") or _("Invalid promo code."))

	return result or {}


def _sales_order_has_field(fieldname: str) -> bool:
	return bool(frappe.db.has_column("Sales Order", fieldname))


def _sales_order_query_fields() -> list[str]:
	fields = [
		"name",
		"customer",
		"customer_name",
		"transaction_date",
		"delivery_date",
		"currency",
		"grand_total",
		"rounded_total",
		"discount_amount",
		"status",
		"docstatus",
		"modified",
		"creation",
	]
	for fieldname in (
		*ORDER_STATUS_FIELDS,
		*order_api.DELIVERY_LATITUDE_FIELDS,
		*order_api.DELIVERY_LONGITUDE_FIELDS,
		"custom_delivery_fee",
	):
		if _sales_order_has_field(fieldname):
			fields.append(fieldname)
	return fields


def _value(row, fieldname, default=None):
	if isinstance(row, dict):
		return row.get(fieldname, default)
	return getattr(row, fieldname, default)


def _first_value(row, fieldnames):
	for fieldname in fieldnames:
		value = _value(row, fieldname)
		if value not in (None, ""):
			return value
	return None


def _order_status(row) -> str:
	return _value(row, "custom_order_status") or _value(row, "status") or "Draft"


def _status_key(status: str) -> str:
	return cstr(status).strip().lower().replace(" ", "_")


def _status_timeline(status: str) -> list[dict]:
	status = status or "Draft"
	steps = list(ORDER_STATUS_STEPS)
	if status in ORDER_TERMINAL_STATUSES and status not in steps:
		steps.append(status)
	elif status not in steps:
		steps.append(status)

	try:
		current_index = steps.index(status)
	except ValueError:
		current_index = 0

	timeline = []
	for index, step in enumerate(steps):
		if index < current_index and status not in ORDER_TERMINAL_STATUSES:
			state = "completed"
		elif step == status:
			state = "current"
		else:
			state = "pending"
		timeline.append({"status": step, "status_key": _status_key(step), "state": state})
	return timeline


def _order_summary(row) -> dict:
	status = _order_status(row)
	return {
		"id": _value(row, "name"),
		"order": _value(row, "name"),
		"doctype": "Sales Order",
		"status": status,
		"status_key": _status_key(status),
		"erp_status": _value(row, "status"),
		"payment_method": _value(row, "custom_payment_method"),
		"payment_status": _value(row, "custom_payment_status"),
		"currency": _value(row, "currency"),
		"grand_total": flt(_value(row, "grand_total")),
		"rounded_total": flt(_value(row, "rounded_total")),
		"discount_amount": flt(_value(row, "discount_amount")),
		"customer": _value(row, "customer"),
		"customer_name": _value(row, "customer_name"),
		"transaction_date": cstr(_value(row, "transaction_date") or ""),
		"delivery_date": cstr(_value(row, "delivery_date") or ""),
		"modified": cstr(_value(row, "modified") or ""),
		"created_at": cstr(_value(row, "creation") or ""),
		"delivery": {
			"driver": _value(row, "custom_driver"),
			"latitude": _first_value(row, order_api.DELIVERY_LATITUDE_FIELDS),
			"longitude": _first_value(row, order_api.DELIVERY_LONGITUDE_FIELDS),
			"fee": flt(_value(row, "custom_delivery_fee")),
		},
	}


def _order_item(row) -> dict:
	return {
		"id": row.name,
		"item_code": row.item_code,
		"item_name": row.item_name,
		"description": row.description,
		"qty": flt(row.qty),
		"rate": flt(row.rate),
		"amount": flt(row.amount),
		"image": row.get("image"),
		"warehouse": row.get("warehouse"),
	}


def _assert_order_access(order_id: str, guardian: dict | None = None):
	order_id = cstr(order_id).strip()
	if not order_id:
		raise MobileOrderError(ORDER_REQUEST_INVALID, _("Order is required."))
	if not frappe.db.exists("Sales Order", order_id):
		raise MobileOrderError(ORDER_NOT_FOUND, _("Order was not found."), 404)

	guardian = guardian or _current_guardian()
	customer = _guardian_customer(guardian)
	order_customer = frappe.db.get_value("Sales Order", order_id, "customer")
	if order_customer != customer:
		raise MobileOrderError(ORDER_NOT_FOUND, _("Order was not found."), 404)

	return frappe.get_doc("Sales Order", order_id)


def _address_notes(address_name) -> str | None:
	"""The delivery note lives on Address, not on the Sales Order.

	Sales Order carries only the link plus ERPNext's rendered address_display HTML, and
	the note is deliberately print_hide so it never appears in that render. Reading it
	takes an explicit lookup - it cannot be picked up from the order's own fields.
	"""
	if not address_name:
		return None
	if not frappe.get_meta("Address").has_field("custom_notes"):
		return None
	return frappe.db.get_value("Address", address_name, "custom_notes")


def _order_detail(doc) -> dict:
	payload = _order_summary(doc)
	payload["items"] = [_order_item(row) for row in doc.items]
	payload["totals"] = {
		"total_qty": flt(doc.total_qty),
		"net_total": flt(doc.net_total),
		"total_taxes_and_charges": flt(doc.total_taxes_and_charges),
		"grand_total": flt(doc.grand_total),
		"rounded_total": flt(doc.rounded_total),
		"discount_amount": flt(doc.discount_amount),
	}
	payload["addresses"] = {
		"customer_address": doc.customer_address,
		"shipping_address_name": doc.shipping_address_name,
		"shipping_address": doc.shipping_address,
		"address_display": doc.address_display,
		"notes": _address_notes(doc.shipping_address_name or doc.customer_address),
	}
	payload["timeline"] = _status_timeline(payload["status"])
	return payload


@frappe.whitelist(methods=["GET"])
@_mobile_order_endpoint
def list_orders(limit=20, cursor=0, status=None, **kwargs):
	guardian = _current_guardian()
	customer = _guardian_customer(guardian)
	limit = max(1, min(cint(limit or 20), 100))
	offset = max(0, cint(cursor or 0))

	filters = {"customer": customer}
	if status:
		status = cstr(status).strip()
		status_field = "custom_order_status" if _sales_order_has_field("custom_order_status") else "status"
		filters[status_field] = status

	rows = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=_sales_order_query_fields(),
		order_by="modified desc",
		limit_start=offset,
		limit_page_length=limit + 1,
		ignore_permissions=True,
	)
	has_more = len(rows) > limit
	rows = rows[:limit]

	return {
		"items": [_order_summary(row) for row in rows],
		"nextCursor": str(offset + limit) if has_more else None,
		"hasMore": has_more,
	}


@frappe.whitelist(methods=["GET"])
@_mobile_order_endpoint
def get_order(order=None, order_id=None, name=None, **kwargs):
	doc = _assert_order_access(order or order_id or name)
	return _order_detail(doc)


@frappe.whitelist(methods=["POST"])
@_mobile_order_endpoint
def quote(items, coupon_code=None, **kwargs):
	_current_guardian()
	return _cart_quote(items, coupon_code=coupon_code)


@frappe.whitelist(methods=["POST"])
@_mobile_order_endpoint
def place_order(
	items,
	payment_method=CASH_PAYMENT_METHOD,
	delivery_lat=None,
	delivery_lng=None,
	shipping_address_name=None,
	coupon_code=None,
	shipping_rule=None,
	**kwargs,
):
	guardian = _current_guardian()
	customer = _guardian_customer(guardian)
	if shipping_address_name:
		assert_customer_address(shipping_address_name, customer)
	payment_method = _normalize_payment_method(payment_method)
	result = order_api.place_order(
		items=_coerce_items(items),
		payment_method=payment_method,
		delivery_lat=delivery_lat,
		delivery_lng=delivery_lng,
		shipping_address_name=shipping_address_name,
		coupon_code=coupon_code,
		shipping_rule=shipping_rule,
		guardian=guardian.get("name"),
	)
	data = _unwrap_api_result(result)
	order_name = data.get("order")
	if order_name and frappe.db.exists("Sales Order", order_name):
		return _order_detail(_assert_order_access(order_name, guardian=guardian))
	return data


@frappe.whitelist(methods=["POST"])
@_mobile_order_endpoint
def cancel_order(order=None, order_id=None, name=None, reason=None, **kwargs):
	doc = _assert_order_access(order or order_id or name)
	status = _order_status(doc)
	if "Cancelled" not in order_api.ALLOWED_TRANSITIONS.get(status, []):
		raise MobileOrderError(
			ORDER_NOT_CANCELLABLE,
			_("Order cannot be cancelled from status {0}.").format(status),
		)

	# Rolled back as a unit, the same shape transition_order uses. save() writes
	# custom_order_status and only then runs on_update_after_submit, so a side effect that
	# throws would otherwise leave the status committed with its stock reversal missing -
	# @_mobile_order_endpoint turns the exception into an error response, and the request
	# commits regardless.
	doc.custom_order_status = "Cancelled"
	doc.flags.ignore_permissions = True
	try:
		doc.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback()
		raise
	if reason:
		doc.add_comment("Comment", _("Mobile cancellation reason: {0}").format(cstr(reason)))
	return _order_detail(doc)


@frappe.whitelist(methods=["POST"])
@_mobile_order_endpoint
def reorder(order=None, order_id=None, name=None, **kwargs):
	doc = _assert_order_access(order or order_id or name)
	items = [
		{
			"item_code": row.item_code,
			"item_name": row.item_name,
			"qty": flt(row.qty),
		}
		for row in doc.items
	]
	return {
		"source_order": doc.name,
		"items": items,
		"message": _("Items are ready to add to cart."),
	}
