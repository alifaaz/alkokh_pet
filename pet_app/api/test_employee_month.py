from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api import employee_month


PREFIX = "Employee Month Test"
CURRENT_START = "2098-06-01"
CURRENT_END = "2098-06-30"
WIDE_START = "2098-05-01"
EMPTY_START = "2099-01-01"
EMPTY_END = "2099-01-31"
GROUP_KEYS = {
	"service_providers",
	"doctors",
	"coordinators",
	"cashiers",
	"receptionists",
	"other_staff",
}


class TestEmployeeMonthDashboard(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._original_user = frappe.session.user
		frappe.set_user("Administrator")
		cls._cleanup_existing()
		cls.created = {
			"users": [],
			"practitioners": [],
			"services": [],
			"visits": [],
			"ratings": [],
			"questionnaires": [],
			"categories": [],
		}

		cls.category = cls._make_category()
		cls.provider_a_user = cls._make_user("provider-a", roles=("Coordinator",))
		cls.provider_b_user = cls._make_user("provider-b")
		cls.provider_zero_user = cls._make_user("provider-zero")
		cls.provider_disabled_user = cls._make_user("provider-disabled")
		cls.doctor_a_user = cls._make_user("doctor-a")
		cls.doctor_b_user = cls._make_user("doctor-b")
		cls.coordinator_user = cls._make_user("coordinator", roles=("Coordinator", "POS Cashier"))
		cls.cashier_user = cls._make_user("cashier", roles=("POS Cashier",))
		cls.receptionist_user = cls._make_user("receptionist", roles=("Reception",))
		cls.other_staff_user = cls._make_user("other-staff", roles=("Desk User",))
		cls.staff_zero_user = cls._make_user("staff-zero", roles=("Coordinator",))

		cls.provider_a = cls._make_practitioner(cls.provider_a_user, "Service Provider", "Provider A")
		cls.provider_b = cls._make_practitioner(cls.provider_b_user, "Service Provider", "Provider B")
		cls.provider_zero = cls._make_practitioner(cls.provider_zero_user, "Service Provider", "Provider Zero")
		cls.provider_disabled = cls._make_practitioner(
			cls.provider_disabled_user, "Service Provider", "Provider Disabled", disabled=1
		)
		cls.doctor_a = cls._make_practitioner(cls.doctor_a_user, "Doctor", "Doctor A")
		cls.doctor_b = cls._make_practitioner(cls.doctor_b_user, "Doctor", "Doctor B")
		frappe.db.set_value("User", cls.provider_a_user, "user_image", "/files/provider-a.png", update_modified=False)
		frappe.db.set_value(
			"Healthcare Practitioner",
			cls.provider_b,
			"photo",
			"/private/files/provider-b.png",
			update_modified=False,
		)

		# Current range services: provider A wins on volume, provider B wins on
		# rating/on-time, doctor B proves service-provider group overlap.
		cls._make_service(cls.provider_a, "Completed", "2098-06-05 10:00:00", "2098-06-05")
		cls._make_service(cls.provider_a, "Completed", "2098-06-06 10:00:00", "2098-06-05")
		cls._make_service(cls.provider_b, "completed", "2098-06-08 10:00:00", "2098-06-10")
		cls._make_service(cls.doctor_b, "Completed", "2098-06-09 10:00:00", "2098-06-09")
		cls._make_service(cls.provider_disabled, "Completed", "2098-06-10 10:00:00", "2098-06-10")
		cls._make_service(cls.provider_a, "Completed", "2098-05-15 10:00:00", "2098-05-15")
		cls._make_service(cls.provider_b, "cancelled", "2098-06-11 10:00:00", "2098-06-11")

		# Current range clinical records: doctor A wins on volume.
		cls._make_visit(cls.doctor_a, "Completed", "2098-06-04 11:00:00")
		cls._make_visit(cls.doctor_a, "Closed", "2098-06-07 11:00:00")
		cls._make_visit(cls.doctor_b, "Completed", "2098-06-08 11:00:00")
		cls._make_visit(cls.doctor_a, "Completed", "2098-05-15 11:00:00")
		cls._make_visit(cls.doctor_b, "Cancelled", "2098-06-09 11:00:00")

		for value in (5, 4):
			cls._rate(cls.provider_a, value, "2098-06-12 09:00:00")
		cls._rate(cls.provider_b, 5, "2098-06-12 09:00:00")
		cls._rate(cls.provider_a, 3, "2098-05-12 09:00:00")
		for value in (5, 4):
			cls._rate(cls.doctor_a, value, "2098-06-13 09:00:00")
		cls._rate(cls.doctor_b, 5, "2098-06-13 09:00:00")
		cls._rate(cls.doctor_a, 2, "2098-05-13 09:00:00")
		for _ in range(3):
			cls._staff_record(cls.coordinator_user, "2098-06-14 09:00:00")
		cls._staff_record(cls.coordinator_user, "2098-05-14 09:00:00")
		for _ in range(2):
			cls._staff_record(cls.cashier_user, "2098-06-15 09:00:00")
		cls._staff_record(cls.receptionist_user, "2098-06-16 09:00:00")
		cls._staff_record(cls.other_staff_user, "2098-06-17 09:00:00")
		cls._staff_record(cls.provider_a_user, "2098-06-18 09:00:00")

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for doctype, key in (
			("Rating", "ratings"),
			("PetCareService", "services"),
			("Vet Visit", "visits"),
			("Healthcare Practitioner", "practitioners"),
			("Rating Questionnaire", "questionnaires"),
			("CategoryCareServices", "categories"),
		):
			for name in reversed(cls.created.get(key, [])):
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

		frappe.db.delete("Notification Log", {"for_user": ["in", cls.created.get("users", [])]})
		for user in reversed(cls.created.get("users", [])):
			if frappe.db.exists("User", user):
				frappe.delete_doc("User", user, force=True, ignore_permissions=True)

		frappe.db.commit()
		frappe.set_user(cls._original_user)
		super().tearDownClass()

	@classmethod
	def _cleanup_existing(cls):
		if frappe.db.exists("DocType", "Rating Questionnaire"):
			questionnaires = frappe.get_all(
				"Rating Questionnaire",
				filters={"questionnaire_name": ["like", f"{PREFIX}%"]},
				pluck="name",
			)
			if questionnaires and frappe.db.exists("DocType", "Rating"):
				for name in frappe.get_all(
					"Rating",
					filters={"reference_name": ["in", questionnaires]},
					pluck="name",
				):
					frappe.delete_doc("Rating", name, force=True, ignore_permissions=True)
			for name in questionnaires:
				frappe.delete_doc("Rating Questionnaire", name, force=True, ignore_permissions=True)

		if frappe.db.exists("DocType", "PetCareService"):
			for name in frappe.get_all(
				"PetCareService",
				filters={"pet_service_name": ["like", f"{PREFIX}%"]},
				pluck="name",
			):
				frappe.delete_doc("PetCareService", name, force=True, ignore_permissions=True)

		if frappe.db.exists("DocType", "Healthcare Practitioner"):
			for name in frappe.get_all(
				"Healthcare Practitioner",
				filters={"practitioner_name": ["like", f"{PREFIX}%"]},
				pluck="name",
			):
				frappe.delete_doc("Healthcare Practitioner", name, force=True, ignore_permissions=True)

		for name in frappe.get_all(
			"CategoryCareServices",
			filters={"category_name": ["like", f"{PREFIX}%"]},
			pluck="name",
		):
			frappe.delete_doc("CategoryCareServices", name, force=True, ignore_permissions=True)

		users = frappe.get_all("User", filters={"first_name": ["like", f"{PREFIX}%"]}, pluck="name")
		if users:
			frappe.db.delete("Notification Log", {"for_user": ["in", users]})
			for user in users:
				frappe.delete_doc("User", user, force=True, ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def _make_user(cls, label, roles=()):
		email = f"employee.month.{label}.{frappe.generate_hash(length=6)}@example.com"
		user_doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": f"{PREFIX} {label}",
				"user_type": "System User",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		for role in roles:
			user_doc.append("roles", {"role": role})
		if roles:
			user_doc.save(ignore_permissions=True)
		user = user_doc.name
		cls.created["users"].append(user)
		return user

	@classmethod
	def _make_category(cls):
		category = frappe.get_doc(
			{
				"doctype": "CategoryCareServices",
				"category_name": f"{PREFIX} {frappe.generate_hash(length=6)}",
			}
		).insert(ignore_permissions=True).name
		cls.created["categories"].append(category)
		return category

	@classmethod
	def _make_practitioner(cls, user, practitioner_type, label, disabled=0):
		practitioner = frappe.get_doc(
			{
				"doctype": "Healthcare Practitioner",
				"practitioner_name": f"{PREFIX} {label}",
				"practitioner_type": practitioner_type,
				"phone": frappe.generate_hash(length=10),
				"user_id": user,
				"disabled": disabled,
			}
		).insert(ignore_permissions=True).name
		cls.created["practitioners"].append(practitioner)
		return practitioner

	@classmethod
	def _make_service(cls, provider, status, end_date, due_date):
		doc = frappe.get_doc(
			{
				"doctype": "PetCareService",
				"pet_service_name": f"{PREFIX} Service {frappe.generate_hash(length=6)}",
				"category": cls.category,
				"provider": provider,
				"status": "pending",
				"due_date": due_date,
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.set_value(
			"PetCareService",
			doc.name,
			{"status": status, "end_date": end_date, "modified": end_date, "creation": end_date},
			update_modified=False,
		)
		cls.created["services"].append(doc.name)
		return doc.name

	@classmethod
	def _make_visit(cls, doctor, status, when):
		doc = frappe.get_doc(
			{
				"doctype": "Vet Visit",
				"doctor": doctor,
				"visit_type": "Consultation",
				"status": "Draft",
				"visit_datetime": when,
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.set_value(
			"Vet Visit",
			doc.name,
			{"status": status, "modified": when, "creation": when},
			update_modified=False,
		)
		cls.created["visits"].append(doc.name)
		return doc.name

	@classmethod
	def _make_questionnaire(cls):
		doc = frappe.get_doc(
			{
				"doctype": "Rating Questionnaire",
				"questionnaire_name": f"{PREFIX} {frappe.generate_hash(length=8)}",
				"active": 1,
			}
		).insert(ignore_permissions=True)
		cls.created["questionnaires"].append(doc.name)
		return doc.name

	@classmethod
	def _rate(cls, performer_id, overall_rating, rated_at):
		target = cls._make_questionnaire()
		doc = frappe.get_doc(
			{
				"doctype": "Rating",
				"reference_doctype": "Rating Questionnaire",
				"reference_name": target,
				"overall_rating": overall_rating,
				"rated_by": "Administrator",
				"rated_at": rated_at,
				"performer_doctype": "Healthcare Practitioner",
				"performer_id": performer_id,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Rating",
			doc.name,
			{"rated_at": rated_at, "modified": rated_at, "creation": rated_at},
			update_modified=False,
		)
		cls.created["ratings"].append(doc.name)
		return doc.name

	@classmethod
	def _staff_record(cls, user, when):
		target = cls._make_questionnaire()
		doc = frappe.get_doc(
			{
				"doctype": "Rating",
				"reference_doctype": "Rating Questionnaire",
				"reference_name": target,
				"overall_rating": 5,
				"rated_by": user,
				"rated_at": when,
				"performer_doctype": "Healthcare Practitioner",
				"performer_id": "EXTERNAL-HCP",
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Rating",
			doc.name,
			{"owner": user, "rated_at": when, "modified": when, "creation": when},
			update_modified=False,
		)
		cls.created["ratings"].append(doc.name)
		return doc.name

	def _dashboard(self, date_from=CURRENT_START, date_to=CURRENT_END):
		frappe.set_user("Administrator")
		return employee_month.get_employee_month_dashboard(date_from=date_from, date_to=date_to)

	def test_endpoint_returns_expected_top_level_shape(self):
		data = self._dashboard()

		self.assertEqual(set(data.keys()), GROUP_KEYS)
		for group in data.values():
			self.assertEqual(set(group.keys()), {"winner", "summary", "leaderboard"})
			self.assertIsInstance(group["leaderboard"], list)

	def test_service_provider_winner_summary_and_metrics(self):
		group = self._dashboard()["service_providers"]
		leaderboard = group["leaderboard"]
		by_id = {row["practitioner_id"]: row for row in leaderboard}

		self.assertEqual(group["winner"], leaderboard[0])
		self.assertEqual(group["winner"]["practitioner_id"], self.provider_a)
		self.assertEqual(group["winner"]["completed_count"], 2)
		self.assertEqual(group["winner"]["rating_average"], 4.5)
		self.assertEqual(group["winner"]["rating_count"], 2)
		self.assertEqual(group["winner"]["on_time_rate"], 50)
		self.assertEqual(group["winner"]["image"], "/files/provider-a.png")
		self.assertEqual(group["winner"]["primary_metric_label"], "Completed services")
		self.assertIn("Most services", group["winner"]["badges"])

		self.assertIn(self.doctor_b, by_id)
		self.assertIsNone(by_id[self.provider_b]["image"])
		self.assertNotIn(self.provider_disabled, by_id)
		self.assertNotIn(self.provider_zero, by_id)
		self.assertEqual(group["summary"]["total_people"], len(leaderboard))
		self.assertEqual(group["summary"]["total_completed"], sum(row["completed_count"] for row in leaderboard))
		self.assertEqual(group["summary"]["top_score"], max(row["score"] for row in leaderboard))

	def test_doctor_winner_summary_and_metrics(self):
		group = self._dashboard()["doctors"]
		leaderboard = group["leaderboard"]
		by_id = {row["practitioner_id"]: row for row in leaderboard}

		self.assertEqual(group["winner"], leaderboard[0])
		self.assertEqual(group["winner"]["practitioner_id"], self.doctor_a)
		self.assertEqual(group["winner"]["completed_count"], 2)
		self.assertEqual(group["winner"]["rating_average"], 4.5)
		self.assertEqual(group["winner"]["rating_count"], 2)
		self.assertIsNone(group["winner"]["on_time_rate"])
		self.assertEqual(group["winner"]["primary_metric_label"], "Completed clinical records")
		self.assertIn("Clinical leader", group["winner"]["badges"])

		self.assertIn(self.doctor_b, by_id)
		self.assertEqual(group["summary"]["total_people"], len(leaderboard))
		self.assertEqual(group["summary"]["total_completed"], sum(row["completed_count"] for row in leaderboard))
		self.assertEqual(group["summary"]["top_score"], max(row["score"] for row in leaderboard))

	def test_date_range_changes_counts_ratings_and_deltas(self):
		narrow = self._dashboard(CURRENT_START, CURRENT_END)
		wide = self._dashboard(WIDE_START, CURRENT_END)

		narrow_provider = narrow["service_providers"]["winner"]
		wide_provider = next(
			row for row in wide["service_providers"]["leaderboard"] if row["practitioner_id"] == self.provider_a
		)
		self.assertEqual(narrow_provider["completed_count"], 2)
		self.assertEqual(wide_provider["completed_count"], 3)
		self.assertEqual(narrow_provider["rating_average"], 4.5)
		self.assertEqual(wide_provider["rating_average"], 4.0)
		self.assertEqual(narrow_provider["delta"], "+1 vs previous period")

		narrow_doctor = narrow["doctors"]["winner"]
		wide_doctor = next(row for row in wide["doctors"]["leaderboard"] if row["practitioner_id"] == self.doctor_a)
		self.assertEqual(narrow_doctor["completed_count"], 2)
		self.assertEqual(wide_doctor["completed_count"], 3)
		self.assertEqual(narrow_doctor["rating_average"], 4.5)
		self.assertEqual(wide_doctor["rating_average"], 3.7)

		narrow_coordinator = narrow["coordinators"]["winner"]
		wide_coordinator = next(
			row for row in wide["coordinators"]["leaderboard"] if row["practitioner_id"] == self.coordinator_user
		)
		self.assertEqual(narrow_coordinator["completed_count"], 3)
		self.assertEqual(wide_coordinator["completed_count"], 4)
		self.assertEqual(narrow_coordinator["delta"], "+2 vs previous period")

	def test_staff_groups_rank_by_created_documents(self):
		data = self._dashboard()

		coordinators = data["coordinators"]
		self.assertEqual(coordinators["winner"], coordinators["leaderboard"][0])
		self.assertEqual(coordinators["winner"]["practitioner_id"], self.coordinator_user)
		self.assertEqual(coordinators["winner"]["completed_count"], 3)
		self.assertEqual(coordinators["winner"]["primary_metric_label"], "Documents created")
		self.assertIsNone(coordinators["winner"]["rating_average"])
		self.assertEqual(coordinators["winner"]["rating_count"], 0)
		self.assertIsNone(coordinators["winner"]["on_time_rate"])
		self.assertEqual(coordinators["winner"]["practitioner_type"], "Coordinator")
		self.assertEqual(coordinators["winner"]["by_doctype"][0]["doctype"], "Rating")
		self.assertEqual(
			coordinators["summary"]["total_completed"],
			sum(row["completed_count"] for row in coordinators["leaderboard"]),
		)

		self.assertEqual(data["cashiers"]["winner"]["practitioner_id"], self.cashier_user)
		self.assertEqual(data["cashiers"]["winner"]["completed_count"], 2)
		self.assertEqual(data["cashiers"]["winner"]["practitioner_type"], "Cashier")
		self.assertEqual(data["receptionists"]["winner"]["practitioner_id"], self.receptionist_user)
		self.assertEqual(data["receptionists"]["winner"]["completed_count"], 1)
		self.assertEqual(data["receptionists"]["winner"]["practitioner_type"], "Receptionist")
		self.assertEqual(data["other_staff"]["winner"]["practitioner_id"], self.other_staff_user)
		self.assertEqual(data["other_staff"]["winner"]["completed_count"], 1)

	def test_staff_groups_do_not_double_count_users(self):
		data = self._dashboard()
		staff_ids = set()
		for key in ("coordinators", "cashiers", "receptionists", "other_staff"):
			for row in data[key]["leaderboard"]:
				self.assertNotIn(row["practitioner_id"], staff_ids)
				staff_ids.add(row["practitioner_id"])

		self.assertNotIn(self.provider_a_user, staff_ids)
		self.assertNotIn(self.staff_zero_user, staff_ids)
		self.assertIn(self.coordinator_user, staff_ids)
		self.assertIn(self.cashier_user, staff_ids)
		self.assertIn(self.receptionist_user, staff_ids)
		self.assertIn(self.other_staff_user, staff_ids)

	def test_empty_state_is_stable(self):
		data = self._dashboard(EMPTY_START, EMPTY_END)

		for group in data.values():
			self.assertIsNone(group["winner"])
			self.assertEqual(group["leaderboard"], [])
			self.assertEqual(
				group["summary"],
				{
					"total_people": 0,
					"total_completed": 0,
					"average_rating": None,
					"top_score": 0,
				},
			)

	def test_strict_date_params_are_required(self):
		with self.assertRaises(Exception):
			employee_month.get_employee_month_dashboard(date_from="2098-6-1", date_to=CURRENT_END)
		with self.assertRaises(Exception):
			employee_month.get_employee_month_dashboard(date_from=CURRENT_END, date_to=CURRENT_START)
