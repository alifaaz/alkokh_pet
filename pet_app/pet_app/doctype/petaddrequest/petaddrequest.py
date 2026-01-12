# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

from frappe.model.document import Document
import frappe
from frappe.utils import now_datetime

import frappe
from frappe.model.document import Document

class PetAddRequest(Document):

    def on_update(self):
        # نشتغل فقط إذا تحولت من Pending إلى Approved
        if self.status != "Approved":
            return

        if self.get_doc_before_save() and self.get_doc_before_save().status == "Approved":
            return

        # 1️⃣ تحديث حالة الحيوان
        frappe.db.set_value(
            "Pet",
            self.pet_id,
            "pet_status",
            "Approved"
        )

        # 2️⃣ إنشاء PetGuardian إذا مو موجود
        if not frappe.db.exists(
            "PetGuardian",
            {
                "pet_id": self.pet_id,
                "guardian_id": self.guardian_id
            }
        ):
            frappe.get_doc({
                "doctype": "PetGuardian",
                "pet_id": self.pet_id,
                "guardian_id": self.guardian_id,
                "role": "primary_owner"
            }).insert(ignore_permissions=True)
