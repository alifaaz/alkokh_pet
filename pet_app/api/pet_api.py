# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

"""
Custom API endpoints for Pet operations.
These are additional endpoints beyond the standard REST API.
"""

import frappe
from frappe import _


@frappe.whitelist(allow_guest=False, methods=['GET'])
def get_pets_by_species(species):
	"""
	Get all pets filtered by species.

	Args:
		species (str): The species to filter by (Dog, Cat, Bird, etc.)

	Returns:
		list: List of pet documents
	"""
	try:
		pets = frappe.get_all(
			"Pet",
			filters={"species": species},
			fields=[
				"name", "pet_name", "species", "breed", "age", "gender",
				"status", "owner_name", "image", "color", "weight"
			]
		)
		return {"success": True, "data": pets}
	except Exception as e:
		frappe.log_error(f"Error fetching pets by species: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=True, methods=['GET'])
def get_available_pets():
	"""
	Get all pets available for adoption.

	Returns:
		list: List of available pets
	"""
	try:
		pets = frappe.get_all(
			"Pet",
			filters={"status": "Available for Adoption"},
			fields=[
				"name", "pet_name", "species", "breed", "age", "gender",
				"description", "image", "color", "weight", "vaccination_status",
				"is_neutered", "special_needs"
			],
			order_by="creation desc"
		)
		return {"success": True, "data": pets}
	except Exception as e:
		frappe.log_error(f"Error fetching available pets: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False, methods=['POST'])
def update_pet_status(pet_id, status):
	"""
	Update the status of a pet.

	Args:
		pet_id (str): The name/ID of the pet
		status (str): New status (Active, Adopted, etc.)

	Returns:
		dict: Success response with updated pet data
	"""
	try:
		pet = frappe.get_doc("Pet", pet_id)
		pet.status = status
		pet.save()

		return {
			"success": True,
			"message": _("Pet status updated successfully"),
			"data": pet.as_dict()
		}
	except frappe.DoesNotExistError:
		return {"success": False, "message": _("Pet not found")}
	except Exception as e:
		frappe.log_error(f"Error updating pet status: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=True, methods=['GET'])
def get_pet_statistics():
	"""
	Get statistics about pets (public endpoint for dashboard).

	Returns:
		dict: Pet statistics
	"""
	try:
		total_pets = frappe.db.count("Pet")

		pets_by_species = frappe.db.sql("""
			SELECT species, COUNT(*) as count
			FROM `tabPet`
			GROUP BY species
		""", as_dict=True)

		pets_by_status = frappe.db.sql("""
			SELECT status, COUNT(*) as count
			FROM `tabPet`
			GROUP BY status
		""", as_dict=True)

		pets_by_gender = frappe.db.sql("""
			SELECT gender, COUNT(*) as count
			FROM `tabPet`
			WHERE gender IS NOT NULL AND gender != ''
			GROUP BY gender
		""", as_dict=True)

		vaccination_stats = frappe.db.sql("""
			SELECT vaccination_status, COUNT(*) as count
			FROM `tabPet`
			WHERE vaccination_status IS NOT NULL AND vaccination_status != ''
			GROUP BY vaccination_status
		""", as_dict=True)

		return {
			"success": True,
			"data": {
				"total_pets": total_pets,
				"by_species": pets_by_species,
				"by_status": pets_by_status,
				"by_gender": pets_by_gender,
				"vaccination_stats": vaccination_stats
			}
		}
	except Exception as e:
		frappe.log_error(f"Error fetching pet statistics: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False, methods=['GET'])
def get_pet_details(pet_id):
	"""
	Get complete details of a specific pet.

	Args:
		pet_id (str): The name/ID of the pet

	Returns:
		dict: Complete pet information
	"""
	try:
		pet = frappe.get_doc("Pet", pet_id)
		return {"success": True, "data": pet.as_dict()}
	except frappe.DoesNotExistError:
		return {"success": False, "message": _("Pet not found")}
	except Exception as e:
		frappe.log_error(f"Error fetching pet details: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False, methods=['GET'])
def get_pets_by_owner(owner):
	"""
	Get all pets belonging to a specific owner.

	Args:
		owner (str): Owner name or User ID

	Returns:
		list: List of pets owned by the specified owner
	"""
	try:
		# Search by both owner link and owner_name
		filters = [
			["owner", "=", owner],
			["owner_name", "like", f"%{owner}%"]
		]

		pets = frappe.get_all(
			"Pet",
			filters=[filters],
			or_filters=True,
			fields=[
				"name", "pet_name", "species", "breed", "age", "gender",
				"status", "image", "vaccination_status", "next_vet_visit"
			],
			order_by="pet_name"
		)
		return {"success": True, "data": pets}
	except Exception as e:
		frappe.log_error(f"Error fetching pets by owner: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False, methods=['GET'])
def get_pets_needing_medical_attention():
	"""
	Get pets that need medical attention (overdue vaccinations, overdue vet visits).

	Returns:
		dict: Lists of pets needing various types of medical attention
	"""
	try:
		from frappe.utils import today, getdate

		today_date = getdate(today())

		# Pets with overdue vaccinations
		overdue_vaccinations = frappe.get_all(
			"Pet",
			filters={"vaccination_status": ["in", ["Overdue", "Due Soon"]]},
			fields=["name", "pet_name", "species", "vaccination_status", "owner_name"],
			order_by="pet_name"
		)

		# Pets with overdue vet visits
		overdue_vet_visits = frappe.get_all(
			"Pet",
			filters={
				"next_vet_visit": ["<", today_date],
				"status": ["!=", "Deceased"]
			},
			fields=["name", "pet_name", "species", "next_vet_visit", "owner_name"],
			order_by="next_vet_visit"
		)

		# Pets in medical care
		in_medical_care = frappe.get_all(
			"Pet",
			filters={"status": "Medical Care"},
			fields=["name", "pet_name", "species", "medical_notes", "owner_name"],
			order_by="modified desc"
		)

		return {
			"success": True,
			"data": {
				"overdue_vaccinations": overdue_vaccinations,
				"overdue_vet_visits": overdue_vet_visits,
				"in_medical_care": in_medical_care
			}
		}
	except Exception as e:
		frappe.log_error(f"Error fetching pets needing medical attention: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False, methods=['GET'])
def search_pets(query, filters=None):
	"""
	Search pets by name, breed, or other criteria.

	Args:
		query (str): Search query string
		filters (dict): Additional filters (optional)

	Returns:
		list: List of matching pets
	"""
	try:
		import json

		if filters and isinstance(filters, str):
			filters = json.loads(filters)

		search_filters = filters or {}

		# Add search conditions
		or_filters = [
			["pet_name", "like", f"%{query}%"],
			["breed", "like", f"%{query}%"],
			["owner_name", "like", f"%{query}%"],
			["microchip_number", "like", f"%{query}%"]
		]

		pets = frappe.get_all(
			"Pet",
			filters=search_filters,
			or_filters=or_filters,
			fields=[
				"name", "pet_name", "species", "breed", "age", "gender",
				"status", "owner_name", "image", "microchip_number"
			],
			order_by="modified desc",
			limit=50
		)

		return {"success": True, "data": pets, "count": len(pets)}
	except Exception as e:
		frappe.log_error(f"Error searching pets: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False, methods=['POST'])
def update_medical_info(pet_id, vaccination_status=None, last_vet_visit=None,
						next_vet_visit=None, medical_notes=None):
	"""
	Update medical information for a pet.

	Args:
		pet_id (str): The name/ID of the pet
		vaccination_status (str): New vaccination status
		last_vet_visit (str): Date of last vet visit
		next_vet_visit (str): Date of next vet visit
		medical_notes (str): Medical notes

	Returns:
		dict: Success response with updated pet data
	"""
	try:
		pet = frappe.get_doc("Pet", pet_id)

		if vaccination_status:
			pet.vaccination_status = vaccination_status
		if last_vet_visit:
			pet.last_vet_visit = last_vet_visit
		if next_vet_visit:
			pet.next_vet_visit = next_vet_visit
		if medical_notes:
			pet.medical_notes = medical_notes

		pet.save()

		return {
			"success": True,
			"message": _("Medical information updated successfully"),
			"data": pet.as_dict()
		}
	except frappe.DoesNotExistError:
		return {"success": False, "message": _("Pet not found")}
	except Exception as e:
		frappe.log_error(f"Error updating medical info: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False, methods=['GET'])
def filter_pets(species=None, status=None, gender=None, vaccination_status=None):
	"""
	Filter pets by multiple criteria.

	Args:
		species (str): Species filter
		status (str): Status filter
		gender (str): Gender filter
		vaccination_status (str): Vaccination status filter

	Returns:
		list: List of filtered pets
	"""
	try:
		filters = {}

		if species:
			filters["species"] = species
		if status:
			filters["status"] = status
		if gender:
			filters["gender"] = gender
		if vaccination_status:
			filters["vaccination_status"] = vaccination_status

		pets = frappe.get_all(
			"Pet",
			filters=filters,
			fields=[
				"name", "pet_name", "species", "breed", "age", "gender",
				"status", "owner_name", "image", "weight", "color",
				"vaccination_status", "date_of_birth"
			],
			order_by="pet_name"
		)

		return {"success": True, "data": pets, "count": len(pets)}
	except Exception as e:
		frappe.log_error(f"Error filtering pets: {str(e)}")
		return {"success": False, "message": str(e)}


@frappe.whitelist(allow_guest=False, methods=['POST'])
def create_pet(pet_data=None, **kwargs):
	"""
	Create a new pet record.

	Args:
		pet_data (str/dict): JSON string or dict containing pet information
		**kwargs: Individual pet fields

	Returns:
		dict: Success response with created pet data
	"""
	try:
		import json

		# Handle different input formats
		if pet_data:
			# Parse pet_data if it's a string
			if isinstance(pet_data, str):
				pet_data = json.loads(pet_data)
		else:
			# Use kwargs as pet_data if pet_data not provided
			pet_data = kwargs

		# Validate required fields
		required_fields = ["pet_name", "species"]
		for field in required_fields:
			if field not in pet_data or not pet_data[field]:
				return {
					"success": False,
					"message": f"Missing required field: {field}"
				}

		# Create new pet document
		pet = frappe.get_doc({
			"doctype": "Pet",
			**pet_data
		})

		# Insert the document
		pet.insert()

		return {
			"success": True,
			"message": _("Pet created successfully"),
			"data": pet.as_dict()
		}

	except frappe.exceptions.DuplicateEntryError:
		return {
			"success": False,
			"message": _("A pet with this microchip number already exists")
		}
	except Exception as e:
		frappe.log_error(f"Error creating pet: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


@frappe.whitelist(allow_guest=False, methods=['PUT'])
def update_pet(pet_id=None, pet_data=None, **kwargs):
	"""
	Update an existing pet record (complete update).

	Args:
		pet_id (str): The name/ID of the pet to update
		pet_data (str/dict): JSON string or dict containing updated pet information
		**kwargs: Individual pet fields

	Returns:
		dict: Success response with updated pet data
	"""
	try:
		import json

		# Validate pet_id
		if not pet_id:
			return {
				"success": False,
				"message": "pet_id is required"
			}

		# Handle different input formats
		if pet_data:
			# Parse pet_data if it's a string
			if isinstance(pet_data, str):
				pet_data = json.loads(pet_data)
		else:
			# Use kwargs as pet_data if pet_data not provided
			pet_data = kwargs

		if not pet_data:
			return {
				"success": False,
				"message": "No data provided to update"
			}

		# Get the existing pet document
		pet = frappe.get_doc("Pet", pet_id)

		# Update fields
		for key, value in pet_data.items():
			if hasattr(pet, key) and key not in ["name", "doctype", "creation", "modified"]:
				setattr(pet, key, value)

		# Save the document
		pet.save()

		return {
			"success": True,
			"message": _("Pet updated successfully"),
			"data": pet.as_dict()
		}

	except frappe.DoesNotExistError:
		return {
			"success": False,
			"message": _("Pet not found")
		}
	except Exception as e:
		frappe.log_error(f"Error updating pet: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


@frappe.whitelist(allow_guest=False, methods=['DELETE'])
def delete_pet(pet_id):
	"""
	Delete a pet record.

	Args:
		pet_id (str): The name/ID of the pet to delete

	Returns:
		dict: Success response
	"""
	try:
		# Check if pet exists
		if not frappe.db.exists("Pet", pet_id):
			return {
				"success": False,
				"message": _("Pet not found")
			}

		# Get pet name for confirmation message
		pet_name = frappe.db.get_value("Pet", pet_id, "pet_name")

		# Delete the pet
		frappe.delete_doc("Pet", pet_id)

		return {
			"success": True,
			"message": _(f"Pet {pet_name} (ID: {pet_id}) deleted successfully")
		}

	except frappe.exceptions.LinkExistsError:
		return {
			"success": False,
			"message": _("Cannot delete pet. It is linked to other documents.")
		}
	except Exception as e:
		frappe.log_error(f"Error deleting pet: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


@frappe.whitelist(allow_guest=False, methods=['GET'])
def get_all_pets(limit=20, offset=0, order_by="modified desc"):
	"""
	Get all pets with pagination.

	Args:
		limit (int): Number of records to return (default: 20)
		offset (int): Number of records to skip (default: 0)
		order_by (str): Sort order (default: "modified desc")

	Returns:
		dict: List of pets with total count
	"""
	try:
		# Get total count
		total_count = frappe.db.count("Pet")

		# Get pets with pagination
		pets = frappe.get_all(
			"Pet",
			fields=[
				"name", "pet_name", "species", "breed", "age", "gender",
				"status", "owner_name", "image", "color", "weight",
				"vaccination_status", "date_of_birth", "modified"
			],
			limit_page_length=int(limit),
			limit_start=int(offset),
			order_by=order_by
		)

		return {
			"success": True,
			"data": pets,
			"count": len(pets),
			"total": total_count,
			"limit": int(limit),
			"offset": int(offset)
		}

	except Exception as e:
		frappe.log_error(f"Error fetching all pets: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


@frappe.whitelist(methods=['GET'])
def get_csrf_token():
	"""
	Get CSRF token for the current session.
	Used by browser-based API clients like Swagger UI.

	Returns:
		dict: CSRF token
	"""
	try:
		# Get CSRF token from session
		if frappe.session and hasattr(frappe.session, 'data'):
			csrf_token = frappe.session.data.get('csrf_token')
		else:
			csrf_token = None

		if not csrf_token:
			# Return error if no session/token available
			return {
				"success": False,
				"message": "No active session. Please authenticate first."
			}

		return {
			"success": True,
			"csrf_token": csrf_token
		}
	except Exception as e:
		frappe.log_error(f"Error getting CSRF token: {str(e)}")
		return {
			"success": False,
			"message": str(e)
		}


## unified and alias endpoints removed to keep only original endpoints
