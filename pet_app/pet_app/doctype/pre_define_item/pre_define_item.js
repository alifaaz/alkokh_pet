// Copyright (c) 2026, solvers and contributors
// For license information, please see license.txt

const PRE_ITEM_MIGRATE_METHOD = "pet_app.pet_app.doctype.pre_define_item.pre_define_item.migrate_pre_define_items";

frappe.ui.form.on("Pre Define Item", {
	setup(frm) {
		frm.set_query("parent_item_group", () => ({ filters: { is_group: 1 } }));
	},

	refresh(frm) {
		if (frm.doc.status === "Migrated") {
			frm.disable_form();
			frm.set_intro(__("Migrated to Item {0}. This row is locked.", [frm.doc.item]), "green");
		} else if (frm.doc.status === "Error") {
			frm.set_intro(__("Last migration failed. Fix the row and migrate again."), "red");
		}

		if (frm.doc.item) {
			frm.add_custom_button(__("Open Item"), () => frappe.set_route("Form", "Item", frm.doc.item));
		}

		if (!frm.is_new() && frm.doc.status !== "Migrated") {
			frm.add_custom_button(__("Migrate to Item"), () => migrate_pre_define_item(frm)).addClass("btn-primary");
		}
	},
});

async function migrate_pre_define_item(frm) {
	if (frm.is_dirty()) {
		await frm.save();
	}
	const r = await frappe.call({
		method: PRE_ITEM_MIGRATE_METHOD,
		args: { names: [frm.doc.name] },
		freeze: true,
		freeze_message: __("Migrating..."),
	});
	const result = r.message || {};
	const failure = result.failed && result.failed[frm.doc.name];
	if (failure) {
		frappe.msgprint({
			title: __("Migration failed"),
			indicator: "red",
			message: frappe.utils.escape_html(failure).replace(/\n/g, "<br>"),
		});
	} else {
		frappe.show_alert({ message: __("Migrated to Item"), indicator: "green" });
	}
	frm.reload_doc();
}
