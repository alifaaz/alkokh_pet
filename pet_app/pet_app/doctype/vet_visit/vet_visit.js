// Copyright (c) 2026, solvers and contributors
// For license information, please see license.txt

frappe.ui.form.on("Vet Visit", {
	setup(frm) {
		frm.set_query("doctor", () => ({
			filters: {
				disabled: 0,
			},
		}));
	},

	refresh(frm) {
		frm.set_df_property("case_sheet", "read_only", !frm.is_new() && !!frm.doc.case_sheet ? 1 : 0);

		if (frm.doc.case_sheet) {
			frm.add_custom_button(__("Open Case Sheet"), () => {
				frappe.set_route("Form", "Vet Case Sheet", frm.doc.case_sheet);
			});
		}

		if (!frm.is_new() && !frm.doc.billed) {
			frm.add_custom_button(__("Create Invoice"), () => {
				const createInvoice = () =>
					frappe.call({
						method: "pet_app.pet_app.doctype.vet_visit.vet_visit.create_sales_invoice",
						args: {
							visit_name: frm.doc.name,
						},
						freeze: true,
						freeze_message: __("Creating Sales Invoice..."),
						callback: ({ message }) => {
							if (!message?.sales_invoice) {
								return;
							}

							frappe.show_alert({
								message: __("Sales Invoice {0} created", [message.sales_invoice]),
								indicator: "green",
							});
							frm.reload_doc().then(() => {
								frappe.set_route("Form", "Sales Invoice", message.sales_invoice);
							});
						},
					});

				if (frm.is_dirty()) {
					frm.save().then(() => createInvoice());
					return;
				}

				createInvoice();
			}).addClass("btn-primary");
		}

		if (frm.doc.sales_invoice) {
			frm.add_custom_button(__("Open Invoice"), () => {
				frappe.set_route("Form", "Sales Invoice", frm.doc.sales_invoice);
			});
		}

		set_child_queries(frm);
	},

	case_sheet(frm) {
		if (!frm.doc.case_sheet) {
			return;
		}

		frappe.db.get_doc("Vet Case Sheet", frm.doc.case_sheet).then((case_sheet) => {
			const updates = {};

			if (!frm.doc.customer) {
				updates.customer = case_sheet.customer;
			}

			if (!frm.doc.animal_patient) {
				updates.animal_patient = case_sheet.animal_patient;
			}

			if (!frm.doc.weight && case_sheet.weight) {
				updates.weight = case_sheet.weight;
			}

			if (!frm.doc.case_summary) {
				const summary = [
					case_sheet.chief_complaint ? __("Chief Complaint: {0}", [case_sheet.chief_complaint]) : "",
					case_sheet.symptom_duration ? __("Duration: {0}", [case_sheet.symptom_duration]) : "",
					case_sheet.intake_notes ? __("Intake Notes: {0}", [case_sheet.intake_notes]) : "",
				]
					.filter(Boolean)
					.join("\n");

				if (summary) {
					updates.case_summary = summary;
				}
			}

			if (Object.keys(updates).length) {
				frm.set_value(updates);
			}
		});
	},
});


function set_child_queries(frm) {
	if (frm.fields_dict.prescribed_medications) {
		frm.fields_dict.prescribed_medications.grid.get_field("medication_item").get_query = () => ({
			filters: {
				disabled: 0,
			},
		});
	}

	if (frm.fields_dict.requested_services) {
		frm.fields_dict.requested_services.grid.get_field("care_service").get_query = () => ({
			filters: {
				animal_species: ["in", get_pet_species_filters(frm)],
			},
		});
	}

	if (frm.fields_dict.lab_requests) {
		frm.fields_dict.lab_requests.grid.get_field("lab_service").get_query = () => ({
			filters: {
				animal_species: ["in", get_pet_species_filters(frm)],
			},
		});
	}
}


function get_pet_species_filters(frm) {
	if (!frm.doc.animal_patient) {
		return ["Mammal", "Bird", "Reptile", "Amphibian", "Fish", "Insect", "Arachnid", "Crustacean"];
	}

	return ["Mammal", "Bird", "Reptile", "Amphibian", "Fish", "Insect", "Arachnid", "Crustacean"];
}


function update_child_amount(cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "amount", (flt(row.qty) || 0) * (flt(row.rate) || 0));
}


async function set_item_pricing(cdt, cdn, item_code) {
	if (!item_code) {
		return;
	}

	const { message } = await frappe.call({
		method: "pet_app.pet_app.doctype.vet_visit.vet_visit.get_item_billing_details",
		args: {
			item_code,
		},
	});

	if (!message) {
		return;
	}

	await frappe.model.set_value(cdt, cdn, "rate", message.rate || 0);
	update_child_amount(cdt, cdn);
}


async function set_care_service_pricing(cdt, cdn, care_service_name, item_fieldname) {
	if (!care_service_name) {
		return;
	}

	const { message } = await frappe.call({
		method: "pet_app.pet_app.doctype.vet_visit.vet_visit.get_care_service_billing_details",
		args: {
			care_service_name,
		},
	});

	if (!message) {
		return;
	}

	await frappe.model.set_value(cdt, cdn, item_fieldname, message.item_code || null);
	await frappe.model.set_value(cdt, cdn, "rate", message.rate || 0);
	update_child_amount(cdt, cdn);
}


frappe.ui.form.on("Vet Visit Medication Item", {
	medication_item(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		set_item_pricing(cdt, cdn, row.medication_item);
	},

	qty(frm, cdt, cdn) {
		update_child_amount(cdt, cdn);
	},

	rate(frm, cdt, cdn) {
		update_child_amount(cdt, cdn);
	},
});


frappe.ui.form.on("Vet Visit Service Item", {
	care_service(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		set_care_service_pricing(cdt, cdn, row.care_service, "item_code");
	},

	qty(frm, cdt, cdn) {
		update_child_amount(cdt, cdn);
	},

	rate(frm, cdt, cdn) {
		update_child_amount(cdt, cdn);
	},
});


frappe.ui.form.on("Vet Visit Lab Request Item", {
	lab_service(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		set_care_service_pricing(cdt, cdn, row.lab_service, "item_code");
	},

	qty(frm, cdt, cdn) {
		update_child_amount(cdt, cdn);
	},

	rate(frm, cdt, cdn) {
		update_child_amount(cdt, cdn);
	},
});
