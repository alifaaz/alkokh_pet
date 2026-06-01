// Copyright (c) 2025, solvers and contributors
// For license information, please see license.txt

frappe.ui.form.on("Pet", {
	setup(frm) {
		frm.set_query("breed", () => {
			const filters = { enabled: 1 };
			if (frm.doc.animal_species) {
				filters.animal_species = frm.doc.animal_species;
			}
			if (frm.doc.animal_type) {
				filters.animal_type = frm.doc.animal_type;
			}
			return { filters };
		});
	},

	animal_species(frm) {
		frm.set_value("breed", null);
	},

	animal_type(frm) {
		frm.set_value("breed", null);
	},
});
