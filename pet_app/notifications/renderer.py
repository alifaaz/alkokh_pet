from __future__ import annotations

import json

import frappe
from frappe.utils import cstr

from pet_app.notifications.context import mask_sensitive_context


def _get_message_jenv():
	"""A message-body Jinja environment that renders missing variables as blanks.

	The shared Frappe environment uses ``DebugUndefined``, which raises on any
	attribute/item access against an undefined value (e.g. ``{{ pet.pet_name }}``
	when there is no related pet) and leaks raw ``{{ ... }}`` tokens into the
	message for a bare undefined. Neither is acceptable for a WhatsApp message
	sent to a guardian, so we render with a forgiving undefined instead.
	"""
	jenv = getattr(frappe.local, "pet_app_message_jenv", None)
	if jenv is not None:
		return jenv

	from jinja2 import ChainableUndefined

	class BlankUndefined(ChainableUndefined):
		"""Undefined that quietly renders as an empty string.

		``ChainableUndefined`` already makes ``pet.pet_name`` / ``pet['name']``
		safe when ``pet`` is missing; overriding ``__str__`` ensures the final
		render is a blank rather than a leaked token.
		"""

		__slots__ = ()

		def __str__(self):
			return ""

	base = frappe.get_jenv()
	jenv = base.overlay(undefined=BlankUndefined)
	frappe.local.pet_app_message_jenv = jenv
	return jenv


def render_message(body: str, context: dict | None = None) -> str:
	"""Render a message body, tolerating missing pet/guardian/etc. variables."""
	body = cstr(body)
	if not body:
		return ""
	if ".__" in body:
		frappe.throw(frappe._("Illegal template"))
	try:
		compiled = _get_message_jenv().from_string(body)
		return compiled.render(context or {})
	except Exception:
		frappe.log_error(
			title="WhatsApp template render failed",
			message=frappe.get_traceback(),
		)
		# Never let a broken template crash the send; drop unresolved tokens.
		return _strip_jinja(body)


def _strip_jinja(body: str) -> str:
	import re

	body = re.sub(r"{%.*?%}", "", body, flags=re.DOTALL)
	body = re.sub(r"{{.*?}}", "", body, flags=re.DOTALL)
	return body.strip()


def render_preview(template_doc, context: dict | None = None, *, mask_sensitive: bool = True) -> str:
	body = cstr(template_doc.get("body_preview") or template_doc.get("template_name") or template_doc.get("template_key"))
	render_context = mask_sensitive_context(context or {}) if mask_sensitive else (context or {})
	return render_message(body, render_context)


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

