frappe.ui.form.on("Rating", {
	setup(frm) {
		frm.set_query("questionnaire", () => {
			return { filters: { active: 1 } };
		});
	},

	questionnaire(frm) {
		return populate_questionnaire_answers(frm);
	},
});

async function populate_questionnaire_answers(frm) {
	if (!frm.doc.questionnaire) {
		frm.clear_table("answers");
		frm.refresh_field("answers");
		return;
	}

	const questionnaire = await frappe.db.get_doc("Rating Questionnaire", frm.doc.questionnaire);
	const existing_by_key = {};

	(frm.doc.answers || []).forEach((row) => {
		if (row.question_key) {
			existing_by_key[row.question_key] = row;
		}
	});

	frm.clear_table("answers");
	(questionnaire.questions || [])
		.slice()
		.sort((left, right) => {
			const left_order = left.sort_order || left.idx || 0;
			const right_order = right.sort_order || right.idx || 0;
			return left_order - right_order;
		})
		.forEach((question) => {
			const existing = existing_by_key[question.question_key] || {};
			const row = frm.add_child("answers");
			row.question_key = question.question_key;
			row.question_text = question.question_text;
			row.required = question.required;
			row.rating = existing.rating;
			row.note = existing.note;
			row.sort_order = question.sort_order || question.idx;
		});

	frm.refresh_field("answers");
}
