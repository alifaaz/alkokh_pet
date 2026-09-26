"""Checkout contract and ledger tests on the isolated database only (rollback per test)."""
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate, flt

from pet_app.api.healthcare import boarding as api
from pet_app.tests.test_boarding_payments import BoardingPaymentBase
from pet_app.utils.boarding_discount import apply_accommodation_discount, distribute_discount
from pet_app.utils.invoice_source import append_marker, has_marker


class TestBoardingDiscount(unittest.TestCase):
    _free_pet = BoardingPaymentBase._free_pet
    _free_room = BoardingPaymentBase._free_room
    _open_stay = BoardingPaymentBase._open_stay

    def setUp(self):
        assert str(frappe.conf.db_name).startswith('test_driver_orders_'), 'Isolated database required'
        frappe.set_user('Administrator')
        frappe.flags.in_test = True
        self.check_in = add_days(nowdate(), -7)
        self.pet, self.guardian = self._free_pet()
        self.room = self._free_room()
        self.stay = self._open_stay()
        doc = frappe.get_doc('Pet Boarding',self.stay)
        doc.billable_items[0].rate = 25000
        doc.save()

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_cache()

    def checkout(self, discount=None):
        result = api.check_out_boarding(self.stay, discount=discount, checkout_notes='Collected')
        self.assertTrue(result['ok'], result)
        return result['data']

    def invoice(self, data):
        return frappe.get_doc('Sales Invoice', data['sales_invoice'])

    def add_charge(self, kind='Service', rate=12345):
        doc = frappe.get_doc('Pet Boarding', self.stay)
        row = doc.append('billable_items', dict(item_code=doc.billable_items[0].item_code,
            item_type=kind, rate=rate, qty=1, status='Billable', item_name='Same item, another charge'))
        doc.save()
        return row.name

    def pay(self, amount):
        # Real submitted Payment Entry; only till lookup is isolated from the acting user's setup.
        account = frappe.db.get_single_value('Pet App Accounting Settings', 'treasury_cash_account')
        with patch.object(api, '_resolve_boarding_deposit_account', return_value=frappe._dict(account=account, profile=None)):
            result = api.record_boarding_payment(self.stay, amount=amount)
        self.assertTrue(result['ok'], result)

    def test_undiscounted_detail_and_retry(self):
        before = api.get_boarding_detail(boarding_id=self.stay)['data']
        self.assertIs(before['capabilities']['boarding_discount_v1'], True)
        self.assertIsNone(before['discount'])
        self.assertIsNone(before['accommodation_subtotal'])
        data = self.checkout()
        self.assertIsNone(data['discount'])
        self.assertEqual(data['accommodation_discount_amount'], 0)
        self.assertEqual(self.invoice(data).grand_total, data['accommodation_subtotal'])
        self.assert_saved_and_repeat(data)

    def assert_saved_and_repeat(self, data):
        detail = api.get_boarding_detail(boarding_id=self.stay)['data']
        repeated = self.checkout(data['discount'])
        for key in ('discount', 'accommodation_subtotal', 'accommodation_discount_amount', 'accommodation_total'):
            self.assertEqual(detail[key], data[key], key)
            self.assertEqual(repeated[key], data[key], key)
        self.assertEqual(repeated['sales_invoice'], data['sales_invoice'])
        self.assertEqual(repeated['deposit_allocated'], data['deposit_allocated'])
        saved = frappe.get_doc('Pet Boarding', self.stay)
        self.assertEqual(saved.discount_recorded_by, 'Administrator')
        self.assertEqual(saved.discount_recorded_at, saved.check_out)
        self.assertEqual(saved.check_out_note, 'Collected')

    def test_amount_rounding_survives_save_and_submit(self):
        data = self.checkout(json.dumps({'type':'amount', 'value':10000.01}))
        self.assertEqual(data['accommodation_discount_amount'], 10000.01)
        invoice = self.invoice(data)
        self.assertEqual(invoice.docstatus, 1)
        self.assertEqual(invoice.is_pos, 0)
        self.assertEqual(invoice.discount_amount, 10000.01)
        self.assertEqual(flt(sum(r.net_amount for r in invoice.items), 2), data['accommodation_total'])
        self.assertEqual(len(invoice.items),1)
        self.assertEqual(invoice.items[0].qty,8)
        self.assertEqual(invoice.items[0].rate,25000)
        self.assertEqual(invoice.items[0].amount,200000)
        self.assertEqual(invoice.items[0].discount_amount,0)
        self.assertEqual(invoice.items[0].distributed_discount_amount,10000.01)
        self.assertEqual(invoice.total,200000)
        self.assert_saved_and_repeat(data)
        bad = api.check_out_boarding(self.stay, discount={'type':'amount','value':1})
        self.assertFalse(bad['ok'])
        self.assertEqual(self.checkout()['sales_invoice'], invoice.name)

    def test_percentage_covers_services_and_medications(self):
        service = self.add_charge()
        medication = self.add_charge('Medication', 555)
        data = self.checkout({'type':'percentage', 'value':12.5})
        invoice = self.invoice(data)
        self.assertEqual(data['accommodation_subtotal'], 200000 + 12345 + 555)
        for source, gross in [(service,12345),(medication,555)]:
            rows = [r for r in invoice.items if has_marker(r.description,'Pet Billable Item',source)]
            self.assertEqual(sum(r.amount for r in rows), gross)
            self.assertLess(sum(r.net_amount for r in rows), gross)
        self.assertAlmostEqual(data['accommodation_discount_amount'], data['accommodation_subtotal'] * .125, places=2)
        self.assert_saved_and_repeat(data)

    def test_invalid_requests_leave_everything_open(self):
        before = frappe.get_doc('Pet Boarding', self.stay).as_dict()
        count = frappe.db.count('Sales Invoice')
        bad_values = [-1, '10', True, None, float('nan'), float('inf'), float('-inf')]
        requests = [{'type':'amount','value':v} for v in bad_values] + [
            {'type':'percentage','value':101}, {'type':'amount','value':999999999},
            {'type':'fixed','value':1}, {}, [], 'bad json', {'type':[], 'value':1}]
        for discount in requests:
            with self.subTest(discount=discount):
                result = api.check_out_boarding(self.stay, discount=discount)
                self.assertFalse(result['ok'], result)
                doc = frappe.get_doc('Pet Boarding', self.stay)
                self.assertEqual(doc.record_status, 'Checked In')
                self.assertEqual(doc.check_out, before.check_out)
                self.assertFalse(doc.sales_invoice)
                self.assertFalse(doc.discount_recorded_at)
                self.assertEqual([r.status for r in doc.occupants], [r.status for r in before.occupants])
                self.assertEqual(frappe.db.count('Sales Invoice'), count)

    def test_failure_after_invoice_submission_rolls_back_ledger_and_departure(self):
        self.pay(50000)
        count = frappe.db.count('Sales Invoice')
        ledger = frappe.db.count('GL Entry')
        original = api._submit_checkout_invoice
        def fail_after_submit(invoice):
            original(invoice)
            frappe.throw('Injected post-submit validation failure')
        with patch.object(api, '_submit_checkout_invoice', side_effect=fail_after_submit):
            result = api.check_out_boarding(self.stay, discount={'type':'amount','value':10000})
        self.assertFalse(result['ok'])
        doc = frappe.get_doc('Pet Boarding', self.stay)
        self.assertEqual(doc.record_status, 'Checked In')
        self.assertTrue(all(r.status == 'Active' for r in doc.occupants))
        self.assertEqual(frappe.db.count('Sales Invoice'), count)
        self.assertEqual(frappe.db.count('GL Entry'), ledger)
        self.assertEqual(sum(r.unallocated_amount for r in api.boarding_payment_entries(self.stay,doc.customer)), 50000)
        self.checkout({'type':'amount','value':10000})

    def test_full_discount_no_invoice_and_credit(self):
        self.pay(50000)
        data = self.checkout({'type':'percentage','value':100})
        self.assertIsNone(data['sales_invoice'])
        self.assertEqual(data['accommodation_total'], 0)
        self.assertEqual(data['deposit_unallocated_remainder'], 50000)
        self.assert_saved_and_repeat(data)

    def test_full_discount_covers_other_charges(self):
        self.add_charge(rate=25000)
        data = self.checkout({'type':'percentage','value':100})
        self.assertEqual(data['accommodation_subtotal'], 225000)
        self.assertEqual(data['accommodation_discount_amount'], 225000)
        self.assertEqual(data['accommodation_total'], 0)

    def test_amount_capped_by_whole_bill_not_room_charges(self):
        service = self.add_charge(rate=50000)
        refused = api.check_out_boarding(self.stay, discount={'type':'amount','value':250001})
        self.assertFalse(refused['ok'], refused)
        self.assertEqual(frappe.db.get_value('Pet Boarding', self.stay, 'record_status'), 'Checked In')
        data = self.checkout({'type':'amount','value':220000})
        invoice = self.invoice(data)
        self.assertEqual(invoice.discount_amount, 220000)
        self.assertEqual(invoice.grand_total, 30000)
        self.assertLess(sum(r.net_amount for r in invoice.items if has_marker(r.description,'Pet Billable Item',service)), 50000)

    def test_original_zero_cost(self):
        doc = frappe.get_doc('Pet Boarding', self.stay)
        doc.set('billable_items', [])
        doc.save()
        data = self.checkout()
        self.assertIsNone(data['sales_invoice'])
        self.assertEqual(data['accommodation_subtotal'], 0)
        self.assert_saved_and_repeat(data)

    def test_deposits_below_equal_above_discounted_total(self):
        for paid, outstanding, credit in [(50000,50000,0),(100000,0,0),(200000,0,100000)]:
            with self.subTest(paid=paid):
                frappe.db.savepoint('deposit_case')
                self.pay(paid / 2)
                self.pay(paid / 2)
                data = self.checkout({'type':'amount','value':100000})
                invoice = self.invoice(data)
                self.assertEqual(invoice.outstanding_amount, outstanding)
                self.assertEqual(data['deposit_unallocated_remainder'], credit)
                self.assertEqual(data['deposit_allocated'], min(paid,100000))
                refs = frappe.get_all('Payment Entry Reference', filters={'reference_name':invoice.name}, fields=['allocated_amount'])
                self.assertEqual(sum(r.allocated_amount for r in refs), min(paid,100000))
                self.assert_saved_and_repeat(data)
                frappe.db.rollback(save_point='deposit_case')

    def test_departed_pet_keeps_its_own_duration_and_is_discounted(self):
        doc = frappe.get_doc('Pet Boarding', self.stay)
        first = doc.occupants[0]
        # Two admissions of the same pet: first departed earlier, second still here.
        first.status = 'Departed'
        first.departed_at = add_days(nowdate(), -5)
        doc.append('occupants', dict(pet=self.pet, boarding_type='Travel', status='Active',
            service_room=self.room, joined_at=add_days(nowdate(),-2)))
        api._ensure_room_stay_billable_item(doc)
        doc.billable_items[0].rate = 10000
        doc.billable_items[1].rate = 20000
        doc.save()
        data = self.checkout({'type':'percentage','value':10})
        saved = frappe.get_doc('Pet Boarding', self.stay)
        self.assertEqual(saved.billable_items[0].qty, 2)
        self.assertEqual(saved.billable_items[0].rate, 10000)
        self.assertEqual(data['accommodation_subtotal'], sum(r.amount for r in saved.billable_items))
        invoice = self.invoice(data)
        for row in saved.billable_items:
            self.assertEqual(sum(r.net_amount for r in invoice.items if has_marker(r.description,'Pet Billable Item',row.name)), row.amount * .9)

    def test_shared_invoice_and_tax_rules(self):
        doc = frappe.get_doc('Pet Boarding', self.stay)
        items = api._build_sales_invoice_items(doc)
        invoice = api.get_or_create_open_invoice(customer=doc.customer,items=items,source_doctype='Pet Boarding',
            source_name=doc.name,branch=doc.branch,branch_authorised=True,force_new=True,ignore_pricing_rule=1).invoice
        other = invoice.append('items', dict(item_code=doc.billable_items[0].item_code,qty=1,rate=333,
            description=append_marker('Another booking','Pet Boarding','another-booking')))
        account = frappe.get_cached_value('Company',invoice.company,'default_income_account')
        invoice.append('taxes',dict(charge_type='On Net Total',account_head=account,rate=10,description='Test tax'))
        invoice.save()
        before = other.amount
        apply_accommodation_discount(doc,invoice,{'type':'amount','value':10000.01},invoice.precision('grand_total'))
        invoice.submit(); invoice.reload()
        self.assertEqual(next(r.amount for r in invoice.items if has_marker(r.description,'Pet Boarding','another-booking')),before)
        self.assertEqual(next(r.net_amount for r in invoice.items if has_marker(r.description,'Pet Boarding','another-booking')),before)
        self.assertEqual(invoice.total_taxes_and_charges, flt(invoice.net_total * .1,2))
        self.assertEqual(invoice.discount_amount,10000.01)

    def test_two_pets_individual_departure_does_not_discount(self):
        frappe.db.set_single_value('Pet Boarding Settings', 'max_pets_per_booking', 3)
        second_pet, _ = self._free_pet()
        if not frappe.db.exists('PetGuardian', {'pet_id':second_pet,'guardian_id':self.guardian}):
            frappe.get_doc(dict(doctype='PetGuardian',pet_id=second_pet,guardian_id=self.guardian)).insert()
        doc = frappe.get_doc('Pet Boarding',self.stay)
        doc.append('occupants', dict(pet=second_pet,boarding_type='Travel',status='Active',
            service_room=self.room,joined_at=add_days(nowdate(),-4)))
        api._ensure_room_stay_billable_item(doc)
        doc.billable_items[1].rate = 15000
        doc.save()
        result = api.depart_occupant(boarding=self.stay,pet=second_pet,departed_at=add_days(nowdate(),-2))
        self.assertTrue(result['ok'],result)
        self.assertIsNone(api.get_boarding_detail(boarding_id=self.stay)['data']['accommodation_discount_amount'])
        data = self.checkout({'type':'amount','value':10000.01})
        saved = frappe.get_doc('Pet Boarding',self.stay)
        row = next(r for r in saved.billable_items if r.pet == second_pet)
        self.assertEqual(row.qty,2)
        self.assertEqual(row.rate,15000)
        invoice = self.invoice(data)
        self.assertLess(sum(r.net_amount for r in invoice.items if has_marker(r.description,'Pet Billable Item',row.name)),30000)
        self.assertEqual(flt(sum(r.net_amount for r in invoice.items),2),data['accommodation_total'])

    def test_cancelled_and_included_room_charges_are_not_discounted(self):
        doc = frappe.get_doc('Pet Boarding',self.stay)
        for status in ('Cancelled','Included'):
            doc.append('billable_items',dict(item_type='Room Stay',item_code=doc.billable_items[0].item_code,
                qty=1,rate=99999,status=status))
        doc.save()
        data = self.checkout({'type':'percentage','value':10})
        self.assertEqual(data['accommodation_subtotal'],200000)
        self.assertEqual(data['accommodation_discount_amount'],20000)

    def test_permissions_still_enforced(self):
        frappe.set_user('Guest')
        try:
            result = api.check_out_boarding(self.stay,discount={'type':'amount','value':1})
            self.assertFalse(result['ok'])
        finally:
            frappe.set_user('Administrator')
        self.assertEqual(frappe.db.get_value('Pet Boarding',self.stay,'record_status'),'Checked In')

    def test_zero_request_is_saved_without_reduction(self):
        data = self.checkout({'type':'amount','value':0})
        self.assertEqual(data['accommodation_subtotal'],data['accommodation_total'])
        self.assert_saved_and_repeat(data)

    def test_invoice_currency_precision_zero_and_three(self):
        for precision, amount in [(0,10001),(3,10000.001)]:
            with self.subTest(precision=precision):
                frappe.db.savepoint('currency_case')
                try:
                    frappe.db.set_default('currency_precision',str(precision))
                    if precision == 0:
                        frappe.db.set_single_value('System Settings','use_number_format_from_currency',1)
                        frappe.db.set_value('Currency','IQD','number_format','#,###')
                    frappe.clear_cache()
                    data = self.checkout({'type':'amount','value':amount})
                    invoice = self.invoice(data)
                    self.assertEqual(invoice.precision('grand_total'),precision)
                    self.assertEqual(data['accommodation_discount_amount'],amount)
                    self.assertEqual(flt(sum(r.net_amount for r in invoice.items),precision),data['accommodation_total'])
                    self.assert_saved_and_repeat(data)
                finally:
                    frappe.db.rollback(save_point='currency_case')
                    frappe.clear_cache()

    def test_missing_schema_disables_capability_and_discounted_checkout(self):
        with patch.object(api,'_boarding_discount_available',return_value=False):
            detail = api.get_boarding_detail(boarding_id=self.stay)['data']
            self.assertIs(detail['capabilities']['boarding_discount_v1'],False)
            result = api.check_out_boarding(self.stay,discount={'type':'amount','value':1})
            self.assertFalse(result['ok'])
            self.assertEqual(frappe.db.get_value('Pet Boarding',self.stay,'record_status'),'Checked In')
            data = self.checkout()
            self.assertIsNone(data['accommodation_subtotal'])

    def test_reported_invoice_shape_three_nights_and_visible_discount(self):
        doc = frappe.get_doc('Pet Boarding',self.stay)
        doc.check_in = add_days(nowdate(),-2)
        doc.occupants[0].joined_at = doc.check_in
        doc.billable_items[0].rate = 15000
        api._ensure_room_stay_billable_item(doc)
        doc.save()
        self.pay(5000)
        self.pay(10000)
        data = self.checkout({'type':'amount','value':5000})
        invoice = self.invoice(data)
        self.assertEqual(len(invoice.items),1)
        row = invoice.items[0]
        self.assertEqual((row.qty,row.rate,row.amount),(3,15000,45000))
        self.assertEqual(invoice.total,45000)
        self.assertEqual(invoice.discount_amount,5000)
        self.assertEqual(invoice.grand_total,40000)
        self.assertEqual(invoice.outstanding_amount,25000)
        self.assertEqual(invoice.total_advance,15000)
        self.assert_saved_and_repeat(data)

    def test_service_only_return_gives_back_its_own_discount_share(self):
        from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
        service = self.add_charge()
        data = self.checkout({'type':'amount','value':10000.01})
        original = next(r for r in self.invoice(data).items if has_marker(r.description,'Pet Billable Item',service))
        self.assertGreater(original.distributed_discount_amount, 0)
        returned = make_sales_return(data['sales_invoice'])
        returned.set('items',[row for row in returned.items if has_marker(row.description,'Pet Billable Item',service)])
        returned.flags.ignore_permissions = True
        returned.insert(); returned.submit(); returned.reload()
        self.assertEqual(returned.discount_amount,-original.distributed_discount_amount)
        self.assertEqual(returned.grand_total,-original.net_amount)
        self.assertEqual(returned.items[0].rate,12345)
        self.assertEqual(returned.items[0].net_amount,-original.net_amount)

    def test_full_return_reverses_discount_once(self):
        from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
        data = self.checkout({'type':'amount','value':10000.01})
        returned = make_sales_return(data['sales_invoice'])
        returned.flags.ignore_permissions = True
        returned.insert(); returned.submit(); returned.reload()
        self.assertEqual(returned.discount_amount,-10000.01)
        self.assertEqual(returned.grand_total,-data['invoice_grand_total'])
        self.assertEqual(returned.items[0].rate,25000)
        self.assertEqual(returned.items[0].qty,-8)

    def test_inclusive_tax_preserves_rates_and_uses_net_subtotal(self):
        doc = frappe.get_doc('Pet Boarding',self.stay)
        invoice = api.get_or_create_open_invoice(customer=doc.customer,
            items=api._build_sales_invoice_items(doc),source_doctype='Pet Boarding',source_name=doc.name,
            branch=doc.branch,branch_authorised=True,force_new=True,ignore_pricing_rule=1).invoice
        account = frappe.get_cached_value('Company',invoice.company,'default_income_account')
        invoice.append('taxes',dict(charge_type='On Net Total',account_head=account,rate=10,
            included_in_print_rate=1,description='Inclusive tax'))
        invoice.save()
        subtotal = invoice.net_total
        apply_accommodation_discount(doc,invoice,{'type':'amount','value':5000},invoice.precision('discount_amount'))
        invoice.submit(); invoice.reload()
        self.assertEqual(invoice.items[0].rate,25000)
        self.assertEqual(invoice.items[0].amount,200000)
        self.assertEqual(doc.accommodation_subtotal,subtotal)
        self.assertEqual(invoice.discount_amount,5000)
        self.assertEqual(invoice.net_total,flt(subtotal-5000,2))
        self.assertEqual(invoice.total_taxes_and_charges,flt(invoice.net_total * .1,2))

    def test_currency_rounding_and_bounded_final_adjustment(self):
        for precision in (0,2,3):
            for amounts in ([1,1,1],[.01,.01,.01],[3,7,19],[0,3,7]):
                rounded = [flt(v,precision) for v in amounts]
                for percentage in (0,50,99.999,100):
                    shares,target = distribute_discount(rounded,{'type':'percentage','value':percentage},precision)
                    self.assertEqual(sum(shares),target)
                    self.assertTrue(all(0 <= s <= Decimal(str(a)) for s,a in zip(shares,rounded)))
