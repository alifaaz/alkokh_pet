# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from pet_app.utils.guardian_customer import (
    ensure_customer_for_web_admin_guardian,
    sync_customer_from_guardian,
    validate_guardian_customer_mapping,
)


class Guardian(Document):
    def validate(self):
        validate_guardian_customer_mapping(self)

    def after_insert(self):
        ensure_customer_for_web_admin_guardian(self)

    def on_update(self):
        sync_customer_from_guardian(self)
