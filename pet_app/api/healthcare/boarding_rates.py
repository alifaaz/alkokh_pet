"""The boarding rate grid: species x Travel/Treatment, read from and saved to the catalogue.

The CareService boarding rows are the ONE place a boarding rate lives - `resolve_boarding_rate`
bills from them and nothing else. The settings screen used to edit the Item Price behind
the deprecated `travel_boarding_item` / `treatment_boarding_item` fields instead, which are
the CAT items: the estimate then quoted every dog at the cat rate, and a rate typed there
never reached an invoice. This module lets that screen edit the real rows instead of a copy.

Saving a template's `default_price` already re-syncs its Standard Selling Item Price
(`CareServicetemplate._ensure_item_price`), so the two stay equal without a second write here.

Existing stays are never repriced: a room row that already has a rate is left alone by
`_ensure_room_stay_billable_item`, so a change applies to rows priced after it.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt, get_datetime

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import begin_action, rollback_action, standardize_response
from pet_app.utils.boarding_pricing import FALLBACK_ANIMAL_TYPE, get_boarding_category

TEMPLATE_DOCTYPE = "CareService template"
BOARDING_TYPES = ("Travel", "Treatment")


def _boarding_rows(category: str) -> list:
	return frappe.get_all(
		TEMPLATE_DOCTYPE,
		filters={"category_id": category, "disabled": 0, "boarding_type": ["in", list(BOARDING_TYPES)]},
		fields=["name", "service_name", "item_code", "animal_type", "boarding_type", "default_price", "modified"],
		order_by="animal_type asc, boarding_type asc, name asc",
	)


def _grid_issues(rows: list) -> list[dict]:
	"""Catalogue faults the screen must show rather than quietly render around."""
	issues = []
	by_key: dict = {}
	for row in rows:
		by_key.setdefault((row.animal_type, row.boarding_type), []).append(row.name)
		if flt(row.default_price) <= 0:
			issues.append({"code": "NO_PRICE", "template": row.name, "animal_type": row.animal_type,
				"boarding_type": row.boarding_type})
		if not cstr(row.item_code).strip():
			issues.append({"code": "NO_ITEM", "template": row.name, "animal_type": row.animal_type,
				"boarding_type": row.boarding_type})
	for (animal_type, boarding_type), names in by_key.items():
		if len(names) > 1:
			# `_match` refuses to bill this pair at all, so check-out is blocked for it.
			issues.append({"code": "DUPLICATE", "templates": names, "animal_type": animal_type,
				"boarding_type": boarding_type})
	return issues


@frappe.whitelist(methods=["GET"])
@standardize_response
def get_boarding_rate_grid():
	"""Every enabled boarding row, as the grid the settings screen draws.

	`species` keeps catalogue order with the "All" fallback row last. An "All" row is the
	rate for any species with no row of its own - Bird, Rabbit, Horse today.
	"""
	require_doctype_permission("Pet Boarding Settings", "read")
	return _grid()


def _grid() -> dict:
	category = get_boarding_category()
	rows = _boarding_rows(category)
	species = sorted({cstr(r.animal_type) for r in rows if r.animal_type},
		key=lambda s: (s == FALLBACK_ANIMAL_TYPE, s))
	return {
		"category": category,
		"boarding_types": list(BOARDING_TYPES),
		"species": species,
		"fallback_animal_type": FALLBACK_ANIMAL_TYPE,
		"rates": [
			{
				"template": r.name,
				"service_name": r.service_name,
				"item_code": r.item_code,
				"animal_type": r.animal_type,
				"boarding_type": r.boarding_type,
				"rate": flt(r.default_price) or None,
				"modified": r.modified,
			}
			for r in rows
		],
		"issues": _grid_issues(rows),
	}


@frappe.whitelist(methods=["POST"])
@standardize_response
def set_boarding_rates(rates=None):
	"""Save changed grid cells: `rates=[{template, rate, modified?}]`. All or nothing.

	`modified` is the value the grid was read with; a row changed since is refused so two
	managers cannot silently overwrite each other.
	"""
	require_doctype_permission("Pet Boarding Settings", "write")
	rates = frappe.parse_json(rates) if isinstance(rates, str) else rates
	if not isinstance(rates, list) or not rates:
		frappe.throw(_("Send at least one rate to save."))

	allowed = {r.name: r for r in _boarding_rows(get_boarding_category())}
	changes = []
	seen = set()
	# Validate every entry before writing any, so a bad row cannot leave half the grid saved.
	for entry in rates:
		if not isinstance(entry, dict):
			frappe.throw(_("Each rate must be an object with a template and a rate."))
		template = cstr(entry.get("template")).strip()
		row = allowed.get(template)
		if not row:
			frappe.throw(_("{0} is not an enabled boarding rate.").format(frappe.bold(template or _("(blank)"))))
		if template in seen:
			frappe.throw(_("{0} was sent twice.").format(frappe.bold(template)))
		seen.add(template)
		rate = entry.get("rate")
		if isinstance(rate, bool) or not isinstance(rate, (int, float, str)) or flt(rate) <= 0:
			frappe.throw(_("The rate for {0} must be a positive number.").format(frappe.bold(row.service_name)))
		if entry.get("modified") and get_datetime(entry["modified"]) != get_datetime(row.modified):
			frappe.throw(_("{0} was changed by someone else. Reload the rates and try again.").format(
				frappe.bold(row.service_name)))
		changes.append((template, flt(rate)))

	begin_action()
	try:
		for template, rate in changes:
			doc = frappe.get_doc(TEMPLATE_DOCTYPE, template)
			if flt(doc.default_price) == rate:
				continue
			doc.default_price = rate
			# The gate above is the settings permission; a boarding manager need not also
			# hold write on the whole care-service catalogue.
			doc.flags.ignore_permissions = True
			doc.save()
	except Exception:
		rollback_action()
		raise
	return _grid()
