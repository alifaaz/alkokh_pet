# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document


class PetAppSidebarConfig(Document):
	def validate(self):
		self.config = _normalize_config(self.config)


def _normalize_config(config) -> str:
	if not config:
		return "{}"

	if isinstance(config, str):
		try:
			parsed = json.loads(config)
		except json.JSONDecodeError:
			frappe.throw(_("Sidebar config must be valid JSON."))
	else:
		parsed = config

	if hasattr(parsed, "as_dict"):
		parsed = parsed.as_dict()

	if not isinstance(parsed, dict):
		frappe.throw(_("Sidebar config must be a JSON object."))

	return json.dumps(parsed, indent=2, sort_keys=True)
