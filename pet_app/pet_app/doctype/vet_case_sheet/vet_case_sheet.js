// Copyright (c) 2026, solvers and contributors
// For license information, please see license.txt

frappe.ui.form.on("Vet Case Sheet", {
	refresh(frm) {
		if (!frm.is_new() && can_start_visit(frm)) {
			frm.add_custom_button(__("Start Visit"), () => {
				frappe.call({
					method: "pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet.start_visit",
					args: {
						case_sheet_name: frm.doc.name,
					},
					freeze: true,
					freeze_message: __("Creating Vet Visit..."),
					callback: ({ message }) => {
						if (!message?.name) {
							return;
						}

						frappe.set_route("Form", message.doctype || "Vet Visit", message.name);
					},
				});
			}).addClass("btn-primary");
		}
	},

	guardian(frm) {
		if (!frm.doc.guardian) {
			return;
		}

		frappe.db.get_value("Guardian", frm.doc.guardian, ["customer_id", "phone"]).then(({ message }) => {
			if (!message) {
				return;
			}

			frm.set_value({
				customer: message.customer_id || null,
				phone_number: message.phone || frm.doc.phone_number,
			});
		});
	},

	animal_patient(frm) {
		if (!frm.doc.animal_patient) {
			return;
		}

		frappe.call({
			method: "pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet.get_pet_context",
			args: {
				pet_name: frm.doc.animal_patient,
			},
			callback: ({ message }) => {
				if (!message) {
					return;
				}

				frm.set_value({
					guardian: frm.doc.guardian || message.guardian,
					customer: frm.doc.customer || message.customer,
					phone_number: frm.doc.phone_number || message.phone_number,
					species: message.species,
					breed: message.breed,
					sex: message.sex,
					age_text: message.age_text,
					weight: frm.doc.weight || message.weight,
				});
			},
		});
	},
});


function can_start_visit(frm) {
	return (
		frappe.model.can_create("Vet Visit") &&
		frm.doc.docstatus === 0 &&
		!frm.doc.vet_visit &&
		["Draft", "Waiting Doctor"].includes(frm.doc.status)
	);
}
