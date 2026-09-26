"""A cancelled order must not stay on an invoice.

Before the billing trigger existed this was very nearly unreachable, and that is why the
old code could simply refuse it. Billing happened at release; release is terminal; a
released order cannot be cancelled - so "billed" and "un-cancellable" were the same set by
accident, and ``_assert_parent_billable_row_can_cancel`` threw on any ``Billed`` row.

Under ``on_request`` and ``on_start`` they stop coinciding. An order is Billed from the
moment it is created or started, and stays cancellable for its whole clinical life, so the
old throw would refuse nearly every cancellation a coordinator makes.

The rule is now about the INVOICE the row reached, not the row's status:

    Draft      -> the line is removed, and the order's billing state is cleared with it
    Submitted  -> refused, ISSUED_INVOICE_CANCEL_MESSAGE, unchanged from before
    Cancelled  -> refused, same message
    none       -> nothing was raised, so nothing to reverse

``row_sales_invoice`` is what makes any of it possible. Per-order billing deliberately
never writes ``Vet Visit.sales_invoice``, so a reader that consults only the parent sees no
invoice at all and concludes the charge cannot be reversed.

Nothing here commits.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from pet_app.utils import visit_billing as vb


def _row(**kwargs):
	row = frappe._dict(
		name="PBI-1",
		status="Billed",
		item_code="ITEM-1",
		item_name="Blood Panel",
		qty=1,
		rate=100,
		amount=100,
		sales_invoice=None,
		linked_doctype=None,
		linked_name=None,
		linked_service_id=None,
	)
	row.update(kwargs)
	row.meta = frappe._dict(has_field=lambda f: f in row)
	return row


def _parent(sales_invoice=None):
	return frappe._dict(doctype="Vet Visit", name="VIS-1", sales_invoice=sales_invoice)


class _Invoice:
	"""A stand-in for a Sales Invoice document.

	Deliberately NOT a frappe._dict: `items` is a dict method, so `_dict(items=[...])`
	stores the list under the key while attribute access still returns the bound method,
	and every `invoice.items` read gets the method instead of the lines.
	"""

	def __init__(self, docstatus=0, items=None, name="SINV-1"):
		self.name = name
		self.docstatus = docstatus
		self.items = items if items is not None else [frappe._dict(item_code="ITEM-1")]


def _invoice(docstatus, items=None):
	return _Invoice(docstatus=docstatus, items=items)


class TestRowSalesInvoice(UnitTestCase):
	"""Where a row's charge actually went, in order of specificity."""

	def test_row_field_wins(self):
		row = _row(sales_invoice="SINV-ROW")
		self.assertEqual(vb.row_sales_invoice(_parent("SINV-PARENT"), row), "SINV-ROW")

	def test_falls_back_to_the_linked_orders_own_invoice(self):
		row = _row(linked_doctype="Lab", linked_name="LAB-1")
		meta = frappe._dict(has_field=lambda f: True)
		with patch.object(vb.frappe, "get_meta", return_value=meta), patch.object(
			vb.frappe.db, "get_value", return_value="SINV-ORDER"
		):
			self.assertEqual(vb.row_sales_invoice(_parent(), row), "SINV-ORDER")

	def test_parses_the_order_out_of_linked_service_id_for_legacy_rows(self):
		# Rows written before linked_doctype/linked_name existed encode the pair here.
		row = _row(linked_service_id="Imaging::IMG-1")
		meta = frappe._dict(has_field=lambda f: True)
		with patch.object(vb.frappe, "get_meta", return_value=meta), patch.object(
			vb.frappe.db, "get_value", return_value="SINV-LEGACY"
		) as read:
			self.assertEqual(vb.row_sales_invoice(_parent(), row), "SINV-LEGACY")
		read.assert_called_once_with("Imaging", "IMG-1", "sales_invoice")

	def test_medication_prefix_is_not_mistaken_for_a_doctype(self):
		# "medication::PBI-1" lives in the same field but names no doctype. The
		# ORDER_ITEM_TYPES membership test is what tells them apart.
		row = _row(linked_service_id="medication::ROW-1")
		self.assertEqual(vb.row_sales_invoice(_parent("SINV-PARENT"), row), "SINV-PARENT")

	def test_falls_back_to_the_parent_last(self):
		self.assertEqual(vb.row_sales_invoice(_parent("SINV-PARENT"), _row()), "SINV-PARENT")

	def test_returns_none_when_nothing_was_billed(self):
		self.assertIsNone(vb.row_sales_invoice(_parent(), _row(status="Billable")))


class TestCancelAssertion(UnitTestCase):
	def _assert_can_cancel(self, row, parent, invoice):
		with patch.object(vb.frappe, "get_doc", return_value=invoice), patch.object(
			vb, "_find_draft_invoice_item_for_billable", return_value=frappe._dict(item_code="ITEM-1")
		):
			vb._assert_parent_billable_row_can_cancel(
				parent, row, sales_invoice=parent.sales_invoice, billed=False
			)

	def test_billed_row_on_a_draft_invoice_can_be_cancelled(self):
		# The behaviour change. This threw BILLED_ROW_LOCK_MESSAGE before, which under an
		# early trigger blocks a coordinator on nearly every cancellation.
		row = _row(sales_invoice="SINV-1")
		self._assert_can_cancel(row, _parent(), _invoice(0))

	def test_billed_row_on_a_submitted_invoice_is_refused(self):
		row = _row(sales_invoice="SINV-1")
		with patch.object(vb.frappe, "get_doc", return_value=_invoice(1)):
			with self.assertRaises(frappe.ValidationError) as caught:
				vb._assert_parent_billable_row_can_cancel(
					_parent(), row, sales_invoice=None, billed=False
				)
		# Referred to the cashier, not auto-reversed with a Credit Note.
		self.assertIn("cashier", str(caught.exception).lower())

	def test_billed_row_on_a_cancelled_invoice_is_refused(self):
		row = _row(sales_invoice="SINV-1")
		with patch.object(vb.frappe, "get_doc", return_value=_invoice(2)):
			with self.assertRaises(frappe.ValidationError):
				vb._assert_parent_billable_row_can_cancel(
					_parent(), row, sales_invoice=None, billed=False
				)

	def test_billed_row_with_no_findable_invoice_is_still_refused(self):
		# A line that cannot be located cannot be removed, and cancelling anyway would
		# leave the customer billed for work that was cancelled.
		with self.assertRaises(frappe.ValidationError):
			vb._assert_parent_billable_row_can_cancel(
				_parent(), _row(), sales_invoice=None, billed=False
			)

	def test_already_cancelled_row_is_a_no_op(self):
		vb._assert_parent_billable_row_can_cancel(
			_parent(), _row(status="Cancelled"), sales_invoice=None, billed=False
		)

	def test_unbilled_row_needs_no_reversal(self):
		vb._assert_parent_billable_row_can_cancel(
			_parent(), _row(status="Billable"), sales_invoice=None, billed=False
		)


class TestInvoiceLineScoping(UnitTestCase):
	"""A per-order line is marked with its ORDER, not with the visit."""

	def _line(self, marker):
		from pet_app.utils.invoice_source import build_marker

		return frappe._dict(description=f"Blood Panel\n{build_marker(*marker)}", item_code="ITEM-1")

	def test_order_marked_lines_are_found_for_a_per_order_row(self):
		lab_line = self._line(("Lab", "LAB-1"))
		visit_line = self._line(("Vet Visit", "VIS-1"))
		invoice = _Invoice(items=[visit_line, lab_line])
		row = _row(linked_doctype="Lab", linked_name="LAB-1")
		self.assertEqual(vb._invoice_lines_for_parent(_parent(), invoice, row=row), [lab_line])

	def test_parent_scope_is_still_tried_for_a_visit_billed_row(self):
		visit_line = self._line(("Vet Visit", "VIS-1"))
		invoice = _Invoice(items=[visit_line])
		row = _row(linked_doctype="Lab", linked_name="LAB-1")
		# The order marker matches nothing, so the parent scope answers.
		self.assertEqual(vb._invoice_lines_for_parent(_parent(), invoice, row=row), [visit_line])

	def test_unmarked_invoice_falls_back_to_every_line(self):
		# Pre-marker invoices necessarily predate merging, so the whole invoice is safe.
		lines = [frappe._dict(description="Blood Panel", item_code="ITEM-1")]
		invoice = _Invoice(items=lines)
		self.assertEqual(vb._invoice_lines_for_parent(_parent(), invoice, row=_row()), lines)

	def test_medication_row_scopes_to_the_visit(self):
		# _commit_medication_billing marks its line with ("Vet Visit", visit).
		visit_line = self._line(("Vet Visit", "VIS-1"))
		invoice = _Invoice(items=[visit_line])
		row = _row(linked_service_id="medication::ROW-1")
		self.assertEqual(vb._invoice_lines_for_parent(_parent(), invoice, row=row), [visit_line])


class TestClearBillingState(UnitTestCase):
	def test_clears_the_row_and_the_order_together(self):
		row = _row(sales_invoice="SINV-1", linked_doctype="Lab", linked_name="LAB-1")
		meta = frappe._dict(has_field=lambda f: True)
		with patch.object(vb.frappe, "get_meta", return_value=meta), patch.object(
			vb.frappe.db, "exists", return_value=True
		), patch.object(vb.frappe.db, "set_value") as write:
			vb._clear_billable_row_billing_state(row)
		self.assertIsNone(row.sales_invoice)
		# Left behind, `billed = 1` makes plan_order_billing refuse to ever bill it again.
		write.assert_called_once_with(
			"Lab", "LAB-1", {"sales_invoice": None, "billed": 0}, update_modified=False
		)

	def test_medication_row_has_no_order_to_clear(self):
		row = _row(sales_invoice="SINV-1", linked_service_id="medication::ROW-1")
		with patch.object(vb.frappe.db, "set_value") as write:
			vb._clear_billable_row_billing_state(row)
		self.assertIsNone(row.sales_invoice)
		write.assert_not_called()


class TestBilledMedicationEditRefused(UnitTestCase):
	"""An invoiced prescription is not editable; the way to change it is cancel + re-prescribe.

	This closes a silent divergence rather than a loud one. For a Billed row
	``upsert_visit_billable_item`` returned early and applied nothing, so the prescription
	said 3 while its invoice line said 1 and no check fired - the visit-level guard compares
	the billable row against itself, and the early return meant it had not changed.
	"""

	def _row(self, **kw):
		row = frappe._dict(
			name="PBI-1", status="Billed", item_code="ITEM-1", item_name="Amoxicillin",
			item_type="Medication", qty=1.0, rate=25.0, amount=25.0,
		)
		row.update(kw)
		row.meta = frappe._dict(has_field=lambda f: f in row)
		return row

	def _check(self, *, visit=None, item_type="Medication", **incoming):
		args = {"item_code": "ITEM-1", "item_type": item_type, "qty": 1.0, "rate": 25.0}
		args.update(incoming)
		vb._assert_billed_medication_row_unchanged(visit, self._row(), **args)

	def test_unchanged_resync_is_allowed(self):
		# Every visit save re-syncs every prescription. Identical values must not throw or
		# the visit becomes unsaveable the moment one medication is billed.
		self._check()

	def test_quantity_change_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._check(qty=3)
		msg = str(caught.exception)
		self.assertIn("already been invoiced", msg)
		# The remedy has to be on the message; the doctor cannot discover it otherwise.
		self.assertIn("Cancel this medication", msg)

	def test_rate_change_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(rate=40)

	def test_item_change_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(item_code="ITEM-2")

	def test_integer_float_serialisation_is_not_a_change(self):
		# The client posts the whole document back and a whole float serialises as a JSON
		# integer. Comparing as text saw 1 against a stored 1.0 and refused a save that
		# changed nothing - the bug billed_row_field_changed exists to prevent.
		self._check(qty=1, rate=25)

	def test_orders_are_not_affected(self):
		# Lab/Imaging/Procedure/Service re-stamp rate from the CareService catalogue on every
		# validate, so a catalogue price change would make every later save of a billed order
		# throw. Deliberately out of scope.
		self._check(item_type="Lab", qty=99, rate=999)

	def test_internal_billing_writes_are_not_blocked(self):
		# Cancellation and status writes re-save the visit with this flag as part of a
		# billing operation, not as an edit to a charge.
		visit = frappe._dict(flags=frappe._dict(ignore_billing_lock=True))
		self._check(visit=visit, qty=3, rate=40)

	def test_no_visit_object_still_refuses(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(visit=None, qty=3)


class TestInvoiceCancelReleasesRows(UnitTestCase):
	"""Cancelling an invoice releases exactly the rows that reached it.

	Medication records its invoice on the row only - never on ``Vet Visit.sales_invoice`` -
	so the parent loop in ``on_sales_invoice_cancel`` never saw it and the row stayed Billed
	against a cancelled document, uncollectable. The same loop had the opposite fault: it
	flipped EVERY Billed row of a linked parent, including rows billed onto a different
	invoice that is still standing, which the next visit close would charge a second time.
	"""

	def _release(self, rows):
		with patch.object(vb.frappe.db, "has_column", return_value=True), patch.object(
			vb.frappe, "get_all", return_value=rows
		), patch.object(vb.frappe.db, "set_value") as write, patch.object(
			vb.frappe, "clear_document_cache"
		) as clear:
			released = vb.release_billable_rows_billed_to_invoice("SINV-1")
		return released, write, clear

	def test_billed_row_is_put_back_and_unlinked(self):
		row = frappe._dict(name="PBI-1", parenttype="Vet Visit", parent="VIS-1", status="Billed")
		released, write, clear = self._release([row])
		self.assertEqual(released, ["PBI-1"])
		write.assert_called_once_with(
			"Pet Billable Item", "PBI-1", {"sales_invoice": None, "status": "Billable"}, update_modified=False
		)
		clear.assert_called_once_with("Vet Visit", "VIS-1")

	def test_included_row_keeps_its_status(self):
		# The rate that absorbed it has not changed; only the pointer is stale.
		row = frappe._dict(name="PBI-2", parenttype="Vet Visit", parent="VIS-1", status="Included")
		_, write, _ = self._release([row])
		write.assert_called_once_with(
			"Pet Billable Item", "PBI-2", {"sales_invoice": None}, update_modified=False
		)

	def test_nothing_named_writes_nothing(self):
		released, write, clear = self._release([])
		self.assertEqual(released, [])
		write.assert_not_called()
		clear.assert_not_called()

	def _cancel(self, rows, parent_invoice="SINV-1"):
		parent = frappe._dict(
			doctype="Vet Visit", name="VIS-1", sales_invoice=parent_invoice, billable_items=rows,
			flags=frappe._dict(),
		)
		parent.meta = frappe._dict(has_field=lambda f: f in ("sales_invoice", "billed"))
		parent.save = MagicMock()
		with patch("pet_app.utils.order_billing.release_orders_billed_to_invoice"), patch.object(
			vb, "release_billable_rows_billed_to_invoice"
		) as release, patch.object(
			vb, "_records_linked_to_invoice", return_value=[("Vet Visit", "VIS-1")]
		), patch.object(vb.frappe, "get_doc", return_value=parent), patch.object(
			vb, "apply_billable_item_amounts", return_value=0
		):
			vb.on_sales_invoice_cancel(frappe._dict(name="SINV-1"))
		release.assert_called_once_with("SINV-1")
		parent.save.assert_called_once()
		return parent

	def test_row_billed_with_the_parent_is_released(self):
		row = _row(sales_invoice=None)
		self._cancel([row])
		self.assertEqual(row.status, "Billable")

	def test_row_billed_onto_another_invoice_stays_billed(self):
		row = _row(sales_invoice="SINV-OTHER", linked_service_id="medication::ROW-1")
		self._cancel([row])
		self.assertEqual(row.status, "Billed")
