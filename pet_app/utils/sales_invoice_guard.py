import frappe
from frappe import _


def before_insert(doc, method=None):
    if frappe.session.user == "Administrator":
        return

    if getattr(doc, "from_custom_flow", False) or getattr(doc.flags, "from_custom_flow", False):
        return

    frappe.throw(_("Direct Sales Invoice creation is not allowed."), frappe.PermissionError)
