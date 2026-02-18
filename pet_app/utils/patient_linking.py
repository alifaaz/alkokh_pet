import frappe


def _pick_patient_sex(pet_gender: str | None) -> str:
    """
    يرجّع قيمة Sex صحيحة حسب خيارات Patient.sex الفعلية في نظامك.
    يدعم: Male/Female/Other/Unknown (حسب الموجود).
    """
    pet_gender = (pet_gender or "").strip()

    meta = frappe.get_meta("Patient")
    sex_field = meta.get_field("sex")
    opts = []
    if sex_field and sex_field.options:
        opts = [o.strip() for o in sex_field.options.split("\n") if o.strip()]

    # أولاً حاول Male/Female
    if pet_gender in ("Male", "Female") and pet_gender in opts:
        return pet_gender

    # Unknown/Other حسب الموجود
    for fallback in ("Other", "Unknown", "Male", "Female"):
        if fallback in opts:
            return fallback

    # إذا ماكو خيارات واضحة (نادر جداً)
    return pet_gender or "Other"


def _get_default_naming_series(doctype: str, fieldname: str = "naming_series") -> str | None:
    """
    يرجع default naming_series إذا الحقل موجود.
    """
    meta = frappe.get_meta(doctype)
    f = meta.get_field(fieldname)
    if not f:
        return None

    # default بالدوكتايب
    if f.default:
        return f.default

    # أول خيار من options
    if f.options:
        options = [o.strip() for o in f.options.split("\n") if o.strip()]
        return options[0] if options else None

    return None


def get_or_create_patient_for_pet(pet_name: str, guardian_id: str | None = None) -> str:
    pet = frappe.get_doc("Pet", pet_name)

    # 0) إذا patient_id موجود بس مو Patient فعلي (مثل z3tr) نكسره
    if getattr(pet, "patient_id", None) and not frappe.db.exists("Patient", pet.patient_id):
        pet.db_set("patient_id", None)

    # 1) إذا مرتبط صح، رجّع
    if getattr(pet, "patient_id", None) and frappe.db.exists("Patient", pet.patient_id):
        return pet.patient_id

    # 2) جيب Guardian
    if not guardian_id:
        guardian_id = (
            frappe.db.get_value("PetGuardian", {"pet_id": pet.name, "role": "primary_owner"}, "guardian_id")
            or frappe.db.get_value("PetGuardian", {"pet_id": pet.name}, "guardian_id")
        )

    if not guardian_id:
        frappe.throw("Cannot create Patient: no Guardian linked to this Pet.")

    guardian = frappe.get_doc("Guardian", guardian_id)

    if not getattr(guardian, "customer_id", None):
        frappe.throw("Guardian missing customer_id. Complete OTP/Profile first.")

    # 3) إذا Patient موجود مسبقاً عبر custom_pet_id
    existing = frappe.db.get_value("Patient", {"custom_pet_id": pet.name}, "name")
    if existing:
        pet.db_set("patient_id", existing)
        return existing

    # 4) Mandatory fields
    first_name = (pet.pet_name or pet.name or "Pet").strip()
    sex = _pick_patient_sex(getattr(pet, "gender", None))

    patient_data = {
        "doctype": "Patient",
        "first_name": first_name,
        "sex": sex,
        # بعض نسخ Healthcare تعتمد patient_name أيضاً
        "patient_name": first_name,
        "customer": guardian.customer_id,
        "custom_pet_id": pet.name,
        "custom_guardian_id": guardian.name,
    }

    # 5) naming_series إذا موجود
    ns = _get_default_naming_series("Patient", "naming_series")
    if ns:
        patient_data["naming_series"] = ns

    patient = frappe.get_doc(patient_data)
    patient.insert(ignore_permissions=True)

    # 6) اربط Pet
    pet.db_set("patient_id", patient.name)
    return patient.name
