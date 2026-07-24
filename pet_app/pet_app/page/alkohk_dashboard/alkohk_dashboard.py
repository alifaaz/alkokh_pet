# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from pet_app.api.dashboard import _require_analytics_access
from pet_app.api.response import standardize_response


HERO_METRICS = (
	{
		"label": "Active Pets",
		"doctype": "Pet",
		"filters": {"pet_status": "Approved"},
		"accent": "sun",
	},
	{
		"label": "Guardians",
		"doctype": "Guardian",
		"accent": "lagoon",
	},
	{
		"label": "Case Sheets",
		"doctype": "Vet Case Sheet",
		"accent": "leaf",
	},
	{
		"label": "Visits",
		"doctype": "Vet Visit",
		"accent": "berry",
	},
)


SECTION_CONFIG = (
	{
		"title": "Command Deck",
		"subtitle": "The fastest routes into the pet and veterinary workflows.",
		"tone": "sun",
		"items": (
			{
				"label": "Visual Dashboard",
				"kind": "Page",
				"target": "alkohk_dashboard",
				"description": "Open the animated control room with live counts and quick navigation.",
				"icon": "es-line-dashboard",
				"accent": "sun",
			},
			{
				"label": "Workspace",
				"kind": "Workspace",
				"target": "Alkohk-Administrator",
				"description": "Jump back to the mapped Desk workspace for shortcuts and grouped links.",
				"icon": "es-line-grid",
				"accent": "lagoon",
			},
		),
	},
	{
		"title": "Veterinary Operations",
		"subtitle": "Intake, consultation, practitioners, and healthcare records.",
		"tone": "leaf",
		"items": (
			{
				"label": "Vet Case Sheet",
				"kind": "DocType",
				"target": "Vet Case Sheet",
				"description": "Structured intake and triage form used before the practitioner encounter.",
				"icon": "es-line-file",
				"accent": "leaf",
			},
			{
				"label": "Vet Visit",
				"kind": "DocType",
				"target": "Vet Visit",
				"description": "Practitioner encounter with assessment, plan, and billing workflow.",
				"icon": "es-line-clipboard",
				"accent": "berry",
			},
			{
				"label": "Healthcare Practitioner",
				"kind": "DocType",
				"target": "Healthcare Practitioner",
				"description": "Practitioner master linked to clinical visit fields and healthcare flows.",
				"icon": "es-line-user",
				"accent": "lagoon",
			},
		),
	},
	{
		"title": "Pet Registry",
		"subtitle": "Core records connecting pets, guardians, requests, and medical profiles.",
		"tone": "lagoon",
		"items": (
			{
				"label": "Pet",
				"kind": "DocType",
				"target": "Pet",
				"description": "The primary pet master used across mobile, healthcare, and clinic operations.",
				"icon": "es-line-heart",
				"accent": "lagoon",
			},
			{
				"label": "Guardian",
				"kind": "DocType",
				"target": "Guardian",
				"description": "Owner profile linked to Customer and User records.",
				"icon": "es-line-users",
				"accent": "berry",
			},
			{
				"label": "PetGuardian",
				"kind": "DocType",
				"target": "PetGuardian",
				"description": "Relationship table connecting pets with one or more guardians.",
				"icon": "es-line-link",
				"accent": "sun",
			},
			{
				"label": "PetAddRequest",
				"kind": "DocType",
				"target": "PetAddRequest",
				"description": "Approval flow for new pet registrations and guardian ownership linkage.",
				"icon": "es-line-check",
				"accent": "leaf",
			},
			{
				"label": "Pet Medical Profile",
				"kind": "DocType",
				"target": "Pet Medical Profile",
				"description": "Pet clinical summary with alerts, notes, and latest visit snapshot.",
				"icon": "es-line-id-card",
				"accent": "lagoon",
			},
		),
	},
	{
		"title": "Services and Care Catalog",
		"subtitle": "Service masters, schedules, boarding, and pricing sources.",
		"tone": "sun",
		"items": (
			{
				"label": "CareService",
				"kind": "DocType",
				"target": "CareService",
				"description": "Master source for service pricing, linked item codes, and billable procedures.",
				"icon": "es-line-briefcase",
				"accent": "sun",
			},
			{
				"label": "CategoryCareServices",
				"kind": "DocType",
				"target": "CategoryCareServices",
				"description": "Service category structure for grouping procedures, labs, and recurring care.",
				"icon": "es-line-layers",
				"accent": "lagoon",
			},
			{
				"label": "PetCareService",
				"kind": "DocType",
				"target": "PetCareService",
				"description": "Pet-specific scheduled or due care service records.",
				"icon": "es-line-calendar",
				"accent": "leaf",
			},
			{
				"label": "Pet Boarding",
				"kind": "DocType",
				"target": "Pet Boarding",
				"description": "Boarding operations with day-based cost calculation.",
				"icon": "es-line-home",
				"accent": "berry",
			},
			{
				"label": "custom services",
				"kind": "DocType",
				"target": "custom services",
				"description": "Custom service rows available in the current Pet App module data model.",
				"icon": "es-line-plus",
				"accent": "sun",
			},
		),
	},
	{
		"title": "Catalog and Sales",
		"subtitle": "Products, customers, invoicing, and selling controls.",
		"tone": "berry",
		"items": (
			{
				"label": "Product",
				"kind": "DocType",
				"target": "Product",
				"description": "Custom product master synchronized with ERPNext Item and Item Price.",
				"icon": "es-line-tag",
				"accent": "berry",
			},
			{
				"label": "Customer",
				"kind": "DocType",
				"target": "Customer",
				"description": "Guardian-linked ERPNext customer used for orders, visits, and invoices.",
				"icon": "es-line-user",
				"accent": "lagoon",
			},
			{
				"label": "Sales Order",
				"kind": "DocType",
				"target": "Sales Order",
				"description": "Order flow used by the current commerce APIs and completion workflow.",
				"icon": "es-line-shopping-cart",
				"accent": "sun",
			},
			{
				"label": "Sales Invoice",
				"kind": "DocType",
				"target": "Sales Invoice",
				"description": "Billing target for order completion, encounter billing, and the new vet visit invoice flow.",
				"icon": "es-line-receipt",
				"accent": "leaf",
			},
			{
				"label": "Price List",
				"kind": "DocType",
				"target": "Price List",
				"description": "ERPNext selling price lists used by items and service-linked billing.",
				"icon": "es-line-price-tag",
				"accent": "berry",
			},
		),
	},
	{
		"title": "Settings and Control",
		"subtitle": "Operational settings, user administration, and workspace management.",
		"tone": "lagoon",
		"items": (
			{
				"label": "WhatsApp Inbox",
				"kind": "Page",
				"target": "whatsapp-inbox",
				"description": "Guardian conversations, attachments, response actions, and review queue.",
				"icon": "es-line-message-circle",
				"accent": "leaf",
			},
			{
				"label": "WhatsApp Action Rules",
				"kind": "DocType",
				"target": "Pet App WhatsApp Action Rule",
				"description": "Dynamic triggers, reply choices, safe executors, and approval policy.",
				"icon": "es-line-git-branch",
				"accent": "berry",
			},
			{
				"label": "Healthcare Settings",
				"kind": "DocType",
				"target": "Healthcare Settings",
				"description": "Healthcare module configuration for practitioner and encounter behavior.",
				"icon": "es-line-settings",
				"accent": "lagoon",
			},
			{
				"label": "Selling Settings",
				"kind": "DocType",
				"target": "Selling Settings",
				"description": "Sales defaults that affect selling and invoice behavior.",
				"icon": "es-line-tool",
				"accent": "sun",
			},
			{
				"label": "Stock Settings",
				"kind": "DocType",
				"target": "Stock Settings",
				"description": "Warehouse and stock defaults used by product and order flows.",
				"icon": "es-line-archive",
				"accent": "leaf",
			},
			{
				"label": "System Settings",
				"kind": "DocType",
				"target": "System Settings",
				"description": "System-wide defaults and low-level Frappe configuration.",
				"icon": "es-line-sliders",
				"accent": "berry",
			},
			{
				"label": "User",
				"kind": "DocType",
				"target": "User",
				"description": "Desk and mobile identities managed inside the platform.",
				"icon": "es-line-id-card",
				"accent": "lagoon",
			},
			{
				"label": "Role Profile",
				"kind": "DocType",
				"target": "Role Profile",
				"description": "Role packaging used by access control and user assignment workflows.",
				"icon": "es-line-lock",
				"accent": "sun",
			},
			{
				"label": "Workspace",
				"kind": "DocType",
				"target": "Workspace",
				"description": "Desk workspace definitions, including this Alkohk control surface.",
				"icon": "es-line-grid",
				"accent": "berry",
			},
		),
	},
)


EMBEDDED_MODELS = (
	"Product Variant",
	"Vet Visit Medication Item",
)


@frappe.whitelist()
@standardize_response
def get_dashboard_payload() -> dict:
	_require_analytics_access()

	return {
		"title": _("Alkohk"),
		"tagline": _("A mapped control room for pet operations, veterinary workflows, pricing, and settings."),
		"hero_metrics": [_build_metric(metric) for metric in HERO_METRICS if _can_read(metric["doctype"])],
		"sections": [
			_build_section(section)
			for section in SECTION_CONFIG
			if _section_has_visible_items(section)
		],
		"embedded_models": [
			{
				"label": doctype,
				"route": _build_route("DocType", doctype),
			}
			for doctype in EMBEDDED_MODELS
			if _doctype_exists(doctype)
		],
	}


def _build_metric(metric_config: dict) -> dict:
	count = 0
	if _doctype_exists(metric_config["doctype"]):
		count = frappe.db.count(metric_config["doctype"], filters=metric_config.get("filters") or {})

	return {
		"label": _(metric_config["label"]),
		"value": count,
		"accent": metric_config["accent"],
		"route": _build_route("DocType", metric_config["doctype"]),
	}


def _build_section(section_config: dict) -> dict:
	return {
		"title": _(section_config["title"]),
		"subtitle": _(section_config["subtitle"]),
		"tone": section_config["tone"],
		"items": [
			_build_item(item)
			for item in section_config["items"]
			if _item_is_visible(item)
		],
	}


def _build_item(item_config: dict) -> dict:
	kind = item_config["kind"]
	target = item_config["target"]
	count = None

	if kind == "DocType" and _doctype_exists(target):
		meta = frappe.get_meta(target)
		if not meta.issingle and not meta.istable:
			count = frappe.db.count(target)

	return {
		"label": _(item_config["label"]),
		"kind": kind,
		"target": target,
		"description": _(item_config["description"]),
		"icon": item_config["icon"],
		"accent": item_config["accent"],
		"count": count,
		"route": _build_route(kind, target),
	}


def _section_has_visible_items(section_config: dict) -> bool:
	return any(_item_is_visible(item) for item in section_config["items"])


def _item_is_visible(item_config: dict) -> bool:
	kind = item_config["kind"]
	target = item_config["target"]

	if kind == "DocType":
		return _doctype_exists(target) and _can_read(target)
	if kind == "Page":
		return frappe.db.exists("Page", target)
	if kind == "Workspace":
		if not frappe.db.exists("Workspace", target):
			return False
		workspace = frappe.db.get_value("Workspace", target, ["public", "for_user"], as_dict=True)
		if not workspace:
			return False
		return bool(workspace.public or not workspace.for_user or workspace.for_user == frappe.session.user)
	return False


def _build_route(kind: str, target: str) -> list[str]:
	if kind == "DocType":
		if not _doctype_exists(target):
			return []
		meta = frappe.get_meta(target)
		if meta.issingle:
			return ["Form", target, target]
		return ["List", target, "List"]

	if kind == "Page":
		return [target]

	if kind == "Workspace":
		workspace = frappe.db.get_value("Workspace", target, ["public", "for_user"], as_dict=True)
		if not workspace:
			return []
		if workspace.public:
			return [frappe.scrub(target).replace("_", "-")]
		base_name = target
		if workspace.for_user and target.endswith(f"-{workspace.for_user}"):
			base_name = target[: -(len(workspace.for_user) + 1)]
		return ["private", frappe.scrub(base_name).replace("_", "-")]

	return []


def _doctype_exists(doctype: str) -> bool:
	return bool(frappe.db.exists("DocType", doctype))


def _can_read(doctype: str) -> bool:
	try:
		return bool(frappe.has_permission(doctype, ptype="read"))
	except frappe.PermissionError:
		return False
