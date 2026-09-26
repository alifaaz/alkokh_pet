// Copyright (c) 2026, solvers and contributors
// For license information, please see license.txt

const PRE_STE_MIGRATE_METHOD =
	"pet_app.pet_app.doctype.pre_define_stock_entry.pre_define_stock_entry.migrate_pre_define_stock_entries";

frappe.ui.form.on("Pre Define Stock Entry", {
	setup(frm) {
		const leaf_warehouse_of_company = () => ({ filters: { is_group: 0, company: frm.doc.company } });
		frm.set_query("from_warehouse", leaf_warehouse_of_company);
		frm.set_query("to_warehouse", leaf_warehouse_of_company);
		frm.set_query("s_warehouse", "items", leaf_warehouse_of_company);
		frm.set_query("t_warehouse", "items", leaf_warehouse_of_company);
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.company) {
			frm.set_value("company", frappe.defaults.get_user_default("Company"));
		}
	},

	refresh(frm) {
		if (frm.doc.status === "Migrated") {
			frm.disable_form();
			frm.set_intro(__("Migrated to Stock Entry {0}. This document is locked.", [frm.doc.stock_entry]), "green");
		} else if (frm.doc.status === "Error") {
			frm.set_intro(__("Last migration failed. Fix the document and migrate again."), "red");
		}

		if (frm.doc.stock_entry) {
			frm.add_custom_button(__("Open Stock Entry"), () =>
				frappe.set_route("Form", "Stock Entry", frm.doc.stock_entry)
			);
		}

		if (!frm.is_new() && frm.doc.status !== "Migrated") {
			frm.add_custom_button(__("Migrate to Stock Entry"), () => migrate_pre_define_stock_entry(frm)).addClass(
				"btn-primary"
			);
		}
	},

	stock_entry_type(frm) {
		if (!frm.doc.stock_entry_type) {
			frm.set_value("purpose", null);
			return;
		}
		frappe.db.get_value("Stock Entry Type", frm.doc.stock_entry_type, "purpose").then((r) => {
			frm.set_value("purpose", (r.message || {}).purpose || null);
		});
	},
});

async function migrate_pre_define_stock_entry(frm) {
	if (frm.is_dirty()) {
		await frm.save();
	}
	const r = await frappe.call({
		method: PRE_STE_MIGRATE_METHOD,
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
		frappe.show_alert({ message: __("Draft Stock Entry created"), indicator: "green" });
	}
	frm.reload_doc();
}
