from __future__ import annotations

import json
import os
from collections import OrderedDict
from collections.abc import Iterable

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime
from pet_app.api.response import standardize_response


FULL_ACCESS_ROLES = {"Administrator"}
ADMIN_ROLES = {"Administrator", "System Manager", "Pet App Admin"}
PAGE_ACCESS_SETTINGS_DOCTYPE = "Pet App Access Settings"
PAGE_ACCESS_CHILD_DOCTYPE = "Pet App Page Access"
PAGE_ACCESS_FULL_ACCESS_ROLES = {"Administrator", "System Manager", "Pet App Admin"}
ACCESS_SNAPSHOT_CACHE_TTL_SECONDS = 60
ACCESS_SNAPSHOT_CACHE_VERSION_KEY = "pet_app:access_snapshot:version"
ACCESS_SNAPSHOT_CACHE_KEY_PREFIX = "pet_app:access_snapshot"

RESTRICTION_TYPES = ("warehouse", "cashier_profile", "practitioner", "branch")
RESTRICTION_ALIASES = {"doctor": "practitioner"}
RESTRICTION_RESPONSE_TYPES = (*RESTRICTION_TYPES, "doctor")
RESTRICTION_DOCTYPE_MAP = {
	"warehouse": "Warehouse",
	"cashier_profile": "POS Profile",
	"practitioner": "Healthcare Practitioner",
	"branch": "Branch",
}

SNAPSHOT_PERMISSION_TYPES = (
	"read",
	"create",
	"write",
	"delete",
	"submit",
	"cancel",
	"amend",
	"print",
	"email",
	"export",
	"import",
	"report",
	"share",
)

OPERATIONAL_HEALTHCARE_ROLES = [
	"Visit Read",
	"Visit Admin",
	"Lab Read",
	"Lab Admin",
	"Radiology Read",
	"Radiology Admin",
	"Reception",
	"Coordinator",
	"Service Provider Manager",
	"POS Cashier",
	"POS Admin",
]

ROLE_PROFILE_SEED = {
	"Alkokh App Owner": ["Desk User", "Pet App Admin", "System Manager"],
	"Alkokh Admin": [
		"Desk User",
		"Pet App Admin",
		"Reception",
		"Coordinator",
		"Visit Admin",
		"Lab Admin",
		"Radiology Admin",
		"POS Admin",
		"E-commerce",
		"Accounts Manager",
		"Sales Manager",
		"Stock Manager",
		"Item Manager",
		"Purchase Manager",
	],
	"Clinic Manager": [
		"Desk User",
		"Reception",
		"Coordinator",
		"Visit Admin",
		"Lab Admin",
		"Radiology Admin",
		"Nursing User",
		"Laboratory User",
	],
	"Veterinarian": [
		"Desk User",
		"Doctor",
		"Visit Admin",
		"Visit Read",
		"Lab Read",
		"Radiology Read",
	],
	"Clinic Reception": ["Desk User", "Reception", "Coordinator", "Visit Read", "Sales User"],
	"Lab And Radiology Operator": [
		"Desk User",
		"Lab Admin",
		"Lab Read",
		"Radiology Admin",
		"Radiology Read",
		"Visit Read",
		"Laboratory User",
	],
	"Service Provider Profile": ["Service Provider", "Nursing User", "Groomer"],
	"Service Provider Manager Profile": ["Desk User", "Service Provider Manager"],
	"Visit Read Profile": ["Desk User", "Visit Read"],
	"Visit Admin Profile": ["Desk User", "Visit Admin"],
	"Lab Read Profile": ["Desk User", "Lab Read"],
	"Lab Admin Profile": ["Desk User", "Lab Admin"],
	"Radiology Read Profile": ["Desk User", "Radiology Read"],
	"Radiology Admin Profile": ["Desk User", "Radiology Admin"],
	"Reception Profile": ["Desk User", "Reception"],
	"Coordinator Profile": ["Desk User", "Coordinator", "Reception"],
	"POS Cashier": ["Desk User", "POS Cashier"],
	"POS Cashier Profile": ["Desk User", "POS Cashier"],
	"POS Supervisor": ["Desk User", "POS Admin", "Accounts User"],
	"POS Admin Profile": ["Desk User", "POS Admin"],
	"Accountant": ["Desk User", "Accounts User"],
	"Accounting Manager": ["Desk User", "Accounts Manager"],
	"Warehouse User": ["Desk User", "Stock User"],
	"Warehouse Manager": ["Desk User", "Stock Manager"],
	"Purchase Officer": ["Desk User", "Stock Manager", "Accounts User"],
	"Auditor": ["Desk User", "Auditor"],
	"HR Officer": ["Desk User", "HR User"],
	"HR Manager": ["Desk User", "HR User", "HR Manager"],
	"Module Viewer": ["Desk User"],
}

DEMO_USERS = {
	"owner@example.com": "Alkokh App Owner",
	"clinic.manager@example.com": "Clinic Manager",
	"vet@example.com": "Veterinarian",
	"reception@example.com": "Clinic Reception",
	"cashier@example.com": "POS Cashier",
	"accountant@example.com": "Accountant",
	"warehouse@example.com": "Warehouse User",
	"auditor@example.com": "Auditor",
}

REQUIRED_CUSTOM_ROLES = {
	"Pet App Admin",
	"Doctor",
	"E-commerce",
	"Service Provider",
	"Nursing User",
	"Groomer",
	"Accounts User",
	"Accounts Manager",
	"Stock User",
	"Stock Manager",
	"Purchase User",
	"Purchase Manager",
	"Auditor",
	*OPERATIONAL_HEALTHCARE_ROLES,
}

# Compatibility names only. The app no longer authorizes via module/page/action/workflow resources.
MODULE_RULES = {}
PAGE_RULES = {}
ACTION_RULES = {}


@frappe.whitelist()
@standardize_response
def get_current_access():
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Authentication required"), frappe.PermissionError)
	return get_cached_access_snapshot(user)


def get_cached_access_snapshot(user: str) -> dict:
	cache = frappe.cache()
	version = _access_snapshot_cache_version()
	cache_key = f"{ACCESS_SNAPSHOT_CACHE_KEY_PREFIX}:{version}:{user}"
	cached = cache.get_value(cache_key)
	if cached:
		return cached

	snapshot = build_access_snapshot(user)
	cache.set_value(cache_key, snapshot, expires_in_sec=ACCESS_SNAPSHOT_CACHE_TTL_SECONDS)
	return snapshot


def clear_access_snapshot_cache(*args, **kwargs):
	frappe.cache().set_value(ACCESS_SNAPSHOT_CACHE_VERSION_KEY, frappe.generate_hash(length=12))


def _access_snapshot_cache_version() -> str:
	return cstr(frappe.cache().get_value(ACCESS_SNAPSHOT_CACHE_VERSION_KEY) or "0")


def build_access_snapshot(user: str | None = None) -> dict:
	user = user or frappe.session.user
	role_profiles = get_user_role_profiles(user)
	roles = get_user_roles(user)
	page_access = _page_access_for_user(user, roles)
	return {
		"roles": sorted(roles),
		"roleProfile": role_profiles[0] if role_profiles else None,
		"roleProfiles": role_profiles,
		"fullAccess": user_has_full_access(user),
		"fullAccessRoles": sorted(FULL_ACCESS_ROLES),
		"modules": page_access["modules"],
		"pages": page_access["pages"],
		"actions": [],
		"doctypes": _doctype_permissions(user),
		"restrictions": get_restrictions_for_user(user),
		"loadedAt": _utc_now_iso(),
	}


def get_user_roles(user: str | None = None) -> set[str]:
	user = user or frappe.session.user
	if not user or user == "Guest":
		return set()

	roles = set(frappe.get_roles(user) or [])
	roles.update(_roles_from_role_profiles(get_user_role_profiles(user)))
	if user == "Administrator":
		roles.add("Administrator")
	return roles


def get_user_role_profiles(user: str | None = None) -> list[str]:
	user = user or frappe.session.user
	if not user or user == "Guest":
		return []

	profiles = OrderedDict()
	for row in _safe_get_all(
		"User Role Profile",
		{"parenttype": "User", "parentfield": "role_profiles", "parent": user},
		fields=["role_profile"],
		order_by="idx asc",
	):
		role_profile = cstr(row.get("role_profile")).strip()
		if role_profile:
			profiles[role_profile] = None
	return list(profiles)


def get_user_role_profile(user: str | None = None) -> str | None:
	role_profiles = get_user_role_profiles(user)
	return role_profiles[0] if role_profiles else None


def user_has_full_access(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator"


def require_doctype_permission(doctype: str, ptype: str, user: str | None = None):
	user = user or frappe.session.user
	if not _doctype_exists(doctype) or not _has_doctype_permission(doctype, ptype, user):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def get_restrictions_for_user(user: str | None = None) -> dict[str, list[str]]:
	user = user or frappe.session.user
	grouped = _empty_restrictions()
	if not user or user == "Guest" or not _doctype_exists("User Permission"):
		return grouped

	restriction_by_doctype = {doctype: key for key, doctype in RESTRICTION_DOCTYPE_MAP.items()}
	rows = _safe_get_all(
		"User Permission",
		{"user": user, "allow": ["in", sorted(restriction_by_doctype)]},
		fields=["allow", "for_value", "hide_descendants"],
		order_by="allow asc, for_value asc",
	)
	for row in rows:
		restriction_type = restriction_by_doctype.get(row.get("allow"))
		value = cstr(row.get("for_value")).strip()
		if not restriction_type or not value:
			continue
		_add_restriction_value(grouped, restriction_type, value)
		for descendant in _restriction_descendants(row.get("allow"), value, row.get("hide_descendants")):
			_add_restriction_value(grouped, restriction_type, descendant)
	grouped["doctor"] = list(grouped.get("practitioner", []))
	return grouped


def get_restriction_values(restriction_type: str, user: str | None = None) -> list[str]:
	return get_restrictions_for_user(user).get(_normalize_restriction_type(restriction_type), [])


def is_restriction_value_allowed(restriction_type: str, value: str | None, user: str | None = None) -> bool:
	if not value or user_has_full_access(user):
		return True
	allowed = get_restriction_values(restriction_type, user)
	return not allowed or value in allowed


def require_restriction_value(restriction_type: str, value: str | None, user: str | None = None):
	if not is_restriction_value_allowed(restriction_type, value, user=user):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def filter_restricted_values(restriction_type: str, values: Iterable[str], user: str | None = None) -> list[str]:
	values = [value for value in values if value]
	if user_has_full_access(user):
		return values
	allowed = get_restriction_values(restriction_type, user)
	if not allowed:
		return values
	return [value for value in values if value in allowed]


@frappe.whitelist()
@standardize_response
def get_user_restrictions(user: str | None = None):
	target_user = user or frappe.session.user
	if target_user != frappe.session.user and not _is_admin():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return get_restrictions_for_user(target_user)


@frappe.whitelist(methods=["POST"])
@standardize_response
def update_user_restrictions(user: str | None = None, restrictions=None, data=None, **kwargs):
	_require_admin()
	payload = _coerce_dict(data)
	payload.update({key: value for key, value in kwargs.items() if value is not None})

	target_user = user or payload.get("user")
	incoming_restrictions = restrictions if restrictions is not None else payload.get("restrictions")
	incoming_restrictions = _coerce_dict(incoming_restrictions)

	if not target_user or not frappe.db.exists("User", target_user):
		frappe.throw(_("Valid user is required."))

	_validate_restrictions_payload(incoming_restrictions)
	_replace_user_restrictions(target_user, incoming_restrictions)
	return get_restrictions_for_user(target_user)


@frappe.whitelist()
@standardize_response
def get_page_access_settings():
	_require_page_access_admin()
	return _page_access_settings_response()


@frappe.whitelist(methods=["POST"])
@standardize_response
def sync_frontend_pages(pages=None, data=None, **kwargs):
	_require_page_access_admin()
	incoming_pages = pages if pages is not None else data
	if incoming_pages is None and kwargs:
		incoming_pages = kwargs

	frontend_pages = _normalize_page_sync_payload(incoming_pages)
	_validate_unique_page_keys(frontend_pages)
	settings = _get_page_access_settings_doc()
	existing_by_key = {
		cstr(row.get("page_key")).strip(): row
		for row in settings.get("pages", [])
		if cstr(row.get("page_key")).strip()
	}

	settings.set("pages", [])
	for page in frontend_pages:
		existing = existing_by_key.get(page["page_key"])
		settings.append(
			"pages",
			{
				"page_key": page["page_key"],
				"label": page["label"],
				"module_key": page["module_key"],
				"page_doctype": page["doctype"],
				"paths_json": _dump_json_list(page["paths"]),
				"path_prefixes_json": _dump_json_list(page["path_prefixes"]),
				"route_names_json": _dump_json_list(page["route_names"]),
				"enabled": cint(existing.get("enabled")) if existing else 1,
				"roles_json": existing.get("roles_json") if existing else _dump_json_list(page["fallback_roles"]),
			},
		)

	settings.save(ignore_permissions=True)
	_clear_page_access_cache()
	return _page_access_settings_response(settings)


@frappe.whitelist(methods=["POST"])
@standardize_response
def update_page_access_settings(pages=None, data=None, **kwargs):
	_require_page_access_admin()
	incoming_pages = pages if pages is not None else data
	if incoming_pages is None and kwargs:
		incoming_pages = kwargs

	updates = _normalize_page_update_payload(incoming_pages)
	_validate_unique_page_keys(updates)
	settings = _get_page_access_settings_doc()
	existing_by_key = {
		cstr(row.get("page_key")).strip(): row
		for row in settings.get("pages", [])
		if cstr(row.get("page_key")).strip()
	}

	for update in updates:
		row = existing_by_key.get(update["page_key"])
		if not row:
			frappe.throw(_("Unknown page key: {0}").format(frappe.bold(update["page_key"])))
		if update.get("enabled") is not None:
			row.enabled = cint(update.get("enabled"))
		if update.get("roles") is not None:
			_validate_roles_exist(update["roles"])
			row.roles_json = _dump_json_list(update["roles"])

	settings.save(ignore_permissions=True)
	_clear_page_access_cache()
	return _page_access_settings_response(settings)


def ensure_roles():
	for role_name in sorted(REQUIRED_CUSTOM_ROLES | FULL_ACCESS_ROLES | _seed_profile_roles()):
		if frappe.db.exists("Role", role_name):
			continue
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)


def ensure_role_profiles():
	for profile_name, expected_roles in ROLE_PROFILE_SEED.items():
		if frappe.db.exists("Role Profile", profile_name):
			continue
		role_profile = frappe.get_doc({"doctype": "Role Profile", "role_profile": profile_name, "roles": []})
		for role_name in expected_roles:
			if frappe.db.exists("Role", role_name):
				role_profile.append("roles", {"role": role_name})
		role_profile.insert(ignore_permissions=True)


def seed_demo_users_if_enabled():
	if not _demo_user_seed_enabled():
		return
	for email, profile in DEMO_USERS.items():
		if frappe.db.exists("User", email):
			continue
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@", 1)[0].replace(".", " ").title(),
				"enabled": 0,
				"send_welcome_email": 0,
			}
		)
		if frappe.db.exists("Role Profile", profile):
			user.append("role_profiles", {"role_profile": profile})
		user.insert(ignore_permissions=True)


def get_allowed_resources(user: str | None = None, resource_type: str | None = None) -> list[str]:
	return []


def has_app_permission(resource_key: str, user: str | None = None) -> bool:
	return False


def require_app_permission(resource_key: str, user: str | None = None):
	frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist()
@standardize_response
def get_access_matrix():
	_require_admin()
	return {
		"deprecated": True,
		"readOnly": True,
		"message": "Custom app-resource permissions were removed. Use Frappe Role Permission Manager and DocPerms.",
		"fullAccessRoles": sorted(FULL_ACCESS_ROLES),
		"modules": [],
		"pages": [],
		"actions": [],
	}


@frappe.whitelist(methods=["POST"])
@standardize_response
def update_access_matrix(matrix=None, data=None, matrix_json=None, replace_roles=1, **kwargs):
	_require_admin()
	return get_access_matrix()


@frappe.whitelist(methods=["POST"])
@standardize_response
def register_frontend_resources(resources=None, data=None, matrix=None, resources_json=None, matrix_json=None, **kwargs):
	_require_admin()
	return get_access_matrix()


@frappe.whitelist()
@standardize_response
def get_permission_integrity_report():
	_require_admin()
	return build_permission_integrity_report()


def validate_permission_integrity(throw: bool = False) -> dict:
	report = build_permission_integrity_report()
	if throw and report["errors"]:
		frappe.throw("<br>".join(report["errors"]))
	return report


def build_permission_integrity_report() -> dict:
	return {
		"ok": True,
		"errors": [],
		"warnings": ["Custom app-resource permissions were removed; Frappe DocPerms are authoritative."],
	}


def _doctype_permissions(user: str) -> dict[str, list[str]]:
	if user_has_full_access(user):
		return _administrator_doctype_permissions(user)

	roles = get_user_roles(user)
	if not roles:
		return {}

	permissions = OrderedDict()
	for row in _permission_rows_for_roles(roles):
		doctype = cstr(row.get("parent")).strip()
		if not doctype or not _doctype_exists(doctype):
			continue
		for ptype in SNAPSHOT_PERMISSION_TYPES:
			if cint(row.get(ptype)):
				permissions.setdefault(doctype, OrderedDict())[ptype] = None

	return {doctype: list(ptypes) for doctype, ptypes in permissions.items() if ptypes}


def _page_access_for_user(user: str, roles: set[str] | None = None) -> dict[str, list[str]]:
	roles = roles or get_user_roles(user)
	pages = []
	modules = OrderedDict()
	full_page_access = _user_has_page_access_full_access(user, roles)

	for row in _page_access_rows():
		if not cint(row.get("enabled")):
			continue
		page_key = cstr(row.get("page_key")).strip()
		if not page_key:
			continue
		assigned_roles = set(_load_json_list(row.get("roles_json")))
		if not full_page_access and not assigned_roles.intersection(roles):
			continue
		pages.append(page_key)
		module_key = cstr(row.get("module_key")).strip()
		if module_key:
			modules[module_key] = None

	return {"modules": list(modules), "pages": pages}


def _page_access_settings_response(settings=None) -> dict:
	settings = settings or _get_page_access_settings_doc()
	return {"pages": [_serialize_page_access_row(row) for row in settings.get("pages", []) if row.get("page_key")]}


def _serialize_page_access_row(row) -> dict:
	return {
		"page_key": cstr(row.get("page_key")).strip(),
		"label": cstr(row.get("label")).strip(),
		"module_key": cstr(row.get("module_key")).strip(),
		"doctype": cstr(row.get("page_doctype") or row.get("doctype")).strip(),
		"paths": _load_json_list(row.get("paths_json")),
		"path_prefixes": _load_json_list(row.get("path_prefixes_json")),
		"route_names": _load_json_list(row.get("route_names_json")),
		"enabled": cint(row.get("enabled")),
		"roles": _load_json_list(row.get("roles_json")),
	}


def _page_access_rows() -> list:
	if not _page_access_doctypes_available():
		return []
	try:
		return list(frappe.get_single(PAGE_ACCESS_SETTINGS_DOCTYPE).get("pages", []))
	except Exception:
		return []


def _get_page_access_settings_doc():
	if not _page_access_doctypes_available():
		frappe.throw(_("Page Access settings are not installed. Run bench migrate."))
	return frappe.get_single(PAGE_ACCESS_SETTINGS_DOCTYPE)


def _page_access_doctypes_available() -> bool:
	return _doctype_exists(PAGE_ACCESS_SETTINGS_DOCTYPE) and _doctype_exists(PAGE_ACCESS_CHILD_DOCTYPE)


def _normalize_page_sync_payload(value) -> list[dict]:
	pages = _coerce_page_payload(value)
	return [_normalize_page_sync_entry(page) for page in pages]


def _normalize_page_update_payload(value) -> list[dict]:
	pages = _coerce_page_payload(value)
	return [_normalize_page_update_entry(page) for page in pages]


def _coerce_page_payload(value) -> list:
	if value is None:
		return []
	if isinstance(value, str):
		value = json.loads(value) if value else []
	if hasattr(value, "as_dict"):
		value = value.as_dict()
	if isinstance(value, dict):
		value = value.get("pages", [])
	if not isinstance(value, list | tuple):
		frappe.throw(_("Pages payload must be a list."))
	return list(value)


def _normalize_page_sync_entry(value) -> dict:
	row = _coerce_page_row(value)
	page_key = cstr(row.get("page_key") or row.get("key")).strip()
	if not page_key:
		frappe.throw(_("Page key is required."))
	return {
		"page_key": page_key,
		"label": cstr(row.get("label")).strip(),
		"module_key": cstr(row.get("module_key") or row.get("moduleKey")).strip(),
		"doctype": cstr(row.get("doctype")).strip(),
		"paths": _normalize_string_list(row.get("paths")),
		"path_prefixes": _normalize_string_list(row.get("path_prefixes") or row.get("pathPrefixes")),
		"route_names": _normalize_string_list(row.get("route_names") or row.get("routeNames")),
		"fallback_roles": _normalize_string_list(row.get("fallback_roles") or row.get("fallbackRoles")),
	}


def _normalize_page_update_entry(value) -> dict:
	row = _coerce_page_row(value)
	page_key = cstr(row.get("page_key") or row.get("key")).strip()
	if not page_key:
		frappe.throw(_("Page key is required."))

	roles = None
	if "roles" in row:
		roles = _normalize_string_list(row.get("roles"))
	elif "roles_json" in row:
		roles = _load_json_list(row.get("roles_json"))

	return {
		"page_key": page_key,
		"enabled": cint(row.get("enabled")) if "enabled" in row else None,
		"roles": roles,
	}


def _coerce_page_row(value) -> dict:
	if isinstance(value, str):
		value = json.loads(value) if value else {}
	if hasattr(value, "as_dict"):
		value = value.as_dict()
	if not isinstance(value, dict):
		frappe.throw(_("Page rows must be objects."))
	return dict(value)


def _validate_unique_page_keys(pages: list[dict]):
	seen = set()
	for page in pages:
		page_key = page["page_key"]
		if page_key in seen:
			frappe.throw(_("Duplicate page key: {0}").format(frappe.bold(page_key)))
		seen.add(page_key)


def _validate_roles_exist(roles: list[str]):
	missing = [role for role in _normalize_string_list(roles) if not frappe.db.exists("Role", role)]
	if missing:
		frappe.throw(_("Role(s) do not exist: {0}").format(", ".join(missing)))


def _load_json_list(value) -> list[str]:
	return _normalize_string_list(value)


def _dump_json_list(values) -> str:
	return json.dumps(_normalize_string_list(values), ensure_ascii=False)


def _user_has_page_access_full_access(user: str | None = None, roles: set[str] | None = None) -> bool:
	user = user or frappe.session.user
	roles = roles or get_user_roles(user)
	return user == "Administrator" or bool(PAGE_ACCESS_FULL_ACCESS_ROLES.intersection(roles))


def _is_page_access_admin(target_user: str | None = None) -> bool:
	return _user_has_page_access_full_access(target_user)


def _require_page_access_admin():
	if not _is_page_access_admin():
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _clear_page_access_cache():
	frappe.clear_cache(doctype=PAGE_ACCESS_SETTINGS_DOCTYPE)
	frappe.clear_cache(doctype=PAGE_ACCESS_CHILD_DOCTYPE)


def _administrator_doctype_permissions(user: str) -> dict[str, list[str]]:
	result = OrderedDict()
	for doctype in _all_doctype_names():
		ptypes = []
		for ptype in SNAPSHOT_PERMISSION_TYPES:
			if ptype in {"submit", "cancel", "amend"} and not _is_submittable(doctype):
				continue
			if _has_doctype_permission(doctype, ptype, user):
				ptypes.append(ptype)
		if ptypes:
			result[doctype] = ptypes
	return dict(result)


def _permission_rows_for_roles(roles: set[str]) -> list[dict]:
	rows = []
	for doctype in ("DocPerm", "Custom DocPerm"):
		if not _doctype_exists(doctype):
			continue
		fields = ["parent", *[ptype for ptype in SNAPSHOT_PERMISSION_TYPES if _meta_has_field(doctype, ptype)]]
		rows.extend(
			_safe_get_all(
				doctype,
				{"role": ["in", sorted(roles)], "permlevel": 0},
				fields=fields,
				order_by="parent asc",
			)
		)
	return rows


def _all_doctype_names() -> list[str]:
	return [
		cstr(name)
		for name in frappe.get_all(
			"DocType",
			filters={"istable": 0},
			pluck="name",
			ignore_permissions=True,
			order_by="name asc",
		)
	]


def _roles_from_role_profiles(role_profiles) -> set[str]:
	roles = set()
	for role_profile in _normalize_string_list(role_profiles):
		try:
			if not frappe.db.exists("Role Profile", role_profile):
				continue
			doc = frappe.get_doc("Role Profile", role_profile)
			roles.update(row.role for row in doc.roles if row.role)
		except Exception:
			continue
	return roles


def _seed_profile_roles() -> set[str]:
	roles = set()
	for profile_roles in ROLE_PROFILE_SEED.values():
		roles.update(profile_roles)
	return roles


def _replace_user_restrictions(user: str, restrictions: dict):
	if not _doctype_exists("User Permission"):
		frappe.throw(_("User Permission DocType is required."))

	allowed_doctypes = set(RESTRICTION_DOCTYPE_MAP.values())
	for name in frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": ["in", sorted(allowed_doctypes)]},
		pluck="name",
		ignore_permissions=True,
	):
		frappe.delete_doc("User Permission", name, ignore_permissions=True, force=True)

	normalized_restrictions = _normalize_restrictions_payload(restrictions)
	for restriction_type in RESTRICTION_TYPES:
		allow_doctype = RESTRICTION_DOCTYPE_MAP.get(restriction_type)
		if not allow_doctype:
			continue
		if normalized_restrictions.get(restriction_type) and not _doctype_exists(allow_doctype):
			frappe.throw(_("Restriction DocType {0} does not exist.").format(frappe.bold(allow_doctype)))
		for value in _normalize_string_list(normalized_restrictions.get(restriction_type)):
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": user,
					"allow": allow_doctype,
					"for_value": value,
					"apply_to_all_doctypes": 1,
				}
			).insert(ignore_permissions=True)
	frappe.clear_cache(user=user)


def _add_restriction_value(grouped: dict[str, list[str]], restriction_type: str, value: str):
	if restriction_type in grouped and value and value not in grouped[restriction_type]:
		grouped[restriction_type].append(value)


def _restriction_descendants(doctype: str, value: str, hide_descendants=0) -> list[str]:
	if cint(hide_descendants) or not _doctype_exists(doctype):
		return []
	try:
		if not frappe.get_meta(doctype).is_nested_set():
			return []
		return [cstr(name) for name in frappe.db.get_descendants(doctype, value) if name]
	except Exception:
		return []


def _has_doctype_permission(doctype: str, ptype: str, user: str) -> bool:
	try:
		return bool(frappe.has_permission(doctype, ptype=ptype, user=user))
	except Exception:
		return False


def _is_submittable(doctype: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).is_submittable)
	except Exception:
		return False


def _doctype_exists(doctype: str) -> bool:
	try:
		return bool(frappe.db.exists("DocType", doctype))
	except Exception:
		return False


def _meta_has_field(doctype: str, fieldname: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).has_field(fieldname))
	except Exception:
		return False


def _safe_get_all(doctype: str, filters: dict, fields: list[str], limit: int | None = None, order_by: str | None = None):
	try:
		kwargs = {
			"filters": filters,
			"fields": fields,
			"ignore_permissions": True,
		}
		if limit:
			kwargs["limit"] = limit
		if order_by:
			kwargs["order_by"] = order_by
		return frappe.get_all(doctype, **kwargs)
	except Exception:
		return []


def _normalize_string_list(values) -> list[str]:
	if values is None:
		return []
	if isinstance(values, str):
		try:
			parsed = json.loads(values)
			values = parsed
		except Exception:
			values = [value.strip() for value in values.split(",")]
	if isinstance(values, dict):
		values = values.values()
	if not isinstance(values, Iterable):
		values = [values]

	normalized = []
	for value in values:
		value = cstr(value).strip()
		if value and value not in normalized:
			normalized.append(value)
	return normalized


def _coerce_dict(value) -> dict:
	if value is None:
		return {}
	if isinstance(value, str):
		value = json.loads(value) if value else {}
	if hasattr(value, "as_dict"):
		value = value.as_dict()
	return dict(value or {})


def _validate_restrictions_payload(restrictions: dict):
	unknown = {_normalize_restriction_type(key) for key in restrictions} - set(RESTRICTION_TYPES)
	if unknown:
		frappe.throw(_("Invalid restriction type(s): {0}").format(", ".join(sorted(unknown))))


def _empty_restrictions() -> dict[str, list[str]]:
	return {restriction_type: [] for restriction_type in RESTRICTION_TYPES}


def _normalize_restrictions_payload(restrictions: dict) -> dict:
	normalized = _empty_restrictions()
	for key, value in (restrictions or {}).items():
		restriction_type = _normalize_restriction_type(key)
		if restriction_type in normalized:
			normalized[restriction_type] = value
	return normalized


def _normalize_restriction_type(restriction_type: str | None) -> str:
	return RESTRICTION_ALIASES.get(cstr(restriction_type), cstr(restriction_type))


def _is_admin(target_user: str | None = None) -> bool:
	user = target_user or frappe.session.user
	if user == "Administrator":
		return True
	for doctype in ("User Permission", "Role Profile"):
		if _has_doctype_permission(doctype, "write", user) or _has_doctype_permission(doctype, "create", user):
			return True
	return False


def _require_admin():
	if not _is_admin():
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _demo_user_seed_enabled() -> bool:
	return os.environ.get("PET_APP_SEED_DEMO_USERS") == "1" or cstr(
		frappe.conf.get("pet_app_seed_demo_users")
	).lower() in {"1", "true", "yes"}


def _utc_now_iso() -> str:
	value = now_datetime().replace(microsecond=0).isoformat()
	return value.replace("+00:00", "Z") if value.endswith("+00:00") else f"{value}Z"
