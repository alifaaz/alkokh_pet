from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr


ASSESSMENT_FINDING = "Assessment Finding"
CLIENT_OBSERVATION = "Client Observation"
OWNER_INSTRUCTION = "Owner Instruction"

SELECTION_FIELD_CATEGORIES = {
	"assessment_findings": ASSESSMENT_FINDING,
	"client_observations": CLIENT_OBSERVATION,
	"owner_instruction_items": OWNER_INSTRUCTION,
}

NOTE_FIELD_BY_SELECTION_FIELD = {
	"assessment_findings": "assessment_note",
	"client_observations": "doctor_note",
	"owner_instruction_items": "owner_instruction_note",
}


@dataclass(frozen=True)
class ClinicalOption:
	code: str
	category: str
	label_en: str
	label_ar: str
	sort_order: int
	enabled: int = 1


CLINICAL_OPTIONS: tuple[ClinicalOption, ...] = (
	ClinicalOption("assessment_emaciated", ASSESSMENT_FINDING, "Emaciated", "هزيل", 10),
	ClinicalOption("assessment_anemia", ASSESSMENT_FINDING, "Anemia", "فقر دم", 20),
	ClinicalOption("assessment_shock", ASSESSMENT_FINDING, "Shock", "صدمة", 30),
	ClinicalOption("assessment_coma", ASSESSMENT_FINDING, "Coma", "غيبوبة", 40),
	ClinicalOption("assessment_hemorrhage", ASSESSMENT_FINDING, "Hemorrhage", "نزيف حاد", 50),
	ClinicalOption("assessment_abdominal_pain", ASSESSMENT_FINDING, "Abdominal pain", "ألم بطني", 60),
	ClinicalOption("assessment_aggressive", ASSESSMENT_FINDING, "Aggressive", "عدواني", 70),
	ClinicalOption("assessment_bleeding", ASSESSMENT_FINDING, "Bleeding", "نزيف", 80),
	ClinicalOption("assessment_bradycardia", ASSESSMENT_FINDING, "Bradycardia", "بطء القلب", 90),
	ClinicalOption("assessment_dysphagia", ASSESSMENT_FINDING, "Dysphagia", "صعوبة البلع", 100),
	ClinicalOption("assessment_paralysis", ASSESSMENT_FINDING, "Paralysis", "شلل", 110),
	ClinicalOption("assessment_liver_issue", ASSESSMENT_FINDING, "Liver issue", "مشكلة كبد", 120),
	ClinicalOption("assessment_tachycardia", ASSESSMENT_FINDING, "Tachycardia", "تسارع القلب", 130),
	ClinicalOption("assessment_odor", ASSESSMENT_FINDING, "Odor", "رائحة غير طبيعية", 140),
	ClinicalOption("assessment_nausea", ASSESSMENT_FINDING, "Nausea", "غثيان", 150),
	ClinicalOption("assessment_irritation", ASSESSMENT_FINDING, "Irritation", "تهيج", 160),
	ClinicalOption("assessment_kidney_issue", ASSESSMENT_FINDING, "Kidney issue", "مشكلة كلى", 170),
	ClinicalOption("assessment_lethargy", ASSESSMENT_FINDING, "Lethargy", "خمول", 180),
	ClinicalOption("assessment_anorexia", ASSESSMENT_FINDING, "Anorexia", "فقدان شهية", 190),
	ClinicalOption("assessment_salivation", ASSESSMENT_FINDING, "Salivation", "سيلان لعاب", 200),
	ClinicalOption("assessment_poor_hair_coat", ASSESSMENT_FINDING, "Poor hair coat", "سوء حالة الفرو", 210),
	ClinicalOption("assessment_congestion", ASSESSMENT_FINDING, "Congestion", "احتقان", 220),
	ClinicalOption("assessment_weakness", ASSESSMENT_FINDING, "Weakness", "ضعف", 230),
	ClinicalOption("assessment_bad_nutrition", ASSESSMENT_FINDING, "Bad nutrition", "سوء تغذية", 240),
	ClinicalOption("assessment_electric_shock", ASSESSMENT_FINDING, "Electric shock", "صدمة كهربائية", 250),
	ClinicalOption("assessment_hair_loss", ASSESSMENT_FINDING, "Hair loss", "تساقط الشعر", 260),
	ClinicalOption("assessment_obesity", ASSESSMENT_FINDING, "Obesity", "سمنة", 270),
	ClinicalOption("assessment_food_disturbance", ASSESSMENT_FINDING, "Food disturbance", "اضطراب غذائي", 280),
	ClinicalOption("assessment_fungal", ASSESSMENT_FINDING, "Fungal", "فطريات", 290),
	ClinicalOption("assessment_abdominal_distention", ASSESSMENT_FINDING, "Abdominal distention", "انتفاخ بطني", 300),
	ClinicalOption("assessment_mouth_problems", ASSESSMENT_FINDING, "Mouth problems", "مشاكل الفم", 310),
	ClinicalOption("assessment_shivering", ASSESSMENT_FINDING, "Shivering", "ارتجاف", 320),
	ClinicalOption("assessment_panting", ASSESSMENT_FINDING, "Panting", "لهاث", 330),
	ClinicalOption("assessment_ocular_discharge", ASSESSMENT_FINDING, "Ocular discharge", "إفرازات العين", 340),
	ClinicalOption("assessment_change_eye_color", ASSESSMENT_FINDING, "Change in eye color", "تغير لون العين", 350),
	ClinicalOption("assessment_allergy", ASSESSMENT_FINDING, "Allergy", "حساسية", 360),
	ClinicalOption("assessment_lameness", ASSESSMENT_FINDING, "Lameness", "عرج", 370),
	ClinicalOption("assessment_trauma", ASSESSMENT_FINDING, "Trauma", "إصابة", 380),
	ClinicalOption("client_many_questions", CLIENT_OBSERVATION, "Asks many questions and doubts everything", "يسأل كثيرا ويشكك في كل شيء", 10),
	ClinicalOption("client_gets_angry_easily", CLIENT_OBSERVATION, "Gets angry easily", "يغضب بسهولة", 20),
	ClinicalOption("client_price_focused", CLIENT_OBSERVATION, "Focuses on price over care quality", "يركز على السعر أكثر من جودة الرعاية", 30),
	ClinicalOption("client_misses_appointments", CLIENT_OBSERVATION, "Does not keep appointments", "لا يلتزم بالمواعيد", 40),
	ClinicalOption("client_nonadherent_home_treatment", CLIENT_OBSERVATION, "Does not follow home treatment instructions", "لا يلتزم بتعليمات العلاج المنزلي", 50),
	ClinicalOption("client_late_paying", CLIENT_OBSERVATION, "Late paying", "يتأخر في الدفع", 60),
	ClinicalOption("client_needs_clear_explanation", CLIENT_OBSERVATION, "Needs clear repeated explanations", "يحتاج إلى شرح واضح ومتكرر", 70),
	ClinicalOption("client_anxious_about_pet", CLIENT_OBSERVATION, "Very anxious about the pet", "قلق جدا على الحيوان", 80),
	ClinicalOption("client_declines_recommended_tests", CLIENT_OBSERVATION, "Declines recommended tests", "يرفض الفحوصات الموصى بها", 90),
	ClinicalOption("client_declines_hospitalization", CLIENT_OBSERVATION, "Declines hospitalization", "يرفض التنويم", 100),
	ClinicalOption("client_requests_unnecessary_treatment", CLIENT_OBSERVATION, "Requests unnecessary treatment", "يطلب علاجا غير ضروري", 110),
	ClinicalOption("client_sensitive_to_cost", CLIENT_OBSERVATION, "Sensitive to treatment cost", "حساس تجاه تكلفة العلاج", 120),
	ClinicalOption("client_prefers_arabic", CLIENT_OBSERVATION, "Prefers Arabic communication", "يفضل التواصل بالعربية", 130),
	ClinicalOption("client_prefers_english", CLIENT_OBSERVATION, "Prefers English communication", "يفضل التواصل بالإنجليزية", 140),
	ClinicalOption("client_requires_senior_doctor", CLIENT_OBSERVATION, "Requests senior doctor review", "يطلب مراجعة طبيب كبير", 150),
	ClinicalOption("client_difficult_to_reach", CLIENT_OBSERVATION, "Difficult to reach by phone", "يصعب الوصول إليه هاتفيا", 160),
	ClinicalOption("client_needs_followup_reminder", CLIENT_OBSERVATION, "Needs follow-up reminders", "يحتاج إلى تذكير بالمتابعة", 170),
	ClinicalOption("client_history_uncertain", CLIENT_OBSERVATION, "Gives uncertain history", "يعطي تاريخا مرضيا غير واضح", 180),
	ClinicalOption("client_multiple_decision_makers", CLIENT_OBSERVATION, "Multiple decision makers involved", "يوجد أكثر من صاحب قرار", 190),
	ClinicalOption("client_record_home_care", CLIENT_OBSERVATION, "Records home care well", "يوثق الرعاية المنزلية بشكل جيد", 200),
	ClinicalOption("client_handles_pet_poorly", CLIENT_OBSERVATION, "Handles pet poorly", "يتعامل مع الحيوان بطريقة غير مناسبة", 210),
	ClinicalOption("client_high_expectations", CLIENT_OBSERVATION, "Has high expectations", "توقعاته عالية", 220),
	ClinicalOption("client_needs_written_plan", CLIENT_OBSERVATION, "Needs written plan", "يحتاج إلى خطة مكتوبة", 230),
	ClinicalOption("client_cooperative", CLIENT_OBSERVATION, "Cooperative", "متعاون", 240),
	ClinicalOption("owner_give_medication_exactly", OWNER_INSTRUCTION, "Give medication exactly as prescribed", "أعط الدواء تماما حسب الوصفة", 10),
	ClinicalOption("owner_keep_appointments", OWNER_INSTRUCTION, "Keep appointments", "التزم بالمواعيد", 20),
	ClinicalOption("owner_report_changes", OWNER_INSTRUCTION, "Report any change in the animal", "أبلغ عن أي تغير في الحيوان", 30),
	ClinicalOption("owner_clean_water", OWNER_INSTRUCTION, "Provide clean water", "وفر ماء نظيفا", 40),
	ClinicalOption("owner_avoid_strenuous_activity", OWNER_INSTRUCTION, "Avoid strenuous activity", "تجنب النشاط المجهد", 50),
	ClinicalOption("owner_monitor_appetite", OWNER_INSTRUCTION, "Monitor appetite", "راقب الشهية", 60),
	ClinicalOption("owner_monitor_urination", OWNER_INSTRUCTION, "Monitor urination and stool", "راقب التبول والبراز", 70),
	ClinicalOption("owner_keep_wound_clean", OWNER_INSTRUCTION, "Keep wound clean and dry", "حافظ على الجرح نظيفا وجافا", 80),
	ClinicalOption("owner_do_not_bathe", OWNER_INSTRUCTION, "Do not bathe until cleared", "لا تحمم الحيوان حتى يسمح الطبيب", 90),
	ClinicalOption("owner_use_collar", OWNER_INSTRUCTION, "Use the protective collar", "استخدم الطوق الواقي", 100),
	ClinicalOption("owner_return_if_worse", OWNER_INSTRUCTION, "Return if symptoms worsen", "راجع العيادة إذا ساءت الأعراض", 110),
	ClinicalOption("owner_isolate_pet", OWNER_INSTRUCTION, "Isolate the pet if advised", "اعزل الحيوان إذا أوصى الطبيب", 120),
	ClinicalOption("owner_food_as_directed", OWNER_INSTRUCTION, "Feed only as directed", "قدم الطعام حسب توجيهات الطبيب فقط", 130),
	ClinicalOption("owner_prevent_licking", OWNER_INSTRUCTION, "Prevent licking or scratching", "امنع اللعق أو الحك", 140),
	ClinicalOption("owner_complete_course", OWNER_INSTRUCTION, "Complete the full treatment course", "أكمل كورس العلاج كاملا", 150),
	ClinicalOption("owner_no_human_medicine", OWNER_INSTRUCTION, "Do not give human medicine", "لا تعط أدوية بشرية", 160),
	ClinicalOption("owner_watch_side_effects", OWNER_INSTRUCTION, "Watch for side effects", "راقب أي آثار جانبية", 170),
	ClinicalOption("owner_call_before_changes", OWNER_INSTRUCTION, "Call before changing treatment", "اتصل قبل تغيير العلاج", 180),
)

OPTIONS_BY_CODE = {option.code: option for option in CLINICAL_OPTIONS}
OPTIONS_BY_CATEGORY = {
	category: tuple(option for option in CLINICAL_OPTIONS if option.category == category)
	for category in (ASSESSMENT_FINDING, CLIENT_OBSERVATION, OWNER_INSTRUCTION)
}


def clinical_options_payload(category: str | None = None) -> dict[str, list[dict[str, Any]]] | list[dict[str, Any]]:
	if category:
		return [option_payload(option) for option in options_for_category(category)]
	return {
		selection_field: [option_payload(option) for option in options_for_category(category)]
		for selection_field, category in SELECTION_FIELD_CATEGORIES.items()
	}


def clinical_catalogue_choices(category: str) -> list[dict[str, str]]:
	return [
		{
			"code": option.code,
			"label_en": option.label_en,
			"label_ar": option.label_ar,
		}
		for option in sorted(options_for_category(category), key=lambda option: option.sort_order)
	]


def option_payload(option: ClinicalOption) -> dict[str, Any]:
	return {
		"name": option.code,
		"code": option.code,
		"option": option.code,
		"category": option.category,
		"label_en": option.label_en,
		"label_ar": option.label_ar,
		"sort_order": option.sort_order,
		"enabled": option.enabled,
	}


def options_for_category(category: str | None = None) -> tuple[ClinicalOption, ...]:
	if not category:
		return CLINICAL_OPTIONS
	return OPTIONS_BY_CATEGORY.get(category, ())


def get_option(code: str | None) -> ClinicalOption | None:
	return OPTIONS_BY_CODE.get(cstr(code).strip())


def category_for_selection_field(fieldname: str | None) -> str | None:
	return SELECTION_FIELD_CATEGORIES.get(cstr(fieldname).strip())


def validate_option_code(code: str | None, *, category: str | None = None, fieldname: str | None = None) -> ClinicalOption:
	code = cstr(code).strip()
	if not code:
		frappe.throw(_("Clinical option is required."))
	option = get_option(code)
	if not option:
		frappe.throw(_("Unknown clinical option: {0}").format(frappe.bold(code)))
	expected_category = category or category_for_selection_field(fieldname)
	if expected_category and option.category != expected_category:
		frappe.throw(
			_("Clinical option {0} belongs to {1}, not {2}.").format(
				frappe.bold(code), frappe.bold(option.category), frappe.bold(expected_category)
			)
		)
	return option


def validate_selection_row(row) -> None:
	validate_option_code(row.get("option"), fieldname=row.get("parentfield"))


def validate_visit_clinical_selections(visit) -> None:
	for fieldname, category in SELECTION_FIELD_CATEGORIES.items():
		seen = set()
		for row in visit.get(fieldname) or []:
			option = validate_option_code(row.get("option"), category=category)
			if option.code in seen:
				frappe.throw(_("Duplicate clinical option: {0}").format(frappe.bold(option.code)))
			seen.add(option.code)


def has_internal_clinical_note(visit) -> bool:
	if cstr(visit.get("doctor_note")).strip():
		return True
	return bool(visit.get("client_observations") or [])


def selection_items_payload(rows, *, fieldname: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
	expected_category = category or category_for_selection_field(fieldname)
	items = []
	for row in rows or []:
		option = validate_option_code(row.get("option"), category=expected_category)
		item = option_payload(option)
		item.update({"idx": row.get("idx"), "row_name": row.get("name")})
		items.append(item)
	items.sort(key=lambda item: (cint(item.get("sort_order")), cint(item.get("idx"))))
	return items


def selection_codes(rows, *, fieldname: str | None = None, category: str | None = None) -> list[str]:
	return [item["code"] for item in selection_items_payload(rows, fieldname=fieldname, category=category)]


def clinical_group_payload(visit, selection_field: str, note_field: str | None = None) -> dict[str, Any]:
	note_field = note_field or NOTE_FIELD_BY_SELECTION_FIELD.get(selection_field)
	items = selection_items_payload(visit.get(selection_field), fieldname=selection_field)
	return {
		"selected": [item["code"] for item in items],
		"items": items,
		"note": visit.get(note_field) if note_field else None,
	}


def visit_clinical_payload(visit) -> dict[str, Any]:
	assessment = clinical_group_payload(visit, "assessment_findings", "assessment_note")
	client = clinical_group_payload(visit, "client_observations", "doctor_note")
	owner = clinical_group_payload(visit, "owner_instruction_items", "owner_instruction_note")
	return {
		"intake_summary": visit.get("intake_summary"),
		"overview": visit.get("overview"),
		"examination": visit.get("examination_notes"),
		"examination_notes": visit.get("examination_notes"),
		"case_summary": visit.get("case_summary"),
		"diagnosis": visit.get("diagnosis"),
		"treatment_plan": visit.get("treatment_plan"),
		"plan": visit.get("treatment_plan"),
		"assessment": assessment,
		"assessment_findings": assessment["items"],
		"assessment_note": assessment["note"],
		"client_observations": client["items"],
		"doctor_note": client["note"],
		"doctor_notes": client,
		"owner_instruction_items": owner["items"],
		"owner_instruction_note": owner["note"],
		"instructions": owner,
	}


def apply_structured_clinical_payload(visit, payload: dict[str, Any]) -> None:
	if not payload:
		return
	_apply_selection_payload(visit, payload, "assessment_findings", aliases=("assessment",))
	_apply_note_payload(visit, payload, "assessment_note", aliases=("assessment",))
	_apply_selection_payload(visit, payload, "client_observations", aliases=("doctor_notes",))
	_apply_note_payload(visit, payload, "doctor_note", aliases=("doctor_notes",))
	_apply_selection_payload(visit, payload, "owner_instruction_items", aliases=("instructions",))
	_apply_note_payload(visit, payload, "owner_instruction_note", aliases=("instructions",))
	validate_visit_clinical_selections(visit)


def clinical_selection_text(visit, selection_field: str, *, language: str = "en") -> str:
	label_field = "label_ar" if language == "ar" else "label_en"
	return ", ".join(cstr(item.get(label_field)).strip() for item in selection_items_payload(visit.get(selection_field), fieldname=selection_field) if item.get(label_field))


def _apply_selection_payload(visit, payload: dict[str, Any], fieldname: str, *, aliases: tuple[str, ...] = ()) -> None:
	if fieldname in payload:
		raw_value = payload.get(fieldname)
	else:
		raw_value = _first_nested_value(payload, aliases, keys=("selected", "items", "options"))
		if raw_value is None:
			return
	if not visit.meta.has_field(fieldname):
		return
	codes = _extract_option_codes(raw_value)
	category = category_for_selection_field(fieldname)
	visit.set(fieldname, [])
	for code in codes:
		option = validate_option_code(code, category=category)
		visit.append(fieldname, {"option": option.code})


def _apply_note_payload(visit, payload: dict[str, Any], fieldname: str, *, aliases: tuple[str, ...] = ()) -> None:
	if fieldname in payload:
		raw_value = payload.get(fieldname)
	else:
		raw_value = _first_nested_value(payload, aliases, keys=("note", "text", "value"))
		if raw_value is None:
			for alias in aliases:
				if isinstance(payload.get(alias), str):
					raw_value = payload.get(alias)
					break
			else:
				return
	if visit.meta.has_field(fieldname):
		visit.set(fieldname, cstr(raw_value).strip() if raw_value is not None else None)


def _first_nested_value(payload: dict[str, Any], aliases: tuple[str, ...], *, keys: tuple[str, ...]) -> Any:
	for alias in aliases:
		value = payload.get(alias)
		if not isinstance(value, dict):
			continue
		for key in keys:
			if key in value:
				return value.get(key)
	return None


def _extract_option_codes(value: Any) -> list[str]:
	if value is None or value == "":
		return []
	if isinstance(value, str):
		return [code for code in (part.strip() for part in value.split(",")) if code]
	if isinstance(value, dict):
		for key in ("code", "option", "name", "value"):
			if value.get(key):
				return [cstr(value.get(key)).strip()]
		return []
	codes = []
	for item in value:
		if isinstance(item, str):
			code = item
		elif isinstance(item, dict):
			code = item.get("option") or item.get("code") or item.get("name") or item.get("value")
		else:
			code = getattr(item, "option", None) or getattr(item, "code", None) or getattr(item, "name", None)
		code = cstr(code).strip()
		if code and code not in codes:
			codes.append(code)
	return codes
