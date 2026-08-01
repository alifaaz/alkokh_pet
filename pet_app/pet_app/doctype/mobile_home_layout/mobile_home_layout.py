from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


LAYOUT_NAME = "mobile-home"
SCHEMA_VERSION = 2


class MobileHomeLayout(Document):
	def autoname(self):
		self.name = LAYOUT_NAME

	def validate(self):
		self.schema_version = SCHEMA_VERSION
		self.revision = max(0, cint(self.revision))
		self.draft_json = _normalize_json_text(self.draft_json)


def _normalize_json_text(value) -> str:
	if not value:
		value = {"schema_version": SCHEMA_VERSION, "filters": [], "blocks": []}
	if isinstance(value, str):
		try:
			parsed = json.loads(value)
		except json.JSONDecodeError:
			frappe.throw(_("Draft JSON must be valid JSON."))
	else:
		parsed = value
	if not isinstance(parsed, dict):
		frappe.throw(_("Draft JSON must be a JSON object."))
	return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
