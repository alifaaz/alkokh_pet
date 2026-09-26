"""Driver orders are not tied to any till (2026-09-19). Remove the till stamps the old
flow wrote on existing records.

- Sales Order / Sales Invoice / Stock Entry of the driver flow: `custom_pos_profile` was
  provenance only (nothing reports on it) and is cleared.
- Payment Entry of the driver flow: the stamp is what a till's summary counts, so it is
  kept ONLY where the money really went into or out of that till's cash account, and
  cleared where it did not (e.g. a customer receipt into the driver's cash that was
  stamped with the cashier's till and so showed up in that till's summary).

Direct SQL on the stamp column only: no document is saved, no ledger row changes.
"""
import frappe


def execute():
    for doctype in ("Sales Order", "Sales Invoice", "Stock Entry"):
        if frappe.db.has_column(doctype, "custom_pos_profile") and frappe.db.has_column(doctype, "custom_driver_flow"):
            frappe.db.sql(f"""update `tab{doctype}` set custom_pos_profile=NULL
                where custom_driver_flow=1 and ifnull(custom_pos_profile, '')!=''""")
    if frappe.db.has_column("Payment Entry", "custom_pos_profile") and frappe.db.has_column("Payment Entry", "custom_driver_flow"):
        frappe.db.sql("""update `tabPayment Entry` pe
            left join `tabPOS Profile` pp on pp.name=pe.custom_pos_profile
            set pe.custom_pos_profile=NULL
            where pe.custom_driver_flow=1 and ifnull(pe.custom_pos_profile, '')!=''
              and (pp.custom_cash_account is null
                   or (pe.paid_to != pp.custom_cash_account and pe.paid_from != pp.custom_cash_account))""")
