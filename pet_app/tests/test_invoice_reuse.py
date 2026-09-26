"""Real invoice/stock tests. Isolated test database only; each test rolls back.

Run through unittest with an initialized Frappe test-site connection, without the
app-wide fixture preloader. No production site may run this suite.
"""
import unittest
import uuid

import frappe
from frappe.utils import add_days, nowdate

from pet_app.utils.invoice_reuse import get_or_create_open_invoice, find_open_invoice
from pet_app.utils.invoice_source import has_marker


class TestInvoiceReuse(unittest.TestCase):
    def setUp(self):
        assert frappe.local.site != 'frappe.localhost', 'Never run on production'
        assert str(frappe.conf.db_name).startswith('test_driver_orders_'), 'Isolated test database required'
        frappe.set_user('Administrator')
        frappe.flags.in_test = True
        self.company = 'Kokh-vet'
        self.warehouse = frappe.db.get_value('Branch', 'main', 'custom_wharehouse')
        suffix = uuid.uuid4().hex[:10]
        self.customer = frappe.get_doc(dict(doctype='Customer', customer_name=f'_Test Reuse {suffix}',
            customer_type='Individual', customer_group=frappe.db.get_value('Customer Group', {}, 'name'),
            territory=frappe.db.get_value('Territory', {}, 'name'))).insert().name
        self.stock = self.item(f'_Test Reuse Stock {suffix}', 1)
        self.service = self.item(f'_Test Reuse Service {suffix}', 0)
        self.receipt = frappe.get_doc(dict(doctype='Stock Entry', stock_entry_type='Material Receipt',
            company=self.company, items=[dict(item_code=self.stock, qty=100, basic_rate=10,
                t_warehouse=self.warehouse)])).insert()
        self.receipt.submit()

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()

    def item(self, name, stock):
        return frappe.get_doc(dict(doctype='Item', item_code=name, item_name=name, is_stock_item=stock,
            stock_uom='ml', item_group=frappe.db.get_value('Item Group', {'is_group': 0}, 'name'))).insert().name

    def charge(self, code, *, branch='main', source=('User', 'Administrator'), **kwargs):
        return get_or_create_open_invoice(customer=self.customer, company=self.company, branch=branch,
            items=[dict(item_code=code, qty=1, rate=100)], source_doctype=source[0], source_name=source[1],
            ignore_permissions=True, branch_authorised=True, **kwargs).invoice

    def movements(self, invoice):
        return frappe.db.sql('''SELECT item_code, SUM(actual_qty) qty FROM `tabStock Ledger Entry`
            WHERE voucher_type='Sales Invoice' AND voucher_no=%s AND is_cancelled=0 GROUP BY item_code''',
            invoice.name, as_dict=True)

    def assert_stock_once(self, invoice, qty=1):
        invoice.submit()
        self.assertEqual({r.item_code: r.qty for r in self.movements(invoice)}, {self.stock: -qty})

    def test_service_then_stock_reuses_and_submits(self):
        service = self.charge(self.service)
        self.assertEqual(service.update_stock, 0)
        stock = self.charge(self.stock, requires_stock=False)
        self.assertEqual(service.name, stock.name)
        self.assertEqual(stock.update_stock, 1)
        self.assertEqual(len(stock.items), 2)
        self.assert_stock_once(stock)

    def test_stock_then_service_reuses_and_submits(self):
        stock = self.charge(self.stock)
        service = self.charge(self.service)
        self.assertEqual(stock.name, service.name)
        self.assertEqual(service.update_stock, 1)
        self.assert_stock_once(service)

    def test_different_dates_reuse_and_branches_do_not(self):
        first = self.charge(self.service, posting_date=add_days(nowdate(), -5), due_date=nowdate())
        # Reproduce a persisted older draft/payment schedule without changing production.
        frappe.db.set_value('Sales Invoice', first.name, 'posting_date', add_days(nowdate(), -5))
        second = self.charge(self.stock, posting_date=nowdate())
        self.assertEqual(first.name, second.name)
        hotel = self.charge(self.service, branch='hotel')
        self.assertNotEqual(hotel.name, first.name)
        self.assertEqual(hotel.branch, 'hotel')

    def test_stock_bundle_without_warehouse_is_detected_and_submitted(self):
        bundle = self.item('_Test Reuse Bundle '+uuid.uuid4().hex[:8], 0)
        frappe.get_doc(dict(doctype='Product Bundle', new_item_code=bundle,
            items=[dict(item_code=self.stock, qty=2)])).insert()
        first = self.charge(self.service)
        invoice = self.charge(bundle)
        self.assertEqual(first.name, invoice.name)
        self.assertEqual(invoice.update_stock, 1)
        self.assertEqual(len(invoice.packed_items), 1)
        self.assert_stock_once(invoice, qty=2)

    def test_warehouse_on_service_does_not_imply_stock(self):
        invoice = get_or_create_open_invoice(customer=self.customer, company=self.company, branch='main',
            source_doctype='User', source_name='Administrator',
            items=[dict(item_code=self.service, qty=1, rate=100, warehouse=self.warehouse)]).invoice
        self.assertEqual(invoice.update_stock, 0)
        invoice.submit()
        self.assertEqual(self.movements(invoice), [])

    def test_pos_return_driver_drafts_excluded(self):
        regular = self.charge(self.service)
        pos = self.charge(self.service, is_pos=1)
        self.assertNotEqual(pos.name, regular.name)
        # Set boundary fields on isolated draft fixtures; actual driver creation is
        # separately covered by test_driver_orders' real dispatch/delivery tests.
        for values in ({'is_return': 1}, {'custom_driver_flow': 1},
                       {'pos_profile': 'Alkokh Vet Store - Main Cashier'}):
            standalone = self.charge(self.service, force_new=True)
            frappe.db.set_value('Sales Invoice', standalone.name, values)
            self.assertEqual(find_open_invoice(customer=self.customer, company=self.company, branch='main'), regular.name)
        due_counter = self.charge(self.service, source=('POS Profile', 'Alkokh Vet Store - Main Cashier'))
        self.assertEqual(due_counter.is_pos, 0)
        self.assertFalse(due_counter.pos_profile)
        self.assertNotEqual(due_counter.name, regular.name)
        self.assertEqual(self.charge(self.stock).name, regular.name)

    def medication(self):
        return frappe.get_doc(dict(doctype='Medication', medication_name=self.stock, linked_item=self.stock,
            default_price=100, default_warehouse=self.warehouse,
            dose_options=[dict(label='half ml', qty=0.5)])).insert()

    def visit(self, medication=None):
        template = frappe.db.get_value('Vet Visit', {'status': 'In Progress'}, 'name')
        visit = frappe.copy_doc(frappe.get_doc('Vet Visit', template))
        for field in visit.meta.get_table_fields():
            visit.set(field.fieldname, [])
        visit.case_sheet = None
        visit.customer = self.customer
        frappe.db.set_value('Guardian', visit.guardian, 'customer_id', self.customer)
        frappe.db.set_value('Guardian', visit.guardian, 'phone', f'07{uuid.uuid4().int % 1000000000:09d}')
        visit.status = 'Draft'
        visit.branch = 'main'
        visit.sales_invoice = None
        visit.billed = 0
        visit.care_episode = None
        if medication:
            visit.append('prescribed_medications', dict(medication=medication.name, medication_item=self.stock,
                qty=2, rate=100, dose_option=medication.dose_options[0].name, dispense_status='Prescribed'))
        return visit.insert(ignore_permissions=True)

    def test_visit_dispense_then_invoice_deducts_once_with_dose_conversion(self):
        from pet_app.api.pharmacy import dispense_visit_medication
        from pet_app.utils.order_billing import plan_medication_billing, commit_order_billing
        frappe.db.set_single_value('Pet App Access Settings', 'medication_billing_trigger', 'on_visit_close')
        medication = self.medication()
        visit = self.visit(medication)
        row = visit.prescribed_medications[0]
        before = frappe.db.count('Stock Ledger Entry', {'item_code': self.stock})
        result = dispense_visit_medication(visit=visit.name, row_name=row.name, qty=2)
        self.assertTrue(result.get('ok'), result)
        visit.reload()
        row = visit.prescribed_medications[0]
        self.assertEqual(row.dispensed_qty, 2)
        self.assertEqual(row.stock_issued_qty, 0)
        self.assertFalse(row.stock_entry)
        self.assertEqual(frappe.db.count('Stock Ledger Entry', {'item_code': self.stock}), before)
        plan = plan_medication_billing(visit, row)
        billed = commit_order_billing(visit, plan)
        invoice = frappe.get_doc('Sales Invoice', billed.sales_invoice)
        self.assertEqual(invoice.items[0].qty, 2)
        self.assertEqual(invoice.items[0].conversion_factor, 0.5)
        self.assertTrue(has_marker(invoice.items[0].description, 'Vet Visit', visit.name))
        self.assert_stock_once(invoice)

    def test_visit_vaccination_uses_same_draft_as_service(self):
        from pet_app.utils.order_billing import plan_order_billing, commit_order_billing
        visit = self.visit()
        service = self.charge(self.service)
        template = frappe.get_doc('CareService template', 'CareService-00022')
        template.item_code = self.stock
        template.save()
        vaccination = frappe.get_doc(dict(doctype='PetCareService', visit=visit.name, pet_id=visit.animal_patient,
            guardian_id=visit.guardian, care_service_id=template.name, category=template.category_id,
            item_code=self.stock, price=100, branch='main', status='pending',
            pet_service_name='Test vaccination', due_date=nowdate())).insert(ignore_permissions=True)
        from pet_app.api.workspace import _update_service_status_atomic
        _update_service_status_atomic(vaccination.name, 'finish_service', {})
        vaccination.reload()
        self.assertEqual(vaccination.sales_invoice, service.name)
        invoice = frappe.get_doc('Sales Invoice', service.name)
        self.assertEqual(vaccination.sales_invoice, invoice.name)
        self.assertTrue(any(has_marker(r.description, 'PetCareService', vaccination.name) for r in invoice.items))
        self.assert_stock_once(invoice)

    def test_historical_issue_blocks_append_and_submit(self):
        medication = self.medication()
        frappe.db.set_single_value('Pet App Access Settings', 'medication_billing_trigger', 'on_visit_close')
        visit = self.visit(medication)
        invoice = self.charge(self.stock, source=('Vet Visit', visit.name))
        issue = frappe.get_doc(dict(doctype='Stock Entry', stock_entry_type='Material Issue', company=self.company,
            items=[dict(item_code=self.stock, qty=1, s_warehouse=self.warehouse)])).insert()
        issue.submit()
        frappe.db.set_value('Vet Visit Medication Item', visit.prescribed_medications[0].name,
            {'stock_issued_qty': 1, 'stock_entry': issue.name})
        frappe.db.set_value('Sales Invoice', invoice.name, 'update_stock', 0)
        with self.assertRaisesRegex(frappe.ValidationError, 'historical stock issue'):
            self.charge(self.service)
        self.assertEqual(frappe.db.get_value('Sales Invoice', invoice.name, 'update_stock'), 0)
        self.assertEqual(frappe.db.count('Sales Invoice Item', {'parent': invoice.name}), 1)
        invoice.reload()
        with self.assertRaisesRegex(frappe.ValidationError, 'historical stock issue'):
            invoice.submit()
        self.assertEqual(self.movements(invoice), [])

    def test_cancel_submission_restores_stock(self):
        invoice = self.charge(self.service)
        invoice = self.charge(self.stock)
        self.assert_stock_once(invoice)
        invoice.cancel()
        self.assertEqual(frappe.db.get_value('Bin', {'item_code': self.stock, 'warehouse': self.warehouse}, 'actual_qty'), 100)
        self.assertEqual(invoice.docstatus, 2)
        self.assertNotEqual(self.charge(self.service).name, invoice.name)

    def boarding(self, medication, included=False):
        # Reuse only the isolated site's active boarding identity/room fixture.
        name = frappe.db.get_value('Pet Boarding', {'record_status': 'Checked In'}, 'name')
        boarding = frappe.get_doc('Pet Boarding', name)
        boarding.customer = self.customer
        boarding.branch = 'main'
        boarding.sales_invoice = None
        boarding.billable_items = []
        boarding.append('billable_items', dict(item_code=self.stock, item_name=self.stock,
            item_type='Medication', qty=2, rate=100, status='Included' if included else 'Billable',
            linked_doctype='Medication', linked_name=medication.name,
            dose_option=medication.dose_options[0].name, dispense_status='Pending Dispense'))
        boarding.save(ignore_permissions=True)
        return boarding

    def test_boarding_dispense_and_billing_deduct_once(self):
        from pet_app.api.healthcare.boarding import dispense_medication, _build_sales_invoice_items
        boarding = self.boarding(self.medication())
        row = boarding.billable_items[0]
        before = frappe.db.count('Stock Ledger Entry', {'item_code': self.stock})
        result = dispense_medication(boarding=boarding.name, item_id=row.name, qty=2)
        self.assertTrue(result.get('ok'), result)
        boarding.reload()
        row = boarding.billable_items[0]
        self.assertEqual(row.stock_issued_qty, 0)
        self.assertFalse(row.stock_entry)
        self.assertEqual(frappe.db.count('Stock Ledger Entry', {'item_code': self.stock}), before)
        self.charge(self.service)
        invoice = get_or_create_open_invoice(customer=self.customer, company=self.company, branch='main',
            source_doctype='Pet Boarding', source_name=boarding.name,
            items=_build_sales_invoice_items(boarding)).invoice
        self.assertEqual(len(invoice.items), 2)
        self.assert_stock_once(invoice)

    def test_included_boarding_records_dispense_without_stock_or_extra_charge(self):
        from pet_app.api.healthcare.boarding import dispense_medication, _build_sales_invoice_items
        boarding = self.boarding(self.medication(), included=True)
        before = frappe.db.count('Stock Ledger Entry', {'item_code': self.stock})
        result = dispense_medication(boarding=boarding.name, item_id=boarding.billable_items[0].name, qty=2)
        self.assertTrue(result.get('ok'), result)
        boarding.reload()
        self.assertEqual(boarding.billable_items[0].dispensed_qty, 2)
        self.assertEqual(_build_sales_invoice_items(boarding), [])
        self.assertEqual(frappe.db.count('Stock Ledger Entry', {'item_code': self.stock}), before)
        self.assertTrue(any('no invoice stock line' in str(m) for m in frappe.local.message_log))

    def test_draft_order_cancellation_removes_only_its_line(self):
        from pet_app.utils.visit_billing import _cancel_parent_billable_row
        visit = self.visit()
        self.charge(self.stock, source=('Vet Visit', visit.name))
        invoice = self.charge(self.service, source=('Vet Visit', visit.name))
        row = frappe.get_doc(dict(doctype='Pet Billable Item', item_code=self.service, item_name=self.service,
            qty=1, rate=100, amount=100, status='Billed', sales_invoice=invoice.name))
        _cancel_parent_billable_row(visit, row, sales_invoice=None, billed=False,
            recalculate=lambda parent: None, save=False)
        invoice.reload()
        self.assertEqual([r.item_code for r in invoice.items], [self.stock])
        self.assertEqual(row.status, 'Cancelled')
        self.assertFalse(row.sales_invoice)
        self.assert_stock_once(invoice)

    def test_standard_credit_note_reverses_only_stock(self):
        from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
        self.charge(self.service)
        invoice = self.charge(self.stock)
        self.assert_stock_once(invoice)
        credit = make_sales_return(invoice.name)
        credit.insert()
        credit.submit()
        self.assertTrue(credit.is_return)
        self.assertEqual({r.item_code: r.qty for r in self.movements(credit)}, {self.stock: 1})
        self.assertEqual(frappe.db.get_value('Bin', {'item_code': self.stock, 'warehouse': self.warehouse}, 'actual_qty'), 100)

    def test_existing_old_draft_cannot_disable_stock_at_submission(self):
        invoice = self.charge(self.stock)
        frappe.db.set_value('Sales Invoice', invoice.name, 'update_stock', 0)
        invoice.reload()
        invoice.flags.from_custom_flow = False
        self.assert_stock_once(invoice)
        self.assertEqual(invoice.update_stock, 1)

    def test_customer_and_company_are_part_of_the_match(self):
        invoice = self.charge(self.service)
        self.assertIsNone(find_open_invoice(customer='_different customer', company=self.company, branch='main'))
        self.assertIsNone(find_open_invoice(customer=self.customer, company='_different company', branch='main'))
        self.assertEqual(find_open_invoice(customer=self.customer, company=self.company, branch='main'), invoice.name)

    def test_invoice_then_dispense_does_not_move_stock_again(self):
        from pet_app.api.pharmacy import dispense_visit_medication
        from pet_app.utils.order_billing import plan_medication_billing, commit_order_billing
        frappe.db.set_single_value('Pet App Access Settings', 'medication_billing_trigger', 'on_visit_close')
        medication = self.medication()
        visit = self.visit(medication)
        row = visit.prescribed_medications[0]
        billed = commit_order_billing(visit, plan_medication_billing(visit, row))
        invoice = frappe.get_doc('Sales Invoice', billed.sales_invoice)
        # Changing the master after billing must not change the invoice's stock quantity.
        frappe.db.set_value('Medication Dose Option', medication.dose_options[0].name, 'qty', 0.75)
        self.assert_stock_once(invoice)
        count = frappe.db.count('Stock Ledger Entry', {'item_code': self.stock})
        result = dispense_visit_medication(visit=visit.name, row_name=row.name, qty=2)
        self.assertTrue(result.get('ok'), result)
        self.assertEqual(frappe.db.count('Stock Ledger Entry', {'item_code': self.stock}), count)
        self.assertEqual(frappe.db.get_value('Bin', {'item_code': self.stock, 'warehouse': self.warehouse}, 'actual_qty'), 99)

    def test_visit_close_keeps_dose_conversion_and_service_on_one_invoice(self):
        from pet_app.pet_app.doctype.vet_visit.vet_visit import _create_sales_invoice_for_visit
        frappe.db.set_single_value('Pet App Access Settings', 'medication_billing_trigger', 'on_visit_close')
        visit = self.visit(self.medication())
        service = self.charge(self.service)
        result = _create_sales_invoice_for_visit(visit.name)
        invoice = result['sales_invoice']
        self.assertEqual(invoice.name, service.name)
        self.assert_stock_once(invoice)

    def test_historical_dispense_return_still_restores_recorded_stock(self):
        from pet_app.api.pharmacy import return_dispensed_medication
        frappe.db.set_single_value('Pet App Access Settings', 'medication_billing_trigger', 'on_visit_close')
        visit = self.visit(self.medication())
        row = visit.prescribed_medications[0]
        issue = frappe.get_doc(dict(doctype='Stock Entry', stock_entry_type='Material Issue', company=self.company,
            items=[dict(item_code=self.stock, qty=1, s_warehouse=self.warehouse)])).insert()
        issue.submit()
        frappe.db.set_value('Vet Visit Medication Item', row.name, {'stock_issued_qty': 1,
            'stock_entry': issue.name, 'dispensed_qty': 2, 'dispense_status': 'Dispensed', 'warehouse': self.warehouse})
        result = return_dispensed_medication(visit=visit.name, row_name=row.name, qty=2)
        self.assertTrue(result.get('ok'), result)
        self.assertEqual(frappe.db.get_value('Bin', {'item_code': self.stock, 'warehouse': self.warehouse}, 'actual_qty'), 100)
        visit.reload()
        self.assertEqual(visit.prescribed_medications[0].stock_issued_qty, 0)

    def test_standalone_vaccination_shares_service_draft_and_issues_only_at_submit(self):
        """A dose with no visit bills onto the branch's open Draft and moves no stock itself.

        Moved off PetCareService with the rest of preventive care: the service path no longer
        bills a vaccination at all, so driving it there would assert nothing.
        """
        from pet_app.api.preventive_care import update_preventive_status
        visit = self.visit()
        first = self.charge(self.service)
        template = frappe.get_doc('CareService template', 'CareService-00022')
        template.item_code = self.stock
        template.save()
        vaccination = frappe.get_doc(dict(doctype='Preventive Care Record', pet=visit.animal_patient,
            guardian=visit.guardian, care_service=template.name, branch='main',
            due_date=nowdate())).insert(ignore_permissions=True)
        before = frappe.db.count('Stock Ledger Entry', {'item_code': self.stock})
        update_preventive_status(vaccination.name, 'administer_preventive', {})
        vaccination.reload()
        self.assertEqual(vaccination.sales_invoice, first.name)
        self.assertFalse(vaccination.stock_entry)
        self.assertEqual(frappe.db.count('Stock Ledger Entry', {'item_code': self.stock}), before)
        self.assert_stock_once(frappe.get_doc('Sales Invoice', first.name))

    def test_separate_care_service_consumable_reports_conflict_without_issuing(self):
        from pet_app.utils.preventive_billing import plan_preventive_stock, commit_preventive_stock
        medication = self.medication()
        option = frappe.db.get_value('Care Service Billing Option', {}, 'name')
        frappe.db.set_value('Care Service Billing Option', option,
            {'stock_deduction_qty': 1, 'medication': medication.name})
        # Not inserted: the planner never mutates, so an unsaved document exercises it
        # exactly as a stored one would. `qty` stands in for what the controller would have
        # defaulted from the band.
        record = frappe.get_doc(dict(doctype='Preventive Care Record', name='_Test Uninvoiced Consumable',
            care_service='CareService-00022', item_code=self.service, service_option=option, qty=1))
        before = frappe.db.count('Stock Ledger Entry', {'item_code': self.stock})
        self.assertIsNone(plan_preventive_stock(record))
        commit_preventive_stock(record, frappe._dict(medication=medication.name, medication_item=self.stock, qty=1, warehouse=self.warehouse))
        self.assertEqual(frappe.db.count('Stock Ledger Entry', {'item_code': self.stock}), before)
        self.assertTrue(any('no corresponding stock item' in str(m) for m in frappe.local.message_log))
