from __future__ import annotations

import functools

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from pet_app.api.mobile.response import error, ok
from pet_app.utils.guardian_customer import get_guardian_by_user


SEARCH_AUTH_ERROR = "auth.wrong_credentials"
SEARCH_REQUEST_INVALID = "search.request_invalid"


class MobileSearchError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _mobile_search_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except MobileSearchError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _current_guardian() -> dict:
	if frappe.session.user == "Guest":
		raise MobileSearchError(SEARCH_AUTH_ERROR, _("Authentication required."), 401)
	guardian = get_guardian_by_user(frappe.session.user)
	if not guardian:
		raise MobileSearchError(SEARCH_AUTH_ERROR, _("No Guardian is linked to the current user."), 401)
	return guardian


def _normalize_query(query) -> str:
	query = " ".join(cstr(query).strip().split())
	if not query:
		raise MobileSearchError(SEARCH_REQUEST_INVALID, _("Search query is required."))
	if len(query) > 140:
		raise MobileSearchError(SEARCH_REQUEST_INVALID, _("Search query is too long."))
	return query


def _row_payload(row) -> dict:
	return {
		"id": row.name,
		"query": row.query,
		"last_searched_at": cstr(row.last_searched_at or row.modified or ""),
	}


def _recent_payload(guardian: str) -> dict:
	rows = frappe.get_all(
		"Mobile Recent Search",
		filters={"guardian": guardian},
		fields=["name", "query", "last_searched_at", "modified"],
		order_by="last_searched_at desc, modified desc",
		limit_page_length=10,
		ignore_permissions=True,
	)
	return {"items": [_row_payload(row) for row in rows]}


def _trim_recent(guardian: str):
	rows = frappe.get_all(
		"Mobile Recent Search",
		filters={"guardian": guardian},
		fields=["name"],
		order_by="last_searched_at desc, modified desc",
		ignore_permissions=True,
	)
	for row in rows[10:]:
		frappe.delete_doc("Mobile Recent Search", row.name, ignore_permissions=True)


@frappe.whitelist(methods=["GET"])
@_mobile_search_endpoint
def list_recent(**kwargs):
	guardian = _current_guardian()
	return _recent_payload(guardian.get("name"))


@frappe.whitelist(methods=["POST"])
@_mobile_search_endpoint
def save_recent(q=None, query=None, **kwargs):
	guardian = _current_guardian()
	query = _normalize_query(query or q)
	existing = frappe.db.get_value(
		"Mobile Recent Search",
		{"guardian": guardian.get("name"), "query": query},
		"name",
	)
	if existing:
		frappe.db.set_value(
			"Mobile Recent Search",
			existing,
			{"last_searched_at": now_datetime(), "user": frappe.session.user},
			update_modified=True,
		)
	else:
		frappe.get_doc(
			{
				"doctype": "Mobile Recent Search",
				"guardian": guardian.get("name"),
				"user": frappe.session.user,
				"query": query,
				"last_searched_at": now_datetime(),
			}
		).insert(ignore_permissions=True)
	_trim_recent(guardian.get("name"))
	return _recent_payload(guardian.get("name"))


@frappe.whitelist(methods=["POST", "DELETE"])
@_mobile_search_endpoint
def clear_recent(**kwargs):
	guardian = _current_guardian()
	for row in frappe.get_all(
		"Mobile Recent Search",
		filters={"guardian": guardian.get("name")},
		fields=["name"],
		ignore_permissions=True,
	):
		frappe.delete_doc("Mobile Recent Search", row.name, ignore_permissions=True)
	return {"items": []}
