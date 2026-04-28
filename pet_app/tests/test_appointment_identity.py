# Copyright (c) 2026, solvers and Contributors
# See license.txt

from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe.tests import IntegrationTestCase


class TestAppointmentIdentity(IntegrationTestCase):
    def test_appointment_links_guardian_and_customer_from_phone(self):
        phone = "07123456789"

        guardian = frappe.get_doc(
            {
                "doctype": "Guardian",
                "phone": phone,
                "full_name": "Appointment Guardian",
                "email_id": "appointment.guardian@example.com",
            }
        ).insert(ignore_permissions=True)

        appointment = frappe.get_doc(
            {
                "doctype": "Appointment",
                "status": "Open",
                "customer_name": "Appointment Guardian",
                "customer_phone_number": phone,
                "customer_email": "appointment.guardian@example.com",
                "scheduled_time": datetime.now() + timedelta(hours=1),
            }
        ).insert(ignore_permissions=True)

        guardian.reload()
        appointment.reload()

        self.assertTrue(guardian.customer_id)
        self.assertEqual(appointment.custom_gurdian, guardian.name)
        self.assertEqual(appointment.custom_customer, guardian.customer_id)
        self.assertEqual(appointment.appointment_with, "Customer")
        self.assertEqual(appointment.party, guardian.customer_id)
