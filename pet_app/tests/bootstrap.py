from __future__ import annotations

import frappe


def before_tests():
	_make_erpnext_bootstrap_price_lists_idempotent()
	_patch_price_list_exists_for_erpnext_bootstrap()
	_patch_legacy_test_record_dependency_loader()
	_skip_legacy_preload_for_pet_app_tests()


def _make_erpnext_bootstrap_price_lists_idempotent():
	"""Align existing standard rows with ERPNext's test bootstrap filters."""
	if frappe.db.exists("Price List", "Standard Buying"):
		frappe.db.set_value(
			"Price List",
			"Standard Buying",
			{"enabled": 1, "buying": 1, "selling": 0, "currency": "IQD"},
			update_modified=False,
		)
	if frappe.db.exists("Price List", "Standard Selling"):
		frappe.db.set_value(
			"Price List",
			"Standard Selling",
			{"enabled": 1, "buying": 0, "selling": 1, "currency": "IQD"},
			update_modified=False,
		)
	frappe.db.commit()


def _patch_price_list_exists_for_erpnext_bootstrap():
	original_exists = frappe.db.exists
	if getattr(original_exists, "_pet_app_price_list_patch", False):
		return

	def patched_exists(dt, dn=None, cache=False, *, debug=False):
		if dt == "Price List" and isinstance(dn, dict):
			price_list_name = dn.get("price_list_name")
			if price_list_name and frappe.db.get_value("Price List", price_list_name, "name"):
				return price_list_name
		return original_exists(dt, dn, cache=cache, debug=debug)

	patched_exists._pet_app_price_list_patch = True
	frappe.db.exists = patched_exists


def _patch_legacy_test_record_dependency_loader():
	from frappe.tests import utils as test_utils
	from frappe.tests.utils import generators

	original = generators.get_missing_records_doctypes
	if getattr(original, "_pet_app_missing_doctype_patch", False):
		return

	def patched_get_missing_records_doctypes(doctype, visited=None):
		if not frappe.db.exists("DocType", doctype):
			return []
		try:
			return original(doctype, visited)
		except frappe.DoesNotExistError:
			return []

	patched_get_missing_records_doctypes._pet_app_missing_doctype_patch = True
	generators.get_missing_records_doctypes = patched_get_missing_records_doctypes
	test_utils.generators.get_missing_records_doctypes = patched_get_missing_records_doctypes


def _skip_legacy_preload_for_pet_app_tests():
	import frappe.deprecation_dumpster as deprecation_dumpster

	if getattr(deprecation_dumpster.compat_preload_test_records_upfront, "_pet_app_skip_patch", False):
		return

	def no_op_legacy_preload(_candidates):
		return None

	no_op_legacy_preload._pet_app_skip_patch = True
	deprecation_dumpster.compat_preload_test_records_upfront = no_op_legacy_preload
