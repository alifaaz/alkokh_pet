# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

from frappe.model.document import Document
import frappe
from pet_app.pet_app.doctype.pet_medical_profile.pet_medical_profile import ensure_pet_medical_profile

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

        # 3️⃣ إنشاء الملف الطبي للحيوان (مرة وحدة فقط)
        ensure_pet_medical_profile(self.pet_id, self.guardian_id)
