from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from pet_app.api import care_plan, case_assignment, visit_workbench
from pet_app.utils.case_assignment import ensure_episode_practitioner
from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian


class TestCaseAssignment(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.reload_doc("pet_app", "doctype", "pet_care_episode_assigned_practitioner")
		frappe.reload_doc("pet_app", "doctype", "pet_care_episode")
		frappe.reload_doc("pet_app", "doctype", "visit_referral")
		frappe.reload_doc("pet_app", "doctype", "vet_visit")

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_supervisor_adds_visitless_doctor_idempotently(self):
		primary = self._make_doctor()
		consultant = self._make_doctor()
		supervisor = self._make_doctor(extra_roles=["Visit Admin"])
		episode = self._make_episode(primary)

		frappe.set_user(supervisor.user_id)
		first = case_assignment.add_episode_doctor(episode=episode.name, practitioner=consultant.name)
		second = case_assignment.add_episode_doctor(episode=episode.name, practitioner=consultant.name)

		self.assertTrue(first["ok"], msg=first)
		self.assertTrue(second["ok"], msg=second)
		self.assertTrue(first["data"]["added"])
		self.assertFalse(second["data"]["added"])
		self.assertIn(consultant.name, _team_practitioners(second))
		self.assertEqual(_team_practitioners(second).count(consultant.name), 1)
		self.assertEqual(second["data"]["visits"], [])

	def test_team_doctor_remove_blocks_open_visits_then_allows_terminal_history(self):
		primary = self._make_doctor()
		other = self._make_doctor()
		episode = self._make_episode(primary)
		self._add_to_team(episode, other)
		open_visit = self._make_visit(episode, other, status="In Progress")

		frappe.set_user(primary.user_id)
		blocked = case_assignment.remove_episode_doctor(episode=episode.name, practitioner=other.name)

		self.assertFalse(blocked["ok"], msg=blocked)
		self.assertEqual(blocked["meta"]["code"], "PRACTITIONER_HAS_OPEN_VISITS")
		self.assertEqual(blocked["data"]["blocking_visits"][0]["visit"], open_visit.name)

		frappe.set_user("Administrator")
		frappe.db.set_value("Vet Visit", open_visit.name, "status", "Cancelled", update_modified=False)

		frappe.set_user(primary.user_id)
		removed = case_assignment.remove_episode_doctor(episode=episode.name, practitioner=other.name)

		self.assertTrue(removed["ok"], msg=removed)
		self.assertTrue(removed["data"]["removed"])
		self.assertNotIn(other.name, _team_practitioners(removed))
		self.assertEqual(removed["data"]["visits"][0]["primary_practitioner"], other.name)

	def test_assign_visit_doctor_is_admin_only_and_syncs_legacy_doctor(self):
		primary = self._make_doctor()
		new_doctor = self._make_doctor()
		episode = self._make_episode(primary)
		visit = self._make_visit(episode, primary, status="In Progress")

		frappe.set_user(primary.user_id)
		result = case_assignment.assign_visit_doctor(visit=visit.name, practitioner=new_doctor.name)
		self.assertFalse(result["ok"], msg=result)
		self.assertEqual(result["meta"]["code"], "PERMISSION_DENIED")
		self.assertIn("referral", result["errors"][0]["message"].lower())

		frappe.set_user("Administrator")
		result = case_assignment.assign_visit_doctor(visit=visit.name, practitioner=new_doctor.name)
		self.assertTrue(result["ok"], msg=result)
		self.assertTrue(result["data"]["added_to_team"])
		self.assertIn(new_doctor.name, _team_practitioners(result))
		visit_values = frappe.db.get_value(
			"Vet Visit",
			visit.name,
			["doctor", "primary_practitioner"],
			as_dict=True,
		)
		self.assertEqual(visit_values.doctor, new_doctor.name)
		self.assertEqual(visit_values.primary_practitioner, new_doctor.name)

	def test_visit_referral_immediately_reassigns_and_workbench_serializes(self):
		primary = self._make_doctor()
		target = self._make_doctor()
		episode = self._make_episode(primary)
		visit = self._make_visit(episode, primary, status="In Progress")
		empty_visit = self._make_visit(episode, primary, status="In Progress", day_offset=1)

		frappe.set_user(primary.user_id)
		before_workbench = visit_workbench.get_visit_workbench(visit=visit.name)
		self.assertTrue(before_workbench["ok"], msg=before_workbench)
		self.assertTrue(before_workbench["data"]["permissions"]["can_refer_visit"])

		created = case_assignment.create_visit_referral(
			visit=visit.name,
			to_practitioner=target.name,
			note="Please take over this visit.",
		)

		self.assertTrue(created["ok"], msg=created)
		referral = created["data"]["referral"]
		self.assertEqual(referral["note"], "Please take over this visit.")
		self.assertEqual(referral["from_practitioner"], primary.name)
		self.assertEqual(referral["to_practitioner"], target.name)
		self.assertEqual(referral["referred_by"], primary.user_id)
		self.assertTrue(referral["referred_at"])
		self.assertNotIn("status", referral)
		self.assertNotIn("response_note", referral)

		visit_values = frappe.db.get_value("Vet Visit", visit.name, ["doctor", "primary_practitioner"], as_dict=True)
		self.assertEqual(visit_values.doctor, target.name)
		self.assertEqual(visit_values.primary_practitioner, target.name)
		episode_doc = frappe.get_doc("Pet Care Episode", episode.name)
		team_rows = {row.practitioner: row.role for row in episode_doc.get("assigned_practitioners")}
		self.assertEqual(team_rows[target.name], "Treating Doctor")

		pending_workbench = visit_workbench.get_visit_workbench(visit=visit.name)
		empty_workbench = visit_workbench.get_visit_workbench(visit=empty_visit.name)
		self.assertTrue(pending_workbench["ok"], msg=pending_workbench)
		self.assertTrue(empty_workbench["ok"], msg=empty_workbench)
		self.assertEqual(pending_workbench["data"]["referrals"][0]["name"], referral["name"])
		self.assertEqual(pending_workbench["data"]["referrals"][0]["referred_by"], primary.user_id)
		self.assertEqual(empty_workbench["data"]["referrals"], [])

		blocked_assign = case_assignment.assign_visit_doctor(visit=visit.name, practitioner=primary.name)
		self.assertFalse(blocked_assign["ok"], msg=blocked_assign)
		self.assertEqual(blocked_assign["meta"]["code"], "PERMISSION_DENIED")
		self.assertIn("referral", blocked_assign["errors"][0]["message"].lower())

	def test_admin_referral_records_previous_doctor_and_referred_by(self):
		primary = self._make_doctor()
		target = self._make_doctor()
		episode = self._make_episode(primary)
		visit = self._make_visit(episode, primary, status="In Progress")

		frappe.set_user("Administrator")
		created = case_assignment.create_visit_referral(
			visit=visit.name,
			to_practitioner=target.name,
			note="Admin transfer reason.",
		)

		self.assertTrue(created["ok"], msg=created)
		referral = created["data"]["referral"]
		self.assertEqual(referral["from_practitioner"], primary.name)
		self.assertEqual(referral["to_practitioner"], target.name)
		self.assertEqual(referral["referred_by"], "Administrator")
		visit_values = frappe.db.get_value("Vet Visit", visit.name, ["doctor", "primary_practitioner"], as_dict=True)
		self.assertEqual(visit_values.doctor, target.name)
		self.assertEqual(visit_values.primary_practitioner, target.name)

		notifications = frappe.get_all(
			"Notification Log",
			filters={"document_type": "Vet Visit", "document_name": visit.name},
			fields=["for_user", "subject"],
			ignore_permissions=True,
		)
		notified_users = {row.for_user for row in notifications}
		self.assertIn(primary.user_id, notified_users)
		self.assertIn(target.user_id, notified_users)

	def test_get_episode_care_team_returns_visitless_team_and_day_history(self):
		primary = self._make_doctor()
		consultant = self._make_doctor()
		day_two_doctor = self._make_doctor()
		episode = self._make_episode(primary)
		self._add_to_team(episode, consultant, role="Consultant")
		self._add_to_team(episode, day_two_doctor)
		day_one = self._make_visit(episode, primary, day_offset=0)
		day_two = self._make_visit(episode, day_two_doctor, day_offset=1)

		frappe.set_user(primary.user_id)
		result = case_assignment.get_episode_care_team(episode=episode.name)

		self.assertTrue(result["ok"], msg=result)
		self.assertIn(consultant.name, _team_practitioners(result))
		self.assertEqual(result["data"]["visits"][0]["visit"], day_one.name)
		self.assertEqual(result["data"]["visits"][0]["day_index"], 1)
		self.assertEqual(result["data"]["visits"][0]["primary_practitioner"], primary.name)
		self.assertEqual(result["data"]["visits"][1]["visit"], day_two.name)
		self.assertEqual(result["data"]["visits"][1]["day_index"], 2)
		self.assertEqual(result["data"]["visits"][1]["primary_practitioner"], day_two_doctor.name)

	def test_get_care_episode_plan_returns_all_days_and_statuses(self):
		primary = self._make_doctor()
		episode = self._make_episode(primary)
		day_one = self._make_visit(episode, primary, day_offset=0)
		day_two = self._make_visit(episode, primary, day_offset=1)
		other_episode = self._make_episode(self._make_doctor())
		other_visit = self._make_visit(other_episode, primary)

		planned = self._make_plan_item(
			episode,
			day_one,
			primary,
			plan_type="Lab Recheck",
			status="Planned",
			title="CBC recheck",
			due_date="2026-07-10",
			due_time="09:00:00",
			instructions="Bring prior lab sheet",
		)
		scheduled = self._make_plan_item(
			episode,
			day_two,
			primary,
			plan_type="Procedure",
			status="Scheduled",
			title="Bandage change",
			due_date="2026-07-08",
			due_time="10:00:00",
		)
		done = self._make_plan_item(
			episode,
			day_two,
			primary,
			plan_type="Owner Instruction",
			status="Done",
			title="Diet instructions",
			due_date="2026-07-11",
		)
		cancelled = self._make_plan_item(
			episode,
			day_one,
			primary,
			plan_type="Imaging Recheck",
			status="Cancelled",
			title="Repeat x-ray",
			due_date="2026-07-12",
		)
		converted = self._make_plan_item(
			episode,
			day_two,
			primary,
			plan_type="Follow-up Visit",
			status="Converted To Visit",
			title="Follow-up converted",
		)
		other = self._make_plan_item(other_episode, other_visit, primary, title="Other episode item")

		result = care_plan.get_care_episode_plan(episode=episode.name)

		self.assertTrue(result["ok"], msg=result)
		self.assertEqual(result["data"]["episode"], episode.name)
		self.assertEqual(result["data"]["plan_items"], result["data"]["items"])
		items_by_name = {row["name"]: row for row in result["data"]["plan_items"]}
		self.assertEqual({planned.name, scheduled.name, done.name, cancelled.name, converted.name}, set(items_by_name))
		self.assertNotIn(other.name, items_by_name)
		self.assertEqual(result["meta"]["priority_options"], ["Low", "Normal", "Important", "Urgent"])

		planned_item = items_by_name[planned.name]
		self.assertEqual(planned_item["plan_type"], "Lab Recheck")
		self.assertEqual(planned_item["item_type"], "Lab Test")
		self.assertEqual(planned_item["raw_status"], "Planned")
		self.assertEqual(planned_item["status"], "Open")
		self.assertEqual(planned_item["owner_instructions"], "Bring prior lab sheet")
		self.assertEqual(planned_item["assigned_to"], primary.name)
		self.assertEqual(planned_item["assigned_to_name"], primary.practitioner_name)
		self.assertEqual(planned_item["source_visit"], day_one.name)
		self.assertEqual(planned_item["care_episode"], episode.name)
		self.assertEqual(str(planned_item["due_date"]), "2026-07-10")
		self.assertEqual(planned_item["due_datetime"], "2026-07-10 09:00:00")
		self.assertEqual(items_by_name[scheduled.name]["status"], "Scheduled")
		self.assertEqual(str(items_by_name[scheduled.name]["scheduled_date"]), "2026-07-08")
		self.assertEqual(items_by_name[done.name]["status"], "Completed")
		self.assertEqual(items_by_name[cancelled.name]["status"], "Cancelled")
		self.assertEqual(items_by_name[converted.name]["status"], "Converted to Visit")
		self.assertEqual(result["data"]["plan_items"][-1]["name"], converted.name)

	def test_get_case_follow_up_table_groups_case_items_and_states(self):
		primary = self._make_doctor()
		episode = self._make_episode(primary)
		day_one = self._make_visit(episode, primary, day_offset=0)
		day_two = self._make_visit(episode, primary, day_offset=1)
		follow_up_visit = self._make_visit(episode, primary, day_offset=2)
		frappe.db.set_value("Vet Visit", follow_up_visit.name, "status", "Completed", update_modified=False)

		overdue = self._make_plan_item(
			episode,
			day_one,
			primary,
			plan_type="Lab Recheck",
			title="Overdue CBC",
			due_date="2000-01-01",
		)
		done = self._make_plan_item(
			episode,
			day_two,
			primary,
			plan_type="Owner Instruction",
			status="Done",
			title="Diet done",
		)
		came = self._make_plan_item(
			episode,
			day_two,
			primary,
			plan_type="Follow-up Visit",
			status="Converted To Visit",
			title="Owner came for recheck",
			converted_visit=follow_up_visit.name,
		)

		result = care_plan.get_case_follow_up_table(filters={"pet": episode.pet})

		self.assertTrue(result["ok"], msg=result)
		self.assertEqual(result["data"]["metrics"]["total_cases"], 1)
		self.assertEqual(result["data"]["metrics"]["total_items"], 3)
		self.assertEqual(result["data"]["metrics"]["overdue_items"], 1)
		self.assertEqual(result["data"]["metrics"]["completed_items"], 1)
		self.assertEqual(result["data"]["metrics"]["converted_items"], 1)
		case = result["data"]["cases"][0]
		self.assertEqual(case["episode"]["name"], episode.name)
		self.assertEqual(case["follow_up_summary"]["visit_count"], 3)

		items_by_name = {row["name"]: row for row in case["items"]}
		self.assertEqual(items_by_name[overdue.name]["follow_up_state"], "overdue")
		self.assertEqual(items_by_name[done.name]["follow_up_state"], "completed")
		self.assertEqual(items_by_name[came.name]["follow_up_state"], "converted_to_visit")
		self.assertTrue(items_by_name[came.name]["patient_came"])
		self.assertEqual(items_by_name[came.name]["linked_visit"], follow_up_visit.name)
		self.assertEqual(items_by_name[came.name]["linked_visit_status"], "Completed")
		self.assertEqual(items_by_name[came.name]["follow_up_label"], "Came / Visit Completed")

		overdue_only = care_plan.get_case_follow_up_table(filters={"pet": episode.pet, "follow_up_state": "overdue"})
		self.assertTrue(overdue_only["ok"], msg=overdue_only)
		self.assertEqual(len(overdue_only["data"]["cases"]), 1)
		self.assertEqual([row["name"] for row in overdue_only["data"]["cases"][0]["items"]], [overdue.name])

	def test_permission_gates_block_outsider_and_allow_visit_admin_assignment(self):
		primary = self._make_doctor()
		target = self._make_doctor()
		outsider = self._make_doctor()
		supervisor = self._make_doctor(extra_roles=["Visit Admin"])
		episode = self._make_episode(primary)
		visit = self._make_visit(episode, primary)

		frappe.set_user(outsider.user_id)
		add_result = case_assignment.add_episode_doctor(episode=episode.name, practitioner=target.name)
		remove_result = case_assignment.remove_episode_doctor(episode=episode.name, practitioner=primary.name)
		assign_result = case_assignment.assign_visit_doctor(visit=visit.name, practitioner=target.name)

		self.assertFalse(add_result["ok"], msg=add_result)
		self.assertEqual(add_result["meta"]["code"], "PERMISSION_DENIED")
		self.assertFalse(remove_result["ok"], msg=remove_result)
		self.assertEqual(remove_result["meta"]["code"], "PERMISSION_DENIED")
		self.assertFalse(assign_result["ok"], msg=assign_result)
		self.assertEqual(assign_result["meta"]["code"], "PERMISSION_DENIED")

		frappe.set_user(supervisor.user_id)
		supervisor_assign = case_assignment.assign_visit_doctor(visit=visit.name, practitioner=target.name)
		self.assertTrue(supervisor_assign["ok"], msg=supervisor_assign)

	def _make_episode(self, primary_doctor):
		guardian, pet = self._make_guardian_pet()
		customer = get_or_create_customer_from_guardian(guardian.name)
		return frappe.get_doc(
			{
				"doctype": "Pet Care Episode",
				"pet": pet.name,
				"guardian": guardian.name,
				"customer": customer,
				"primary_doctor": primary_doctor.name,
				"episode_title": "Assignment Case",
				"episode_status": "Open",
			}
		).insert(ignore_permissions=True)

	def _make_visit(self, episode, doctor, *, status="In Progress", day_offset=0):
		data = {
			"doctype": "Vet Visit",
			"guardian": episode.guardian,
			"customer": episode.customer,
			"animal_patient": episode.pet,
			"doctor": doctor.name,
			"status": status,
			"priority": "Normal",
			"visit_type": "Consultation",
			"visit_datetime": add_to_date(now_datetime(), days=day_offset),
		}
		meta = frappe.get_meta("Vet Visit")
		if meta.has_field("primary_practitioner"):
			data["primary_practitioner"] = doctor.name
		if meta.has_field("care_episode"):
			data["care_episode"] = episode.name
		return frappe.get_doc(data).insert(ignore_permissions=True)

	def _make_plan_item(
		self,
		episode,
		visit,
		doctor,
		*,
		plan_type="Monitoring",
		status="Planned",
		title="Plan item",
		due_date=None,
		due_time=None,
		instructions=None,
		converted_visit=None,
	):
		data = {
			"doctype": "Pet Care Plan Item",
			"pet": episode.pet,
			"guardian": episode.guardian,
			"customer": episode.customer,
			"care_episode": episode.name,
			"source_visit": visit.name,
			"doctor": doctor.name,
			"plan_type": plan_type,
			"title": title,
			"status": status,
			"priority": "Normal",
			"instructions": instructions,
			"due_date": due_date,
			"due_time": due_time,
		}
		if plan_type == "Follow-up Visit":
			data["requires_appointment"] = 1
		if status == "Converted To Visit":
			data["converted_to_visit"] = 1
		if converted_visit:
			data["converted_visit"] = converted_visit
		return frappe.get_doc(data).insert(ignore_permissions=True)

	def _add_to_team(self, episode, doctor, role="Treating Doctor"):
		episode_doc = frappe.get_doc("Pet Care Episode", episode.name)
		ensure_episode_practitioner(episode_doc, doctor.name, role=role)
		episode_doc.save(ignore_permissions=True)

	def _make_guardian_pet(self):
		suffix = frappe.generate_hash(length=8)
		phone_digits = self._digits(suffix, 9)
		guardian = frappe.get_doc(
			{
				"doctype": "Guardian",
				"phone": f"07{phone_digits}",
				"full_name": f"Guardian {suffix}",
				"email_id": f"guardian.{suffix}@example.com",
			}
		).insert(ignore_permissions=True)
		pet = frappe.get_doc(
			{
				"doctype": "Pet",
				"pet_name": f"Pet {suffix}",
				"animal_species": "Mammal",
				"animal_type": "Dog",
				"pet_status": "Approved",
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "PetGuardian",
				"pet_id": pet.name,
				"guardian_id": guardian.name,
				"role": "primary_owner",
			}
		).insert(ignore_permissions=True)
		return guardian, pet

	def _make_doctor(self, *, extra_roles=None):
		suffix = frappe.generate_hash(length=8)
		phone_digits = self._digits(suffix, 9)
		doctor = frappe.get_doc(
			{
				"doctype": "Healthcare Practitioner",
				"practitioner_name": f"Practitioner {suffix}",
				"practitioner_type": "Doctor",
				"phone": f"07{phone_digits}",
			}
		).insert(ignore_permissions=True)
		if extra_roles:
			self._grant_roles(doctor.user_id, extra_roles)
		return doctor

	def _grant_roles(self, user_id, roles):
		user = frappe.get_doc("User", user_id)
		existing = {row.role for row in user.roles}
		for role in roles:
			self._ensure_role(role)
			if role not in existing:
				user.append("roles", {"role": role})
		user.save(ignore_permissions=True)

	def _ensure_role(self, role):
		if frappe.db.exists("Role", role):
			return
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)

	def _digits(self, value, length):
		digits = "".join(str(ord(char) % 10) for char in value)
		while len(digits) < length:
			digits += digits
		return digits[:length]


def _team_practitioners(result: dict) -> list[str]:
	return [row["practitioner"] for row in result["data"]["care_team"]]
