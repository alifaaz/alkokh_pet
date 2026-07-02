"""Tests for the admin user performance profile dashboard."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api import user_performance


PREFIX = "User Performance Test"
OLD_DATE = "2020-01-01 10:00:00"
RECENT_DATE = "2025-06-01 10:00:00"


class TestUserPerformanceDashboard(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._original_user = frappe.session.user
        frappe.set_user("Administrator")
        cls._cleanup_existing()

        cls.doctor_user = cls._make_user("doctor")
        cls.provider_user = cls._make_user("provider")
        cls.outsider_user = cls._make_user("outsider")
        cls.doctor_practitioner = cls._make_practitioner(cls.doctor_user, "Doctor")
        cls.provider_practitioner = cls._make_practitioner(cls.provider_user, "Service Provider")
        cls.category = cls._make_category()

        cls.open_visit = cls._make_visit(cls.doctor_practitioner, "Draft", RECENT_DATE, owner=cls.doctor_user)
        cls.completed_visit = cls._make_visit(
            cls.doctor_practitioner, "Completed", OLD_DATE, owner=cls.doctor_user
        )
        cls.cancelled_visit = cls._make_visit(
            cls.doctor_practitioner, "Cancelled", RECENT_DATE, owner=cls.doctor_user
        )

        cls.pending_service = cls._make_service("pending", RECENT_DATE)
        cls.completed_service_recent = cls._make_service("Completed", RECENT_DATE, completed=True)
        cls.completed_service_old = cls._make_service("Completed", OLD_DATE, completed=True)
        cls.cancelled_service = cls._make_service("Cancelled", RECENT_DATE)

        cls.received_recent = cls._rate(
            rated_by="Administrator",
            performer_id=cls.doctor_practitioner,
            overall_rating=5,
            rated_at=RECENT_DATE,
            reference_doctype="Vet Visit",
            reference_name=cls.open_visit,
        )
        cls.received_old = cls._rate(
            rated_by="Administrator",
            performer_id=cls.doctor_practitioner,
            overall_rating=3,
            rated_at=OLD_DATE,
        )
        cls.given_recent = cls._rate(
            rated_by=cls.doctor_user,
            performer_id="EXTERNAL-HCP",
            overall_rating=4,
            rated_at=RECENT_DATE,
            owner=cls.doctor_user,
        )
        cls.given_old = cls._rate(
            rated_by=cls.doctor_user,
            performer_id="EXTERNAL-HCP",
            overall_rating=2,
            rated_at=OLD_DATE,
            owner=cls.doctor_user,
        )
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        for dt, names in (
            ("Rating", [cls.received_recent, cls.received_old, cls.given_recent, cls.given_old]),
            (
                "PetCareService",
                [
                    cls.pending_service,
                    cls.completed_service_recent,
                    cls.completed_service_old,
                    cls.cancelled_service,
                ],
            ),
            ("Vet Visit", [cls.open_visit, cls.completed_visit, cls.cancelled_visit]),
        ):
            for name in names:
                if frappe.db.exists(dt, name):
                    frappe.delete_doc(dt, name, force=True, ignore_permissions=True)

        for name in frappe.get_all(
            "Rating Questionnaire",
            filters={"questionnaire_name": ["like", f"{PREFIX}%"]},
            pluck="name",
        ):
            frappe.delete_doc("Rating Questionnaire", name, force=True, ignore_permissions=True)

        for practitioner in (cls.doctor_practitioner, cls.provider_practitioner):
            if frappe.db.exists("Healthcare Practitioner", practitioner):
                frappe.delete_doc("Healthcare Practitioner", practitioner, force=True, ignore_permissions=True)
        if frappe.db.exists("CategoryCareServices", cls.category):
            frappe.delete_doc("CategoryCareServices", cls.category, force=True, ignore_permissions=True)

        frappe.db.delete("Notification Log", {"for_user": ["in", [cls.doctor_user, cls.provider_user]]})
        for user in (cls.doctor_user, cls.provider_user, cls.outsider_user):
            if frappe.db.exists("User", user):
                frappe.delete_doc("User", user, force=True, ignore_permissions=True)
        frappe.db.commit()
        frappe.set_user(cls._original_user)
        super().tearDownClass()

    @classmethod
    def _cleanup_existing(cls):
        questionnaires = []
        if frappe.db.exists("DocType", "Rating Questionnaire"):
            questionnaires = frappe.get_all(
                "Rating Questionnaire",
                filters={"questionnaire_name": ["like", f"{PREFIX}%"]},
                pluck="name",
            )
        if questionnaires and frappe.db.exists("DocType", "Rating"):
            for name in frappe.get_all(
                "Rating",
                filters={"reference_doctype": "Rating Questionnaire", "reference_name": ["in", questionnaires]},
                pluck="name",
            ):
                frappe.delete_doc("Rating", name, force=True, ignore_permissions=True)
        for name in questionnaires:
            frappe.delete_doc("Rating Questionnaire", name, force=True, ignore_permissions=True)

        for name in frappe.get_all(
            "PetCareService", filters={"pet_service_name": ["like", f"{PREFIX}%"]}, pluck="name"
        ):
            frappe.delete_doc("PetCareService", name, force=True, ignore_permissions=True)
        for name in frappe.get_all(
            "CategoryCareServices", filters={"category_name": ["like", f"{PREFIX}%"]}, pluck="name"
        ):
            frappe.delete_doc("CategoryCareServices", name, force=True, ignore_permissions=True)
        if frappe.db.exists("DocType", "Healthcare Practitioner"):
            for name in frappe.get_all(
                "Healthcare Practitioner",
                filters={"practitioner_name": ["like", f"{PREFIX}%"]},
                pluck="name",
            ):
                frappe.delete_doc("Healthcare Practitioner", name, force=True, ignore_permissions=True)
        users = frappe.get_all("User", filters={"first_name": ["like", f"{PREFIX}%"]}, pluck="name")
        if users:
            frappe.db.delete("Notification Log", {"for_user": ["in", users]})
            for user in users:
                frappe.delete_doc("User", user, force=True, ignore_permissions=True)
        frappe.db.commit()

    @classmethod
    def _make_user(cls, label):
        email = f"user.performance.{label}.{frappe.generate_hash(length=6)}@example.com"
        return frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": f"{PREFIX} {label}",
                "user_type": "System User",
                "send_welcome_email": 0,
            }
        ).insert(ignore_permissions=True).name

    @classmethod
    def _make_practitioner(cls, user, practitioner_type):
        return frappe.get_doc(
            {
                "doctype": "Healthcare Practitioner",
                "practitioner_name": f"{PREFIX} {practitioner_type}",
                "practitioner_type": practitioner_type,
                "phone": frappe.generate_hash(length=10),
                "user_id": user,
                "disabled": 0,
            }
        ).insert(ignore_permissions=True).name

    @classmethod
    def _make_category(cls):
        return frappe.get_doc(
            {
                "doctype": "CategoryCareServices",
                "category_name": f"{PREFIX} {frappe.generate_hash(length=6)}",
            }
        ).insert(ignore_permissions=True).name

    @classmethod
    def _make_visit(cls, practitioner, status, when, owner):
        doc = frappe.get_doc(
            {
                "doctype": "Vet Visit",
                "doctor": practitioner,
                "visit_type": "Consultation",
                "status": "Draft",
                "visit_datetime": when,
            }
        ).insert(ignore_permissions=True, ignore_mandatory=True)
        frappe.db.set_value(
            "Vet Visit",
            doc.name,
            {"status": status, "visit_datetime": when, "creation": when, "owner": owner},
            update_modified=False,
        )
        return doc.name

    @classmethod
    def _make_service(cls, status, when, completed=False):
        doc = frappe.get_doc(
            {
                "doctype": "PetCareService",
                "pet_service_name": f"{PREFIX} Service {frappe.generate_hash(length=5)}",
                "category": cls.category,
                "provider": cls.provider_practitioner,
                "status": "pending",
                "due_date": when[:10],
            }
        ).insert(ignore_permissions=True, ignore_mandatory=True)
        values = {"status": status, "creation": when}
        if completed:
            values.update({"start_date": "2025-06-01 09:00:00", "end_date": when})
            if when == OLD_DATE:
                values["start_date"] = "2020-01-01 09:00:00"
        frappe.db.set_value("PetCareService", doc.name, values, update_modified=False)
        return doc.name

    @classmethod
    def _make_questionnaire(cls):
        return frappe.get_doc(
            {
                "doctype": "Rating Questionnaire",
                "questionnaire_name": f"{PREFIX} {frappe.generate_hash(length=8)}",
                "active": 1,
            }
        ).insert(ignore_permissions=True).name

    @classmethod
    def _rate(
        cls,
        rated_by,
        performer_id,
        overall_rating,
        rated_at,
        owner=None,
        reference_doctype=None,
        reference_name=None,
    ):
        target = None
        if not reference_doctype or not reference_name:
            target = cls._make_questionnaire()
            reference_doctype = "Rating Questionnaire"
            reference_name = target
        doc = frappe.get_doc(
            {
                "doctype": "Rating",
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "overall_rating": overall_rating,
                "rated_by": rated_by,
                "rated_at": rated_at,
                "performer_doctype": "Healthcare Practitioner",
                "performer_id": performer_id,
            }
        ).insert(ignore_permissions=True)
        values = {"rated_at": rated_at, "creation": rated_at}
        if owner:
            values["owner"] = owner
        frappe.db.set_value("Rating", doc.name, values, update_modified=False)
        return doc.name

    def test_admin_can_request_doctor_dashboard(self):
        frappe.set_user("Administrator")
        data = user_performance.get_user_profile_dashboard(self.doctor_user)

        self.assertEqual(data["identity"]["user_id"], self.doctor_user)
        self.assertEqual(data["practitioner"]["id"], self.doctor_practitioner)
        self.assertIsNotNone(data["medical"])
        self.assertIsNone(data["services"])
        self.assertEqual(data["ratings"]["received_count"], 2)
        latest = data["ratings"]["latest_reviews"][0]
        self.assertEqual(latest["id"], self.received_recent)
        self.assertEqual(latest["reference_doctype"], "Vet Visit")
        self.assertEqual(latest["reference_name"], self.open_visit)
        self.assertEqual(latest["route_to"], f"/healthcare/visits/{self.open_visit}")

    def test_non_admin_cannot_view_another_user(self):
        frappe.set_user(self.outsider_user)
        try:
            with self.assertRaises(frappe.PermissionError):
                user_performance.get_user_profile_dashboard(self.doctor_user)
        finally:
            frappe.set_user("Administrator")

    def test_active_work_excludes_terminal_records(self):
        frappe.set_user("Administrator")
        data = user_performance.get_user_profile_dashboard(self.doctor_user)
        active_ids = {row["id"] for row in data["active_work"]}

        self.assertIn(self.open_visit, active_ids)
        self.assertNotIn(self.completed_visit, active_ids)
        self.assertNotIn(self.cancelled_visit, active_ids)
        self.assertLessEqual(len(data["active_work"]), user_performance.ACTIVE_WORK_LIMIT)
        self.assertFalse(any(key.startswith("_") for row in data["active_work"] for key in row))

    def test_service_provider_gets_services_not_medical(self):
        frappe.set_user("Administrator")
        full = user_performance.get_user_profile_dashboard(self.provider_user)
        narrow = user_performance.get_user_profile_dashboard(
            self.provider_user, from_date="2024-01-01", to_date="2099-01-01"
        )

        self.assertIsNone(full["medical"])
        self.assertIsNotNone(full["services"])
        self.assertGreater(full["services"]["completed"], narrow["services"]["completed"])
        self.assertEqual(narrow["services"]["completed"], 1)

    def test_empty_state_user_dashboard_is_valid(self):
        frappe.set_user("Administrator")
        data = user_performance.get_user_profile_dashboard(self.outsider_user)

        self.assertEqual(data["identity"]["user_id"], self.outsider_user)
        self.assertEqual(data["identity"]["role_profiles"], [])
        self.assertIsNone(data["practitioner"])
        self.assertIsNone(data["medical"])
        self.assertIsNone(data["services"])
        self.assertEqual(data["active_work"], [])
        self.assertEqual(data["ratings"]["received_count"], 0)
        self.assertEqual(data["ratings"]["given_count"], 0)
        self.assertEqual(data["ratings"]["distribution"], {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0})
        self.assertEqual(data["activity"]["records_created"]["total"], 0)

    def test_date_range_changes_medical_ratings_and_activity(self):
        frappe.set_user("Administrator")
        full = user_performance.get_user_profile_dashboard(self.doctor_user)
        narrow = user_performance.get_user_profile_dashboard(
            self.doctor_user, from_date="2024-01-01", to_date="2099-01-01"
        )

        self.assertGreater(full["medical"]["visits"], narrow["medical"]["visits"])
        self.assertGreater(full["ratings"]["received_count"], narrow["ratings"]["received_count"])
        self.assertGreater(
            full["activity"]["records_created"]["total"],
            narrow["activity"]["records_created"]["total"],
        )
