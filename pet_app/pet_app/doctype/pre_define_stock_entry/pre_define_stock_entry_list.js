// Copyright (c) 2026, solvers and contributors
// For license information, please see license.txt

frappe.listview_settings["Pre Define Stock Entry"] = {
	add_fields: ["status", "stock_entry"],

	get_indicator(doc) {
		const colors = { Draft: "orange", Migrated: "green", Error: "red" };
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},

	onload(listview) {
		listview.page.add_action_item(__("Migrate selected"), () => {
			const names = listview.get_checked_items(true);
			if (!names.length) {
				frappe.msgprint(__("Select at least one row."));
				return;
			}
			frappe
				.call({
					method: "pet_app.pet_app.doctype.pre_define_stock_entry.pre_define_stock_entry.migrate_pre_define_stock_entries",
					args: { names },
					freeze: true,
					freeze_message: __("Migrating {0} documents...", [names.length]),
				})
				.then((r) => {
					show_stock_entry_migration_result(r.message || {});
					listview.refresh();
				});
		});
	},
};

function show_stock_entry_migration_result(result) {
	if (result.queued) {
		frappe.msgprint(
			__("{0} documents were queued for migration in the background. Refresh the list in a minute.", [result.count])
		);
		return;
	}
	const failed = Object.keys(result.failed || {});
	const migrated = (result.migrated || []).length;
	let message = __("Migrated: {0}", [migrated]);
	if (failed.length) {
		message += "<br>" + __("Failed: {0}", [failed.length]) + "<br>";
		message += failed
			.map((name) => `<b>${frappe.utils.escape_html(name)}</b>: ${frappe.utils.escape_html(result.failed[name])}`)
			.join("<br>");
	}
	frappe.msgprint({ title: __("Migration result"), message, indicator: failed.length ? "orange" : "green" });
}
