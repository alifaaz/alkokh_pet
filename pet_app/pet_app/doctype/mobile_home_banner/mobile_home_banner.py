from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, get_datetime


class MobileHomeBanner(Document):
	def validate(self):
		self.banner_id = cstr(self.banner_id or self.name).strip()
		self.block_type = cstr(self.block_type or "banner_carousel").strip()
		self.block_id = cstr(self.block_id).strip() or (
			"hero-banners" if self.block_type == "banner_carousel" else self.banner_id
		)
		self.title_en = cstr(self.title_en or self.title).strip()
		self.subtitle_en = cstr(self.subtitle_en or self.subtitle).strip()
		self.button_title_en = cstr(self.button_title_en or self.button_title or _("Shop Now")).strip()
		self.title = self.title_en
		self.subtitle = self.subtitle_en
		self.button_title = self.button_title_en or _("Shop Now")
		self.title_ar = cstr(self.title_ar).strip()
		self.subtitle_ar = cstr(self.subtitle_ar).strip()
		self.button_title_ar = cstr(self.button_title_ar).strip()
		self.gradient_start = _hex_or_default(self.gradient_start, "#FF9A56")
		self.gradient_end = _hex_or_default(self.gradient_end, "#FF5E62")
		self.display_order = cint(self.display_order) or 10
		self.action_type = cstr(self.action_type or "list").strip()
		self.action_value = cstr(self.action_value).strip()

		if self.starts_at and self.ends_at and get_datetime(self.ends_at) <= get_datetime(self.starts_at):
			frappe.throw(_("Ends At must be later than Starts At."))

		if not cint(self.enabled):
			return

		if self.block_type not in {"banner_carousel", "single_banner"}:
			frappe.throw(_("Block Type must be banner_carousel or single_banner."))
		if not self.image:
			frappe.throw(_("Image is required for enabled mobile banners."))
		if not self.action_type or not self.action_value:
			frappe.throw(_("Action Type and Action Value are required for enabled mobile banners."))
		if self.action_type not in {"product", "category", "brand", "list", "url"}:
			frappe.throw(_("Action Type must be product, category, brand, list, or url."))
		if self.action_type == "url" and not re.match(r"^https?://", self.action_value, flags=re.IGNORECASE):
			frappe.throw(_("URL banner actions require an absolute http(s) URL."))


def _hex_or_default(value, default: str) -> str:
	value = cstr(value).strip()
	return value if re.match(r"^#[0-9a-fA-F]{6}$", value) else default
