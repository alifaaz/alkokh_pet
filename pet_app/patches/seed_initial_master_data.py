from __future__ import annotations

import frappe
from frappe.utils import add_days, flt, getdate, now_datetime

from pet_app.utils.guardian_customer import get_or_create_customer_from_guardian


ITEM_GROUPS = (
	{
		"item_group_name": "Veterinary Services",
		"parent_item_group": "All Item Groups",
		"is_group": 0,
	},
	{
		"item_group_name": "Pet Supplies",
		"parent_item_group": "All Item Groups",
		"is_group": 1,
	},
)

PRICE_LISTS = (
	{"price_list_name": "Standard Selling", "selling": 1, "buying": 0},
)

BOARDING_ITEMS = (
	{
		"item_code": "PET-BOARDING-TRAVEL",
		"item_name": "Travel Boarding Day",
		"standard_rate": 25,
		"settings_field": "travel_boarding_item",
	},
	{
		"item_code": "PET-BOARDING-TREATMENT",
		"item_name": "Treatment Boarding Day",
		"standard_rate": 45,
		"settings_field": "treatment_boarding_item",
	},
)

CARE_SERVICE_CATEGORIES = (
	{"category_name": "Consultation", "description": "Doctor visits and follow-up consultations."},
	{"category_name": "Vaccination", "description": "Preventive vaccination services."},
	{"category_name": "Diagnostics", "description": "Laboratory diagnostics and screening."},
	{"category_name": "Imaging", "description": "Radiology and diagnostic imaging."},
	{"category_name": "Grooming", "description": "General grooming and hygiene services."},
	{"category_name": "Boarding", "description": "Daily room-stay services for boarding workflows."},
)

CARE_SERVICES = (
	{
		"item_code": "VET-SVC-GENERAL-CONSULT",
		"service_name": "General Consultation",
		"animal_species": "Mammal",
		"frequency": "onetime",
		"category_name": "Consultation",
		"default_price": 20,
		"description": "Routine veterinary consultation.",
	},
	{
		"item_code": "VET-SVC-FOLLOW-UP",
		"service_name": "Follow-up Consultation",
		"animal_species": "Mammal",
		"frequency": "onetime",
		"category_name": "Consultation",
		"default_price": 12,
		"description": "Follow-up review after an earlier visit.",
	},
	{
		"item_code": "VET-SVC-CORE-VACCINE",
		"service_name": "Core Vaccination",
		"animal_species": "Mammal",
		"frequency": "annul",
		"category_name": "Vaccination",
		"default_price": 18,
		"description": "Annual core vaccination.",
	},
	{
		"item_code": "VET-SVC-CBC",
		"service_name": "CBC Lab Test",
		"animal_species": "Mammal",
		"frequency": "onetime",
		"category_name": "Diagnostics",
		"default_price": 22,
		"specimen": "Blood",
		"estimated_turnaround": "Same day",
		"description": "Complete blood count laboratory test.",
	},
	{
		"item_code": "VET-SVC-XRAY",
		"service_name": "X-Ray Imaging",
		"animal_species": "Mammal",
		"frequency": "onetime",
		"category_name": "Imaging",
		"default_price": 35,
		"modality": "X-Ray",
		"description": "Standard radiology imaging.",
	},
	{
		"item_code": "VET-SVC-BASIC-GROOM",
		"service_name": "Basic Grooming",
		"animal_species": "Mammal",
		"frequency": "onetime",
		"category_name": "Grooming",
		"default_price": 15,
		"description": "Basic grooming and hygiene service.",
	},
)

SERVICE_ROOMS = (
	{
		"room_code": "KENNEL-01",
		"room_name": "Kennel 01",
		"room_type": "Standard Kennel",
		"notes": "General travel boarding room.",
	},
	{
		"room_code": "KENNEL-02",
		"room_name": "Kennel 02",
		"room_type": "Standard Kennel",
		"notes": "General travel boarding room.",
	},
	{
		"room_code": "WARD-01",
		"room_name": "Treatment Ward 01",
		"room_type": "Treatment Ward",
		"notes": "Treatment boarding and observation room.",
	},
	{
		"room_code": "ICU-01",
		"room_name": "ICU 01",
		"room_type": "Critical Care",
		"notes": "Critical care observation room.",
	},
)

BRANDS = (
	{"brand": "Royal Canin"},
	{"brand": "Purina"},
	{"brand": "Hill's"},
)

FOOD_TYPES = (
	{"type_name": "Dry Food", "description": "Dry pet food."},
	{"type_name": "Wet Food", "description": "Wet pet food."},
	{"type_name": "Treats", "description": "Snacks and training treats."},
)

DEMO_GUARDIANS = (
	{
		"phone": "+9647700001001",
		"full_name": "Sara Ahmed",
		"email_id": "sara.ahmed@petapp.local",
		"address_line1": "Karrada Main Street",
		"city": "Baghdad",
	},
	{
		"phone": "+9647700001002",
		"full_name": "Omar Hassan",
		"email_id": "omar.hassan@petapp.local",
		"address_line1": "Mansour District",
		"city": "Baghdad",
	},
)

DEMO_PETS = (
	{
		"pet_name": "Luna",
		"guardian_phone": "+9647700001001",
		"animal_species": "Mammal",
		"animal_type": "Cat",
		"breed": "Persian",
		"gender": "Female",
		"birth_date": "2022-03-15",
		"weight": 4.2,
		"color": "White",
		"food_brand": "Royal Canin",
		"food_type": "Dry Food",
	},
	{
		"pet_name": "Max",
		"guardian_phone": "+9647700001002",
		"animal_species": "Mammal",
		"animal_type": "Dog",
		"breed": "Golden Retriever",
		"gender": "Male",
		"birth_date": "2021-06-20",
		"weight": 28.5,
		"color": "Golden",
		"food_brand": "Purina",
		"food_type": "Wet Food",
	},
	{
		"pet_name": "Coco",
		"guardian_phone": "+9647700001001",
		"animal_species": "Bird",
		"animal_type": "Bird",
		"breed": "Cockatiel",
		"gender": "Unknown",
		"birth_date": "2023-02-01",
		"weight": 0.09,
		"color": "Grey",
		"food_brand": None,
		"food_type": None,
	},
)

DEMO_PRACTITIONERS = (
	{
		"practitioner_name": "Dr. Layla Karim",
		"practitioner_type": "Doctor",
		"phone": "+9647700002001",
		"specialization": "General Veterinary",
	},
)

DEMO_MANAGER_USER = {
	"email": "demo.manager@petapp.local",
	"first_name": "Demo Manager",
	"password": "demo1234",
	"roles": (
		"Healthcare",
		"Healthcare Practitioner",
		"Pet",
		"Guardians",
		"E-commerce",
		"Order",
		"POS",
		"Accounting",
		"Warehouse",
		"Audit",
		"Users",
		"Setting",
	),
}

DEMO_PRODUCTS = (
	{
		"product_name": "Royal Canin Cat Dry Food 2kg",
		"sku": "PET-DEMO-RC-CAT-2KG",
		"price": 32,
		"discounted_price": 29,
		"brand": "Royal Canin",
		"category": "Pet Supplies",
		"tags": "cat,dry-food",
	},
	{
		"product_name": "Purina Dog Wet Food 12 Pack",
		"sku": "PET-DEMO-PURINA-DOG-WET",
		"price": 24,
		"discounted_price": 0,
		"brand": "Purina",
		"category": "Pet Supplies",
		"tags": "dog,wet-food",
	},
	{
		"product_name": "Basic Pet Shampoo",
		"sku": "PET-DEMO-SHAMPOO",
		"price": 9,
		"discounted_price": 0,
		"brand": None,
		"category": "Pet Supplies",
		"tags": "grooming,hygiene",
	},
)


def execute():
	original_in_patch = frappe.flags.in_patch
	original_in_migrate = frappe.flags.in_migrate
	frappe.flags.in_patch = True
	frappe.flags.in_migrate = True
	try:
		if should_seed_master_data() or should_seed_demo_data():
			seed_master_data()
		if should_seed_demo_data():
			seed_demo_data()
		frappe.clear_cache()
	finally:
		frappe.flags.in_patch = original_in_patch
		frappe.flags.in_migrate = original_in_migrate


def seed_master_data():
	ensure_price_lists()
	ensure_item_groups()
	ensure_boarding_items()
	ensure_pet_boarding_settings()
	ensure_care_service_categories()
	ensure_care_services()
	ensure_service_rooms()
	ensure_brands()
	ensure_food_types()


def should_seed_master_data() -> bool:
	return bool(frappe.conf.get("pet_app_seed_master_data"))


def should_seed_demo_data() -> bool:
	return bool(frappe.conf.get("pet_app_seed_demo_data"))


def seed_demo_data():
	ensure_demo_guardians()
	ensure_demo_pets()
	ensure_demo_practitioners()
	ensure_demo_manager_user()
	ensure_demo_products()
	ensure_demo_case_sheets_and_visits()
	ensure_demo_boarding_records()


def ensure_price_lists():
	if not frappe.db.exists("DocType", "Price List"):
		return

	currency = _default_currency()
	for row in PRICE_LISTS:
		name = row["price_list_name"]
		if frappe.db.exists("Price List", name):
			frappe.db.set_value(
				"Price List",
				name,
				{"enabled": 1, "selling": row["selling"], "buying": row["buying"]},
				update_modified=False,
			)
			continue

		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": name,
				"currency": currency,
				"enabled": 1,
				"selling": row["selling"],
				"buying": row["buying"],
			}
		).insert(ignore_permissions=True)


def ensure_item_groups():
	if not frappe.db.exists("DocType", "Item Group"):
		return

	for row in ITEM_GROUPS:
		name = row["item_group_name"]
		if frappe.db.exists("Item Group", name):
			frappe.db.set_value("Item Group", name, "is_group", row["is_group"], update_modified=False)
			continue

		doc = frappe.get_doc({"doctype": "Item Group", **row})
		doc.insert(ignore_permissions=True)


def ensure_boarding_items():
	if not frappe.db.exists("DocType", "Item"):
		return

	for row in BOARDING_ITEMS:
		doc = _get_or_new_doc("Item", row["item_code"])
		doc.item_code = row["item_code"]
		doc.item_name = row["item_name"]
		doc.item_group = _existing_item_group("Veterinary Services") or "All Item Groups"
		doc.stock_uom = _existing_uom("Nos") or "Nos"
		doc.is_stock_item = 0
		doc.maintain_stock = 0
		doc.is_sales_item = 1
		doc.is_purchase_item = 0
		doc.standard_rate = flt(row["standard_rate"])
		_save_db_doc(doc)
		ensure_item_prices(doc.name, row["standard_rate"])


def ensure_item_prices(item_code: str, rate: float):
	if not frappe.db.exists("DocType", "Item Price"):
		return

	currency = _default_currency()
	for price_list in ("Standard Selling",):
		if not frappe.db.exists("Price List", price_list):
			continue

		filters = {"item_code": item_code, "price_list": price_list, "selling": 1}
		price_name = frappe.db.get_value("Item Price", filters, "name")
		if price_name:
			frappe.db.set_value(
				"Item Price",
				price_name,
				{"price_list_rate": flt(rate), "currency": currency},
				update_modified=False,
			)
			continue

		doc = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": flt(rate),
				"currency": currency,
				"selling": 1,
			}
		)
		_save_db_doc(doc)


def ensure_pet_boarding_settings():
	if not frappe.db.exists("DocType", "Pet Boarding Settings"):
		return

	values = {"default_boarding_type": "Travel"}
	for row in BOARDING_ITEMS:
		if frappe.db.exists("Item", row["item_code"]):
			values[row["settings_field"]] = row["item_code"]
	frappe.db.set_single_value("Pet Boarding Settings", values)


def ensure_care_service_categories():
	if not frappe.db.exists("DocType", "CategoryCareServices"):
		return

	for row in CARE_SERVICE_CATEGORIES:
		doc = _find_by_field("CategoryCareServices", "category_name", row["category_name"])
		if not doc:
			doc = frappe.new_doc("CategoryCareServices")
		doc.category_name = row["category_name"]
		doc.description = row["description"]
		_save_doc(doc)


def ensure_care_services():
	if not frappe.db.exists("DocType", "CareService template"):
		return

	for row in CARE_SERVICES:
		category = _find_by_field("CategoryCareServices", "category_name", row["category_name"])
		if not category:
			continue

		item_code = ensure_service_item(row["item_code"], row["service_name"], row["default_price"])
		doc = _find_by_field("CareService template", "service_name", row["service_name"])
		if not doc:
			doc = frappe.new_doc("CareService template")
		doc.service_name = row["service_name"]
		doc.item_code = item_code
		doc.animal_species = row["animal_species"]
		doc.frequency = row["frequency"]
		doc.category_id = category.name
		doc.default_price = flt(row["default_price"])
		doc.price_list = "Standard Selling" if frappe.db.exists("Price List", "Standard Selling") else None
		doc.description = row.get("description")
		doc.specimen = row.get("specimen")
		doc.modality = row.get("modality")
		doc.body_part = row.get("body_part")
		doc.service_area = row.get("service_area")
		doc.estimated_turnaround = row.get("estimated_turnaround")
		doc.disabled = 0
		_save_doc(doc)


def ensure_service_item(item_code: str, item_name: str, rate: float) -> str:
	if not frappe.db.exists("DocType", "Item"):
		return item_code

	doc = _get_or_new_doc("Item", item_code)
	doc.item_code = item_code
	doc.item_name = item_name
	doc.item_group = _existing_item_group("Veterinary Services") or "All Item Groups"
	doc.stock_uom = _existing_uom("Nos") or "Nos"
	doc.is_stock_item = 0
	doc.maintain_stock = 0
	doc.is_sales_item = 1
	doc.is_purchase_item = 0
	doc.standard_rate = flt(rate)
	_save_db_doc(doc)
	ensure_item_prices(doc.name, rate)
	return doc.name


def ensure_service_rooms():
	if not frappe.db.exists("DocType", "Service Room"):
		return

	for row in SERVICE_ROOMS:
		doc = _get_or_new_doc("Service Room", row["room_code"])
		doc.room_code = row["room_code"]
		doc.room_name = row["room_name"]
		doc.room_type = row["room_type"]
		doc.status = "Active"
		doc.notes = row["notes"]
		_save_doc(doc)


def ensure_brands():
	if not frappe.db.exists("DocType", "Brand"):
		return

	for row in BRANDS:
		name = row["brand"]
		if frappe.db.exists("Brand", name):
			frappe.db.set_value("Brand", name, "brand", name, update_modified=False)
			continue

		frappe.get_doc({"doctype": "Brand", "brand": name}).insert(ignore_permissions=True)


def ensure_food_types():
	if not frappe.db.exists("DocType", "FoodType"):
		return

	for row in FOOD_TYPES:
		doc = _find_by_field("FoodType", "type_name", row["type_name"])
		if not doc:
			doc = frappe.new_doc("FoodType")
		doc.type_name = row["type_name"]
		doc.description = row["description"]
		_save_doc(doc)


def ensure_demo_guardians():
	if not frappe.db.exists("DocType", "Guardian"):
		return

	for row in DEMO_GUARDIANS:
		doc = _find_by_field("Guardian", "phone", row["phone"])
		if not doc:
			doc = frappe.new_doc("Guardian")
		doc.phone = row["phone"]
		doc.full_name = row["full_name"]
		doc.email_id = row["email_id"]
		doc.address_line1 = row["address_line1"]
		doc.city = row["city"]
		doc.country = "Iraq"
		doc.is_active = 1
		doc.otp_verified = 1
		_save_doc(doc)
		get_or_create_customer_from_guardian(doc.name)


def ensure_demo_pets():
	if not frappe.db.exists("DocType", "Pet"):
		return

	for row in DEMO_PETS:
		guardian = _find_by_field("Guardian", "phone", row["guardian_phone"])
		if not guardian:
			continue

		doc = _find_pet(row["pet_name"], guardian.name)
		if not doc:
			doc = frappe.new_doc("Pet")
		doc.pet_name = row["pet_name"]
		doc.animal_species = row["animal_species"]
		doc.animal_type = row["animal_type"]
		doc.breed = row["breed"]
		doc.gender = row["gender"]
		doc.birth_date = row["birth_date"]
		doc.registration_date = getdate()
		doc.status = "Available"
		doc.pet_status = "Approved"
		doc.weight = flt(row["weight"])
		doc.color = row["color"]
		doc.food_brand = row["food_brand"] if row["food_brand"] and frappe.db.exists("Brand", row["food_brand"]) else None
		doc.food_type = _food_type_name(row["food_type"]) if row["food_type"] else None
		doc.description = f"Demo {row['animal_type'].lower()} profile."
		doc.note = "Seeded demo record for app testing."
		_save_doc(doc)
		ensure_pet_guardian_link(doc.name, guardian.name, "primary_owner")


def ensure_pet_guardian_link(pet: str, guardian: str, role: str):
	if not frappe.db.exists("DocType", "PetGuardian"):
		return

	name = frappe.db.get_value("PetGuardian", {"pet_id": pet, "guardian_id": guardian}, "name")
	if name:
		frappe.db.set_value("PetGuardian", name, "role", role, update_modified=False)
		return

	doc = frappe.get_doc(
		{
			"doctype": "PetGuardian",
			"pet_id": pet,
			"guardian_id": guardian,
			"role": role,
		}
	)
	doc.insert(ignore_permissions=True)


def ensure_demo_practitioners():
	if not frappe.db.exists("DocType", "Healthcare Practitioner"):
		return

	for row in DEMO_PRACTITIONERS:
		doc = _find_by_field("Healthcare Practitioner", "phone", row["phone"])
		if not doc:
			doc = frappe.new_doc("Healthcare Practitioner")
		doc.practitioner_name = row["practitioner_name"]
		doc.practitioner_type = row["practitioner_type"]
		doc.phone = row["phone"]
		doc.specialization = row["specialization"]
		doc.disabled = 0
		_save_doc(doc)


def ensure_demo_manager_user():
	if not frappe.db.exists("DocType", "User"):
		return

	user = _get_or_new_user(DEMO_MANAGER_USER["email"], DEMO_MANAGER_USER["first_name"])
	user.enabled = 1
	user.user_type = "System User"
	user.send_welcome_email = 0
	user.flags.no_welcome_mail = True
	_save_doc(user)

	from frappe.utils.password import update_password

	update_password(DEMO_MANAGER_USER["email"], DEMO_MANAGER_USER["password"])
	ensure_user_roles(DEMO_MANAGER_USER["email"], DEMO_MANAGER_USER["roles"])


def ensure_user_roles(user_id: str, roles: tuple[str, ...]):
	if not frappe.db.exists("User", user_id):
		return

	user = frappe.get_doc("User", user_id)
	existing_roles = {row.role for row in user.roles if row.role}
	changed = False
	for role in roles:
		if not frappe.db.exists("Role", role) or role in existing_roles:
			continue
		user.append("roles", {"role": role})
		existing_roles.add(role)
		changed = True

	if changed:
		user.save(ignore_permissions=True)


def ensure_demo_products():
	if not frappe.db.exists("DocType", "Product"):
		return

	for row in DEMO_PRODUCTS:
		item_code = ensure_stock_item(row["sku"], row["product_name"], row["category"], row.get("brand"), row["price"])
		doc = _find_by_field("Product", "sku", row["sku"])
		if not doc:
			doc = frappe.new_doc("Product")
			doc.name = row["sku"]
		doc.product_name = row["product_name"]
		doc.sku = row["sku"]
		doc.description = f"Demo product: {row['product_name']}"
		doc.price = flt(row["price"])
		doc.discounted_price = flt(row["discounted_price"])
		doc.in_stock = 1
		doc.charge_tax = 0
		doc.brand = row["brand"] if row.get("brand") and frappe.db.exists("Brand", row["brand"]) else None
		if frappe.db.exists("DocType", "Product Category") and frappe.db.exists("Product Category", row["category"]):
			doc.category = row["category"]
			doc.item_group = frappe.db.get_value("Product Category", row["category"], "item_group")
		else:
			doc.category = row["category"] if frappe.db.exists("Item Group", row["category"]) else "All Item Groups"
			doc.item_group = doc.category if frappe.db.exists("Item Group", doc.category) else None
		doc.status = "Active"
		doc.tags = row["tags"]
		doc.item = item_code
		doc.item_price = _item_price_name(item_code, "Standard Selling")
		doc.has_variants = 0
		_save_product_doc(doc)


def ensure_stock_item(item_code: str, item_name: str, item_group: str, brand: str | None, rate: float) -> str:
	if not frappe.db.exists("DocType", "Item"):
		return item_code

	doc = _get_or_new_doc("Item", item_code)
	doc.item_code = item_code
	doc.item_name = item_name
	doc.item_group = item_group if frappe.db.exists("Item Group", item_group) else "All Item Groups"
	doc.brand = brand if brand and frappe.db.exists("Brand", brand) else None
	doc.stock_uom = _existing_uom("Nos") or "Nos"
	doc.is_stock_item = 1
	doc.is_sales_item = 1
	doc.is_purchase_item = 1
	doc.standard_rate = flt(rate)
	_save_db_doc(doc)
	ensure_item_prices(doc.name, rate)
	return doc.name


def ensure_demo_case_sheets_and_visits():
	if not frappe.db.exists("DocType", "Vet Case Sheet") or not frappe.db.exists("DocType", "Vet Visit"):
		return

	doctor = _first_demo_doctor()
	luna = _find_pet("Luna", _guardian_by_phone("+9647700001001"))
	max_pet = _find_pet("Max", _guardian_by_phone("+9647700001002"))
	if not doctor or not luna or not max_pet:
		return

	luna_case = ensure_case_sheet(
		pet=luna.name,
		guardian=_guardian_by_phone("+9647700001001"),
		chief_complaint="Vaccination",
		symptom_duration="Unknown",
		intake_notes="Annual vaccine visit seeded for demo workflow.",
	)
	ensure_visit(
		case_sheet=luna_case.name,
		doctor=doctor,
		status="Completed",
		illness="Preventive Care",
		diagnosis="Healthy patient. Preventive vaccination visit.",
		treatment_plan="Administer core vaccine and monitor for adverse reaction.",
		doctor_notes="Normal exam. No complications observed.",
	)

	max_case = ensure_case_sheet(
		pet=max_pet.name,
		guardian=_guardian_by_phone("+9647700001002"),
		chief_complaint="Loss of Appetite",
		symptom_duration="1-3 days",
		intake_notes="Reduced appetite with mild lethargy.",
		has_loss_of_appetite=1,
		has_lethargy=1,
	)
	ensure_visit(
		case_sheet=max_case.name,
		doctor=doctor,
		status="In Progress",
		illness="Gastrointestinal",
		diagnosis="Pending doctor assessment.",
		treatment_plan="Initial exam and CBC if symptoms persist.",
		doctor_notes="Seeded open visit for workflow testing.",
	)


def ensure_case_sheet(pet: str, guardian: str, **values):
	name = frappe.db.get_value(
		"Vet Case Sheet",
		{
			"animal_patient": pet,
			"guardian": guardian,
			"chief_complaint": values["chief_complaint"],
			"intake_notes": values["intake_notes"],
		},
		"name",
	)
	doc = frappe.get_doc("Vet Case Sheet", name) if name else frappe.new_doc("Vet Case Sheet")
	doc.guardian = guardian
	doc.animal_patient = pet
	doc.case_sheet_date = now_datetime()
	doc.status = "Waiting Practitioner"
	doc.priority = "Normal"
	doc.chief_complaint = values["chief_complaint"]
	doc.symptom_duration = values["symptom_duration"]
	doc.vaccination_status = "Unknown"
	doc.neutered_spayed = "Unknown"
	doc.appetite_status = "Reduced" if values.get("has_loss_of_appetite") else "Normal"
	doc.water_intake_status = "Normal"
	doc.urination_status = "Normal"
	doc.defecation_status = "Normal"
	doc.activity_status = "Less Active" if values.get("has_lethargy") else "Normal"
	doc.feeding_type = "Dry Food"
	doc.housing_environment = "Indoor"
	doc.intake_notes = values["intake_notes"]
	doc.has_loss_of_appetite = values.get("has_loss_of_appetite", 0)
	doc.has_lethargy = values.get("has_lethargy", 0)
	_save_doc(doc)
	return doc


def ensure_visit(case_sheet: str, doctor: str, **values):
	name = frappe.db.get_value("Vet Visit", {"case_sheet": case_sheet}, "name")
	doc = frappe.get_doc("Vet Visit", name) if name else frappe.new_doc("Vet Visit")
	doc.case_sheet = case_sheet
	doc.doctor = doctor
	doc.visit_datetime = now_datetime()
	doc.visit_type = "Consultation"
	doc.status = values["status"]
	doc.illness = values["illness"]
	doc.diagnosis = values["diagnosis"]
	doc.treatment_plan = values["treatment_plan"]
	doc.doctor_notes = values["doctor_notes"]
	doc.examination_notes = "Seeded clinical examination data."
	doc.temperature = 38.4
	doc.heart_rate = 92
	doc.respiratory_rate = 24
	_save_doc(doc)
	return doc


def ensure_demo_boarding_records():
	if not frappe.db.exists("DocType", "Pet Boarding"):
		return

	luna = _find_pet("Luna", _guardian_by_phone("+9647700001001"))
	max_pet = _find_pet("Max", _guardian_by_phone("+9647700001002"))
	if luna:
		ensure_boarding_record(
			room="KENNEL-01",
			pet=luna.name,
			guardian=_guardian_by_phone("+9647700001001"),
			boarding_type="Travel",
			record_status="Reserved",
			deposit=10,
			note="Reserved travel boarding demo record.",
		)
	if max_pet:
		ensure_boarding_record(
			room="WARD-01",
			pet=max_pet.name,
			guardian=_guardian_by_phone("+9647700001002"),
			boarding_type="Treatment",
			record_status="Checked In",
			deposit=25,
			note="Checked-in treatment boarding demo record.",
		)


def ensure_boarding_record(room: str, pet: str, guardian: str, boarding_type: str, record_status: str, deposit: float, note: str):
	if not (frappe.db.exists("Service Room", room) and pet and guardian):
		return

	name = frappe.db.get_value(
		"Pet Boarding",
		{"service_room": room, "pet": pet, "guardian": guardian, "docstatus": ["<", 2]},
		"name",
	)
	doc = frappe.get_doc("Pet Boarding", name) if name else frappe.new_doc("Pet Boarding")
	doc.service_room = room
	doc.pet = pet
	doc.guardian = guardian
	doc.boarding_type = boarding_type
	doc.record_status = record_status
	doc.check_in = now_datetime() if record_status == "Checked In" else add_days(now_datetime(), 1)
	doc.check_out = add_days(doc.check_in, 2)
	doc.deposit = flt(deposit)
	doc.note = note
	item_code = "PET-BOARDING-TREATMENT" if boarding_type == "Treatment" else "PET-BOARDING-TRAVEL"
	rate = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": "Standard Selling"}, "price_list_rate")
	doc.set("billable_items", [])
	doc.append(
		"billable_items",
		{
			"item_name": frappe.db.get_value("Item", item_code, "item_name"),
			"item_code": item_code,
			"item_type": "Room Stay",
			"qty": 2,
			"rate": flt(rate),
			"status": "Billable",
			"note": "Seeded room stay charge.",
			"linked_service_id": f"room_stay:{boarding_type.lower()}",
		},
	)
	_save_doc(doc)
	return doc


def _get_or_new_doc(doctype: str, name: str):
	if frappe.db.exists(doctype, name):
		return frappe.get_doc(doctype, name)

	doc = frappe.new_doc(doctype)
	doc.name = name
	return doc


def _get_or_new_user(email: str, first_name: str):
	if frappe.db.exists("User", email):
		return frappe.get_doc("User", email)

	return frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first_name,
			"full_name": first_name,
			"enabled": 1,
			"user_type": "System User",
			"send_welcome_email": 0,
		}
	)


def _find_by_field(doctype: str, fieldname: str, value: str):
	name = frappe.db.get_value(doctype, {fieldname: value}, "name")
	if name:
		return frappe.get_doc(doctype, name)
	return None


def _save_doc(doc):
	doc.flags.ignore_permissions = True
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc


def _save_product_doc(doc):
	return _save_db_doc(doc)


def _save_db_doc(doc):
	doc.flags.ignore_permissions = True
	if doc.is_new():
		if not doc.name:
			doc.set_new_name()
		doc.db_insert()
	else:
		doc.db_update()
	return doc


def _default_currency() -> str:
	return (
		frappe.db.get_single_value("Global Defaults", "default_currency")
		or frappe.db.get_default("currency")
		or "USD"
	)


def _existing_item_group(item_group: str) -> str | None:
	if frappe.db.exists("Item Group", item_group):
		return item_group
	return None


def _existing_uom(uom: str) -> str | None:
	if frappe.db.exists("UOM", uom):
		return uom
	return None


def _guardian_by_phone(phone: str) -> str | None:
	return frappe.db.get_value("Guardian", {"phone": phone}, "name")


def _find_pet(pet_name: str, guardian: str | None):
	if not guardian:
		return None

	rows = frappe.db.sql(
		"""
		SELECT p.name
		FROM `tabPet` p
		INNER JOIN `tabPetGuardian` pg ON pg.pet_id = p.name
		WHERE p.pet_name = %s
		  AND pg.guardian_id = %s
		ORDER BY p.creation ASC
		LIMIT 1
		""",
		(pet_name, guardian),
		as_dict=True,
	)
	if rows:
		return frappe.get_doc("Pet", rows[0].name)

	name = frappe.db.get_value("Pet", {"pet_name": pet_name}, "name")
	return frappe.get_doc("Pet", name) if name else None


def _food_type_name(type_name: str) -> str | None:
	return frappe.db.get_value("FoodType", {"type_name": type_name}, "name")


def _item_price_name(item_code: str, price_list: str) -> str | None:
	return frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": price_list, "selling": 1}, "name")


def _first_demo_doctor() -> str | None:
	for row in DEMO_PRACTITIONERS:
		name = frappe.db.get_value("Healthcare Practitioner", {"phone": row["phone"]}, "name")
		if name:
			return name
	return None
