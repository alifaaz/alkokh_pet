from __future__ import annotations

import functools

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from pet_app.api.mobile.response import error, ok
from pet_app.utils.guardian_customer import get_guardian_by_user


REVIEW_AUTH_ERROR = "auth.wrong_credentials"
REVIEW_NOT_FOUND = "review.not_found"
REVIEW_REQUEST_INVALID = "review.request_invalid"


class MobileReviewError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _mobile_review_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except MobileReviewError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _current_guardian() -> dict:
	if frappe.session.user == "Guest":
		raise MobileReviewError(REVIEW_AUTH_ERROR, _("Authentication required."), 401)
	guardian = get_guardian_by_user(frappe.session.user)
	if not guardian:
		raise MobileReviewError(REVIEW_AUTH_ERROR, _("No Guardian is linked to the current user."), 401)
	return guardian


def _require_product(product: str) -> str:
	product = cstr(product).strip()
	if not product or not frappe.db.exists("Product", {"name": product, "status": "Active"}):
		raise MobileReviewError(REVIEW_NOT_FOUND, _("Product was not found."), 404)
	return product


def review_summary(product: str) -> dict:
	rows = frappe.get_all(
		"Rating",
		filters={"reference_doctype": "Product", "reference_name": product},
		fields=["overall_rating"],
		ignore_permissions=True,
	)
	count = len(rows)
	average = round(sum(flt(row.overall_rating) for row in rows) / count, 1) if count else 0
	return {"count": count, "average": average}


def _review_payload(row) -> dict:
	return {
		"id": row.name,
		"rating": cint(row.overall_rating),
		"notes": row.notes,
		"rated_by": row.rated_by,
		"rated_at": cstr(row.rated_at or row.creation or ""),
		"performer_name": row.performer_name,
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_review_endpoint
def list_product_reviews(product=None, product_id=None, limit=20, cursor=0, **kwargs):
	product = _require_product(product or product_id)
	limit = max(1, min(cint(limit or 20), 100))
	offset = max(0, cint(cursor or 0))
	rows = frappe.get_all(
		"Rating",
		filters={"reference_doctype": "Product", "reference_name": product},
		fields=["name", "overall_rating", "notes", "rated_by", "rated_at", "creation", "performer_name"],
		order_by="rated_at desc, creation desc",
		limit_start=offset,
		limit_page_length=limit + 1,
		ignore_permissions=True,
	)
	has_more = len(rows) > limit
	rows = rows[:limit]
	return {
		"items": [_review_payload(row) for row in rows],
		"summary": review_summary(product),
		"nextCursor": str(offset + limit) if has_more else None,
		"hasMore": has_more,
	}


@frappe.whitelist(methods=["POST"])
@_mobile_review_endpoint
def upsert_product_review(product=None, product_id=None, rating=None, notes=None, **kwargs):
	_current_guardian()
	product = _require_product(product or product_id)
	rating = cint(rating)
	if rating < 1 or rating > 5:
		raise MobileReviewError(REVIEW_REQUEST_INVALID, _("Rating must be between 1 and 5."))

	existing = frappe.db.sql(
		"""
		select name
		from `tabRating`
		where reference_doctype = 'Product'
			and reference_name = %s
			and rated_by = %s
			and ifnull(questionnaire, '') = ''
		limit 1
		""",
		(product, frappe.session.user),
		as_dict=True,
	)
	if existing:
		doc = frappe.get_doc("Rating", existing[0].name)
		doc.overall_rating = rating
		doc.notes = cstr(notes).strip()
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Rating",
				"reference_doctype": "Product",
				"reference_name": product,
				"overall_rating": rating,
				"notes": cstr(notes).strip(),
				"rated_by": frappe.session.user,
			}
		)
		doc.insert(ignore_permissions=True)
	return _review_payload(doc)
