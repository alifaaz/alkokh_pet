from __future__ import annotations

import functools
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, get_url, now_datetime

from pet_app.api.mobile.response import error, ok
from pet_app.pet_app.doctype.product_category.product_category import get_store_root_category


CATALOG_NOT_FOUND = "catalog.not_found"
CATALOG_REQUEST_INVALID = "catalog.request_invalid"
HOME_SCHEMA_VERSION = 2
HOME_CACHE_TTL_SECONDS = 300

HOME_FILTERS = (
	("all", "All"),
	("dog", "Dog"),
	("cat", "Cat"),
	("fish", "Fish"),
	("bird", "Bird"),
	("other", "Other"),
)
HOME_FILTER_KEYS = {key for key, _label in HOME_FILTERS}

HOME_LIST_ALIASES = {
	"recently_added": "recently-added",
	"recently-added": "recently-added",
	"new-arrivals": "recently-added",
	"new_arrivals": "recently-added",
	"best_sellers": "best-sellers",
	"best-sellers": "best-sellers",
	"back_in_stock": "back-in-stock",
	"back-in-stock": "back-in-stock",
}

HOME_CATEGORY_COLORS = (
	"#FDE8E8",
	"#E8F4FD",
	"#FDF6E8",
	"#E8FDF0",
	"#F3E8FD",
	"#FDE8F6",
)

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


def _mobile_catalog_admin_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			from pet_app.api.mobile import home_builder

			home_builder._require_admin()
			return ok(fn(*args, **kwargs))
		except frappe.PermissionError as exc:
			return error("PERMISSION_DENIED", cstr(exc) or _("Not permitted."), 403)
		except MobileCatalogError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _published_product_filters():
	return {"status": "Active"}


def _doctype_exists(doctype: str) -> bool:
	return bool(frappe.db.exists("DocType", doctype))


def _safe_limit(limit, default=20, maximum=100):
	return max(1, min(cint(limit or default), maximum))


def _safe_offset(cursor):
	return max(0, cint(cursor or 0))


def _absolute_url(value) -> str:
	url = cstr(value).strip()
	if not url:
		return ""
	if url.startswith("?"):
		return ""
	if re.match(r"^https?://", url, flags=re.IGNORECASE):
		return url
	if url.startswith("//"):
		return f"https:{url}"
	if not url.startswith("/") and "/" not in url and "." not in url:
		return ""
	return get_url(url)


def _money_int(value) -> int:
	return int(round(flt(value)))


def _hex_color(value, default: str) -> str:
	color = cstr(value).strip()
	return color if re.match(r"^#[0-9a-fA-F]{6}$", color) else default


def _iso_z(value) -> str:
	try:
		dt = get_datetime(value)
	except Exception:
		dt = now_datetime()
	text = dt.replace(microsecond=0).isoformat()
	if text.endswith("+00:00"):
		return f"{text[:-6]}Z"
	if re.search(r"[+-]\d{2}:\d{2}$", text):
		return text
	return f"{text}Z"


def _public_file_payload(row) -> dict:
	return {
		"name": row.get("name"),
		"file_url": _absolute_url(row.get("file_url")),
		"file_name": row.get("file_name"),
		"is_default": bool(cint(row.get("custom_is_default"))),
	}


def _product_images(product_name: str) -> list[dict]:
	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Product", "attached_to_name": product_name, "is_private": 0},
		fields=["name", "file_url", "file_name", "custom_is_default"],
		order_by="custom_is_default desc, creation asc",
		ignore_permissions=True,
	)
	return [_public_file_payload(row) for row in rows]


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


def _product_unit(item_code: str | None) -> str:
	if not item_code:
		return ""
	try:
		return cstr(frappe.db.get_value("Item", item_code, "stock_uom") or "")
	except Exception:
		return ""


def _review_summary(product: str) -> dict:
	try:
		from pet_app.api.mobile.reviews import review_summary

		return review_summary(product)
	except Exception:
		return {"count": 0, "average": 0}


def _product_tag_values(row) -> set[str]:
	return {tag.strip().lower() for tag in cstr(row.get("tags")).split(",") if tag.strip()}


def _product_filter(row) -> str:
	parts = [
		row.get("category"),
		row.get("tags"),
		row.get("product_name"),
		row.get("name"),
	]
	text = " ".join(cstr(part).lower() for part in parts if part)
	if "dog" in text:
		return "dog"
	if "cat" in text:
		return "cat"
	if "fish" in text or "aquatic" in text or "aqua" in text:
		return "fish"
	if "bird" in text:
		return "bird"
	return "other"


def _product_matches_filter(row, filter_key: str | None) -> bool:
	if not filter_key or filter_key == "all":
		return True
	return _product_filter(row) == filter_key


def _product_matches_tag(row, tag: str | None) -> bool:
	if not tag:
		return True
	return tag.lower() in _product_tag_values(row)


def _category_summary(category: str | None) -> dict:
	if not category or not frappe.db.exists("Product Category", category):
		return {"id": category, "name": category, "image": ""}
	row = frappe.db.get_value(
		"Product Category",
		category,
		["name", "category_name", "image", "description", "parent_product_category"],
		as_dict=True,
	)
	return {
		"id": row.name,
		"name": row.category_name or row.name,
		"image": _absolute_url(row.image),
		"description": row.description,
		"parent": row.parent_product_category,
	}


def _brand_summary(brand: str | None) -> dict:
	if not brand or not frappe.db.exists("Brand", brand):
		return {"id": brand, "name": brand, "image": ""}
	row = frappe.db.get_value("Brand", brand, ["name", "brand", "image"], as_dict=True)
	return {"id": row.name, "name": row.brand or row.name, "image": _absolute_url(row.image)}


def _product_summary(row, *, include_images=True) -> dict:
	price = flt(row.get("price"))
	discounted_price = flt(row.get("discounted_price"))
	effective_price = discounted_price if discounted_price > 0 else price
	images = _product_images(row.name) if include_images else []
	image = row.get("image") or next((img.get("file_url") for img in images), None)
	qty = _product_qty(row.get("item"))
	reviews = _review_summary(row.name)

	return {
		"id": row.name,
		"product_id": row.name,
		"name": row.get("product_name") or row.name,
		"sku": row.get("sku"),
		"description": row.get("description"),
		"image": _absolute_url(image),
		"images": images,
		"price": price,
		"discounted_price": discounted_price or None,
		"effective_price": effective_price,
		"original_price": _money_int(price) if discounted_price > 0 and discounted_price < price else None,
		"currency": "IQD",
		"unit": _product_unit(row.get("item")),
		"in_stock": bool(row.get("in_stock")) or qty > 0,
		"qty": qty,
		"rating": reviews.get("average") or 0,
		"rating_count": cint(reviews.get("count")),
		"filter": _product_filter(row),
		"category": _category_summary(row.get("category")),
		"brand": _brand_summary(row.get("brand")),
		"tags": [tag.strip() for tag in cstr(row.get("tags")).split(",") if tag.strip()],
		"has_variants": bool(cint(row.get("has_variants"))),
		"item_code": row.get("item"),
		"modified": cstr(row.get("modified") or ""),
		"created_at": cstr(row.get("creation") or ""),
	}


def _home_product_card(row) -> dict | None:
	price = flt(row.get("price"))
	discounted_price = flt(row.get("discounted_price"))
	effective_price = discounted_price if discounted_price > 0 else price
	images = _product_images(row.name)
	image = row.get("image") or next((img.get("file_url") for img in images), "")
	image = _absolute_url(image)
	if not image:
		return None
	qty = _product_qty(row.get("item"))
	reviews = _review_summary(row.name)
	return {
		"id": row.name,
		"name": row.get("product_name") or row.name,
		"image": image,
		"price": _money_int(effective_price),
		"original_price": _money_int(price) if discounted_price > 0 and discounted_price < price else None,
		"currency": "IQD",
		"unit": _product_unit(row.get("item")),
		"in_stock": bool(row.get("in_stock")) or qty > 0,
		"rating": reviews.get("average") or 0,
		"rating_count": cint(reviews.get("count")),
		"filter": _product_filter(row),
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


def _products_page(filters, *, limit=20, cursor=0, order_by="modified desc", mapper=None):
	limit = _safe_limit(limit)
	offset = _safe_offset(cursor)
	mapper = mapper or _product_summary
	rows = _product_rows(filters, order_by=order_by, limit=limit + 1, cursor=offset)
	has_more = len(rows) > limit
	rows = rows[:limit]
	return {
		"items": [mapper(row) for row in rows],
		"nextCursor": str(offset + limit) if has_more else None,
		"hasMore": has_more,
	}


def _post_filtered_products_page(filters, *, limit=20, cursor=0, order_by="modified desc", mapper=None, row_filter=None):
	if not row_filter:
		return _products_page(filters, limit=limit, cursor=cursor, order_by=order_by, mapper=mapper)

	limit = _safe_limit(limit)
	offset = _safe_offset(cursor)
	mapper = mapper or _product_summary
	matches = []
	scan_offset = 0
	chunk_size = 100

	while len(matches) <= offset + limit:
		rows = _product_rows(filters, order_by=order_by, limit=chunk_size, cursor=scan_offset)
		if not rows:
			break
		matches.extend(row for row in rows if row_filter(row))
		scan_offset += len(rows)
		if len(rows) < chunk_size:
			break

	page_rows = matches[offset : offset + limit]
	has_more = len(matches) > offset + limit
	return {
		"items": [mapper(row) for row in page_rows],
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
				"image": _absolute_url(row.image),
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
	return {"items": [{"id": row.name, "name": row.brand or row.name, "image": _absolute_url(row.image)} for row in rows]}


def _home_product_list_config(list_id: str | None) -> dict | None:
	if not list_id:
		return None
	normalized = HOME_LIST_ALIASES.get(cstr(list_id).strip(), cstr(list_id).strip())
	configs = {
		"best-sellers": {
			"id": "best-sellers",
			"title": _("Best Sellers"),
			"filters": {},
			"order_by": "modified desc",
			"respects_filter": True,
			"see_all": True,
			"sort_order": 20,
		},
		"recently-added": {
			"id": "recently-added",
			"title": _("Recently Added"),
			"filters": {},
			"order_by": "creation desc",
			"respects_filter": True,
			"see_all": True,
			"sort_order": 25,
		},
		"back-in-stock": {
			"id": "back-in-stock",
			"title": _("Back in Stock"),
			"filters": {"in_stock": 1},
			"order_by": "modified desc",
			"respects_filter": False,
			"see_all": True,
			"sort_order": 45,
		},
	}
	return configs.get(normalized)


def _home_filters() -> list[dict]:
	return [{"key": key, "label": _(label)} for key, label in HOME_FILTERS]


def _home_banner_payload(row) -> dict | None:
	image = _absolute_url(row.get("image"))
	action_type = cstr(row.get("action_type") or "list").strip()
	action_value = cstr(row.get("action_value")).strip()
	if not image or not action_type or not action_value:
		return None
	return {
		"id": cstr(row.get("banner_id") or row.get("name")),
		"image": image,
		"title": cstr(row.get("title")),
		"subtitle": cstr(row.get("subtitle")),
		"button_title": cstr(row.get("button_title") or _("Shop Now")),
		"gradient": [
			_hex_color(row.get("gradient_start"), "#FF9A56"),
			_hex_color(row.get("gradient_end"), "#FF5E62"),
		],
		"action": {"type": action_type, "value": action_value},
	}


def _home_banner_blocks() -> list[dict]:
	if not _doctype_exists("Mobile Home Banner"):
		return []
	rows = frappe.get_all(
		"Mobile Home Banner",
		filters={"enabled": 1},
		fields=[
			"name",
			"banner_id",
			"block_id",
			"block_type",
			"display_order",
			"image",
			"title",
			"subtitle",
			"button_title",
			"gradient_start",
			"gradient_end",
			"action_type",
			"action_value",
		],
		order_by="display_order asc, modified desc",
		ignore_permissions=True,
	)

	grouped: dict[tuple[str, str], dict] = {}
	for row in rows:
		block_type = cstr(row.get("block_type") or "banner_carousel").strip()
		if block_type not in {"banner_carousel", "single_banner"}:
			continue
		block_id = cstr(row.get("block_id")).strip() or (
			"hero-banners" if block_type == "banner_carousel" else cstr(row.get("banner_id") or row.get("name"))
		)
		banner = _home_banner_payload(row)
		if not banner:
			continue
		key = (block_type, block_id)
		group = grouped.setdefault(
			key,
			{
				"id": block_id,
				"type": block_type,
				"sort_order": cint(row.get("display_order")) or 10,
				"banners": [],
			},
		)
		group["banners"].append(banner)
		group["sort_order"] = min(cint(group["sort_order"]), cint(row.get("display_order")) or cint(group["sort_order"]))

	blocks = []
	for group in grouped.values():
		if group["type"] == "single_banner":
			blocks.append(
				{
					"id": group["id"],
					"type": "single_banner",
					"sort_order": group["sort_order"],
					"data": {"banner": group["banners"][0]},
				}
			)
		else:
			blocks.append(
				{
					"id": group["id"],
					"type": "banner_carousel",
					"sort_order": group["sort_order"],
					"data": {"banners": group["banners"]},
				}
			)
	return blocks


def _home_product_list_block(list_id: str, *, limit=12) -> dict | None:
	config = _home_product_list_config(list_id)
	if not config:
		return None
	filters = _published_product_filters()
	filters.update(config.get("filters") or {})
	rows = _product_rows(filters, order_by=config["order_by"], limit=_safe_limit(limit, default=12, maximum=50), cursor=0)
	products = [card for row in rows if (card := _home_product_card(row))]
	if not products:
		return None
	return {
		"id": config["id"],
		"type": "product_list",
		"sort_order": config["sort_order"],
		"data": {
			"title": config["title"],
			"see_all": bool(config["see_all"]),
			"respects_filter": bool(config["respects_filter"]),
			"products": products,
		},
	}


def _category_emoji(title: str) -> str:
	text = cstr(title).lower()
	if "dog" in text:
		return "🐶"
	if "cat" in text:
		return "🐱"
	if "bird" in text:
		return "🐦"
	if "fish" in text or "aquatic" in text or "aqua" in text:
		return "🐠"
	if "groom" in text:
		return "🛁"
	if "toy" in text:
		return "🧸"
	return "🐾"


def _home_category_grid() -> dict | None:
	categories = []
	# Resolved, not hardcoded: the storefront root name lives in one constant.
	parent = get_store_root_category()
	for idx, row in enumerate(_categories_payload(parent=parent).get("items", [])[:12]):
		title = row.get("name") or row.get("id") or ""
		categories.append(
			{
				"id": row.get("id") or "",
				"title": title,
				"emoji": _category_emoji(title),
				"background_color": HOME_CATEGORY_COLORS[idx % len(HOME_CATEGORY_COLORS)],
			}
		)
	if not categories:
		return None
	return {
		"id": "shop-categories",
		"type": "category_grid",
		"sort_order": 30,
		"data": {"title": _("Shop by Category"), "categories": categories},
	}


def _home_brand_strip() -> dict | None:
	mobile_categories = []
	_store_root = get_store_root_category()
	if _store_root:
		mobile_categories = frappe.get_all(
			"Product Category",
			filters={"parent_product_category": _store_root, "enabled": 1},
			pluck="name",
			order_by="display_order asc, category_name asc",
			ignore_permissions=True,
		)
	if mobile_categories:
		brand_names = frappe.get_all(
			"Product",
			filters={"status": "Active", "category": ["in", mobile_categories], "brand": ["!=", ""]},
			pluck="brand",
			ignore_permissions=True,
		)
		brand_names = sorted({brand for brand in brand_names if brand})
		rows = frappe.get_all(
			"Brand",
			filters={"name": ["in", brand_names]} if brand_names else {"name": "__never__"},
			fields=["name", "brand", "image"],
			order_by="brand asc",
			ignore_permissions=True,
		)
		brands = [{"id": row.name, "name": row.brand or row.name, "image": _absolute_url(row.image)} for row in rows]
	else:
		brands = _brands_payload(limit=12).get("items", [])
	brands = [brand for brand in brands if brand.get("image")]
	if not brands:
		return None
	return {
		"id": "brands",
		"type": "brand_strip",
		"sort_order": 50,
		"data": {"title": _("Shop by Brand"), "brands": brands},
	}


def _home_updated_at() -> str:
	values = []
	for doctype in ("Mobile Home Banner", "Product", "Product Category", "Brand"):
		if not _doctype_exists(doctype):
			continue
		rows = frappe.get_all(
			doctype,
			fields=["modified"],
			order_by="modified desc",
			limit_page_length=1,
			ignore_permissions=True,
		)
		if rows:
			values.append(rows[0].modified)
	return _iso_z(max(values) if values else now_datetime())


def _home_locale(lang=None) -> str:
	try:
		header = frappe.get_request_header("Accept-Language")
	except Exception:
		header = None
	raw = cstr(lang or header or "en").lower()
	return "ar" if raw.startswith("ar") else "en"


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
	home_list = cstr(kwargs.get("list") or kwargs.get("list_id") or kwargs.get("listId")).strip()
	home_list_config = _home_product_list_config(home_list) if home_list else None
	if home_list and not home_list_config:
		raise MobileCatalogError(CATALOG_REQUEST_INVALID, _("Unknown product list {0}.").format(home_list))

	filter_key = cstr(kwargs.get("filter") or kwargs.get("filter_key") or kwargs.get("filterKey")).strip().lower()
	if filter_key and filter_key not in HOME_FILTER_KEYS:
		raise MobileCatalogError(CATALOG_REQUEST_INVALID, _("Unknown product filter {0}.").format(filter_key))
	tag = cstr(kwargs.get("tag")).strip().lower()

	filters = _published_product_filters()
	if home_list_config:
		filters.update(home_list_config.get("filters") or {})
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

	default_order_by = home_list_config["order_by"] if home_list_config else "modified desc"
	order_by = {
		"priceAsc": "price asc",
		"priceDesc": "price desc",
		"newest": "creation desc",
	}.get(cstr(sort), default_order_by)

	def row_filter(row):
		return _product_matches_filter(row, filter_key) and _product_matches_tag(row, tag)

	return _post_filtered_products_page(
		filters,
		limit=limit,
		cursor=cursor,
		order_by=order_by,
		row_filter=row_filter if filter_key or tag else None,
	)


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
			{"id": row.name, "label": row.product_name or row.name, "image": _absolute_url(row.image)}
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
				"items": _products_page(
					_published_product_filters(),
					limit=12,
					cursor=0,
					order_by="creation desc",
				)["items"],
			}
		],
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_catalog_endpoint
def home_v2(lang=None, **kwargs):
	from pet_app.api.mobile import home_builder

	published = home_builder.resolve_active_home(
		locale=lang or kwargs.get("locale"),
		filter_key=kwargs.get("filter_key") or kwargs.get("filterKey") or kwargs.get("filter") or "all",
	)
	if published:
		return published

	blocks = []
	blocks.extend(_home_banner_blocks())
	for block in (
		_home_product_list_block("best-sellers"),
		_home_category_grid(),
		_home_product_list_block("back-in-stock", limit=8),
		_home_brand_strip(),
	):
		if block:
			blocks.append(block)

	blocks = sorted(blocks, key=lambda item: (cint(item.get("sort_order")), cstr(item.get("id"))))
	for block in blocks:
		block.pop("sort_order", None)

	return {
		"schema_version": HOME_SCHEMA_VERSION,
		"version": HOME_SCHEMA_VERSION,
		"updated_at": _home_updated_at(),
		"cache_ttl_seconds": HOME_CACHE_TTL_SECONDS,
		"locale": _home_locale(lang or kwargs.get("locale")),
		"filters": _home_filters(),
		"blocks": blocks,
	}


@frappe.whitelist(methods=["GET"])
@_mobile_catalog_admin_endpoint
def home_draft(lang=None, locale=None, filter_key="all", **kwargs):
	from pet_app.api.mobile import home_builder

	return home_builder.resolve_draft_home(
		locale=lang or locale or kwargs.get("locale"),
		filter_key=kwargs.get("filter_key") or kwargs.get("filterKey") or kwargs.get("filter") or filter_key or "all",
	)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_catalog_endpoint
def home_block_v2(block_id=None, locale=None, filter_key="all", limit_start=0, limit_page_length=20, **kwargs):
	from pet_app.api.mobile import home_builder

	payload = home_builder.resolve_active_home_block(
		block_id=block_id,
		locale=locale or kwargs.get("lang"),
		filter_key=kwargs.get("filter_key") or kwargs.get("filterKey") or kwargs.get("filter") or filter_key or "all",
		limit_start=limit_start,
		limit_page_length=limit_page_length,
	)
	if not payload:
		raise MobileCatalogError(CATALOG_NOT_FOUND, _("Home block was not found."), 404)
	return payload
