import frappe


def get_permission_query_conditions(user=None):
	if not user:
		user = frappe.session.user

	roles = set(frappe.get_roles(user))

	full_access = {
		"System Manager",
		"Administrator",
		"Pet App Admin",
		"Healthcare Administrator",
		"Healthcare Coordinator Read",
		"Coordinator",
		"Coordinatorr",
		"Doctor",
		"Lab Admin",
		"Radiology Admin",
	}
	service_provider_roles = {"Service Provider", "Groomer", "Nursing User", "Service Provider Manager"}

	# Service Provider sees only their own services.
	from pet_app.utils.practitioner import get_practitioner_for_user

	practitioner = get_practitioner_for_user(user)

	if roles & service_provider_roles and not roles & full_access:
		if practitioner:
			return f"`tabPetCareService`.`provider` = {frappe.db.escape(practitioner)}"
		return "1=0"

	# Full access roles see everything.
	if roles & full_access:
		return ""

	if practitioner:
		return f"`tabPetCareService`.`provider` = {frappe.db.escape(practitioner)}"

	# No practitioner linked means see nothing.
	return "1=0"
