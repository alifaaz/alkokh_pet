from __future__ import annotations

import json
from pathlib import Path

import frappe


def execute():
	workspace_path = (
		Path(frappe.get_app_path("pet_app"))
		/ "pet_app"
		/ "workspace"
		/ "alkohk_administrator"
		/ "alkohk_administrator.json"
	)

	if not workspace_path.exists():
		return

	with workspace_path.open() as handle:
		payload = json.load(handle)

	name = payload["name"]
	if not frappe.db.exists("Workspace", name):
		frappe.get_doc(
			{
				"doctype": "Workspace",
				"name": name,
				"label": payload.get("label"),
				"title": payload.get("title"),
				"module": payload.get("module"),
				"for_user": payload.get("for_user"),
				"icon": payload.get("icon"),
				"sequence_id": payload.get("sequence_id") or 0,
				"public": payload.get("public") or 0,
				"is_hidden": payload.get("is_hidden") or 0,
				"hide_custom": payload.get("hide_custom") or 0,
				"parent_page": payload.get("parent_page") or "",
				"content": payload.get("content") or "[]",
			}
		).insert(ignore_permissions=True)

	frappe.db.set_value(
		"Workspace",
		name,
		{
			"label": payload.get("label"),
			"title": payload.get("title"),
			"module": payload.get("module"),
			"for_user": payload.get("for_user"),
			"icon": payload.get("icon"),
			"sequence_id": payload.get("sequence_id") or 0,
			"public": payload.get("public") or 0,
			"is_hidden": payload.get("is_hidden") or 0,
			"hide_custom": payload.get("hide_custom") or 0,
			"parent_page": payload.get("parent_page") or "",
			"content": payload.get("content") or "[]",
		},
	)

	_sync_children(name, "shortcuts", "Workspace Shortcut", payload.get("shortcuts") or [])
	_sync_children(name, "links", "Workspace Link", payload.get("links") or [])
	_sync_children(name, "charts", "Workspace Chart", payload.get("charts") or [])
	_sync_children(name, "number_cards", "Workspace Number Card", payload.get("number_cards") or [])
	_sync_children(name, "quick_lists", "Workspace Quick List", payload.get("quick_lists") or [])
	_sync_children(name, "custom_blocks", "Workspace Custom Block", payload.get("custom_blocks") or [])
	_sync_children(name, "roles", "Has Role", payload.get("roles") or [])

	frappe.db.commit()


def _sync_children(parent_name: str, parentfield: str, child_doctype: str, rows: list[dict]):
	frappe.db.delete(
		child_doctype,
		{
			"parent": parent_name,
			"parenttype": "Workspace",
			"parentfield": parentfield,
		},
	)

	for idx, row in enumerate(rows, start=1):
		if _references_missing_link(row):
			continue
		payload = {
			"doctype": child_doctype,
			"parent": parent_name,
			"parenttype": "Workspace",
			"parentfield": parentfield,
			"idx": idx,
		}
		payload.update(row)
		frappe.get_doc(payload).insert(ignore_permissions=True)


def _references_missing_link(row: dict) -> bool:
	link_to = row.get("link_to")
	link_type = row.get("link_type")

	if not link_to or row.get("type") == "Card Break":
		return False

	if link_type == "DocType":
		return not frappe.db.exists("DocType", link_to)

	if link_type == "Page":
		return not frappe.db.exists("Page", link_to)

	return False
