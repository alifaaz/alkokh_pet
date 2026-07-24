from __future__ import annotations

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from pet_app.notifications.designer import SECTION_FIELDS, parse_config_section
from pet_app.patches.p1_5_notification_engine_schema import code, ensure_doctype, link


def execute():
	ensure_rule_designer_schema()
	normalize_legacy_rule_sections()
	frappe.clear_cache()


def ensure_rule_designer_schema():
	ensure_doctype(
		"Pet App Notification Settings",
		{
			"issingle": 1,
			"fields": [link("whatsapp_full_access_role", "Role")],
		},
	)
	ensure_doctype(
		"Pet App WhatsApp Action Request",
		{
			"fields": [
				code("rule_snapshot_json"),
				code("validation_json"),
				code("resolved_recipient_json"),
			],
		},
	)
	create_custom_fields(
		{
			"ToDo": [
				{
					"fieldname": "pet_app_whatsapp_conversation",
					"fieldtype": "Link",
					"label": "WhatsApp Conversation",
					"options": "Pet App WhatsApp Conversation",
					"insert_after": "reference_name",
					"read_only": 1,
				},
				{
					"fieldname": "pet_app_whatsapp_action_request",
					"fieldtype": "Link",
					"label": "WhatsApp Action Request",
					"options": "Pet App WhatsApp Action Request",
					"insert_after": "pet_app_whatsapp_conversation",
					"read_only": 1,
				},
			]
		},
		update=True,
	)


def normalize_legacy_rule_sections():
	if not frappe.db.exists("DocType", "Pet App WhatsApp Action Rule"):
		return
	for row in frappe.get_all(
		"Pet App WhatsApp Action Rule",
		fields=["name", *[legacy_key for _canonical, legacy_key, _default in SECTION_FIELDS.values()]],
		ignore_permissions=True,
	):
		updates = {}
		for section, (_canonical_key, legacy_key, default) in SECTION_FIELDS.items():
			parsed, error = parse_config_section(row.get(legacy_key), default, section)
			if error:
				continue
			normalized = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"), default=str)
			if row.get(legacy_key) != normalized:
				updates[legacy_key] = normalized
		if updates:
			frappe.db.set_value(
				"Pet App WhatsApp Action Rule",
				row.name,
				updates,
				update_modified=False,
			)
