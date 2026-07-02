from frappe.model.document import Document
import frappe
from frappe import _
from frappe.utils import cstr


class MobileRecentSearch(Document):
	def validate(self):
		self.query = " ".join(cstr(self.query).strip().split())
		if not self.guardian or not self.query:
			return
		existing = frappe.db.get_value(
			"Mobile Recent Search",
			{
				"guardian": self.guardian,
				"query": self.query,
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if existing:
			frappe.throw(_("This search query already exists for the Guardian."))
