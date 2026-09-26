"""Back-post delivery fees earned before fees were booked. NOT in patches.txt; run by hand:

    bench --site frappe.localhost execute pet_app.patches.driver_historic_fees.execute
    bench --site frappe.localhost execute pet_app.patches.driver_historic_fees.execute --kwargs "{'commit': 1}"

The first form is a dry run: it lists what it would post and writes nothing.

Before `driver_fee_accounting`, a delivery fee never entered the books (the old
"direct fee" rule). The customer paid it to the driver and the driver kept it. So the fee
was collected and paid out at the same moment, and booking it now must not move any
balance. Per invoice it posts only the P&L:

    Dr fee expense (624200) / Cr fee income (708500)    amount = custom_direct_delivery_fee

It never touches the driver's cash or fee account (the driver owes nothing and is owed
nothing for these), never touches the invoice, and runs idempotently: an invoice that
already has a "Historic Fee" entry is skipped.
"""
import frappe
from frappe.utils import cint, flt

from pet_app.utils.driver_orders import fee_config

KIND = "Historic Fee"


def candidates():
    return frappe.db.sql("""select si.name, si.posting_date, si.company, si.branch, si.cost_center,
            si.custom_driver, si.custom_direct_delivery_fee as fee
        from `tabSales Invoice` si
        where si.docstatus=1 and si.custom_driver_flow=1 and si.is_return=0
          and coalesce(si.custom_direct_delivery_fee, 0) > 0
          and not exists (select 1 from `tabJournal Entry` je where je.docstatus=1
              and je.custom_driver_invoice=si.name and je.custom_driver_journal_kind in ('Historic Fee', 'Fee'))
        order by si.posting_date, si.name""", as_dict=True)


def execute(commit=0):
    fees = fee_config()
    if not fees or not frappe.get_meta("Journal Entry").has_field("custom_driver_journal_kind"):
        print("Fee accounting is not installed/configured yet; run bench migrate first.")
        return
    rows = candidates()
    print(f"{len(rows)} invoice(s), total {sum(flt(r.fee) for r in rows)}:")
    for r in rows:
        print(f"  {r.name}  {r.posting_date}  {r.custom_driver}  {r.branch}  fee {r.fee}")
    if not cint(commit):
        print("Dry run: nothing posted. Re-run with --kwargs \"{'commit': 1}\" to post.")
        return
    posted = []
    for r in rows:
        cost_center = r.cost_center or frappe.get_cached_value("Company", r.company, "cost_center")
        accounts = [{"account": fees.expense, "debit_in_account_currency": flt(r.fee), "cost_center": cost_center, "branch": r.branch},
            {"account": fees.income, "credit_in_account_currency": flt(r.fee), "cost_center": cost_center, "branch": r.branch}]
        je = frappe.get_doc({"doctype": "Journal Entry", "voucher_type": "Journal Entry", "company": r.company,
            "posting_date": r.posting_date, "accounts": accounts,
            "user_remark": f"Historic delivery fee on {r.name}: collected and kept by driver {r.custom_driver} before fees were booked",
            "custom_driver": r.custom_driver, "custom_driver_invoice": r.name, "custom_driver_journal_kind": KIND})
        je.flags.ignore_permissions = True
        je.insert(ignore_permissions=True)
        je.submit()
        posted.append(je.name)
    frappe.db.commit()
    print(f"Posted {len(posted)}: {', '.join(posted)}")
