from frappe.model.document import Document
import frappe
from frappe import _


class MobileFavorite(Document):
	def validate(self):
		if not self.guardian or not self.product:
			return
		existing = frappe.db.get_value(
			"Mobile Favorite",
			{
				"guardian": self.guardian,
				"product": self.product,
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if existing:
			frappe.throw(_("This product is already in the Guardian's favorites."))
