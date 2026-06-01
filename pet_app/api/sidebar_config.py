from __future__ import annotations

import json

import frappe
from frappe import _
from pet_app.api.response import standardize_response


SIDEBAR_CONFIG_DOCTYPE = "Pet App Sidebar Config"
SIDEBAR_CONFIG_ROLES = {"Pet App Admin", "System Manager"}


def _require_sidebar_config_access():
	user_roles = set(frappe.get_roles(frappe.session.user) or [])
	if not user_roles.intersection(SIDEBAR_CONFIG_ROLES):
		frappe.throw(_("Not authorized."), frappe.PermissionError)


def _ensure_sidebar_config_doctype():
	if not frappe.db.exists("DocType", SIDEBAR_CONFIG_DOCTYPE):
		frappe.throw(
			_("{0} is not installed yet. Run bench migrate for pet_app.").format(
				SIDEBAR_CONFIG_DOCTYPE
			)
		)


def _parse_config(config) -> dict:
	if not config:
		return {}

	if isinstance(config, str):
		try:
			config = json.loads(config)
		except json.JSONDecodeError:
			frappe.throw(_("Sidebar config must be valid JSON."))

	if hasattr(config, "as_dict"):
		config = config.as_dict()

	if not isinstance(config, dict):
		frappe.throw(_("Sidebar config must be a JSON object."))

	return config


def _serialize_config(config) -> str:
	return json.dumps(_parse_config(config), indent=2, sort_keys=True)


@frappe.whitelist()
@standardize_response
def get_sidebar_config():
	_require_sidebar_config_access()
	_ensure_sidebar_config_doctype()

	doc = frappe.get_single(SIDEBAR_CONFIG_DOCTYPE)
	return _parse_config(doc.get("config"))


@frappe.whitelist(methods=["POST"])
@standardize_response
def save_sidebar_config(config):
	_require_sidebar_config_access()
	_ensure_sidebar_config_doctype()

	doc = frappe.get_single(SIDEBAR_CONFIG_DOCTYPE)
	doc.config = _serialize_config(config)
	doc.save(ignore_permissions=True)

	return _parse_config(doc.config)
