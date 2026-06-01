# Copyright (c) 2025, solvers and contributors
from __future__ import annotations
import frappe
from frappe import _
from frappe.utils import flt
from frappe.model.document import Document


class CareServicetemplate(Document):

    def validate(self):
        self.service_name = (self.service_name or "").strip()
        if not self.service_name:
            frappe.throw(_("Service Name is required."))

        if not self.price_list:
            self.price_list = "Clinic"

        self._validate_active_category()
        self._validate_duplicate_service()
        self._ensure_item()  # خلق Item تلقائي بدل الرفض

    def after_insert(self):
        self._ensure_item_price()

    def on_update(self):
        self._ensure_item_price()

    # ─────────────────────────────────────────

    def _ensure_item(self):
        if self.item_code and frappe.db.exists("Item", self.item_code):
            item = frappe.get_doc("Item", self.item_code)
            if item.item_name != self.service_name:
                item.item_name = self.service_name
                item.flags.ignore_permissions = True
                item.save()
            return

        # تحديد Item Group
        if frappe.db.exists("Item Group", "Veterinary Services"):
            item_group = "Veterinary Services"
        elif frappe.db.exists("Item Group", "Services"):
            item_group = "Services"
        else:
            frappe.throw(_("Create an Item Group named 'Services' or 'Veterinary Services' first."))

        item = frappe.new_doc("Item")
        item.item_code    = self.service_name
        item.item_name    = self.service_name
        item.item_group   = item_group
        item.stock_uom    = "Nos"
        item.is_stock_item = 0

        item.flags.ignore_permissions = True
        item.insert()

        self.item_code = item.name

    def _ensure_item_price(self):
        rate       = flt(self.default_price)
        item_code  = self.item_code
        price_list = self.price_list or "Clinic"
        currency   = frappe.defaults.get_global_default("currency") or "IQD"

        if rate <= 0 or not item_code:
            return

        filters = {"item_code": item_code, "price_list": price_list, "selling": 1}
        existing_prices = frappe.get_all("Item Price", filters=filters, pluck="name")

        if len(existing_prices) > 1:
            return

        existing = existing_prices[0] if existing_prices else None
        ip = frappe.get_doc("Item Price", existing) if existing else frappe.new_doc("Item Price")

        if not existing:
            ip.item_code  = item_code
            ip.price_list = price_list
            ip.selling    = 1

        ip.price_list_rate = rate
        ip.currency        = currency

        ip.flags.ignore_permissions = True
        ip.save() if existing else ip.insert()

    def _validate_active_category(self):
        if not self.category_id:
            return
        category = frappe.db.get_value("CategoryCareServices", self.category_id, ["name", "active"], as_dict=True)
        if not category:
            frappe.throw(_("Category {0} was not found.").format(frappe.bold(self.category_id)))
        if category.get("active") == 0:
            frappe.throw(_("Category {0} is inactive.").format(frappe.bold(self.category_id)))

    def _validate_duplicate_service(self):
        filters = {
            "service_name": self.service_name,
            "animal_species": self.animal_species,
            "category_id": self.category_id,
            "name": ["!=", self.name],
        }
        if frappe.db.exists("CareService template", filters):
            frappe.throw(_("A Care Service template already exists for this service, species, and category."))
