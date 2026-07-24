// Copyright (c) 2026, solvers and contributors
// For license information, please see license.txt

frappe.ui.form.on("Vet Visit", {
	refresh(frm) {
		frm.set_df_property("case_sheet", "read_only", !frm.is_new() && !!frm.doc.case_sheet ? 1 : 0);

		if (frm.doc.case_sheet) {
			frm.add_custom_button(__("Open Case Sheet"), () => {
				frappe.set_route("Form", "Vet Case Sheet", frm.doc.case_sheet);
			});
		}

		if (!frm.is_new() && !frm.doc.billed) {
			frm.add_custom_button(__("New Lab"), () => {
				frappe.new_doc("Lab", {
					visit: frm.doc.name,
					pet: frm.doc.animal_patient,
					doctor: frm.doc.doctor,
				});
			}, __("Create"));

			frm.add_custom_button(__("New Imaging"), () => {
				frappe.new_doc("Imaging", {
					visit: frm.doc.name,
					pet: frm.doc.animal_patient,
					doctor: frm.doc.doctor,
				});
			}, __("Create"));
		}

		if (!frm.is_new()) {
			frm.add_custom_button(__("View Labs"), () => {
				frappe.set_route("List", "Lab", {
					visit: frm.doc.name,
				});
			}, __("View"));

			frm.add_custom_button(__("View Imaging"), () => {
				frappe.set_route("List", "Imaging", {
					visit: frm.doc.name,
				});
			}, __("View"));
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

			if (!frm.doc.guardian) {
				updates.guardian = case_sheet.guardian;
			}

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

	const clinicalCategories = {
		assessment_findings: "Assessment Finding",
		client_observations: "Client Observation",
		owner_instruction_items: "Owner Instruction",
	};

	Object.entries(clinicalCategories).forEach(([parentfield, category]) => {
		if (!frm.fields_dict[parentfield]) {
			return;
		}
		frm.set_query("option", parentfield, () => ({
			filters: {
				category,
				enabled: 1,
			},
		}));
	});
}
