# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from pet_app.utils.guardian_customer import sync_customer_from_guardian, validate_guardian_customer_mapping


class Guardian(Document):
    def validate(self):
        validate_guardian_customer_mapping(self)

    def on_update(self):
        sync_customer_from_guardian(self)
