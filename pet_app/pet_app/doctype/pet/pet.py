# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today, date_diff
from datetime import datetime


class Pet(Document):
	"""Pet DocType Controller"""

	def validate(self):
		"""Validate the Pet document before saving"""
		self.calculate_age()
		self.fetch_owner_details()
		self.validate_dates()
		self.validate_microchip()

	def calculate_age(self):
		"""Calculate age from date of birth"""
		if self.date_of_birth:
			dob = getdate(self.date_of_birth)
			today_date = getdate(today())

			# Calculate years, months, and days
			years = today_date.year - dob.year
			months = today_date.month - dob.month
			days = today_date.day - dob.day

			# Adjust for negative months or days
			if days < 0:
				months -= 1
			if months < 0:
				years -= 1
				months += 12

			# Format age string
			age_parts = []
			if years > 0:
				age_parts.append(f"{years} year{'s' if years != 1 else ''}")
			if months > 0:
				age_parts.append(f"{months} month{'s' if months != 1 else ''}")

			self.age = ", ".join(age_parts) if age_parts else "Less than 1 month"

			# Validate that date of birth is not in the future
			if dob > today_date:
				frappe.throw("Date of Birth cannot be in the future")

	def fetch_owner_details(self):
		"""Fetch owner name from User if owner link is set"""
		if self.owner and not self.owner_name:
			user = frappe.get_doc("User", self.owner)
			self.owner_name = user.full_name or user.first_name

	def validate_dates(self):
		"""Validate various date fields"""
		today_date = getdate(today())

		# Validate acquisition date
		if self.acquisition_date and getdate(self.acquisition_date) > today_date:
			frappe.throw("Acquisition Date cannot be in the future")

		# Validate adoption date
		if self.adoption_date and getdate(self.adoption_date) > today_date:
			frappe.throw("Adoption Date cannot be in the future")

		# Validate vet visit dates
		if self.last_vet_visit and getdate(self.last_vet_visit) > today_date:
			frappe.throw("Last Vet Visit cannot be in the future")

		# Check if next vet visit is overdue
		if self.next_vet_visit and getdate(self.next_vet_visit) < today_date:
			frappe.msgprint(
				f"Next Vet Visit is overdue (was scheduled for {self.next_vet_visit})",
				indicator="orange",
				alert=True
			)

	def validate_microchip(self):
		"""Validate microchip number format and uniqueness"""
		if self.microchip_number:
			# Remove any spaces or dashes
			self.microchip_number = self.microchip_number.replace(" ", "").replace("-", "")

			# Check if it's alphanumeric
			if not self.microchip_number.isalnum():
				frappe.throw("Microchip Number must be alphanumeric")

	def before_save(self):
		"""Hook that runs before saving the document"""
		# Set adoption date automatically when status changes to Adopted
		if self.status == "Adopted" and not self.adoption_date:
			self.adoption_date = today()

	def after_insert(self):
		"""Hook that runs after inserting a new document"""
		# Send notification for new pet registration
		frappe.msgprint(
			f"Pet {self.pet_name} has been successfully registered with ID: {self.name}",
			indicator="green",
			alert=True
		)

	def on_update(self):
		"""Hook that runs after updating the document"""
		# Check vaccination status and send reminders
		self.check_vaccination_reminder()

	def check_vaccination_reminder(self):
		"""Check if vaccination is due soon"""
		if self.vaccination_status == "Due Soon":
			frappe.msgprint(
				f"Reminder: Vaccination is due soon for {self.pet_name}",
				indicator="orange",
				alert=True
			)
		elif self.vaccination_status == "Overdue":
			frappe.msgprint(
				f"Warning: Vaccination is overdue for {self.pet_name}",
				indicator="red",
				alert=True
			)

	def on_trash(self):
		"""Hook that runs before deleting the document"""
		# Log deletion
		frappe.log_error(
			f"Pet record deleted: {self.pet_name} (ID: {self.name})",
			"Pet Deletion"
		)
