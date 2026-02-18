# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

from frappe.model.document import Document
import frappe
from pet_app.utils.patient_linking import get_or_create_patient_for_pet  # ✅ add this

class PetAddRequest(Document):

    def on_update(self):
        # نشتغل فقط إذا تحولت من Pending إلى Approved
        if self.status != "Approved":
            return

        before = self.get_doc_before_save()
        if before and before.status == "Approved":
            return

        # 1️⃣ تحديث حالة الحيوان
        frappe.db.set_value("Pet", self.pet_id, "pet_status", "Approved")

        # 2️⃣ إنشاء PetGuardian إذا مو موجود
        if not frappe.db.exists("PetGuardian", {"pet_id": self.pet_id, "guardian_id": self.guardian_id}):
            frappe.get_doc({
                "doctype": "PetGuardian",
                "pet_id": self.pet_id,
                "guardian_id": self.guardian_id,
                "role": "primary_owner"
            }).insert(ignore_permissions=True)

        # 3️⃣ ✅ إنشاء/ربط Patient للحيوان (مرة وحدة فقط)
        get_or_create_patient_for_pet(self.pet_id, self.guardian_id)
