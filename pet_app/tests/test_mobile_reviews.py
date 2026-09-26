"""Storefront review endpoints, with attention to the empty state.

``list_product_reviews`` is ``allow_guest=True``. After p1_19 deleted the eight seeded
Product ratings, and with no real customer review ever having been written on this site,
zero rows is the *normal* state of this endpoint rather than an edge case - so the empty
path is the one that most needs a test. An empty state on a public endpoint must be a
200 carrying an empty list, never a 500 and never an error envelope.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api.mobile import reviews


class TestMobileReviews(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.product = frappe.db.get_value("Product", {"status": "Active"}, "name")
		if not self.product:
			self.skipTest("No Active Product on this site.")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_empty_review_summary_does_not_divide_by_zero(self):
		summary = reviews.review_summary(self.product)
		self.assertEqual(summary["count"], 0)
		self.assertEqual(summary["average"], 0)

	def test_guest_listing_with_no_reviews_returns_200_and_an_empty_list(self):
		frappe.set_user("Guest")
		response = reviews.list_product_reviews(product=self.product)

		self.assertTrue(response.get("ok"), f"expected an ok() envelope, got {response}")
		self.assertNotIn("error", response)
		# ok() leaves http_status_code unset, which Frappe serves as 200. error() is the
		# only thing in this module that sets it.
		self.assertIsNone(frappe.local.response.get("http_status_code"))

		data = response["data"]
		self.assertEqual(data["items"], [])
		self.assertFalse(data["hasMore"])
		self.assertIsNone(data["nextCursor"])
		self.assertEqual(data["summary"], {"count": 0, "average": 0})

	def test_listing_asks_for_customer_rows_not_merely_product_rows(self):
		"""The type is the filter that keeps internal notes off the storefront.

		reference_doctype says which thing was rated; it says nothing about who wrote
		the row. An Internal rating of a Product is a legitimate thing for a staff
		member to create, and this endpoint renders ``notes`` verbatim to the public.
		"""
		self.assertEqual(
			reviews._customer_review_filters(self.product),
			{
				"reference_doctype": "Product",
				"reference_name": self.product,
				"rating_type": "Customer",
			},
		)

	def test_unknown_product_is_a_clean_404(self):
		frappe.set_user("Guest")
		response = reviews.list_product_reviews(product="NO-SUCH-PRODUCT")

		self.assertIn("error", response)
		self.assertEqual(response["error"]["code"], "review.not_found")
		self.assertEqual(frappe.local.response.get("http_status_code"), 404)

	def test_no_seeded_product_ratings_survive(self):
		"""p1_19 deleted them; nothing should re-create them untyped."""
		untyped = frappe.db.sql(
			"""
			select name from `tabRating`
			where reference_doctype = 'Product' and ifnull(rating_type, '') = ''
			""",
			as_dict=True,
		)
		self.assertEqual([row.name for row in untyped], [])
