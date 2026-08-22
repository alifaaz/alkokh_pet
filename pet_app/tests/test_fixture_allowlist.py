from __future__ import annotations

import ast
import json
import os
from unittest import TestCase

import frappe

APP_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHES_DIR = os.path.join(APP_PATH, "patches")
FIXTURE_PATH = os.path.join(APP_PATH, "fixtures", "custom_field.json")

# Fields that a patch creates AND that are also shipped as fixture entries. Every one of
# these predates the "patches own what they create" rule. They are recorded rather than
# fixed so that a NEW overlap fails the build; this set should only ever shrink.
KNOWN_DUAL_OWNED = {
	"Appointment-custom_cancelled_due_to_death",
	"Appointment-custom_care_plan_item",
	"Appointment-custom_client_request_id",
	"Appointment-custom_death_record",
	"Appointment-custom_doctor",
	"Appointment-custom_duration_minutes",
	"Appointment-custom_idempotency_key",
	"Appointment-custom_last_synced_at",
	"Appointment-custom_room",
	"POS Profile-custom_cash_account",
}


def _allowlist_from_hooks() -> set[str]:
	"""The names in the Custom Field fixture filter, read from the live hooks."""
	for entry in frappe.get_hooks("fixtures") or []:
		if not isinstance(entry, dict) or entry.get("dt") != "Custom Field":
			continue
		for condition in entry.get("filters") or []:
			if len(condition) == 3 and condition[0] == "name" and condition[1] == "in":
				return set(condition[2])
	return set()


def _fields_created_by_patches() -> dict[str, str]:
	"""Every (dt, fieldname) a pet_app patch hands to create_custom_fields.

	Parsed rather than executed. The argument is always a literal
	{"DocType": [{"fieldname": ...}, ...]} mapping, but the values inside may be names
	rather than strings - address_delivery_notes passes a FIELDNAME constant - so
	module-level assignments are resolved before the mapping is walked.

	Returns {custom_field_name: patch_module}. A patch whose call cannot be read
	statically raises rather than being skipped: silently missing a field here would
	defeat the whole point of the guard.
	"""
	created: dict[str, str] = {}

	for filename in sorted(os.listdir(PATCHES_DIR)):
		if not filename.endswith(".py") or filename == "__init__.py":
			continue
		path = os.path.join(PATCHES_DIR, filename)
		with open(path, encoding="utf-8") as handle:
			source = handle.read()
		if "create_custom_fields(" not in source:
			continue

		tree = ast.parse(source, filename=path)
		# Walked, not just tree.body: cashier_backend_setup builds its mapping inside the
		# function, and address_delivery_notes binds its fieldname to a module constant.
		constants = {
			target.id: node.value.value
			for node in ast.walk(tree)
			if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
			for target in node.targets
			if isinstance(target, ast.Name)
		}

		for node in ast.walk(tree):
			if not (isinstance(node, ast.Call) and _is_create_custom_fields(node)):
				continue
			if not node.args:
				continue
			mapping = _resolve(node.args[0], tree)
			if mapping is None:
				raise AssertionError(
					f"{filename}: create_custom_fields() argument could not be read statically. "
					f"Pass a literal mapping, or this guard cannot verify the allow-list."
				)
			for doctype, definitions in _walk_mapping(mapping, constants):
				for fieldname in definitions:
					created[f"{doctype}-{fieldname}"] = f"pet_app.patches.{filename[:-3]}"

	return created


def _is_create_custom_fields(node: ast.Call) -> bool:
	func = node.func
	if isinstance(func, ast.Name):
		return func.id == "create_custom_fields"
	return isinstance(func, ast.Attribute) and func.attr == "create_custom_fields"


def _resolve(node, tree):
	"""A dict literal, or any name in the file bound to one."""
	if isinstance(node, ast.Dict):
		return node
	if isinstance(node, ast.Name):
		for stmt in ast.walk(tree):
			if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Dict):
				for target in stmt.targets:
					if isinstance(target, ast.Name) and target.id == node.id:
						return stmt.value
	return None


def _walk_mapping(mapping: ast.Dict, constants: dict):
	for key, value in zip(mapping.keys, mapping.values):
		if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
			continue
		if not isinstance(value, ast.List):
			continue
		fieldnames = []
		for element in value.elts:
			name = _fieldname_of(element, constants)
			if name:
				fieldnames.append(name)
		if fieldnames:
			yield key.value, fieldnames


def _fieldname_of(element, constants: dict):
	"""The fieldname out of a {...} or dict(...) field definition."""
	pairs = []
	if isinstance(element, ast.Dict):
		pairs = list(zip(element.keys, element.values))
	elif isinstance(element, ast.Call) and isinstance(element.func, ast.Name) and element.func.id == "dict":
		pairs = [(ast.Constant(value=kw.arg), kw.value) for kw in element.keywords]

	for key, value in pairs:
		if not (isinstance(key, ast.Constant) and key.value == "fieldname"):
			continue
		if isinstance(value, ast.Constant) and isinstance(value.value, str):
			return value.value
		if isinstance(value, ast.Name) and value.id in constants:
			return constants[value.id]
	return None


class TestFixtureAllowlist(TestCase):
	"""Guards the Custom Field fixture allow-list in hooks.py.

	The allow-list replaced a doctype filter that swept up other apps' fields - see
	pet_app.patches.remove_uae_vat_custom_fields. Its failure mode is the opposite one:
	a field added by a patch but never listed simply stops shipping, and nothing notices
	until a fresh install is missing it. This test is what notices.
	"""

	def test_no_field_is_shipped_by_both_a_patch_and_a_fixture(self):
		"""A field has one owner. A patch that creates it, or a fixture entry - never both.

		Two mechanisms creating one field is a silent drift source: migrate runs the patch
		and then sync_fixtures overwrites the result with whatever JSON was last exported,
		in that order. That is how the coordinate fields ended up defined twice.

		The ten below predate the rule and are grandfathered rather than fixed here -
		removing them is a separate change with its own verification. A NEW overlap fails.
		"""
		allowlist = _allowlist_from_hooks()
		self.assertTrue(allowlist, "hooks.py has no name-based Custom Field fixture filter")

		created = _fields_created_by_patches()
		new_overlaps = {
			name: patch
			for name, patch in created.items()
			if name in allowlist and name not in KNOWN_DUAL_OWNED
		}

		self.assertFalse(
			new_overlaps,
			"These Custom Fields are created by a patch AND shipped as a fixture. Pick one "
			"owner: leave the patch as the source of truth and drop the fixture entry from "
			"the hooks.py allow-list and custom_field.json.\n"
			+ "\n".join(f"  {name}  <- {patch}" for name, patch in sorted(new_overlaps.items())),
		)

	def test_grandfathered_overlaps_have_not_grown(self):
		"""KNOWN_DUAL_OWNED should only ever shrink. Anything still listed but no longer
		overlapping has been cleaned up, and the entry should be deleted from the set."""
		allowlist = _allowlist_from_hooks()
		created = _fields_created_by_patches()
		stale = sorted(n for n in KNOWN_DUAL_OWNED if not (n in allowlist and n in created))
		self.assertFalse(
			stale,
			f"No longer dual-owned - remove from KNOWN_DUAL_OWNED: {stale}",
		)

	def test_allowlist_matches_the_exported_fixture_file(self):
		"""The two must not drift: an entry in one and not the other is a bug either way."""
		with open(FIXTURE_PATH, encoding="utf-8") as handle:
			exported = {row["name"] for row in json.load(handle)}
		allowlist = _allowlist_from_hooks()

		self.assertFalse(
			exported - allowlist,
			"In custom_field.json but not in the hooks.py allow-list - the next "
			f"export-fixtures will drop them: {sorted(exported - allowlist)}",
		)
		self.assertFalse(
			allowlist - exported,
			"In the hooks.py allow-list but not in custom_field.json - either the field "
			f"was removed and the list not updated, or an export is pending: {sorted(allowlist - exported)}",
		)

	def test_no_uae_vat_fields_are_shipped(self):
		"""The specific regression this whole change exists to prevent."""
		from pet_app.patches.remove_uae_vat_custom_fields import UAE_FIELDS

		allowlist = _allowlist_from_hooks()
		leaked = [f"{dt}-{fn}" for dt, fn in UAE_FIELDS if f"{dt}-{fn}" in allowlist]
		self.assertFalse(leaked, f"UAE VAT fields are back in the fixture allow-list: {leaked}")
