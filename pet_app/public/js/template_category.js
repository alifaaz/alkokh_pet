// Pet App Template Screen (and, before p1_31, Pet App Template Category): surface_key and page_key
// are picked from their registries, not typed. Since p1_31 a screen record lists the categories it
// shows; the category's own keys are read-only legacy values, so its form shows no pickers.
// Loaded on every desk page through app_include_js (hooks.py): doctype_js is never applied to a
// custom doctype. It only registers a form handler, so it is inert everywhere else.
//
// WHY A DIALOG AND NOT A PICKER FIELD. Both keys must stay unique (the DB index is the
// backstop for SURFACE_KEY_TAKEN / PAGE_KEY_TAKEN), and Frappe allows `unique` only on Data,
// Link, Read Only and Int - not Select or Autocomplete. A Select would also refuse, on every save, a stored
// key the registry no longer holds; a Link would need a doctype kept in sync with the code
// registry. So the field stays Data, and the picker lives here:
//
// * the input is made read-only, so a key cannot be mistyped;
// * "Choose send surface" opens a searchable list of send_targets.SEND_SURFACES, and
//   "Choose page" one of the page.* rows in Pet App Access Settings -> Page Access, each
//   loaded at runtime - a surface added in code, or a page added in desk, appears with no patch;
// * a stored key its registry does not hold is listed too, "(not in registry)", and shown
//   as-is on the form, so it is never silently blanked;
// * choosing nothing clears the binding.
//
// If this script does not load, each field is plain Data again - editable free text, still
// unique and still validated on the server.
//
// Wrapped so nothing leaks into the global scope of every desk page, and the labels are built
// at form refresh (not at load) so they are translated.
(() => {
// One picker per binding key. Both follow the same rules; only the source and wording differ.
const pickers = () => [
	{
		fieldname: "surface_key",
		method: "pet_app.api.notifications.list_surface_key_options",
		button: __("Choose send surface"),
		label: __("Send surface"),
		help: __("The send dialog this screen record is for. Its categories replace its page's when both are passed."),
		unknown: __("is not in the send surface registry, so no dialog matches it."),
		clear: __("Leave empty if this record is for a page instead."),
	},
	{
		fieldname: "page_key",
		method: "pet_app.api.notifications.list_page_key_options",
		button: __("Choose page"),
		label: __("Page"),
		help: __("The page this screen record is for. A send surface record on the same page replaces it."),
		unknown: __("matches no row in Page Access, so no page resolves to it."),
		clear: __("Leave empty if this record is for a send surface instead."),
	},
];

const handlers = {
	refresh(frm) {
		frm.__picker_options = frm.__picker_options || {};
		// Only a screen's categories are picked here, and only enabled ones are offered.
		if (frm.fields_dict.categories) frm.set_query("categories", () => ({ filters: { enabled: 1 } }));
		for (const picker of pickers()) {
			const field = frm.fields_dict[picker.fieldname];
			// A read-only key is a legacy value (the category's, since p1_31): shown, not edited.
			if (!field || field.df.read_only) continue;
			frm.set_df_property(picker.fieldname, "read_only", 1);
			load_picker_options(frm, picker).then(options => {
				describe_picker_value(frm, picker, options);
				frm.add_custom_button(picker.button, () => choose_picker_value(frm, picker, options));
			});
		}
	},
	surface_key(frm) {
		redescribe(frm, "surface_key");
	},
	page_key(frm) {
		redescribe(frm, "page_key");
	},
};
frappe.ui.form.on("Pet App Template Screen", handlers);
frappe.ui.form.on("Pet App Template Category", handlers);

function redescribe(frm, fieldname) {
	const picker = pickers().find(p => p.fieldname === fieldname);
	describe_picker_value(frm, picker, (frm.__picker_options || {})[fieldname] || []);
}

function load_picker_options(frm, picker) {
	return frappe.call({ method: picker.method }).then(({ message }) => {
		const options = (message && message.ok && message.data && message.data.options) || [];
		frm.__picker_options[picker.fieldname] = options;
		return options;
	});
}

function describe_picker_value(frm, picker, options) {
	const value = frm.doc[picker.fieldname];
	const match = options.find(option => option.value === value);
	let text = picker.help;
	if (value && match && match.registered) text = __("{0} ({1}).", [match.label, value]) + " " + text;
	else if (value) text = value + " " + picker.unknown + " " + text;
	frm.set_df_property(picker.fieldname, "description", text);
}

function choose_picker_value(frm, picker, options) {
	const dialog = new frappe.ui.Dialog({
		title: picker.button,
		fields: [
			{
				fieldname: "value",
				fieldtype: "Autocomplete",
				label: picker.label,
				options: options.map(({ value, label, description }) => ({ value, label, description })),
				default: frm.doc[picker.fieldname] || "",
				description: picker.clear,
			},
		],
		primary_action_label: __("Set"),
		primary_action(values) {
			frm.set_value(picker.fieldname, values.value || "");
			dialog.hide();
		},
	});
	dialog.show();
}
})();
