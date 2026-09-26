"""When a billable item becomes an invoice line is one setting, and it has one reader.

The tests that matter here are not "does on_request bill at request" - that is a wiring
question and the wiring is a one-line gate at each call site. They are the three places
where getting it wrong costs money:

* ``get_billing_trigger`` answers ``on_release`` for every way of not having an answer -
  field absent (deployed, not yet migrated), blank (migrated, not yet seeded), and a
  value nobody recognises. If those ever diverge, a site mid-deploy silently stops
  billing or starts billing twice.

* ``should_bill_now`` makes release a catch-up for the earlier triggers. clinical_state
  admits a release straight from Pending, so without this an order billing ``on_start``
  that was never started would never be charged at all - the charge does not arrive late,
  it never arrives.

* ``on_visit_close`` stands every per-order call site down. If one of them still fired,
  the charge would be raised twice: once per-order and once by the visit's own close.

The medication planner is tested for its stock decision rather than its arithmetic. A
medication line that carries a warehouse makes the invoice deduct stock, and dispensing
deducts it again; which of the two owns the deduction is the whole question, and an
absent key is a third, worse answer because ERPNext backfills it from Item Default.

Nothing here commits. IntegrationTestCase rolls each test back.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from pet_app.utils import billing_trigger as bt

SETTINGS = "Pet App Access Settings"


class TestGetBillingTrigger(UnitTestCase):
	"""Every way of having no answer resolves to the behaviour that predates the setting."""

	def _resolve(self, *, has_field=True, stored=None):
		meta = type("Meta", (), {"has_field": staticmethod(lambda _f: has_field)})()
		with patch.object(bt.frappe, "get_meta", return_value=meta), patch.object(
			bt.frappe.db, "get_single_value", return_value=stored
		):
			return bt.get_billing_trigger()

	def test_missing_field_resolves_to_on_release(self):
		# Deployed but not migrated. get_single_value would THROW here, not return None,
		# so the guard has to run before the read or every billing call in the clinic
		# raises until someone runs bench migrate.
		self.assertEqual(self._resolve(has_field=False), "on_release")

	def test_missing_field_never_reads_the_value(self):
		meta = type("Meta", (), {"has_field": staticmethod(lambda _f: False)})()
		with patch.object(bt.frappe, "get_meta", return_value=meta), patch.object(
			bt.frappe.db, "get_single_value"
		) as read:
			bt.get_billing_trigger()
		read.assert_not_called()

	def test_blank_resolves_to_on_release(self):
		# Migrated, not yet seeded. The Select ships with an empty first option.
		self.assertEqual(self._resolve(stored=None), "on_release")
		self.assertEqual(self._resolve(stored=""), "on_release")
		self.assertEqual(self._resolve(stored="   "), "on_release")

	def test_unrecognised_value_resolves_to_on_release(self):
		# A direct database edit, or a value retired by a later version. Must not read as
		# "no trigger matches, so never bill".
		self.assertEqual(self._resolve(stored="on_tuesday"), "on_release")

	def test_each_configured_value_is_returned(self):
		for value in bt.BILLING_TRIGGERS:
			self.assertEqual(self._resolve(stored=value), value)


class TestShouldBillNow(UnitTestCase):
	def _with(self, trigger):
		return patch.object(bt, "get_billing_trigger", return_value=trigger)

	def test_each_trigger_fires_at_its_own_moment(self):
		for trigger in ("on_request", "on_start", "on_release"):
			with self._with(trigger):
				self.assertTrue(bt.should_bill_now(trigger), trigger)

	def test_a_trigger_does_not_fire_at_another_moment(self):
		with self._with("on_start"):
			self.assertFalse(bt.should_bill_now("on_request"))
		with self._with("on_release"):
			self.assertFalse(bt.should_bill_now("on_request"))
			self.assertFalse(bt.should_bill_now("on_start"))

	def test_release_is_a_catch_up_for_the_earlier_triggers(self):
		# clinical_state.ACTION_ALLOWED_STATUSES admits "release" from Pending, so an order
		# under on_start may reach release having never been started. Without this the
		# charge is not late - it is lost.
		for trigger in ("on_request", "on_start"):
			with self._with(trigger):
				self.assertTrue(bt.should_bill_now("on_release"), trigger)

	def test_on_visit_close_stands_every_per_order_moment_down(self):
		# Any one of these firing would charge the customer twice: once here and once when
		# get_billable_invoice_items emits the row at close.
		with self._with("on_visit_close"):
			for moment in ("on_request", "on_start", "on_release"):
				self.assertFalse(bt.should_bill_now(moment), moment)
			self.assertTrue(bt.bills_at_visit_close())

	def test_bills_at_visit_close_is_false_for_the_others(self):
		for trigger in ("on_request", "on_start", "on_release"):
			with self._with(trigger):
				self.assertFalse(bt.bills_at_visit_close(), trigger)


class TestBillingTriggerField(IntegrationTestCase):
	"""The field itself, once migrated."""

	def test_field_exists_with_the_four_options_and_a_blank_first(self):
		meta = frappe.get_meta(SETTINGS)
		if not meta.has_field("billing_trigger"):
			self.skipTest("billing_trigger not migrated yet")
		field = meta.get_field("billing_trigger")
		self.assertEqual(field.fieldtype, "Select")
		# Leading newline: the blank option. Without it a site that has never opened the
		# form still shows a value, and "unset" becomes indistinguishable from "chosen".
		self.assertTrue(field.options.startswith("\n"))
		self.assertEqual(
			[o for o in field.options.split("\n") if o],
			list(bt.BILLING_TRIGGERS),
		)

	def test_stored_value_round_trips(self):
		if not frappe.get_meta(SETTINGS).has_field("billing_trigger"):
			self.skipTest("billing_trigger not migrated yet")
		frappe.db.set_single_value(SETTINGS, "billing_trigger", "on_start")
		self.assertEqual(bt.get_billing_trigger(), "on_start")


class TestMedicationBillingPlan(UnitTestCase):
	"""Invoice submission owns stock; historical dispense issues require review."""

	def _plan(self, *, is_stock=1, issued=0, opted_in=False, warehouse="MAIN"):
		from pet_app.utils import order_billing as ob
		from pet_app.utils import medication_stock as ms

		visit = frappe._dict(name="VIS-1")
		row = frappe._dict(
			name="ROW-1",
			idx=1,
			qty=3,
			rate=25,
			medication="MED-1",
			medication_item="ITEM-1",
			warehouse=warehouse,
			stock_issued_qty=issued,
			dispense_status="Prescribed",
		)

		def _get_value(doctype, name, fieldname=None, **kwargs):
			if doctype == "Item":
				return frappe._dict(name="ITEM-1", item_name="Amoxicillin", is_stock_item=is_stock, stock_uom="ml")
			if doctype == "Vet Visit":
				return {"branch": "BR-1", "customer": "CUST-1"}.get(fieldname)
			return None

		with patch.object(ob, "_medication_billing_state", return_value={"billed": False, "status": "Billable", "row": "PBI-1"}), \
			patch.object(ob.frappe.db, "get_value", side_effect=_get_value), \
			patch.object(ob.frappe.db, "exists", return_value=True), \
			patch.object(ms.frappe, "get_all", return_value=[frappe._dict(name="DOSE", label="dose", qty=0.5)] if opted_in else []), \
			patch.object(ms, "resolve_dispense_warehouse", return_value="RESOLVED-WH"):
			return ob.plan_medication_billing(visit, row)

	def test_qty_comes_from_the_prescription_not_a_hardcoded_one(self):
		# The order path bills qty=1 because an order is one thing. A prescription is not.
		self.assertEqual(self._plan().qty, 3)

	def test_unissued_non_opted_in_medication_keeps_the_invoice_as_the_stock_document(self):
		# Nothing else will ever deduct this one, so the invoice must.
		self.assertEqual(self._plan(issued=0, opted_in=False).warehouse, "RESOLVED-WH")

	def test_configured_medication_still_deducts_on_invoice(self):
		plan = self._plan(issued=0, opted_in=True)
		self.assertEqual(plan.warehouse, "RESOLVED-WH")
		self.assertEqual(plan.stock_context["conversion_factor"], 0.5)

	def test_already_issued_medication_requires_review(self):
		for opted_in in (True, False):
			with self.assertRaisesRegex(frappe.ValidationError, "historical stock issue"):
				self._plan(issued=5, opted_in=opted_in)

	def test_unconfigured_medication_uses_stock_uom(self):
		plan = self._plan()
		self.assertEqual(plan.stock_context["uom"], "ml")
		self.assertEqual(plan.stock_context["conversion_factor"], 1)

	def test_non_stock_medication_carries_no_warehouse(self):
		self.assertEqual(self._plan(is_stock=0).warehouse, "")

	def test_cancelled_prescription_is_not_billed(self):
		from pet_app.utils import order_billing as ob

		row = frappe._dict(name="ROW-1", idx=1, qty=1, rate=5, dispense_status="Cancelled",
		                   medication_item="ITEM-1")
		self.assertIsNone(ob.plan_medication_billing(frappe._dict(name="VIS-1"), row))

	def test_included_row_is_never_invoiced(self):
		# The boarding medical rate absorbed it. It keeps its row and its real price; it
		# must not become a line. See order_billing._medication_included.
		from pet_app.utils import order_billing as ob

		row = frappe._dict(name="ROW-1", idx=1, qty=1, rate=5, dispense_status="Prescribed",
		                   medication_item="ITEM-1")
		with patch.object(ob, "_medication_billing_state",
		                  return_value={"billed": True, "status": "Included", "row": "PBI-1"}):
			self.assertIsNone(ob.plan_medication_billing(frappe._dict(name="VIS-1"), row))

	def test_already_billed_row_is_not_billed_again(self):
		from pet_app.utils import order_billing as ob

		row = frappe._dict(name="ROW-1", idx=1, qty=1, rate=5, dispense_status="Prescribed",
		                   medication_item="ITEM-1")
		with patch.object(ob, "_medication_billing_state",
		                  return_value={"billed": True, "status": "Billed", "row": "PBI-1"}):
			self.assertIsNone(ob.plan_medication_billing(frappe._dict(name="VIS-1"), row))

	def test_row_without_a_billable_row_yet_is_not_billed(self):
		# Billing without being able to record it re-charges on the next visit save.
		from pet_app.utils import order_billing as ob

		row = frappe._dict(name="ROW-1", idx=1, qty=1, rate=5, dispense_status="Prescribed",
		                   medication_item="ITEM-1")
		with patch.object(ob, "_medication_billing_state",
		                  return_value={"billed": False, "status": None, "row": None}):
			self.assertIsNone(ob.plan_medication_billing(frappe._dict(name="VIS-1"), row))


class TestIncludeVisitMedicationInBoarding(UnitTestCase):
	"""The Treatment-rate switch, and the three places Included has to hold.

	Marking a row Included is only half of it. The row keeps its real price on purpose -
	so the owner can see what the rate absorbed - so anything that turns rows into money
	has to know to skip it, or the "free" medication arrives as a line at visit close.
	"""

	def _included(self, *, setting=True, status=None, boarding=None, boarding_type="Treatment"):
		from pet_app.utils import order_billing as ob

		state = {"status": status, "row": "PBI-1", "billed": False}
		row = frappe._dict(name="ROW-1", pet="PET-1")
		doc = frappe._dict(occupants=[frappe._dict(pet="PET-1", boarding_type=boarding_type)],
		                   boarding_type=boarding_type)
		with patch.object(ob, "include_visit_medication_in_boarding", return_value=setting), \
			patch("pet_app.utils.boarding_occupancy.find_active_boarding_for_pet", return_value=boarding), \
			patch.object(ob.frappe, "get_doc", return_value=doc):
			return ob._medication_included(state, "VIS-1", row)

	def test_off_means_billed_normally_whatever_the_boarding(self):
		self.assertFalse(self._included(setting=False, boarding={"name": "BOARD-1"}))

	def test_on_with_no_active_stay_bills_normally(self):
		self.assertFalse(self._included(setting=True, boarding=None))

	def test_on_with_an_active_treatment_stay_is_included(self):
		self.assertTrue(self._included(setting=True, boarding={"name": "BOARD-1"}))

	def test_on_with_a_non_treatment_stay_bills_normally(self):
		# A pet boarding on ordinary terms pays for its medication; only the medical rate
		# absorbs it.
		self.assertFalse(
			self._included(setting=True, boarding={"name": "BOARD-1"}, boarding_type="Standard")
		)

	def test_a_row_already_included_stays_included_whatever_the_setting(self):
		# Set by the boarding flow for boarding-ordered medication. Turning the visit
		# setting off must not start billing medication another rate already covered.
		self.assertTrue(self._included(setting=False, status="Included", boarding=None))

	def test_included_plan_raises_no_invoice(self):
		from pet_app.utils import order_billing as ob

		plan = frappe._dict(included=True, billable_row="PBI-1", name="ROW-1", visit="VIS-1")
		with patch.object(ob.frappe.db, "set_value") as write, patch.object(
			ob, "get_or_create_open_invoice"
		) as invoice:
			result = ob._commit_medication_billing(plan)
		invoice.assert_not_called()
		write.assert_called_once_with(
			"Pet Billable Item", "PBI-1", {"status": "Included"}, update_modified=False
		)
		self.assertTrue(result.included)
		self.assertIsNone(result.sales_invoice)

	def test_visit_close_skips_included_rows(self):
		# get_billable_invoice_items reads this set. Without "Included" the absorbed row
		# becomes a line at close and the guardian pays twice.
		from pet_app.utils.visit_billing import SKIP_INVOICE_ROW_STATUSES

		self.assertIn("Included", SKIP_INVOICE_ROW_STATUSES)

	def test_included_amount_is_not_owed(self):
		from pet_app.utils.visit_billing import summarise_visit_billing, visit_billing_status

		visit = frappe._dict(
			name="VIS-1",
			sales_invoice=None,
			billable_items=[
				frappe._dict(status="Included", amount=80, qty=1, rate=80, linked_service_id="medication::R1"),
			],
		)
		with patch("pet_app.utils.visit_billing.frappe.get_meta",
		           return_value=frappe._dict(has_field=lambda f: False)):
			summary = summarise_visit_billing(visit)
		# Still worth 80 clinically; nothing to collect.
		self.assertEqual(summary.total, 80)
		self.assertEqual(summary.included, 80)
		self.assertEqual(summary.unbilled, 0)
		self.assertEqual(summary.outstanding, 0)
		self.assertEqual(visit_billing_status(visit, summary), "Unbilled")


class TestMedicationBillingTrigger(UnitTestCase):
	"""Medication follows its own setting, and the two must not leak into each other."""

	def _resolve(self, *, has_field=True, stored=None):
		meta = type("Meta", (), {"has_field": staticmethod(lambda _f: has_field)})()
		with patch.object(bt.frappe, "get_meta", return_value=meta), patch.object(
			bt.frappe.db, "get_single_value", return_value=stored
		):
			return bt.get_medication_billing_trigger()

	def test_missing_field_resolves_to_on_request(self):
		self.assertEqual(self._resolve(has_field=False), "on_request")

	def test_missing_field_never_reads_the_value(self):
		meta = type("Meta", (), {"has_field": staticmethod(lambda _f: False)})()
		with patch.object(bt.frappe, "get_meta", return_value=meta), patch.object(
			bt.frappe.db, "get_single_value"
		) as read:
			bt.get_medication_billing_trigger()
		read.assert_not_called()

	def test_blank_and_unrecognised_resolve_to_on_request(self):
		for stored in (None, "", "   ", "on_tuesday"):
			self.assertEqual(self._resolve(stored=stored), "on_request", repr(stored))

	def test_each_configured_value_is_returned(self):
		for value in bt.BILLING_TRIGGERS:
			self.assertEqual(self._resolve(stored=value), value)

	def test_unset_does_not_inherit_the_order_trigger(self):
		# Inheriting would make medication's answer change silently the next time somebody
		# edited billing_trigger for the four order types.
		meta = type("Meta", (), {"has_field": staticmethod(lambda _f: True)})()
		with patch.object(bt.frappe, "get_meta", return_value=meta), patch.object(
			bt.frappe.db, "get_single_value", return_value=None
		), patch.object(bt, "get_billing_trigger", return_value="on_visit_close"):
			self.assertEqual(bt.get_medication_billing_trigger(), "on_request")

	def test_explicit_trigger_overrides_the_order_setting(self):
		# The whole point of the split: should_bill_now must answer for the trigger it is
		# handed, not for billing_trigger.
		with patch.object(bt, "get_billing_trigger", return_value="on_visit_close"):
			self.assertTrue(bt.should_bill_now("on_request", trigger="on_request"))
			self.assertFalse(bt.should_bill_now("on_request", trigger="on_start"))
			self.assertFalse(bt.should_bill_now("on_request", trigger="on_visit_close"))

	def test_explicit_trigger_keeps_the_release_catch_up(self):
		for trigger in ("on_request", "on_start"):
			self.assertTrue(bt.should_bill_now("on_release", trigger=trigger), trigger)

	def test_omitting_trigger_still_reads_the_order_setting(self):
		# Every existing order call site passes no trigger and must be unaffected.
		with patch.object(bt, "get_billing_trigger", return_value="on_start"):
			self.assertTrue(bt.should_bill_now("on_start"))
			self.assertFalse(bt.should_bill_now("on_request"))

	def test_medication_driver_uses_the_medication_trigger(self):
		# The wiring itself: bill_visit_medications_at must not consult billing_trigger.
		from pet_app.utils import order_billing as ob

		visit = frappe._dict(name="VIS-1", prescribed_medications=[])
		with patch.object(ob, "get_medication_billing_trigger", return_value="on_start") as med, \
			patch.object(ob, "should_bill_now", return_value=False) as gate:
			ob.bill_visit_medications_at(visit, "on_start")
		med.assert_called_once()
		gate.assert_called_once_with("on_start", trigger="on_start")
