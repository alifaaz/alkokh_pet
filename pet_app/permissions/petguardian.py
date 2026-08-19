import frappe

# Roles that administer the whole tenant. Checked FIRST, before the guardian
# link below, because clinic staff own pets too: a vet whose own dog is on file
# is a Guardian row as well as an employee, and narrowing them to their own
# animal would lock them out of their job rather than protect anything.
FULL_ACCESS_ROLES = {
	"Administrator",
	"System Manager",
	"Pet App Admin",
	"Healthcare Administrator",
}


def get_permission_query_conditions(user=None, doctype=None):
	"""Scope raw PetGuardian list reads to what a method would have returned.

	`/api/resource/PetGuardian?filters=[["guardian_id","=",X]]` is how the pet
	pickers actually load a guardian's animals - no whitelisted method is
	involved, so nothing enforced inside one applies. Role permission alone does
	not help: read is granted to 16 roles and none of them is row-scoped, so any
	holder can swap X for any other guardian and enumerate that owner's pets.

	This mirrors `pet_app.api.pet._require_pet_read_access`, so the raw path can
	no longer return MORE than the method would:

	  - full-access roles      -> everything
	  - a user linked to a Guardian -> only their own links
	  - anyone else            -> whatever their role grants, unscoped

	It deliberately says nothing about deceased pets. Hiding the dead is a
	property of the CALL SITE, not of the user - the same operator needs them
	hidden in a boarding picker and shown in a death report - and a condition
	here would apply to both. That half lives in
	`pet_app.api.pet.get_guardian_pets`, behind `include_deceased`.

	Note the asymmetry with Pet: a `permission_query_conditions` hook is only
	consulted for the query's PRIMARY doctype (frappe/model/db_query.py, in
	get_permission_query_conditions), so a condition on Pet would NOT constrain
	the `pet_id.*` columns this query joins in. Scoping the link table is what
	actually binds.
	"""
	if not user:
		user = frappe.session.user

	if user == "Administrator":
		return ""

	if set(frappe.get_roles(user)) & FULL_ACCESS_ROLES:
		return ""

	guardian = frappe.db.get_value("Guardian", {"user_id": user}, "name")
	if guardian:
		return f"`tabPetGuardian`.`guardian_id` = {frappe.db.escape(guardian)}"

	# Staff with no Guardian record of their own: UNSCOPED, and deliberately so.
	#
	# This is the gap, stated plainly rather than left to be discovered: a
	# receptionist at one clinic can still list every guardian link on the site.
	# It is narrower than what shipped before - a guardian could previously read
	# any other guardian's pets, which is the disclosure that actually mattered -
	# and narrowing it correctly is worth more than extending it half-designed.
	#
	# Closing it needs three things this file cannot decide on its own:
	#
	#   1. A column to scope BY. Neither PetGuardian nor Pet carries `branch`, so
	#      there is nothing to filter on today. `pet_app.utils.branch` is the
	#      existing machinery - SCOPED_DOCTYPES plus stamp_branch_on_insert - and
	#      closing this means adding `branch` to one of the two doctypes and
	#      joining that set, not inventing a second scoping scheme here.
	#   2. A backfill, because scoping an unstamped column empties every picker on
	#      the site at once. Live rows need a branch before the filter goes on,
	#      which is a patch, and a rule for rows whose branch cannot be inferred.
	#   3. An answer for the pet treated at more than one clinic. A pet is a
	#      long-lived record that outlives any single visit, so branch on the ANIMAL
	#      is the wrong axis unless cross-branch care is explicitly allowed for -
	#      the reason Pet Boarding is deliberately kept out of SCOPED_DOCTYPES is
	#      the same shape of problem.
	#
	# Until those are settled, guessing here would silently empty working pickers
	# and read as a security control while enforcing nothing.
	return ""
