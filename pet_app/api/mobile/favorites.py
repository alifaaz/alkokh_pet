from __future__ import annotations

import functools

import frappe
from frappe import _
from frappe.utils import cint, cstr

from pet_app.api.mobile import catalog
from pet_app.api.mobile.response import error, ok
from pet_app.utils.guardian_customer import get_guardian_by_user


FAVORITE_AUTH_ERROR = "auth.wrong_credentials"
FAVORITE_NOT_FOUND = "favorite.not_found"
FAVORITE_REQUEST_INVALID = "favorite.request_invalid"


class MobileFavoriteError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _mobile_favorite_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except MobileFavoriteError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _current_guardian() -> dict:
	if frappe.session.user == "Guest":
		raise MobileFavoriteError(FAVORITE_AUTH_ERROR, _("Authentication required."), 401)
	guardian = get_guardian_by_user(frappe.session.user)
	if not guardian:
		raise MobileFavoriteError(FAVORITE_AUTH_ERROR, _("No Guardian is linked to the current user."), 401)
	return guardian


def _require_product(product: str) -> str:
	product = cstr(product).strip()
	if not product or not frappe.db.exists("Product", {"name": product, "status": "Active"}):
		raise MobileFavoriteError(FAVORITE_NOT_FOUND, _("Product was not found."), 404)
	return product


def _favorite_name(guardian: str, product: str) -> str | None:
	return frappe.db.get_value("Mobile Favorite", {"guardian": guardian, "product": product}, "name")


def _favorite_payload(name: str) -> dict:
	row = frappe.db.get_value("Mobile Favorite", name, ["name", "product", "creation"], as_dict=True)
	product_row = frappe.db.get_value("Product", row.product, catalog.PRODUCT_FIELDS, as_dict=True)
	payload = {
		"id": row.name,
		"favorite_id": row.name,
		"product_id": row.product,
		"created_at": cstr(row.creation or ""),
		"is_favorite": True,
	}
	if product_row:
		payload["product"] = catalog._product_summary(product_row)
	return payload


@frappe.whitelist(methods=["GET"])
@_mobile_favorite_endpoint
def list_favorites(limit=20, cursor=0, **kwargs):
	guardian = _current_guardian()
	limit = max(1, min(cint(limit or 20), 100))
	offset = max(0, cint(cursor or 0))
	rows = frappe.get_all(
		"Mobile Favorite",
		filters={"guardian": guardian.get("name")},
		fields=["name"],
		order_by="modified desc",
		limit_start=offset,
		limit_page_length=limit + 1,
		ignore_permissions=True,
	)
	has_more = len(rows) > limit
	rows = rows[:limit]
	return {
		"items": [_favorite_payload(row.name) for row in rows],
		"nextCursor": str(offset + limit) if has_more else None,
		"hasMore": has_more,
	}


@frappe.whitelist(methods=["POST"])
@_mobile_favorite_endpoint
def toggle_favorite(product=None, product_id=None, **kwargs):
	guardian = _current_guardian()
	product = _require_product(product or product_id)
	existing = _favorite_name(guardian.get("name"), product)
	if existing:
		frappe.delete_doc("Mobile Favorite", existing, ignore_permissions=True)
		return {"product_id": product, "is_favorite": False}

	doc = frappe.get_doc(
		{
			"doctype": "Mobile Favorite",
			"guardian": guardian.get("name"),
			"user": frappe.session.user,
			"product": product,
		}
	)
	doc.insert(ignore_permissions=True)
	return _favorite_payload(doc.name)


@frappe.whitelist(methods=["POST", "DELETE"])
@_mobile_favorite_endpoint
def remove_favorite(product=None, product_id=None, **kwargs):
	guardian = _current_guardian()
	product = _require_product(product or product_id)
	existing = _favorite_name(guardian.get("name"), product)
	if existing:
		frappe.delete_doc("Mobile Favorite", existing, ignore_permissions=True)
	return {"product_id": product, "is_favorite": False}
