from __future__ import annotations

import functools

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from pet_app.api.mobile.response import error, ok


CATALOG_NOT_FOUND = "catalog.not_found"
CATALOG_REQUEST_INVALID = "catalog.request_invalid"

PRODUCT_FIELDS = [
	"name",
	"product_name",
	"sku",
	"description",
	"image",
	"price",
	"discounted_price",
	"in_stock",
	"category",
	"status",
	"tags",
	"has_variants",
	"item",
	"brand",
	"modified",
	"creation",
]


class MobileCatalogError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _mobile_catalog_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except MobileCatalogError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _published_product_filters():
	return {"status": "Active"}


def _safe_limit(limit, default=20, maximum=100):
	return max(1, min(cint(limit or default), maximum))


def _safe_offset(cursor):
	return max(0, cint(cursor or 0))


def _product_images(product_name: str) -> list[dict]:
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Product", "attached_to_name": product_name, "is_private": 0},
		fields=["name", "file_url", "file_name", "custom_is_default"],
		order_by="custom_is_default desc, creation asc",
		ignore_permissions=True,
	)


def _product_qty(item_code: str | None) -> float:
	if not item_code:
		return 0.0
	result = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(actual_qty), 0)
		FROM `tabBin`
		WHERE item_code = %s
		""",
		(item_code,),
	)
	return flt(result[0][0]) if result else 0.0


def _category_summary(category: str | None) -> dict:
	if not category or not frappe.db.exists("Product Category", category):
		return {"id": category, "name": category, "image": None}
	row = frappe.db.get_value(
		"Product Category",
		category,
		["name", "category_name", "image", "description", "parent_product_category"],
		as_dict=True,
	)
	return {
		"id": row.name,
		"name": row.category_name or row.name,
		"image": row.image,
		"description": row.description,
		"parent": row.parent_product_category,
	}


def _brand_summary(brand: str | None) -> dict:
	if not brand or not frappe.db.exists("Brand", brand):
		return {"id": brand, "name": brand, "image": None}
	row = frappe.db.get_value("Brand", brand, ["name", "brand", "image"], as_dict=True)
	return {"id": row.name, "name": row.brand or row.name, "image": row.image}


def _product_summary(row, *, include_images=True) -> dict:
	price = flt(row.get("price"))
	discounted_price = flt(row.get("discounted_price"))
	effective_price = discounted_price if discounted_price > 0 else price
	images = _product_images(row.name) if include_images else []
	image = row.get("image") or next((img.get("file_url") for img in images), None)
	qty = _product_qty(row.get("item"))

	return {
		"id": row.name,
		"product_id": row.name,
		"name": row.get("product_name") or row.name,
		"sku": row.get("sku"),
		"description": row.get("description"),
		"image": image,
		"images": images,
		"price": price,
		"discounted_price": discounted_price or None,
		"effective_price": effective_price,
		"currency": "IQD",
		"in_stock": bool(row.get("in_stock")) or qty > 0,
		"qty": qty,
		"category": _category_summary(row.get("category")),
		"brand": _brand_summary(row.get("brand")),
		"tags": [tag.strip() for tag in cstr(row.get("tags")).split(",") if tag.strip()],
		"has_variants": bool(cint(row.get("has_variants"))),
		"item_code": row.get("item"),
		"modified": cstr(row.get("modified") or ""),
		"created_at": cstr(row.get("creation") or ""),
	}


def _product_detail(row) -> dict:
	payload = _product_summary(row)
	doc = frappe.get_doc("Product", row.name)
	payload["variants"] = [
		{
			"options": variant.options,
			"value": variant.value,
			"price": flt(variant.price) or payload["effective_price"],
		}
		for variant in doc.product_variant
	]
	try:
		from pet_app.api.mobile.reviews import review_summary

		payload["review_summary"] = review_summary(row.name)
	except Exception:
		payload["review_summary"] = {"count": 0, "average": 0}
	return payload


def _product_rows(filters, *, order_by="modified desc", limit=20, cursor=0):
	return frappe.get_all(
		"Product",
		filters=filters,
		fields=PRODUCT_FIELDS,
		order_by=order_by,
		limit_start=cursor,
		limit_page_length=limit,
		ignore_permissions=True,
	)


def _products_page(filters, *, limit=20, cursor=0, order_by="modified desc"):
	limit = _safe_limit(limit)
	offset = _safe_offset(cursor)
	rows = _product_rows(filters, order_by=order_by, limit=limit + 1, cursor=offset)
	has_more = len(rows) > limit
	rows = rows[:limit]
	return {
		"items": [_product_summary(row) for row in rows],
		"nextCursor": str(offset + limit) if has_more else None,
		"hasMore": has_more,
	}


def _categories_payload(parent=None, search=None, enabled_only=1) -> dict:
	filters = {}
	if cint(enabled_only):
		filters["enabled"] = 1
	if parent is not None:
		parent = cstr(parent).strip()
		filters["parent_product_category"] = parent or ["in", ["", None]]

	or_filters = None
	if search:
		search = cstr(search).strip()
		or_filters = [
			["Product Category", "name", "like", f"%{search}%"],
			["Product Category", "category_name", "like", f"%{search}%"],
		]

	query = {
		"filters": filters,
		"fields": [
			"name",
			"category_name",
			"parent_product_category",
			"is_group",
			"enabled",
			"image",
			"description",
			"display_order",
		],
		"order_by": "display_order asc, category_name asc",
		"ignore_permissions": True,
	}
	if or_filters:
		query["or_filters"] = or_filters

	rows = frappe.get_all("Product Category", **query)
	return {
		"items": [
			{
				"id": row.name,
				"name": row.category_name or row.name,
				"parent": row.parent_product_category,
				"is_group": bool(cint(row.is_group)),
				"enabled": bool(cint(row.enabled)),
				"image": row.image,
				"description": row.description,
				"display_order": cint(row.display_order),
			}
			for row in rows
		]
	}


def _brands_payload(search=None, limit=100) -> dict:
	filters = {}
	if search:
		filters["brand"] = ["like", f"%{cstr(search).strip()}%"]
	rows = frappe.get_all(
		"Brand",
		filters=filters,
		fields=["name", "brand", "image"],
		order_by="brand asc",
		limit_page_length=_safe_limit(limit, default=100, maximum=200),
		ignore_permissions=True,
	)
	return {"items": [{"id": row.name, "name": row.brand or row.name, "image": row.image} for row in rows]}


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_catalog_endpoint
def list_products(
	category=None,
	brandId=None,
	minPrice=None,
	maxPrice=None,
	inStock=None,
	sort=None,
	limit=20,
	cursor=0,
	**kwargs,
):
	filters = _published_product_filters()
	if category:
		filters["category"] = cstr(category).strip()
	if brandId:
		filters["brand"] = cstr(brandId).strip()
	if cint(inStock):
		filters["in_stock"] = 1

	if minPrice not in (None, "") and maxPrice not in (None, ""):
		filters["price"] = ["between", [flt(minPrice), flt(maxPrice)]]
	elif minPrice not in (None, ""):
		filters["price"] = [">=", flt(minPrice)]
	elif maxPrice not in (None, ""):
		filters["price"] = ["<=", flt(maxPrice)]

	order_by = {
		"priceAsc": "price asc",
		"priceDesc": "price desc",
		"newest": "creation desc",
	}.get(cstr(sort), "modified desc")
	return _products_page(filters, limit=limit, cursor=cursor, order_by=order_by)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_catalog_endpoint
def get_product(product=None, product_id=None, id=None, **kwargs):
	name = cstr(product or product_id or id).strip()
	if not name or not frappe.db.exists("Product", {"name": name, "status": "Active"}):
		raise MobileCatalogError(CATALOG_NOT_FOUND, _("Product was not found."), 404)
	row = frappe.db.get_value("Product", name, PRODUCT_FIELDS, as_dict=True)
	return _product_detail(row)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_catalog_endpoint
def list_categories(parent=None, search=None, enabled_only=1, **kwargs):
	return _categories_payload(parent=parent, search=search, enabled_only=enabled_only)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_catalog_endpoint
def list_brands(search=None, limit=100, **kwargs):
	return _brands_payload(search=search, limit=limit)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_catalog_endpoint
def search(q=None, limit=20, cursor=0, **kwargs):
	term = cstr(q).strip()
	filters = _published_product_filters()
	if term:
		filters["product_name"] = ["like", f"%{term}%"]
	return _products_page(filters, limit=limit, cursor=cursor)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_catalog_endpoint
def suggest(q=None, limit=8, **kwargs):
	term = cstr(q).strip()
	if not term:
		return {"items": []}
	rows = frappe.get_all(
		"Product",
		filters={"status": "Active", "product_name": ["like", f"%{term}%"]},
		fields=["name", "product_name", "image"],
		order_by="modified desc",
		limit_page_length=_safe_limit(limit, default=8, maximum=20),
		ignore_permissions=True,
	)
	return {
		"items": [
			{"id": row.name, "label": row.product_name or row.name, "image": row.image}
			for row in rows
		]
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_catalog_endpoint
def home(**kwargs):
	return {
		"categories": _categories_payload().get("items", []),
		"brands": _brands_payload(limit=12).get("items", []),
		"sections": [
			{
				"key": "recently_added",
				"title": _("Recently Added"),
				"items": _products_page(_published_product_filters(), limit=12, cursor=0, order_by="creation desc")[
					"items"
				],
			}
		],
	}
