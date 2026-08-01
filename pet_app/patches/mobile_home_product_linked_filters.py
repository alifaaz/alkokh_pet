from __future__ import annotations

import frappe


def execute():
	from pet_app.api.mobile import home_builder

	if not frappe.db.exists("DocType", home_builder.LAYOUT_DOCTYPE):
		return
	if not frappe.db.exists(home_builder.LAYOUT_DOCTYPE, home_builder.LAYOUT_NAME):
		return

	draft_json = frappe.db.get_value(home_builder.LAYOUT_DOCTYPE, home_builder.LAYOUT_NAME, "draft_json")
	config = home_builder._load_json(draft_json, home_builder._empty_config())
	home_builder.import_legacy_filters_from_config(config)
	normalized = home_builder.normalize_config(config)
	frappe.db.set_value(
		home_builder.LAYOUT_DOCTYPE,
		home_builder.LAYOUT_NAME,
		"draft_json",
		home_builder._json_pretty(normalized),
		update_modified=False,
	)
