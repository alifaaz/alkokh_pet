"""Give Item Group the three fields the storefront needs, so it can be the only tree.

Item Group already carried everything the CLINIC needs - stock, accounting, the
pharmacological taxonomy - but nothing about how a group should present itself in the
shop. That metadata lived on Product Category, which is exactly why a second tree had to
be maintained by hand and drifted until the mobile catalogue could see 3 of 2163 items.

Two of the four pieces already existed: `image` (standard) and `arabic_name` (custom).
These are the missing three. With them, Item Group carries the whole projection and
pet_app.utils.store_category_mirror can regenerate Product Category from it.

OWNERSHIP: created here via create_custom_fields, and deliberately NOT added to the
fixture allowlist in hooks.py. tests/test_fixture_allowlist.py asserts one owner per
field - a field may be patch-created or fixture-shipped, never both, or sync_fixtures
overwrites the patch definition on every migrate. Patches run on fresh installs too, so
nothing is lost by choosing the patch.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

SHOW_IN_SHOP_FIELD = "custom_show_in_shop"
DISPLAY_ORDER_FIELD = "custom_shop_display_order"
DESCRIPTION_FIELD = "custom_shop_description"

# Groups that must never face a customer under their current name, whatever else is true
# of them. `Uncategorised` holds 235 items and is a migration staging bucket, not a
# category anybody should be asked to browse.
HIDE_ON_CREATE = ("Uncategorised",)

FIELDS = {
	"Item Group": [
		{
			"fieldname": SHOW_IN_SHOP_FIELD,
			"label": "Show in Shop",
			"fieldtype": "Check",
			"default": "1",
			"insert_after": "arabic_name",
			# Defaults to 1 because membership of the Store subtree is ALREADY the gate -
			# the mirror is lft/rgt-bounded, so a clinical group is out of the shop by
			# position and never needs a flag. This field is therefore a per-node HIDE
			# switch for groups that are in the shop's tree but should not be browsable,
			# not an opt-in that would start every storefront empty.
			"description": (
				"Uncheck to hide this group, and everything under it, from the mobile storefront. "
				"Has no effect outside the Store tree - clinical groups are already excluded by position."
			),
			"module": "Pet App",
			"translatable": 0,
		},
		{
			"fieldname": DISPLAY_ORDER_FIELD,
			"label": "Shop Display Order",
			"fieldtype": "Int",
			"default": "0",
			"insert_after": SHOW_IN_SHOP_FIELD,
			"description": "Lower sorts first in the storefront. Ties fall back to name.",
			"module": "Pet App",
			"translatable": 0,
		},
		{
			"fieldname": DESCRIPTION_FIELD,
			"label": "Shop Description",
			"fieldtype": "Text Editor",
			"insert_after": DISPLAY_ORDER_FIELD,
			# Text Editor to match Product Category.description exactly, so the value
			# round-trips through the mirror without a format conversion either way.
			"description": "Customer-facing blurb for this category. Mirrored to Product Category.",
			"module": "Pet App",
			"translatable": 1,
		},
	]
}


def execute():
	first_run = not frappe.db.exists(
		"Custom Field", {"dt": "Item Group", "fieldname": SHOW_IN_SHOP_FIELD}
	)

	create_custom_fields(FIELDS, ignore_validate=True)

	if not first_run:
		# Re-running must not un-hide a group somebody deliberately hid.
		return

	# The ALTER carries the column default, but that governs future INSERTs only; rows
	# that already existed are filled explicitly so no group silently vanishes from the
	# storefront on the run that adds the field.
	frappe.db.sql(
		"UPDATE `tabItem Group` SET `{0}` = 1 WHERE `{0}` IS NULL".format(SHOW_IN_SHOP_FIELD)
	)

	for group in HIDE_ON_CREATE:
		if frappe.db.exists("Item Group", group):
			frappe.db.set_value("Item Group", group, SHOW_IN_SHOP_FIELD, 0, update_modified=False)

	frappe.db.commit()
