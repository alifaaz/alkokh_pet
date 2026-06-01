from __future__ import annotations

import json

import frappe
from frappe.utils import cstr

from pet_app.notifications.context import mask_sensitive_context


def render_preview(template_doc, context: dict | None = None, *, mask_sensitive: bool = True) -> str:
	body = cstr(template_doc.get("body_preview") or template_doc.get("template_name") or template_doc.get("template_key"))
	render_context = mask_sensitive_context(context or {}) if mask_sensitive else (context or {})
	return frappe.render_template(body, render_context)


def template_components(template_doc, context: dict | None = None, *, mask_sensitive: bool = False) -> list:
	if template_doc.get("components_json"):
		components = json.loads(template_doc.components_json)
	else:
		components = [{"type": "body", "parameters": _body_parameters(context or {}, mask_sensitive=mask_sensitive)}]
	return components


def _body_parameters(context: dict, *, mask_sensitive: bool) -> list[dict]:
	data = mask_sensitive_context(context) if mask_sensitive else context
	parameters = []
	for key in sorted(data):
		value = data[key]
		if isinstance(value, (dict, list)):
			value = json.dumps(value, default=str)
		parameters.append({"type": "text", "parameter_name": key, "text": cstr(value)})
	return parameters

