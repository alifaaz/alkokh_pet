frappe.ui.form.on("Pet App WhatsApp Action Rule", {
	refresh(frm) {
		frm.add_custom_button(__("Configure Trigger"), () => configure_trigger(frm));
		frm.add_custom_button(__("Configure Replies"), () => configure_replies(frm));
		frm.add_custom_button(__("Preview"), () => preview_action(frm));
	},
});

function parse_json(value, fallback) {
	try { return value ? JSON.parse(value) : fallback; }
	catch (error) { return fallback; }
}

function configure_trigger(frm) {
	const stored = parse_json(frm.doc.condition_json, { all: [] });
	const dialog = new frappe.ui.Dialog({
		title: __("Trigger Conditions"),
		fields: [
			{ fieldname: "match", fieldtype: "Select", label: __("Match"), options: "All\nAny", default: stored.any ? "Any" : "All" },
			{
				fieldname: "conditions", fieldtype: "Table", label: __("Conditions"), in_place_edit: true,
				data: stored.all || stored.any || [],
				fields: [
					{ fieldname: "field", fieldtype: "Data", label: __("Field"), reqd: 1, in_list_view: 1 },
					{ fieldname: "operator", fieldtype: "Select", label: __("Operator"), options: "equals\nnot_equals\nin\nnot_in\nis_set\nis_not_set\nchanged\nchanged_to", reqd: 1, in_list_view: 1 },
					{ fieldname: "value_text", fieldtype: "Data", label: __("Value(s)"), in_list_view: 1 },
				],
			},
		],
		primary_action_label: __("Apply"),
		primary_action(values) {
			const rows = (values.conditions || []).map((row) => {
				const raw = row.value_text !== undefined ? row.value_text : row.value;
				const value = ["in", "not_in", "changed_to"].includes(row.operator)
					? String(raw || "").split(",").map((part) => part.trim()).filter(Boolean)
					: raw;
				return { field: row.field, operator: row.operator, value };
			});
			frm.set_value("condition_json", JSON.stringify({ [values.match === "Any" ? "any" : "all"]: rows }, null, 2));
			dialog.hide();
		},
	});
	const table = dialog.fields_dict.conditions.df.data;
	table.forEach((row) => { if (row.value_text === undefined) row.value_text = Array.isArray(row.value) ? row.value.join(", ") : row.value; });
	dialog.show();
}

function configure_replies(frm) {
	const stored = parse_json(frm.doc.response_config_json, { options: [] });
	const dialog = new frappe.ui.Dialog({
		title: __("WhatsApp Replies"),
		fields: [
			{ fieldname: "response_type", fieldtype: "Select", label: __("Type"), options: "Buttons\nList\nTyped Reply", default: frm.doc.response_type || "Typed Reply", reqd: 1 },
			{ fieldname: "button_text", fieldtype: "Data", label: __("Menu Button"), default: stored.button_text || "Choose" },
			{ fieldname: "section_title", fieldtype: "Data", label: __("Section Title"), default: stored.section_title || "Options" },
			{
				fieldname: "options", fieldtype: "Table", label: __("Reply Options"), in_place_edit: true,
				data: (stored.options || []).map((row) => ({ ...row, aliases_text: (row.aliases || []).join(", ") })),
				fields: [
					{ fieldname: "key", fieldtype: "Data", label: __("Key"), reqd: 1, in_list_view: 1 },
					{ fieldname: "label", fieldtype: "Data", label: __("Label"), reqd: 1, in_list_view: 1 },
					{ fieldname: "value", fieldtype: "Data", label: __("Value"), in_list_view: 1 },
					{ fieldname: "aliases_text", fieldtype: "Data", label: __("Aliases"), in_list_view: 1 },
					{ fieldname: "description", fieldtype: "Data", label: __("Description") },
				],
			},
		],
		primary_action_label: __("Apply"),
		primary_action(values) {
			const options = (values.options || []).map((row) => ({
				key: row.key,
				label: row.label,
				value: row.value || row.key,
				aliases: String(row.aliases_text || "").split(",").map((part) => part.trim()).filter(Boolean),
				...(row.description ? { description: row.description } : {}),
			}));
			frm.set_value("response_type", values.response_type);
			frm.set_value("response_config_json", JSON.stringify({ button_text: values.button_text, section_title: values.section_title, options }, null, 2));
			dialog.hide();
		},
	});
	dialog.show();
}

async function preview_action(frm) {
	if (!frm.doc.template_key) {
		frappe.msgprint(__("Choose a message template first."));
		return;
	}
	if (frm.is_dirty()) await frm.save();
	const templateResponse = await frappe.xcall("pet_app.api.notifications.get_template", {
		name: frm.doc.template_key,
	});
	const template = unwrap(templateResponse).template || {};
	const context = parse_json(template.sample_context_json, {});
	const previewResponse = await frappe.xcall("pet_app.api.notifications.preview_template", {
		template_key: template.template_key || frm.doc.template_key,
		context,
	});
	const message = unwrap(previewResponse).preview || template.body_preview || "";
	const config = parse_json(frm.doc.response_config_json, { options: [] });
	const options = config.options || [];
	const controls = render_preview_controls(frm.doc.response_type, config, options);
	frappe.msgprint({
		title: __("WhatsApp Action Preview"),
		message: `
			<div style="max-width:560px;padding:18px;background:#f1eee7;border:1px solid var(--border-color)">
				<div style="max-width:480px;margin-left:auto;background:#e4f4ec;border:1px solid #c7e5d7;border-radius:8px;overflow:hidden;box-shadow:0 1px 1px rgba(20,32,30,.08)">
					<div style="padding:12px 13px 8px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere">${format_whatsapp_text(message)}</div>
					${controls}
					<div style="padding:3px 10px 7px;color:#687673;font-size:10px;text-align:right">${frappe.datetime.now_time()}</div>
				</div>
			</div>`,
		wide: true,
	});
}

function render_preview_controls(responseType, config, options) {
	if (responseType === "Buttons") {
		return `<div style="border-top:1px solid #c7e5d7">${options.map((row) => `
			<div style="min-height:38px;padding:9px 12px;border-bottom:1px solid #c7e5d7;color:#147052;text-align:center">${frappe.utils.escape_html(row.label || row.key || "")}</div>
		`).join("")}</div>`;
	}
	if (responseType === "List") {
		return `<div style="padding:0 10px 10px">
			<div style="min-height:38px;padding:9px 12px;border:1px solid #b8d9cc;border-radius:4px;background:#fff;color:#147052;text-align:center">${frappe.utils.escape_html(config.button_text || __("Choose"))}</div>
			<div style="margin-top:6px;border:1px solid #dfe3e3;background:#fff">
				${options.map((row) => `<div style="padding:8px 10px;border-bottom:1px solid #edf0ef"><strong>${frappe.utils.escape_html(row.label || row.key || "")}</strong>${row.description ? `<br><small>${frappe.utils.escape_html(row.description)}</small>` : ""}</div>`).join("")}
			</div>
		</div>`;
	}
	if (responseType === "Typed Reply" && options.length) {
		return `<div style="padding:0 12px 9px;color:#687673;font-size:11px">${options.map((row) => frappe.utils.escape_html(row.label || row.key || "")).join(" · ")}</div>`;
	}
	return "";
}

function format_whatsapp_text(value) {
	return frappe.utils.escape_html(String(value || ""))
		.replace(/`([^`\n]+)`/g, "<code>$1</code>")
		.replace(/\*([^*\n]+)\*/g, "<strong>$1</strong>")
		.replace(/_([^_\n]+)_/g, "<em>$1</em>")
		.replace(/~([^~\n]+)~/g, "<s>$1</s>");
}

function unwrap(response) {
	if (response && response.ok === false) {
		frappe.throw(response.errors?.[0]?.message || response.message || __("Request failed"));
	}
	return (response && response.data) || response || {};
}
