from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


class MobileHomeBanner(Document):
	def validate(self):
		self.banner_id = cstr(self.banner_id or self.name).strip()
		self.block_type = cstr(self.block_type or "banner_carousel").strip()
		self.block_id = cstr(self.block_id).strip() or (
			"hero-banners" if self.block_type == "banner_carousel" else self.banner_id
		)
		self.button_title = cstr(self.button_title or _("Shop Now")).strip()
		self.gradient_start = _hex_or_default(self.gradient_start, "#FF9A56")
		self.gradient_end = _hex_or_default(self.gradient_end, "#FF5E62")
		self.display_order = cint(self.display_order) or 10

		if not cint(self.enabled):
			return

		if self.block_type not in {"banner_carousel", "single_banner"}:
			frappe.throw(_("Block Type must be banner_carousel or single_banner."))
		if not self.image:
			frappe.throw(_("Image is required for enabled mobile banners."))
		if not self.action_type or not self.action_value:
			frappe.throw(_("Action Type and Action Value are required for enabled mobile banners."))


def _hex_or_default(value, default: str) -> str:
	value = cstr(value).strip()
	return value if re.match(r"^#[0-9a-fA-F]{6}$", value) else default
