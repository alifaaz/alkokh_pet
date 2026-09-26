# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import cstr

from pet_app.pet_app.doctype.product_category.product_category import apply_product_category_to_product


class Product(Document):
	def validate(self):
		self._normalize_barcode()
		apply_product_category_to_product(self)

	def _normalize_barcode(self):
		"""Trim the scanned code, and store "no barcode" as NULL rather than ''.

		Here rather than in the API endpoints so every write path is covered - the
		storefront endpoints, the desk form, the seed patches and any import all
		reach this. A trim living in create_product/update_product would leave the
		other four able to insert a value the lookup can never match.

		Both halves are load-bearing under the UNIQUE index on this column:

		- Trimming, because a scanner appends a terminator (CR/LF, sometimes a tab)
		  and an operator pasting a code brings spaces with it. "5941 " and "5941"
		  are one barcode to the person holding the gun but two distinct keys to the
		  index, so an untrimmed write both escapes the uniqueness rule and stores a
		  code that an exact-match lookup will miss.
		- Nulling the blank, because MariaDB counts NULL as always-distinct but
		  treats '' as an ordinary value: the second product saved with an empty
		  barcode would collide with the first. Frappe already does this much for
		  unique fields in `BaseDocument.get_valid_dict`, but only for a value that
		  is ALREADY blank - it does not trim, so "   " reaches it as truthy on some
		  paths and "ABC " is stored with its space. Doing it here makes the stored
		  value and the validated document agree.
		"""
		barcode = cstr(self.barcode).strip()
		self.barcode = barcode or None
