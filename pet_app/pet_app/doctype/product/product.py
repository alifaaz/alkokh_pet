# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from pet_app.pet_app.doctype.product_category.product_category import apply_product_category_to_product


class Product(Document):
	def validate(self):
		apply_product_category_to_product(self, ignore_permissions=True)
