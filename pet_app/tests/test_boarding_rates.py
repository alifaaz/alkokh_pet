# -*- coding: utf-8 -*-
"""The boarding rate grid and the per-pet daily rate the check-out estimate displays.

The estimate used to read one rate per boarding type from the deprecated settings items,
which are the cat items, so every dog was quoted at the cat rate (BRD-00237: 60,000 shown,
100,000 billed). And a rate edited on the settings screen went to an Item Price the invoice
never reads. Both are asserted against the catalogue the invoice actually bills from.

Nothing here commits; IntegrationTestCase rolls each test back.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import flt

from pet_app.api.healthcare import boarding as api
from pet_app.api.healthcare.boarding_rates import get_boarding_rate_grid, set_boarding_rates
from pet_app.tests.test_boarding_payments import BoardingPaymentBase, _data, _ok
from pet_app.utils.boarding_pricing import resolve_boarding_rate
from pet_app.utils.price_list import get_veterinary_selling_price_list


class TestBoardingRates(BoardingPaymentBase):
	def _grid(self) -> dict:
		res = get_boarding_rate_grid()
		self.assertTrue(_ok(res), msg=res)
		return _data(res)

	def _cell(self, grid, animal_type, boarding_type):
		cells = [r for r in grid["rates"] if r["animal_type"] == animal_type and r["boarding_type"] == boarding_type]
		if not cells:
			self.skipTest(f"no {animal_type} {boarding_type} boarding row on this site")
		return cells[0]

	def test_grid_is_the_catalogue_the_invoice_bills_from(self):
		grid = self._grid()
		dog = self._cell(grid, "Dog", "Travel")
		self.assertEqual(dog["rate"], resolve_boarding_rate(self.pet, "Travel").rate)
		self.assertEqual(dog["item_code"], resolve_boarding_rate(self.pet, "Travel").item_code)
		self.assertEqual(grid["boarding_types"], ["Travel", "Treatment"])
		self.assertEqual(grid["issues"], [])

	def test_saved_rate_is_the_rate_billed(self):
		cell = self._cell(self._grid(), "Dog", "Travel")
		new_rate = flt(cell["rate"]) + 1000
		res = set_boarding_rates(rates=[{"template": cell["template"], "rate": new_rate, "modified": cell["modified"]}])
		self.assertTrue(_ok(res), msg=res)
		self.assertEqual(resolve_boarding_rate(self.pet, "Travel").rate, new_rate)
		# The template's own on_update keeps the Standard Selling Item Price equal.
		self.assertEqual(flt(frappe.db.get_value("Item Price", {
			"item_code": cell["item_code"], "price_list": get_veterinary_selling_price_list(), "selling": 1,
		}, "price_list_rate")), new_rate)

	def test_bad_requests_write_nothing(self):
		grid = self._grid()
		dog = self._cell(grid, "Dog", "Travel")
		cat = self._cell(grid, "Cat", "Travel")
		good = {"template": cat["template"], "rate": flt(cat["rate"]) + 500}
		for bad in (
			{"template": dog["template"], "rate": 0},
			{"template": dog["template"], "rate": "abc"},
			{"template": dog["template"], "rate": True},
			{"template": "not-a-boarding-row", "rate": 1000},
			{"template": dog["template"], "rate": 1000, "modified": "2000-01-01 00:00:00"},
		):
			with self.subTest(bad=bad):
				res = set_boarding_rates(rates=[good, bad])
				self.assertFalse(_ok(res), msg=res)
				self.assertEqual(resolve_boarding_rate(self.pet, "Travel").rate, dog["rate"])
				self.assertEqual(flt(frappe.db.get_value("CareService template", cat["template"], "default_price")),
					flt(cat["rate"]))
		res = set_boarding_rates(rates=[good, good])
		self.assertFalse(_ok(res), msg=res)

	def test_dog_estimate_rate_is_the_dog_rate(self):
		stay = self._open_stay()
		res = api.get_boarding_detail(boarding_id=stay)
		self.assertTrue(_ok(res), msg=res)
		occupant = _data(res)["occupants"][0]
		expected = resolve_boarding_rate(self.pet, "Travel")
		self.assertEqual(occupant["daily_rate"], expected.rate)
		self.assertEqual(occupant["daily_rate_item"], expected.item_code)

	def test_a_staff_corrected_room_rate_wins(self):
		stay = self._open_stay()
		doc = frappe.get_doc("Pet Boarding", stay)
		row = next(r for r in doc.billable_items if r.item_type == "Room Stay")
		row.rate = 12345
		doc.save()
		occupant = _data(api.get_boarding_detail(boarding_id=stay))["occupants"][0]
		self.assertEqual(occupant["daily_rate"], 12345)

	def test_a_catalogue_gap_is_unknown_and_silent(self):
		stay = self._open_stay()
		doc = frappe.get_doc("Pet Boarding", stay)
		doc.set("billable_items", [])
		doc.save()

		def gap(*args, **kwargs):
			frappe.throw("No boarding rate found")

		messages_before = len(frappe.local.message_log)
		with patch.object(api, "resolve_boarding_rate", side_effect=gap):
			res = api.get_boarding_detail(boarding_id=stay)
		self.assertTrue(_ok(res), msg=res)
		self.assertIsNone(_data(res)["occupants"][0]["daily_rate"])
		self.assertEqual(len(frappe.local.message_log), messages_before)
