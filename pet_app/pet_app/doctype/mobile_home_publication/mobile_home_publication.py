from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


SCHEMA_VERSION = 2


class MobileHomePublication(Document):
	def validate(self):
		self.schema_version = SCHEMA_VERSION
		self.version = max(1, cint(self.version))
		self.revision = max(0, cint(self.revision))
		if not self.checksum:
			frappe.throw(_("Checksum is required."))
		self.snapshot_json = _normalize_json_text(self.snapshot_json)

	def on_update_after_submit(self):
		frappe.throw(_("Mobile Home Publications are immutable."))


def _normalize_json_text(value) -> str:
	if not value:
		frappe.throw(_("Snapshot JSON is required."))
	if isinstance(value, str):
		try:
			parsed = json.loads(value)
		except json.JSONDecodeError:
			frappe.throw(_("Snapshot JSON must be valid JSON."))
	else:
		parsed = value
	if not isinstance(parsed, dict):
		frappe.throw(_("Snapshot JSON must be a JSON object."))
	return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
