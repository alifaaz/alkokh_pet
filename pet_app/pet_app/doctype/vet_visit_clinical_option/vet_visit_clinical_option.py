from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr

from pet_app.utils.clinical_options import get_option, option_payload, options_for_category


class VetVisitClinicalOption(Document):
	def load_from_db(self):
		option = get_option(self.name)
		if not option:
			raise frappe.DoesNotExistError
		super(Document, self).__init__(option_payload(option))

	@staticmethod
	def get_list(args=None, **kwargs):
		if isinstance(args, dict):
			kwargs = {**args, **kwargs}
		filters = kwargs.get("filters") or {}
		or_filters = kwargs.get("or_filters") or []
		txt = _text_filter(or_filters) or cstr(kwargs.get("txt") or "")
		txt = txt.lower()
		start = cint(kwargs.get("start") if kwargs.get("start") is not None else kwargs.get("limit_start") or 0)
		page_length = cint(
			kwargs.get("page_length") if kwargs.get("page_length") is not None else kwargs.get("limit_page_length") or 0
		)
		category = _filter_value(filters, "category")
		rows = []
		for option in options_for_category(category):
			item = option_payload(option)
			if txt and not any(txt in cstr(item.get(field)).lower() for field in ("code", "label_en", "label_ar")):
				continue
			item["_relevance"] = 0
			rows.append(frappe._dict(item))
		if page_length:
			rows = rows[start : start + page_length]
		else:
			rows = rows[start:]
		if kwargs.get("as_list"):
			fields = [_field_key(field) for field in kwargs.get("fields") or ["name", "label_en", "_relevance"]]
			return [[row.get(field) for field in fields] for row in rows]
		return rows

	@staticmethod
	def get_count(args=None, **kwargs):
		return len(VetVisitClinicalOption.get_list(args=args, **kwargs))

	@staticmethod
	def get_stats(args=None, **kwargs):
		return {}

	def db_insert(self, *args, **kwargs):
		frappe.throw(_("Vet Visit Clinical Options are fixed in code and cannot be created from Desk."))

	def db_update(self, *args, **kwargs):
		frappe.throw(_("Vet Visit Clinical Options are fixed in code and cannot be edited from Desk."))

	def delete(self):
		frappe.throw(_("Vet Visit Clinical Options are fixed in code and cannot be deleted from Desk."))


def _filter_value(filters, fieldname: str) -> str | None:
	if isinstance(filters, dict):
		value = filters.get(fieldname)
		if isinstance(value, (list, tuple)):
			return value[-1]
		return value
	for row in filters or []:
		if isinstance(row, (list, tuple)) and len(row) >= 3 and row[-3] == fieldname:
			return row[-1]
	return None



def _field_key(field) -> str:
	if isinstance(field, dict):
		return cstr(field.get("as") or field.get("fieldname") or "").strip()
	value = cstr(field).strip()
	lower_value = value.lower()
	if " as " in lower_value:
		return value[lower_value.rindex(" as ") + 4 :].strip().strip("`")
	if "." in value:
		value = value.rsplit(".", 1)[-1]
	return value.strip().strip("`")


def _text_filter(filters) -> str | None:
	for row in filters or []:
		if not isinstance(row, (list, tuple)) or len(row) < 4:
			continue
		if row[-3] not in {"name", "label_en", "label_ar", "code"}:
			continue
		if cstr(row[-2]).lower() != "like":
			continue
		return cstr(row[-1]).replace("%", "")
	return None
