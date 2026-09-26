"""A row's warehouse belongs to the invoice's branch, whether or not stock moves.

The bug these cover: a hotel X-ray came out with `branch = hotel` on the invoice and
`المخزن الرئيسي` - the *main* store - on the item row, and the hotel cashier was then
refused on their own invoice. `has_user_permission` walks child rows for every restricted
link, so a warehouse that disagrees with the branch is a lockout, not a cosmetic mismatch.

It arrived there without anyone setting it. `commit_order_billing` sends a service line
with no warehouse key on purpose - `invoice_reuse` reads that key's presence to decide
`update_stock` - so the row is empty when `stamp_branch_and_warehouse` runs. The hook used
to decline to invent one, and ERPNext's `set_missing_item_details` then filled the gap from
`Stock Settings.default_warehouse`.

Which is why the first test goes through `get_or_create_open_invoice` and a real save rather
than calling the hook on a stub: the whole failure lived in the handoff between the hook and
ERPNext's own defaulting, and a stub cannot see that handoff at all.

Nothing here commits; IntegrationTestCase rolls each test back.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from pet_app.utils.branch_warehouse import branch_warehouse, stamp_branch_and_warehouse
from pet_app.utils.invoice_reuse import get_or_create_open_invoice

HOTEL = "hotel"
UNWIRED_BRANCH = "_Test Branch Without A Warehouse"


class BranchWarehouseStamping(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.company = frappe.db.get_value("Global Defaults", None, "default_company") or (
			frappe.db.get_value("Company", {}, "name")
		)
		self.hotel_warehouse = branch_warehouse(HOTEL)
		if not self.hotel_warehouse:
			self.skipTest(f"Branch {HOTEL} has no custom_wharehouse on this site")

		self.customer = self._customer()
		self.service_item = self._service_item()

	def _customer(self) -> str:
		name = "_Test Branch Warehouse Customer"
		if not frappe.db.exists("Customer", name):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": name,
					"customer_type": "Individual",
					"customer_group": frappe.db.get_value("Customer Group", {}, "name"),
					"territory": frappe.db.get_value("Territory", {}, "name"),
				}
			).insert(ignore_permissions=True)
		return name

	def _service_item(self) -> str:
		"""Non-stock, like an X-ray or a lab test - the case that was broken."""
		code = "_Test Branch Warehouse Service"
		if not frappe.db.exists("Item", code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert(ignore_permissions=True)
		return code

	def _service_invoice(self, branch: str):
		"""The exact shape `commit_order_billing` raises: no warehouse key on the line."""
		return get_or_create_open_invoice(
			customer=self.customer,
			items=[{"item_code": self.service_item, "qty": 1, "rate": 250}],
			source_doctype="Guardian",
			source_name="_probe",
			company=self.company,
			branch=branch,
			requires_stock=False,
			force_new=True,
			ignore_permissions=True,
			branch_authorised=True,
		).invoice

	def test_service_line_takes_the_branch_warehouse_and_stays_non_stock(self):
		invoice = self._service_invoice(HOTEL)

		self.assertEqual(invoice.items[0].warehouse, self.hotel_warehouse)
		self.assertEqual(invoice.items[0].branch, HOTEL)
		# The guard that must not be collateral damage: adding a warehouse in a hook
		# cannot turn a service invoice into one that relieves stock.
		self.assertEqual(invoice.update_stock, 0)
		# And the header keeps no shipping point, because nothing ships.
		self.assertFalse(invoice.set_warehouse)

	def test_erpnext_does_not_overwrite_the_stamped_warehouse_on_resave(self):
		"""`set_missing_item_details` runs again on every save - it must lose every time."""
		invoice = self._service_invoice(HOTEL)
		invoice.flags.from_custom_flow = True
		invoice.save(ignore_permissions=True)

		self.assertEqual(invoice.items[0].warehouse, self.hotel_warehouse)

	def test_a_branch_with_no_warehouse_configured_leaves_the_row_alone(self):
		"""`None` is a real answer: an unwired branch must not borrow another's stock."""
		if not frappe.db.exists("Branch", UNWIRED_BRANCH):
			frappe.get_doc({"doctype": "Branch", "branch": UNWIRED_BRANCH}).insert(
				ignore_permissions=True
			)
		self.assertIsNone(branch_warehouse(UNWIRED_BRANCH))

		invoice = self._service_invoice(UNWIRED_BRANCH)

		# Whatever ERPNext worked out survives - the hook returned early rather than
		# stamping a blank or another branch's shelf.
		self.assertNotEqual(invoice.items[0].warehouse, self.hotel_warehouse)

	def test_a_stock_leg_still_stamps_the_invoice_header(self):
		"""The `update_stock` half of the rule is unchanged."""
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": self.customer,
				"company": self.company,
				"branch": HOTEL,
				"update_stock": 1,
				"items": [{"item_code": self.service_item, "qty": 1, "rate": 250}],
			}
		)

		stamp_branch_and_warehouse(doc)

		self.assertEqual(doc.set_warehouse, self.hotel_warehouse)
		self.assertEqual(doc.items[0].warehouse, self.hotel_warehouse)
