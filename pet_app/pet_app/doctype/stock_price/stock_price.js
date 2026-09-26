// Copyright (c) 2026, solvers and contributors
// For license information, please see license.txt

const GET_ITEMS_METHOD = "pet_app.pet_app.doctype.stock_price.stock_price.get_items";

frappe.ui.form.on("Stock Price", {
	setup(frm) {
		// Item Price cannot exist for a template item; ERPNext throws rather than skips.
		frm.set_query("item_code", "items", () => {
			return { filters: { has_variants: 0 } };
		});
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Get Items"), () => open_get_items(frm)).addClass("btn-primary");
			frm.set_intro(
				__("Leave a new price blank or 0 to keep the current one. Save to see what each row will do."),
				"blue"
			);
			return;
		}

		if (frm.doc.docstatus === 1) {
			frm.set_intro(
				__("Applied {0} selling and {1} buying prices on {2}. Cancel restores them.", [
					frm.doc.selling_changes || 0,
					frm.doc.buying_changes || 0,
					frappe.datetime.str_to_user(frm.doc.applied_on),
				]),
				"green"
			);
		} else {
			frm.set_intro(__("Cancelled. The prices this document had set were restored."), "red");
		}
	},
});

function open_get_items(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Get Items"),
		fields: [
			{ fieldname: "item_group", fieldtype: "Link", label: __("Item Group"), options: "Item Group" },
			{
				fieldname: "include_child_groups",
				fieldtype: "Check",
				label: __("Include Sub-groups"),
				default: 1,
				depends_on: "item_group",
			},
			{ fieldname: "brand", fieldtype: "Link", label: __("Brand"), options: "Brand" },
			{ fieldname: "cb", fieldtype: "Column Break" },
			{ fieldname: "search", fieldtype: "Data", label: __("Name or Code Contains") },
			{ fieldname: "include_disabled", fieldtype: "Check", label: __("Include Disabled Items") },
			{
				fieldname: "unpriced_only",
				fieldtype: "Check",
				label: __("Only Items With No Selling Price"),
			},
			{ fieldname: "limit", fieldtype: "Int", label: __("Maximum Rows"), default: 500 },
		],
		primary_action_label: __("Add Items"),
		primary_action(values) {
			dialog.hide();
			return fetch_items(frm, values);
		},
	});
	dialog.show();
}

async function fetch_items(frm, values) {
	const response = await frappe.call({
		method: GET_ITEMS_METHOD,
		args: values,
		freeze: true,
		freeze_message: __("Loading items..."),
	});

	const payload = response.message || {};
	if (payload.ok === false) {
		const error = (payload.errors || [])[0] || {};
		frappe.msgprint({
			title: __("Could not load items"),
			message: error.message || __("Request failed."),
			indicator: "red",
		});
		return;
	}

	add_rows(frm, (payload.data || {}).items || [], payload.meta || {});
}

function add_rows(frm, items, meta) {
	// Items already in the table keep whatever was typed into them - validate refuses a
	// duplicate item_code anyway, so re-adding one is never the right answer.
	const present = new Set((frm.doc.items || []).map((row) => row.item_code).filter(Boolean));
	let added = 0;
	let skipped = 0;

	items.forEach((item) => {
		if (present.has(item.item_code)) {
			skipped += 1;
			return;
		}
		present.add(item.item_code);

		const row = frm.add_child("items");
		row.item_code = item.item_code;
		row.item_name = item.item_name;
		row.stock_uom = item.stock_uom;
		row.disabled = item.disabled;
		row.current_selling_rate = item.current_selling_rate || 0;
		row.current_buying_rate = item.current_buying_rate || 0;
		row.selling_item_price = item.selling_item_price;
		row.buying_item_price = item.buying_item_price;
		added += 1;
	});

	frm.refresh_field("items");

	const lines = [__("Added {0} items.", [added])];
	if (skipped) {
		lines.push(__("{0} were already in the table and were left as they are.", [skipped]));
	}
	if (meta.truncated) {
		lines.push(
			__("More items matched than the limit of {0}. Narrow the filters or raise the limit.", [
				meta.limit,
			])
		);
	}
	frappe.show_alert({ message: lines.join(" "), indicator: added ? "green" : "orange" }, 7);
}
