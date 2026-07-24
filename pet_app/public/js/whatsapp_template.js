frappe.ui.form.on("Pet App WhatsApp Template", {
	refresh(frm) {
		frm.add_custom_button(__("Insert Variable"), () => open_variable_picker(frm));
		frm.add_custom_button(__("Preview"), () => preview_template(frm));
	},
});

async function open_variable_picker(frm) {
	const response = await frappe.xcall("pet_app.api.notifications.list_whatsapp_template_variables", {
		source_doctype: frm.doc.source_doctype || null,
	});
	const variables = ((response.data || response).variables || []);
	const dialog = new frappe.ui.Dialog({
		title: __("Insert Variable"),
		fields: [
			{ fieldname: "search", fieldtype: "Data", label: __("Search") },
			{ fieldname: "variables", fieldtype: "HTML" },
		],
	});
	const render = (term = "") => {
		term = term.trim().toLowerCase();
		const rows = variables.filter((row) => row.key.toLowerCase().includes(term));
		dialog.fields_dict.variables.$wrapper.html(`
			<div class="wa-variable-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px;max-height:360px;overflow:auto">
				${rows.map((row) => `<button class="btn btn-default btn-sm text-left" data-token="${frappe.utils.escape_html(row.token)}">${frappe.utils.escape_html(row.key)}</button>`).join("")}
			</div>
		`);
		dialog.fields_dict.variables.$wrapper.find("button").on("click", (event) => {
			const token = event.currentTarget.dataset.token;
			const control = frm.fields_dict.body_preview;
			const current = frm.doc.body_preview || "";
			const input = control.$input && control.$input[0];
			const start = input && Number.isInteger(input.selectionStart) ? input.selectionStart : current.length;
			frm.set_value("body_preview", `${current.slice(0, start)}${token}${current.slice(start)}`);
			dialog.hide();
		});
	};
	dialog.fields_dict.search.$input.on("input", (event) => render(event.currentTarget.value));
	render();
	dialog.show();
}

async function preview_template(frm) {
	if (frm.is_dirty()) await frm.save();
	let context = {};
	try { context = JSON.parse(frm.doc.sample_context_json || "{}"); }
	catch (error) { frappe.throw(__("Sample Context JSON is invalid")); }
	const response = await frappe.xcall("pet_app.api.notifications.preview_template", {
		template_key: frm.doc.template_key,
		context,
	});
	const preview = (response.data || response).preview || "";
	frappe.msgprint({
		title: __("WhatsApp Preview"),
		message: `<div style="max-width:520px;padding:14px;border:1px solid var(--border-color);border-radius:8px;background:#e4f4ec;white-space:pre-wrap">${frappe.utils.escape_html(preview)}</div>`,
		wide: true,
	});
}
