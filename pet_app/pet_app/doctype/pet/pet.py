# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class Pet(Document):

    def after_insert(self):
        """
        Guardian adds Pet → auto create PetAddRequest (Pending)
        Admin adds Pet → do nothing
        """

        # 1️⃣ إذا الإضافة من Administrator → لا Request
        if frappe.session.user == "Administrator":
            return

        # 2️⃣ جيب Guardian المرتبط بالـ User الحالي
        guardian_id = frappe.db.get_value(
            "Guardian",
            {"user_id": frappe.session.user},
            "name"
        )

        # إذا ماكو Guardian مرتبط → وقف
        if not guardian_id:
            frappe.log_error(
                title="Pet Creation Error",
                message=f"No Guardian linked to user {frappe.session.user}"
            )
            return

        # 3️⃣ خزّن requested_by تلقائي
        frappe.db.set_value(
            "Pet",
            self.name,
            "requested_by",
            guardian_id
        )

        # 4️⃣ أنشئ PetAddRequest (Pending)
        frappe.get_doc({
            "doctype": "PetAddRequest",
            "pet_id": self.name,
            "guardian_id": guardian_id,
            "status": "Pending",
            "requested_at": now_datetime()
        }).insert(ignore_permissions=True)

        frappe.logger().info(
            f"Pet {self.name} added by Guardian {guardian_id} → PetAddRequest created"
        )
