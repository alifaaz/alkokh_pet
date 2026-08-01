from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, now_datetime, nowdate


SCHEMA_VERSION = 2
LAYOUT_DOCTYPE = "Mobile Home Layout"
LAYOUT_NAME = "mobile-home"
PUBLICATION_DOCTYPE = "Mobile Home Publication"
COLLECTION_DOCTYPE = "Mobile Home Product Collection"
FILTER_DOCTYPE = "Mobile Home Filter"
PRODUCT_FILTER_FIELD = "mobile_home_filter"
HOME_CACHE_TTL_SECONDS = 300
HOME_CACHE_VERSION_KEY = "pet_app:mobile_home:cache_version"
HOME_CACHE_PREFIX = "pet_app:mobile_home:v2"
ADMIN_ROLES = {"Administrator", "System Manager", "Pet App Admin", "E-commerce"}
BLOCK_TYPES = {"banner_carousel", "single_banner", "product_list", "category_grid", "brand_strip"}
PRODUCT_SOURCES = {"manual", "best_sellers", "newest", "recently_restocked", "saved_collection"}
SYSTEM_LIST_IDS = {"best-sellers", "recently-added", "back-in-stock"}
ACTION_TYPES = {"product", "category", "brand", "list", "url"}


class BuilderError(Exception):
	def __init__(self, message: str, *, code: str = "ERROR", http_status: int = 400, errors=None, meta=None):
		super().__init__(message)
		self.message = message
		self.code = code
		self.http_status = http_status
		self.errors = errors
		self.meta = meta or {}


class StaleDraftError(BuilderError):
	def __init__(self, current_revision: int):
		super().__init__(
			_("Draft revision is stale."),
			code="STALE_DRAFT",
			http_status=409,
			meta={"current_revision": current_revision},
		)
		self.current_revision = current_revision


def _admin_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			_require_admin()
			return _ok(fn(*args, **kwargs))
		except StaleDraftError as exc:
			frappe.local.response["http_status_code"] = exc.http_status
			meta = {"code": exc.code, "current_revision": exc.current_revision}
			return _fail(exc.message, code=exc.code, meta=meta)
		except BuilderError as exc:
			frappe.local.response["http_status_code"] = exc.http_status
			meta = {"code": exc.code, **exc.meta}
			return _fail(exc.message, code=exc.code, meta=meta, errors=exc.errors)
		except frappe.PermissionError as exc:
			frappe.local.response["http_status_code"] = 403
			return _fail(cstr(exc) or _("Not permitted."), code="PERMISSION_DENIED", meta={"code": "PERMISSION_DENIED"})
		except Exception as exc:
			frappe.local.response["http_status_code"] = 400
			code = getattr(exc, "code", None) or exc.__class__.__name__
			return _fail(cstr(exc), code=code, meta={"code": code})

	return wrapper


def _ok(data=None, meta=None) -> dict:
	return {"ok": True, "data": data or {}, "meta": meta or {}, "errors": []}


def _fail(message, *, code: str, meta=None, errors=None) -> dict:
	return {
		"ok": False,
		"data": {},
		"meta": meta or {"code": code},
		"errors": errors
		or [
			{
				"path": "",
				"severity": "error",
				"code": code,
				"message": message or _("Request failed."),
			}
		],
	}


def _require_admin():
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError(_("Authentication required."))
	roles = set(frappe.get_roles(user) or [])
	if user == "Administrator":
		roles.add("Administrator")
	if not roles.intersection(ADMIN_ROLES):
		raise frappe.PermissionError(_("You do not have access to the mobile home builder."))


@frappe.whitelist(methods=["GET"])
@_admin_endpoint
def get_builder():
	return _builder_payload(_get_layout(create=True))


@frappe.whitelist(methods=["POST"])
@_admin_endpoint
def save_draft(config=None, expected_revision=None):
	layout = _get_layout(create=True)
	_lock_layout()
	layout.reload()
	_assert_revision(layout, expected_revision)
	import_legacy_filters_from_config(config)
	normalized = normalize_config(config)
	layout.draft_json = _json_pretty(normalized)
	layout.revision = cint(layout.revision) + 1
	layout.save(ignore_permissions=True)
	return _builder_payload(layout)


@frappe.whitelist(methods=["POST"])
@_admin_endpoint
def validate_draft(config=None):
	normalized = normalize_config(config)
	issues = validate_config(normalized)
	checksum = _snapshot_checksum(_snapshot_config(normalized, include_missing=True))
	return {"valid": not _has_error(issues), "checksum": checksum, "issues": issues}


@frappe.whitelist(methods=["POST"])
@_admin_endpoint
def preview_draft(config=None, locale="en", filter_key="all"):
	normalized = normalize_config(config)
	return resolve_home_from_config(normalized, locale=locale, filter_key=filter_key, preview=True)


@frappe.whitelist(methods=["POST"])
@_admin_endpoint
def publish(expected_revision=None, validation_checksum=None, accept_warnings=0):
	layout = _get_layout(create=True)
	_lock_layout()
	layout.reload()
	_assert_revision(layout, expected_revision)
	normalized = _load_json(layout.draft_json, _empty_config())
	issues = validate_config(normalized)
	errors = [issue for issue in issues if issue.get("severity") == "error"]
	warnings = [issue for issue in issues if issue.get("severity") == "warning"]
	snapshot = _snapshot_config(normalized, include_missing=False)
	checksum = _snapshot_checksum(snapshot)
	if cstr(validation_checksum).strip() != checksum:
		raise BuilderError(
			_("Draft validation checksum is no longer current."),
			code="CHECKSUM_MISMATCH",
			errors=[
				_issue(
					"",
					"error",
					"CHECKSUM_MISMATCH",
					_("Validate the draft again before publishing."),
				)
			],
		)
	if errors:
		raise BuilderError(_("Draft has validation errors."), code="VALIDATION_ERROR", errors=errors)
	if warnings and not cint(accept_warnings):
		return {"published": False, "requires_confirmation": True, "issues": warnings}

	publication = _create_or_reuse_publication(layout, snapshot, checksum)
	clear_home_cache()
	return {
		"published": True,
		"requires_confirmation": False,
		"issues": warnings,
		"publication": _publication_summary(publication),
	}


@frappe.whitelist(methods=["GET"])
@_admin_endpoint
def list_versions(limit_start=0, limit_page_length=20):
	limit_start = max(0, cint(limit_start))
	limit_page_length = max(1, min(cint(limit_page_length or 20), 100))
	rows = frappe.get_all(
		PUBLICATION_DOCTYPE,
		fields=["name", "version", "schema_version", "published_at", "published_by", "checksum", "is_active", "snapshot_json"],
		order_by="version desc",
		limit_start=limit_start,
		limit_page_length=limit_page_length,
		ignore_permissions=True,
	)
	data = []
	for row in rows:
		snapshot = _load_json(row.snapshot_json, {})
		config = snapshot.get("config") or {}
		data.append(
			{
				"version": cint(row.version),
				"schema_version": cint(row.schema_version),
				"published_at": cstr(row.published_at or ""),
				"published_by": row.published_by,
				"checksum": row.checksum,
				"block_count": len(config.get("blocks") or []),
				"filter_count": len(config.get("filters") or []),
				"is_active": bool(cint(row.is_active)),
			}
		)
	return {"data": data, "total": frappe.db.count(PUBLICATION_DOCTYPE)}


@frappe.whitelist(methods=["GET"])
@_admin_endpoint
def get_version(version=None):
	publication = _publication_by_version(version)
	snapshot = _load_json(publication.snapshot_json, {})
	return {"version": _version_summary(publication), "config": snapshot.get("config") or _empty_config()}


@frappe.whitelist(methods=["POST"])
@_admin_endpoint
def restore_version(version=None, expected_revision=None):
	layout = _get_layout(create=True)
	_lock_layout()
	layout.reload()
	_assert_revision(layout, expected_revision)
	publication = _publication_by_version(version)
	snapshot = _load_json(publication.snapshot_json, {})
	config = snapshot.get("config") or _empty_config()
	import_legacy_filters_from_config(config)
	layout.draft_json = _json_pretty(normalize_config(config))
	layout.revision = cint(layout.revision) + 1
	layout.save(ignore_permissions=True)
	return _builder_payload(layout)


def normalize_config(config=None) -> dict:
	config = _load_json(config, _empty_config())
	if not isinstance(config, dict):
		raise BuilderError(_("Mobile home config must be a JSON object."), code="INVALID_CONFIG")
	filters = _normalize_filters(config.get("filters") or [])
	blocks = _normalize_blocks(config.get("blocks") or [])
	return {"schema_version": SCHEMA_VERSION, "filters": filters, "blocks": blocks}


def import_legacy_filters_from_config(config=None) -> bool:
	"""Create filter records and Product links from old draft filter assignments."""
	config = _load_json(config, _empty_config())
	if not isinstance(config, dict) or not _doctype_exists(FILTER_DOCTYPE) or not _product_filter_field_exists():
		return False
	changed = False
	for idx, row in enumerate(config.get("filters") or []):
		if hasattr(row, "as_dict"):
			row = row.as_dict()
		if not isinstance(row, dict):
			continue
		key = _slug(row.get("key"))
		if not key or key == "all":
			continue
		changed = _ensure_filter_record(row, idx) or changed
		for product_id in _unique_strings(row.get("product_ids") or []):
			if not frappe.db.exists("Product", product_id):
				continue
			current = cstr(frappe.db.get_value("Product", product_id, PRODUCT_FILTER_FIELD)).strip()
			if current:
				continue
			frappe.db.set_value("Product", product_id, PRODUCT_FILTER_FIELD, key, update_modified=False)
			changed = True
	if changed:
		clear_home_cache()
	return changed


def validate_config(config: dict) -> list[dict]:
	config = normalize_config(config)
	issues: list[dict] = []
	_validate_filters(config, issues)
	product_list_targets = {
		block["id"]
		for block in config.get("blocks") or []
		if block.get("enabled") and block.get("type") == "product_list" and block.get("show_all")
	}
	seen_blocks = set()
	for idx, block in enumerate(config.get("blocks") or []):
		path = f"blocks.{idx}"
		block_id = block.get("id")
		if block_id in seen_blocks:
			issues.append(_issue(f"{path}.id", "error", "DUPLICATE_BLOCK_ID", _("Block IDs must be unique.")))
		seen_blocks.add(block_id)
		_validate_block(block, path, issues, product_list_targets)
	_validate_filter_surface(config, issues)
	return issues


def resolve_active_home(locale="en", filter_key="all") -> dict | None:
	publication = get_active_publication()
	if not publication:
		return None
	locale = _locale(locale)
	filter_key = _filter_key(filter_key)
	cache_key = _home_cache_key(publication, locale, filter_key)
	cached = frappe.cache().get_value(cache_key)
	if cached:
		_set_public_etag(publication)
		return cached
	snapshot = _load_json(publication.snapshot_json, {})
	payload = _resolve_snapshot(snapshot, locale=locale, filter_key=filter_key, publication=publication)
	frappe.cache().set_value(cache_key, payload, expires_in_sec=HOME_CACHE_TTL_SECONDS)
	_set_public_etag(publication)
	return payload


def resolve_home_from_config(config: dict, *, locale="en", filter_key="all", preview=False) -> dict:
	snapshot = _snapshot_config(normalize_config(config), include_missing=True)
	return _resolve_snapshot(snapshot, locale=_locale(locale), filter_key=_filter_key(filter_key), publication=None, preview=preview)


def resolve_draft_home(locale="en", filter_key="all") -> dict:
	layout = _get_layout(create=True)
	config = _load_json(layout.draft_json, _empty_config())
	payload = resolve_home_from_config(config, locale=locale, filter_key=filter_key, preview=True)
	payload["draft_revision"] = cint(layout.revision)
	return payload


def resolve_active_home_block(block_id, locale="en", filter_key="all", limit_start=0, limit_page_length=20) -> dict | None:
	publication = get_active_publication()
	if not publication:
		return None
	snapshot = _load_json(publication.snapshot_json, {})
	block = next(
		(
			item
			for item in snapshot.get("blocks") or []
			if item.get("id") == cstr(block_id).strip() and item.get("enabled") and item.get("type") == "product_list"
		),
		None,
	)
	if not block or not block.get("show_all"):
		return None
	locale = _locale(locale)
	filter_key = _filter_key(filter_key)
	limit_start = max(0, cint(limit_start))
	limit_page_length = max(1, min(cint(limit_page_length or 20), 50))
	product_ids = _resolve_product_ids_for_block(block, snapshot=snapshot, filter_key=filter_key)
	total = len(_existing_active_products(product_ids))
	page_ids = product_ids[limit_start : limit_start + limit_page_length]
	products = _product_cards_for_ids(page_ids)
	return {
		"block_id": block.get("id"),
		"title": _localized(block.get("title"), locale),
		"data": {"products": products},
		"total": total,
		"limit_start": limit_start,
		"limit_page_length": limit_page_length,
		"has_more": limit_start + limit_page_length < total,
	}


def get_active_publication():
	if not _doctype_exists(PUBLICATION_DOCTYPE):
		return None
	active_name = None
	if _doctype_exists(LAYOUT_DOCTYPE) and frappe.db.exists(LAYOUT_DOCTYPE, LAYOUT_NAME):
		active_name = frappe.db.get_value(LAYOUT_DOCTYPE, LAYOUT_NAME, "active_publication")
	if active_name and frappe.db.exists(PUBLICATION_DOCTYPE, active_name):
		return frappe.get_doc(PUBLICATION_DOCTYPE, active_name)
	active_name = frappe.db.get_value(PUBLICATION_DOCTYPE, {"is_active": 1}, "name")
	return frappe.get_doc(PUBLICATION_DOCTYPE, active_name) if active_name else None


def clear_home_cache(*args, **kwargs):
	try:
		frappe.cache().set_value(HOME_CACHE_VERSION_KEY, frappe.generate_hash(length=12))
	except Exception:
		pass


def _empty_config() -> dict:
	return {
		"schema_version": SCHEMA_VERSION,
		"filters": [{"key": "all", "label": {"en": "All", "ar": "الكل"}, "order": 10, "product_ids": [], "built_in": True}],
		"blocks": [],
	}


def _get_layout(*, create: bool):
	if frappe.db.exists(LAYOUT_DOCTYPE, LAYOUT_NAME):
		return frappe.get_doc(LAYOUT_DOCTYPE, LAYOUT_NAME)
	if not create:
		return None
	doc = frappe.new_doc(LAYOUT_DOCTYPE)
	doc.name = LAYOUT_NAME
	doc.schema_version = SCHEMA_VERSION
	doc.revision = 0
	doc.draft_json = _json_pretty(_seed_initial_draft())
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc


def _seed_initial_draft() -> dict:
	blocks = []
	if _doctype_exists("Mobile Home Banner"):
		rows = frappe.get_all(
			"Mobile Home Banner",
			filters={"enabled": 1},
			fields=["name", "banner_id", "block_type", "block_id", "display_order", "title"],
			order_by="display_order asc, modified desc",
			ignore_permissions=True,
		)
		groups: dict[tuple[str, str], dict] = {}
		for row in rows:
			block_type = cstr(row.block_type or "banner_carousel").strip()
			if block_type not in {"banner_carousel", "single_banner"}:
				continue
			banner_id = cstr(row.banner_id or row.name).strip()
			block_id = cstr(row.block_id).strip() or ("hero-banners" if block_type == "banner_carousel" else banner_id)
			group = groups.setdefault(
				(block_type, block_id),
				{
					"id": block_id,
					"type": block_type,
					"enabled": True,
					"order": cint(row.display_order) or 10,
					"title": {"en": cstr(row.title or ""), "ar": ""},
					"banner_ids": [],
				},
			)
			group["banner_ids"].append(banner_id)
			group["order"] = min(cint(group["order"]), cint(row.display_order) or cint(group["order"]))
		for group in groups.values():
			if group["type"] == "single_banner":
				group["banner_id"] = group["banner_ids"][0] if group["banner_ids"] else ""
				group.pop("banner_ids", None)
			else:
				group["auto_advance_seconds"] = 5
				group["show_dots"] = True
			blocks.append(group)
	return normalize_config({"schema_version": SCHEMA_VERSION, "filters": [], "blocks": blocks})


def _builder_payload(layout) -> dict:
	active = None
	if layout.active_publication and frappe.db.exists(PUBLICATION_DOCTYPE, layout.active_publication):
		active = _publication_summary(frappe.get_doc(PUBLICATION_DOCTYPE, layout.active_publication))
	return {
		"draft": normalize_config(_load_json(layout.draft_json, _empty_config())),
		"revision": cint(layout.revision),
		"modified": cstr(layout.modified or ""),
		"active_publication": active,
	}


def _lock_layout():
	if not frappe.db.exists(LAYOUT_DOCTYPE, LAYOUT_NAME):
		return
	frappe.db.sql(f"select name from `tab{LAYOUT_DOCTYPE}` where name = %s for update", LAYOUT_NAME)


def _assert_revision(layout, expected_revision):
	current = cint(layout.revision)
	if cint(expected_revision) != current:
		raise StaleDraftError(current)


def _normalize_filters(filters) -> list[dict]:
	if not isinstance(filters, list):
		filters = []
	all_label = {"en": "All", "ar": "الكل"}
	for row in filters:
		if hasattr(row, "as_dict"):
			row = row.as_dict()
		if not isinstance(row, dict):
			continue
		key = _slug(row.get("key"))
		if key == "all":
			label = _localized_dict(row.get("label"))
			all_label = {"en": label.get("en") or "All", "ar": label.get("ar") or "الكل"}
			break
	if _doctype_exists(FILTER_DOCTYPE):
		return _filters_from_records(all_label)
	return _normalize_legacy_filters(filters, all_label)


def _normalize_legacy_filters(filters, all_label: dict) -> list[dict]:
	custom = []
	for row in filters:
		if hasattr(row, "as_dict"):
			row = row.as_dict()
		if not isinstance(row, dict):
			continue
		key = _slug(row.get("key"))
		label = _localized_dict(row.get("label"))
		if key == "all":
			continue
		if not key:
			key = f"filter-{len(custom) + 1}"
		custom.append(
			{
				"key": key,
				"label": label,
				"order": cint(row.get("order")) or 9999,
				"product_ids": _unique_strings(row.get("product_ids") or []),
			}
		)
	filters = [{"key": "all", "label": all_label, "order": 10, "product_ids": [], "built_in": True}]
	for idx, row in enumerate(sorted(custom, key=lambda item: cint(item.get("order")) or 9999), start=2):
		row["order"] = idx * 10
		filters.append(row)
	return filters


def _filters_from_records(all_label: dict) -> list[dict]:
	filters = [{"key": "all", "label": all_label, "order": 10, "product_ids": [], "built_in": True}]
	for idx, row in enumerate(_mobile_filter_rows(), start=2):
		key = _filter_record_key(row)
		if not key:
			continue
		filters.append(
			{
				"key": key,
				"label": {"en": cstr(row.get("label_en")).strip(), "ar": cstr(row.get("label_ar")).strip()},
				"order": idx * 10,
				"product_ids": _products_for_filter(key),
			}
		)
	return filters


def _mobile_filter_rows(enabled_only=True) -> list[dict]:
	if not _doctype_exists(FILTER_DOCTYPE):
		return []
	filters = {"enabled": 1} if enabled_only else None
	return frappe.get_all(
		FILTER_DOCTYPE,
		filters=filters,
		fields=["name", "filter_key", "enabled", "label_en", "label_ar", "display_order"],
		order_by="display_order asc, label_en asc, name asc",
		ignore_permissions=True,
	)


def _filter_record_key(row) -> str:
	return _slug(row.get("filter_key") or row.get("name"))


def _products_for_filter(filter_key) -> list[str]:
	filter_key = _slug(filter_key)
	if not filter_key or not _product_filter_field_exists():
		return []
	return frappe.get_all(
		"Product",
		filters={"status": "Active", PRODUCT_FILTER_FIELD: filter_key},
		pluck="name",
		order_by="modified desc, name asc",
		ignore_permissions=True,
	)


def _ensure_filter_record(row: dict, idx: int) -> bool:
	key = _slug(row.get("key"))
	if not key or key == "all":
		return False
	label = _localized_dict(row.get("label"))
	existing = frappe.db.exists(FILTER_DOCTYPE, key) or frappe.db.get_value(FILTER_DOCTYPE, {"filter_key": key}, "name")
	if not existing:
		doc = frappe.new_doc(FILTER_DOCTYPE)
		doc.filter_key = key
		doc.enabled = 1
		doc.label_en = label.get("en") or key.replace("-", " ").title()
		doc.label_ar = label.get("ar") or doc.label_en
		doc.display_order = cint(row.get("order")) or ((idx + 1) * 10)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		return True
	doc = frappe.get_doc(FILTER_DOCTYPE, existing)
	changed = False
	if not cstr(doc.label_en).strip() and label.get("en"):
		doc.label_en = label.get("en")
		changed = True
	if not cstr(doc.label_ar).strip() and label.get("ar"):
		doc.label_ar = label.get("ar")
		changed = True
	if cint(doc.display_order) == 0:
		doc.display_order = cint(row.get("order")) or ((idx + 1) * 10)
		changed = True
	if changed:
		doc.save(ignore_permissions=True)
	return changed


def _normalize_blocks(blocks) -> list[dict]:
	if not isinstance(blocks, list):
		blocks = []
	result = []
	for idx, raw in enumerate(blocks):
		if hasattr(raw, "as_dict"):
			raw = raw.as_dict()
		if not isinstance(raw, dict):
			continue
		block_type = cstr(raw.get("type")).strip()
		block = {
			"id": _slug(raw.get("id")) or f"block-{idx + 1}",
			"type": block_type,
			"enabled": _bool(raw.get("enabled"), True),
			"order": cint(raw.get("order")) or 9999,
			"title": _localized_dict(raw.get("title")),
		}
		if block_type == "banner_carousel":
			block.update(
				{
					"banner_ids": _unique_strings(raw.get("banner_ids") or []),
					"auto_advance_seconds": max(1, cint(raw.get("auto_advance_seconds") or 5)),
					"show_dots": _bool(raw.get("show_dots"), True),
				}
			)
		elif block_type == "single_banner":
			block["banner_id"] = cstr(raw.get("banner_id")).strip()
		elif block_type == "product_list":
			source = cstr(raw.get("source") or "manual").strip()
			block.update(
				{
					"source": source,
					"manual_product_ids": _unique_strings(raw.get("manual_product_ids") or []),
					"collection_id": cstr(raw.get("collection_id")).strip() or None,
					"lookback_days": max(1, cint(raw.get("lookback_days") or (7 if source == "recently_restocked" else 30))),
					"max_items": max(1, min(cint(raw.get("max_items") or 10), 20)),
					"show_all": _bool(raw.get("show_all"), True),
					"respects_filter": _bool(raw.get("respects_filter"), True),
				}
			)
		elif block_type == "category_grid":
			block["items"] = [_normalize_category_item(item) for item in raw.get("items") or [] if isinstance(item, dict)]
		elif block_type == "brand_strip":
			block["brand_ids"] = _unique_strings(raw.get("brand_ids") or [])
		result.append(block)
	for idx, block in enumerate(sorted(result, key=lambda item: cint(item.get("order")) or 9999), start=1):
		block["order"] = idx * 10
	return result


def _normalize_category_item(item) -> dict:
	return {
		"category_id": cstr(item.get("category_id")).strip(),
		"label": _localized_dict(item.get("label")),
		"emoji": cstr(item.get("emoji")).strip(),
		"background_color": cstr(item.get("background_color") or "#FDE8E8").strip(),
	}


def _validate_filters(config, issues):
	seen = set()
	assigned_products = {}
	for idx, row in enumerate(config.get("filters") or []):
		path = f"filters.{idx}"
		key = row.get("key")
		if key in seen:
			issues.append(_issue(f"{path}.key", "error", "DUPLICATE_FILTER_KEY", _("Filter keys must be unique.")))
		seen.add(key)
		_validate_localized(row.get("label"), f"{path}.label", issues)
		if key == "all":
			if row.get("product_ids"):
				issues.append(_issue(f"{path}.product_ids", "error", "ALL_FILTER_PRODUCTS", _("The All filter cannot have product assignments.")))
			continue
		for product in row.get("product_ids") or []:
			if product in assigned_products:
				issues.append(
					_issue(
						f"{path}.product_ids",
						"error",
						"DUPLICATE_FILTER_PRODUCT",
						_("A product can only be assigned to one custom filter."),
					)
				)
			assigned_products[product] = key
		_validate_products(row.get("product_ids") or [], f"{path}.product_ids", issues, allow_empty=True)


def _validate_block(block, path, issues, product_list_targets):
	if block.get("type") not in BLOCK_TYPES:
		issues.append(_issue(f"{path}.type", "error", "UNKNOWN_BLOCK_TYPE", _("Unknown mobile home block type.")))
		return
	if not block.get("enabled"):
		return
	block_type = block.get("type")
	if block_type in {"product_list", "category_grid", "brand_strip"}:
		_validate_localized(block.get("title"), f"{path}.title", issues)
	if block_type == "banner_carousel":
		if not block.get("banner_ids"):
			issues.append(_issue(f"{path}.banner_ids", "error", "EMPTY_BLOCK", _("A visible banner carousel needs at least one banner.")))
		for banner_id in block.get("banner_ids") or []:
			_validate_banner(banner_id, f"{path}.banner_ids", issues, product_list_targets)
	elif block_type == "single_banner":
		if not block.get("banner_id"):
			issues.append(_issue(f"{path}.banner_id", "error", "EMPTY_BLOCK", _("A visible single banner block needs a banner.")))
		else:
			_validate_banner(block.get("banner_id"), f"{path}.banner_id", issues, product_list_targets)
	elif block_type == "product_list":
		_validate_product_list(block, path, issues)
	elif block_type == "category_grid":
		if not block.get("items"):
			issues.append(_issue(f"{path}.items", "error", "EMPTY_BLOCK", _("A visible category grid needs at least one category.")))
		for idx, item in enumerate(block.get("items") or []):
			_validate_category_item(item, f"{path}.items.{idx}", issues)
	elif block_type == "brand_strip":
		if not block.get("brand_ids"):
			issues.append(_issue(f"{path}.brand_ids", "error", "EMPTY_BLOCK", _("A visible brand strip needs at least one brand.")))
		for brand_id in block.get("brand_ids") or []:
			if not frappe.db.exists("Brand", brand_id):
				issues.append(_issue(f"{path}.brand_ids", "error", "MISSING_BRAND", _("Brand {0} was not found.").format(brand_id)))


def _validate_product_list(block, path, issues):
	source = block.get("source")
	if source not in PRODUCT_SOURCES:
		issues.append(_issue(f"{path}.source", "error", "UNKNOWN_SOURCE", _("Unknown product source.")))
		return
	if not (1 <= cint(block.get("max_items")) <= 20):
		issues.append(_issue(f"{path}.max_items", "error", "MAX_ITEMS_RANGE", _("Max items must be between 1 and 20.")))
	if source == "manual":
		if not block.get("manual_product_ids"):
			issues.append(_issue(f"{path}.manual_product_ids", "error", "EMPTY_BLOCK", _("A visible manual product list needs at least one product.")))
		_validate_products(block.get("manual_product_ids") or [], f"{path}.manual_product_ids", issues)
	elif source == "saved_collection":
		collection = _collection_snapshot(block.get("collection_id"))
		if not collection:
			issues.append(_issue(f"{path}.collection_id", "error", "MISSING_COLLECTION", _("Enabled product collection was not found.")))
		elif not collection.get("product_ids"):
			issues.append(_issue(f"{path}.collection_id", "error", "EMPTY_COLLECTION", _("Product collection has no products.")))
	else:
		product_ids = _resolve_auto_product_ids(source, lookback_days=block.get("lookback_days"), limit=block.get("max_items"))
		if not product_ids:
			issues.append(_issue(path, "warning", "EMPTY_AUTO_SOURCE", _("This automatic source currently resolves no products.")))


def _validate_filter_surface(config, issues):
	custom_filters = [row for row in config.get("filters") or [] if row.get("key") != "all"]
	if not custom_filters:
		return
	has_filter_aware_list = any(
		block.get("enabled") and block.get("type") == "product_list" and block.get("respects_filter")
		for block in config.get("blocks") or []
	)
	if not has_filter_aware_list:
		issues.append(
			_issue(
				"blocks",
				"warning",
				"FILTERS_NOT_SURFACED",
				_("Custom filter assignments are not surfaced by any filter-aware product list."),
			)
		)


def _validate_banner(banner_id, path, issues, product_list_targets):
	row = _banner_row(banner_id)
	if not row:
		issues.append(_issue(path, "error", "MISSING_BANNER", _("Banner {0} was not found.").format(banner_id)))
		return
	if not cint(row.get("enabled")):
		issues.append(_issue(path, "error", "DISABLED_BANNER", _("Banner {0} is disabled.").format(banner_id)))
	if not row.get("image"):
		issues.append(_issue(path, "error", "BANNER_IMAGE_REQUIRED", _("Enabled banners need an image.")))
	_validate_banner_localized(row, path, issues)
	action_type = cstr(row.get("action_type") or "list").strip()
	action_value = cstr(row.get("action_value")).strip()
	if action_type not in ACTION_TYPES or not action_value:
		issues.append(_issue(path, "error", "INVALID_ACTION", _("Banner actions need a supported type and value.")))
	elif action_type == "url" and not re.match(r"^https?://", action_value, flags=re.IGNORECASE):
		issues.append(_issue(path, "error", "INVALID_URL", _("URL banner actions require an absolute http(s) URL.")))
	elif action_type == "list" and action_value not in SYSTEM_LIST_IDS and action_value not in product_list_targets:
		issues.append(_issue(path, "error", "INVALID_LIST_TARGET", _("Banner list actions need a supported list or published Show-all block.")))
	if row.get("starts_at") and row.get("ends_at") and get_datetime(row.ends_at) <= get_datetime(row.starts_at):
		issues.append(_issue(path, "error", "INVALID_SCHEDULE", _("Banner ends_at must be later than starts_at.")))
	if _banner_outside_window(row):
		issues.append(_issue(path, "warning", "SCHEDULE_OUTSIDE_WINDOW", _("Banner is currently outside its schedule window.")))


def _validate_banner_localized(row, path, issues):
	for base in ("title", "subtitle", "button_title"):
		en = cstr(row.get(f"{base}_en") or row.get(base)).strip()
		ar = cstr(row.get(f"{base}_ar")).strip()
		if en and not ar:
			issues.append(_issue(path, "error", "MISSING_TRANSLATION", _("Banner {0} needs Arabic content.").format(base)))
		if ar and not en:
			issues.append(_issue(path, "error", "MISSING_TRANSLATION", _("Banner {0} needs English content.").format(base)))


def _validate_category_item(item, path, issues):
	_validate_localized(item.get("label"), f"{path}.label", issues)
	category_id = item.get("category_id")
	if not category_id or not frappe.db.exists("Product Category", category_id):
		issues.append(_issue(f"{path}.category_id", "error", "MISSING_CATEGORY", _("Category was not found.")))
	elif not cint(frappe.db.get_value("Product Category", category_id, "enabled")):
		issues.append(_issue(f"{path}.category_id", "error", "DISABLED_CATEGORY", _("Category is disabled.")))
	if not re.match(r"^#[0-9a-fA-F]{6}$", cstr(item.get("background_color"))):
		issues.append(_issue(f"{path}.background_color", "error", "INVALID_COLOR", _("Category background color must be #RRGGBB.")))


def _validate_localized(value, path, issues):
	value = value or {}
	if not cstr(value.get("en")).strip() or not cstr(value.get("ar")).strip():
		issues.append(_issue(path, "error", "MISSING_TRANSLATION", _("English and Arabic text are required.")))


def _validate_products(product_ids, path, issues, allow_empty=False):
	if not product_ids and not allow_empty:
		issues.append(_issue(path, "error", "EMPTY_PRODUCTS", _("At least one product is required.")))
	for product in product_ids:
		if not frappe.db.exists("Product", {"name": product, "status": "Active"}):
			issues.append(_issue(path, "error", "MISSING_PRODUCT", _("Product {0} was not found or is not active.").format(product)))


def _snapshot_config(config: dict, *, include_missing: bool) -> dict:
	config = normalize_config(config)
	blocks = []
	for block in config.get("blocks") or []:
		snapshot = copy.deepcopy(block)
		if block.get("type") == "banner_carousel":
			snapshot["banners"] = [_banner_snapshot(banner_id) for banner_id in block.get("banner_ids") or []]
			snapshot["banners"] = [item for item in snapshot["banners"] if item or include_missing]
		elif block.get("type") == "single_banner":
			snapshot["banner"] = _banner_snapshot(block.get("banner_id"))
		elif block.get("type") == "product_list":
			if block.get("source") == "saved_collection":
				collection = _collection_snapshot(block.get("collection_id"))
				snapshot["collection"] = collection
				snapshot["collection_product_ids"] = (collection or {}).get("product_ids") or []
			elif block.get("source") == "manual":
				snapshot["product_ids"] = list(block.get("manual_product_ids") or [])
		elif block.get("type") == "category_grid":
			snapshot["items"] = copy.deepcopy(block.get("items") or [])
		elif block.get("type") == "brand_strip":
			snapshot["brand_ids"] = list(block.get("brand_ids") or [])
		blocks.append(snapshot)
	return {"schema_version": SCHEMA_VERSION, "config": config, "filters": copy.deepcopy(config.get("filters") or []), "blocks": blocks}


def _create_or_reuse_publication(layout, snapshot: dict, checksum: str):
	existing = frappe.db.get_value(PUBLICATION_DOCTYPE, {"checksum": checksum}, "name")
	if existing:
		publication = frappe.get_doc(PUBLICATION_DOCTYPE, existing)
	else:
		publication = frappe.new_doc(PUBLICATION_DOCTYPE)
		publication.version = _next_publication_version()
		publication.schema_version = SCHEMA_VERSION
		publication.revision = cint(layout.revision)
		publication.checksum = checksum
		publication.snapshot_json = _json_pretty(snapshot)
		publication.published_at = now_datetime()
		publication.published_by = frappe.session.user
		publication.is_active = 0
		publication.flags.ignore_permissions = True
		publication.insert(ignore_permissions=True)
	frappe.db.sql(f"update `tab{PUBLICATION_DOCTYPE}` set is_active = 0 where is_active = 1")
	frappe.db.set_value(PUBLICATION_DOCTYPE, publication.name, "is_active", 1, update_modified=False)
	frappe.db.set_value(LAYOUT_DOCTYPE, layout.name, "active_publication", publication.name)
	publication.reload()
	return publication


def _next_publication_version() -> int:
	value = frappe.db.sql(f"select max(version) from `tab{PUBLICATION_DOCTYPE}`")
	return cint(value[0][0]) + 1 if value and value[0][0] else 1


def _publication_by_version(version):
	name = frappe.db.get_value(PUBLICATION_DOCTYPE, {"version": cint(version)}, "name")
	if not name:
		raise BuilderError(_("Publication version was not found."), code="NOT_FOUND", http_status=404)
	return frappe.get_doc(PUBLICATION_DOCTYPE, name)


def _publication_summary(publication) -> dict:
	return {
		"version": cint(publication.version),
		"published_at": cstr(publication.published_at or ""),
		"published_by": publication.published_by,
		"checksum": publication.checksum,
	}


def _version_summary(publication) -> dict:
	snapshot = _load_json(publication.snapshot_json, {})
	config = snapshot.get("config") or {}
	return {
		**_publication_summary(publication),
		"schema_version": cint(publication.schema_version),
		"revision": cint(publication.revision),
		"block_count": len(config.get("blocks") or []),
		"filter_count": len(config.get("filters") or []),
		"is_active": bool(cint(publication.is_active)),
	}


def _resolve_snapshot(snapshot: dict, *, locale: str, filter_key: str, publication=None, preview=False) -> dict:
	blocks = []
	for block in snapshot.get("blocks") or []:
		if not block.get("enabled"):
			continue
		resolved = _resolve_block(block, snapshot=snapshot, locale=locale, filter_key=filter_key)
		if resolved:
			blocks.append(resolved)
	blocks = sorted(blocks, key=lambda item: (cint(item.get("order")), cstr(item.get("id"))))
	for block in blocks:
		block.pop("order", None)
	payload = {
		"schema_version": SCHEMA_VERSION,
		"version": cint(publication.version) if publication else 0,
		"updated_at": _publication_updated_at(publication) if publication else _iso_z(now_datetime()),
		"cache_ttl_seconds": HOME_CACHE_TTL_SECONDS,
		"locale": locale,
		"filters": _public_filters(snapshot, locale=locale),
		"blocks": blocks,
	}
	if preview:
		payload["preview"] = True
	return payload


def _resolve_block(block, *, snapshot, locale, filter_key) -> dict | None:
	block_type = block.get("type")
	if block_type == "banner_carousel":
		banners = [_public_banner(item, locale) for item in block.get("banners") or [] if item and _banner_snapshot_in_window(item)]
		banners = [item for item in banners if item]
		if not banners:
			return None
		return {
			"id": block.get("id"),
			"type": "banner_carousel",
			"title": _localized(block.get("title"), locale),
			"order": block.get("order"),
			"data": {
				"banners": banners,
				"auto_advance_seconds": cint(block.get("auto_advance_seconds") or 5),
				"show_dots": bool(block.get("show_dots")),
			},
		}
	if block_type == "single_banner":
		banner = _public_banner(block.get("banner"), locale) if _banner_snapshot_in_window(block.get("banner")) else None
		if not banner:
			return None
		return {
			"id": block.get("id"),
			"type": "single_banner",
			"title": _localized(block.get("title"), locale),
			"order": block.get("order"),
			"data": {"banner": banner},
		}
	if block_type == "product_list":
		product_ids = _resolve_product_ids_for_block(block, snapshot=snapshot, filter_key=filter_key)
		products = _product_cards_for_ids(product_ids[: cint(block.get("max_items") or 10)])
		if not products:
			return None
		return {
			"id": block.get("id"),
			"type": "product_list",
			"title": _localized(block.get("title"), locale),
			"order": block.get("order"),
			"data": {
				"title": _localized(block.get("title"), locale),
				"see_all": bool(block.get("show_all")),
				"respects_filter": bool(block.get("respects_filter")),
				"products": products,
			},
		}
	if block_type == "category_grid":
		categories = [
			{
				"id": item.get("category_id"),
				"title": _localized(item.get("label"), locale),
				"emoji": item.get("emoji"),
				"background_color": item.get("background_color"),
			}
			for item in block.get("items") or []
			if item.get("category_id")
		]
		if not categories:
			return None
		return {
			"id": block.get("id"),
			"type": "category_grid",
			"title": _localized(block.get("title"), locale),
			"order": block.get("order"),
			"data": {"title": _localized(block.get("title"), locale), "categories": categories},
		}
	if block_type == "brand_strip":
		brands = [_brand_card(brand_id) for brand_id in block.get("brand_ids") or []]
		brands = [item for item in brands if item]
		if not brands:
			return None
		return {
			"id": block.get("id"),
			"type": "brand_strip",
			"title": _localized(block.get("title"), locale),
			"order": block.get("order"),
			"data": {"title": _localized(block.get("title"), locale), "brands": brands},
		}
	return None


def _resolve_product_ids_for_block(block, *, snapshot, filter_key: str) -> list[str]:
	source = block.get("source")
	if source == "manual":
		product_ids = list(block.get("product_ids") or block.get("manual_product_ids") or [])
	elif source == "saved_collection":
		product_ids = list(block.get("collection_product_ids") or [])
	elif source in {"best_sellers", "newest", "recently_restocked"}:
		product_ids = _resolve_auto_product_ids(source, lookback_days=block.get("lookback_days"), limit=200)
	else:
		product_ids = []
	if block.get("respects_filter") and filter_key != "all":
		filter_products = set(_filter_product_ids(snapshot, filter_key))
		product_ids = [product for product in product_ids if product in filter_products]
	return _existing_active_products(product_ids)


def _public_filters(snapshot, *, locale: str) -> list[dict]:
	result = []
	all_active_count = frappe.db.count("Product", {"status": "Active"}) if _doctype_exists("Product") else 0
	for row in snapshot.get("filters") or []:
		key = row.get("key")
		count = all_active_count if key == "all" else len(_existing_active_products(row.get("product_ids") or []))
		result.append({"key": key, "label": _localized(row.get("label"), locale), "product_count": count})
	return result


def _public_banner(item, locale: str) -> dict | None:
	if not item or not item.get("image"):
		return None
	return {
		"id": item.get("id"),
		"image": _absolute_url(item.get("image")),
		"title": _localized(item.get("title"), locale),
		"subtitle": _localized(item.get("subtitle"), locale),
		"button_title": _localized(item.get("button_title"), locale) or _("Shop Now"),
		"gradient": item.get("gradient") or ["#FF9A56", "#FF5E62"],
		"action": item.get("action") or {},
	}


def _product_cards_for_ids(product_ids: list[str]) -> list[dict]:
	from pet_app.api.mobile import catalog

	cards = []
	for product in product_ids:
		row = frappe.db.get_value("Product", {"name": product, "status": "Active"}, catalog.PRODUCT_FIELDS, as_dict=True)
		if not row:
			continue
		card = catalog._home_product_card(row)
		if card:
			cards.append(card)
	return cards


def _brand_card(brand_id):
	from pet_app.api.mobile import catalog

	if not brand_id or not frappe.db.exists("Brand", brand_id):
		return None
	row = frappe.db.get_value("Brand", brand_id, ["name", "brand", "image"], as_dict=True)
	return {"id": row.name, "name": row.brand or row.name, "image": catalog._absolute_url(row.image)}


def _resolve_auto_product_ids(source, *, lookback_days=None, limit=20) -> list[str]:
	limit = max(1, min(cint(limit or 20), 200))
	if not _doctype_exists("Product"):
		return []
	if source == "newest":
		return frappe.get_all(
			"Product",
			filters={"status": "Active"},
			pluck="name",
			order_by="creation desc",
			limit_page_length=limit,
			ignore_permissions=True,
		)
	cutoff = _cutoff_date(lookback_days or 30)
	if source == "best_sellers" and _table_exists("Sales Invoice Item") and _table_exists("Sales Invoice"):
		rows = frappe.db.sql(
			"""
			SELECT p.name
			FROM `tabSales Invoice Item` sii
			JOIN `tabSales Invoice` si ON si.name = sii.parent
			JOIN `tabProduct` p ON p.item = sii.item_code
			WHERE si.docstatus = 1
			  AND si.posting_date >= %s
			  AND p.status = 'Active'
			GROUP BY p.name
			ORDER BY COALESCE(SUM(sii.qty), 0) DESC, MAX(si.posting_date) DESC
			LIMIT %s
			""",
			(cutoff, limit),
			as_dict=True,
		)
		return [row.name for row in rows]
	if source == "recently_restocked" and _table_exists("Stock Entry Detail") and _table_exists("Stock Entry"):
		rows = frappe.db.sql(
			"""
			SELECT p.name
			FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name = sed.parent
			JOIN `tabProduct` p ON p.item = sed.item_code
			WHERE se.docstatus = 1
			  AND se.stock_entry_type = 'Material Receipt'
			  AND se.posting_date >= %s
			  AND p.status = 'Active'
			GROUP BY p.name
			ORDER BY MAX(se.posting_date) DESC, MAX(se.posting_time) DESC
			LIMIT %s
			""",
			(cutoff, limit),
			as_dict=True,
		)
		return [row.name for row in rows]
	return []


def _cutoff_date(lookback_days) -> str:
	return cstr(get_datetime(nowdate()).date() - timedelta(days=max(1, cint(lookback_days))))


def _existing_active_products(product_ids: list[str]) -> list[str]:
	result = []
	seen = set()
	for product in product_ids:
		product = cstr(product).strip()
		if not product or product in seen:
			continue
		if frappe.db.exists("Product", {"name": product, "status": "Active"}):
			result.append(product)
			seen.add(product)
	return result


def _filter_product_ids(snapshot, filter_key) -> list[str]:
	for row in snapshot.get("filters") or []:
		if row.get("key") == filter_key:
			return row.get("product_ids") or []
	return []


def _banner_snapshot(banner_id):
	row = _banner_row(banner_id)
	if not row:
		return None
	return {
		"id": cstr(row.get("banner_id") or row.get("name")),
		"image": row.get("image"),
		"title": {
			"en": cstr(row.get("title_en") or row.get("title")).strip(),
			"ar": cstr(row.get("title_ar")).strip(),
		},
		"subtitle": {
			"en": cstr(row.get("subtitle_en") or row.get("subtitle")).strip(),
			"ar": cstr(row.get("subtitle_ar")).strip(),
		},
		"button_title": {
			"en": cstr(row.get("button_title_en") or row.get("button_title") or _("Shop Now")).strip(),
			"ar": cstr(row.get("button_title_ar")).strip(),
		},
		"gradient": [_hex(row.get("gradient_start"), "#FF9A56"), _hex(row.get("gradient_end"), "#FF5E62")],
		"action": {"type": cstr(row.get("action_type") or "list").strip(), "value": cstr(row.get("action_value")).strip()},
		"starts_at": cstr(row.get("starts_at") or ""),
		"ends_at": cstr(row.get("ends_at") or ""),
	}


def _banner_row(banner_id):
	if not _doctype_exists("Mobile Home Banner"):
		return None
	fields = [
		"name",
		"enabled",
		"banner_id",
		"image",
		"title",
		"subtitle",
		"button_title",
		"title_en",
		"title_ar",
		"subtitle_en",
		"subtitle_ar",
		"button_title_en",
		"button_title_ar",
		"gradient_start",
		"gradient_end",
		"action_type",
		"action_value",
		"starts_at",
		"ends_at",
	]
	name = frappe.db.get_value("Mobile Home Banner", {"banner_id": banner_id}, "name") or (
		banner_id if frappe.db.exists("Mobile Home Banner", banner_id) else None
	)
	return frappe.db.get_value("Mobile Home Banner", name, fields, as_dict=True) if name else None


def _collection_snapshot(collection_id):
	if not collection_id or not _doctype_exists(COLLECTION_DOCTYPE):
		return None
	row = frappe.db.get_value(
		COLLECTION_DOCTYPE,
		{"collection_id": collection_id, "enabled": 1},
		["collection_id", "title_en", "title_ar", "product_ids"],
		as_dict=True,
	)
	if not row:
		return None
	return {
		"collection_id": row.collection_id,
		"title": {"en": row.title_en, "ar": row.title_ar},
		"product_ids": _unique_strings(_load_json(row.product_ids, [])),
	}


def _banner_outside_window(row) -> bool:
	now = now_datetime()
	if row.get("starts_at") and now < get_datetime(row.starts_at):
		return True
	if row.get("ends_at") and now > get_datetime(row.ends_at):
		return True
	return False


def _banner_snapshot_in_window(item) -> bool:
	if not item:
		return False
	now = now_datetime()
	if item.get("starts_at") and now < get_datetime(item.get("starts_at")):
		return False
	if item.get("ends_at") and now > get_datetime(item.get("ends_at")):
		return False
	return True


def _home_cache_key(publication, locale, filter_key) -> str:
	version = cstr(frappe.cache().get_value(HOME_CACHE_VERSION_KEY) or "0")
	return f"{HOME_CACHE_PREFIX}:{version}:{publication.version}:{publication.checksum}:{locale}:{filter_key}"


def _set_public_etag(publication):
	try:
		frappe.local.response.setdefault("headers", {})["ETag"] = f"W/\"mobile-home-{publication.version}-{publication.checksum}\""
	except Exception:
		pass


def _publication_updated_at(publication) -> str:
	return _iso_z(publication.published_at or publication.modified or now_datetime())


def _snapshot_checksum(snapshot: dict) -> str:
	payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_json(value, default):
	if value is None or value == "":
		return copy.deepcopy(default)
	if hasattr(value, "as_dict"):
		value = value.as_dict()
	if isinstance(value, str):
		try:
			return json.loads(value)
		except json.JSONDecodeError:
			raise BuilderError(_("JSON payload is invalid."), code="INVALID_JSON")
	return copy.deepcopy(value)


def _json_pretty(value) -> str:
	return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _has_error(issues) -> bool:
	return any(issue.get("severity") == "error" for issue in issues)


def _issue(path: str, severity: str, code: str, message: str) -> dict:
	return {"path": path, "severity": severity, "code": code, "message": message}


def _localized_dict(value) -> dict:
	if hasattr(value, "as_dict"):
		value = value.as_dict()
	if isinstance(value, dict):
		return {"en": cstr(value.get("en")).strip(), "ar": cstr(value.get("ar")).strip()}
	return {"en": cstr(value).strip(), "ar": ""}


def _localized(value, locale: str) -> str:
	value = _localized_dict(value)
	return value.get(locale) or value.get("en") or value.get("ar") or ""


def _locale(value) -> str:
	return "ar" if cstr(value).lower().startswith("ar") else "en"


def _filter_key(value) -> str:
	return _slug(value) or "all"


def _slug(value) -> str:
	value = cstr(value).strip().lower()
	value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
	return value


def _unique_strings(values) -> list[str]:
	if hasattr(values, "as_list"):
		values = values.as_list()
	if isinstance(values, str):
		values = _load_json(values, [])
	if not isinstance(values, list):
		return []
	result = []
	seen = set()
	for value in values:
		text = cstr(value).strip()
		if text and text not in seen:
			result.append(text)
			seen.add(text)
	return result


def _bool(value, default=False) -> bool:
	if value is None or value == "":
		return bool(default)
	if isinstance(value, str):
		return value.strip().lower() not in {"0", "false", "no", "off"}
	return bool(cint(value)) if isinstance(value, int) else bool(value)


def _hex(value, default: str) -> str:
	value = cstr(value).strip()
	return value if re.match(r"^#[0-9a-fA-F]{6}$", value) else default


def _absolute_url(value) -> str:
	from pet_app.api.mobile import catalog

	return catalog._absolute_url(value)


def _iso_z(value) -> str:
	from pet_app.api.mobile import catalog

	return catalog._iso_z(value)


def _doctype_exists(doctype: str) -> bool:
	return bool(frappe.db.exists("DocType", doctype))


def _product_filter_field_exists() -> bool:
	if not _doctype_exists("Product"):
		return False
	try:
		return bool(frappe.get_meta("Product").has_field(PRODUCT_FILTER_FIELD))
	except Exception:
		return False


def _table_exists(table: str) -> bool:
	return bool(frappe.db.table_exists(table))
