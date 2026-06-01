# Frontend Case Sheet JSON

This is the focused handoff for rendering and working with `Vet Case Sheet` on the frontend.

## Read APIs

List open case sheets from the workspace queue:

```js
frappe.call({
  method: "pet_app.api.workspace.get_my_workspace",
  args: {
    mode: "coordinator",
    source_type: "Case Sheet",
    limit: 50
  }
})
```

List item shape:

```json
{
  "id": "Case Sheet:VCS-2026-00001",
  "name": "VCS-2026-00001",
  "source_type": "Case Sheet",
  "source_doctype": "Vet Case Sheet",
  "title": "Luna - Vomiting - VCS-2026-00001",
  "subtitle": "Omar Hassan",
  "status": "Waiting Practitioner",
  "priority": "Normal",
  "pet": {
    "id": "PET-00001",
    "name": "PET-00001",
    "name_label": "Luna",
    "pet_name": "Luna",
    "species": "Cat",
    "breed": "Domestic Shorthair",
    "gender": "Female",
    "weight": 4.2,
    "image": null
  },
  "guardian": {
    "id": "GUARDIAN-00001",
    "name": "GUARDIAN-00001",
    "name_label": "Omar Hassan",
    "full_name": "Omar Hassan",
    "phone": "+96500000000",
    "email": null,
    "customer_id": "CUST-00001",
    "image": null
  },
  "assignee": {},
  "scheduled_at": "2026-05-15 09:30:00",
  "due_at": null,
  "overdue": false,
  "next_task": "Convert to visit",
  "modified": "2026-05-15 09:30:00",
  "creation": "2026-05-15 09:30:00",
  "badges": ["Case Sheet", "Normal"]
}
```

Detail view:

```js
frappe.call({
  method: "pet_app.api.workspace.get_record",
  args: { source_type: "Case Sheet", name: caseSheetId }
})
```

Use this aggregate for cards, header panels, notes, attachments, timeline, and routing. Use the full DocType record when rendering the editable intake form:

```js
frappe.call({
  method: "frappe.client.get",
  args: { doctype: "Vet Case Sheet", name: caseSheetId }
})
```

For an unconverted case sheet, `get_record` returns:

```json
{
  "summary": {
    "name": "VCS-2026-00001",
    "source_type": "Case Sheet",
    "source_doctype": "Vet Case Sheet",
    "title": "Luna - Vomiting - VCS-2026-00001",
    "status": "Waiting Practitioner",
    "priority": "Normal",
    "next_task": "Convert to visit",
    "creation": "2026-05-15 09:30:00",
    "modified": "2026-05-15 09:30:00"
  },
  "pet": {
    "id": "PET-00001",
    "name": "PET-00001",
    "name_label": "Luna",
    "pet_name": "Luna",
    "species": "Cat",
    "breed": "Domestic Shorthair",
    "gender": "Female",
    "weight": 4.2,
    "image": null
  },
  "guardian": {
    "id": "GUARDIAN-00001",
    "name": "GUARDIAN-00001",
    "name_label": "Omar Hassan",
    "full_name": "Omar Hassan",
    "phone": "+96500000000",
    "email": null,
    "customer_id": "CUST-00001",
    "image": null
  },
  "assignee": {},
  "clinical": {
    "chief_complaint": "Vomiting",
    "intake_summary": "Vomiting twice since morning.",
    "overview": null,
    "examination": null,
    "assessment": null,
    "plan": null,
    "instructions": null
  },
  "follow_up": {},
  "consult_requests": [],
  "diagnoses": [],
  "orders": [],
  "linked_records": [],
  "notes": [],
  "attachments": [],
  "billing": {},
  "timeline": []
}
```

If the case sheet is already converted, `get_record("Case Sheet", id)` returns the linked `Vet Visit` aggregate and adds `focus_source` for the original case sheet.

## Create APIs

Use this for walk-in intake because it creates both the case sheet and queue ticket:

```js
frappe.call({
  method: "pet_app.api.appointment.create_walkin_case_sheet",
  args: {
    payload: {
      pet: "PET-00001",
      guardian: "GUARDIAN-00001",
      priority: "Normal",
      chief_complaint: "Vomiting",
      intake_notes: "Vomiting twice since morning.",
      weight: 4.2,
      doctor: "HCP-00001",
      room: "Room 1",
      branch: "Main"
    }
  }
})
```

For direct DocType insert, send:

```js
frappe.call({
  method: "frappe.client.insert",
  args: { doc: caseSheetDoc }
})
```

```json
{
  "doctype": "Vet Case Sheet",
  "status": "Waiting Practitioner",
  "priority": "Normal",
  "case_sheet_date": "2026-05-15 09:30:00",
  "appointment": null,
  "guardian": "GUARDIAN-00001",
  "animal_patient": "PET-00001",
  "phone_number": "+96500000000",
  "weight": 4.2,
  "chief_complaint": "Vomiting",
  "symptom_duration": "Less than 24 hours",
  "has_vomiting": 1,
  "has_diarrhea": 0,
  "intake_notes": "Vomiting twice since morning."
}
```

Before creating or when pet changes, use pet context to autofill guardian/customer/pet snapshot fields:

```js
frappe.call({
  method: "pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet.get_pet_context",
  args: { pet_name: petId }
})
```

Response:

```json
{
  "guardian": "GUARDIAN-00001",
  "customer": "CUST-00001",
  "phone_number": "+96500000000",
  "species": "Cat",
  "breed": "Domestic Shorthair",
  "sex": "Female",
  "weight": 4.2,
  "age_text": "2y 4m"
}
```

## Render Schema

Use this JSON to render the full frontend form:

```json
{
  "doctype": "Vet Case Sheet",
  "title": "Case Sheet",
  "statuses": ["Draft", "Waiting Practitioner", "In Consultation", "Converted to Visit", "Closed"],
  "priorities": ["Low", "Normal", "Urgent", "Emergency"],
  "required": ["status", "priority", "case_sheet_date", "guardian", "animal_patient", "chief_complaint"],
  "read_only": ["vet_visit", "customer", "species", "breed", "age_text"],
  "conditional_required": [
    {"field": "complaint_other", "when": {"chief_complaint": "Other"}},
    {"field": "previous_vet_details", "when": {"previous_vet_visit": 1}},
    {"field": "chronic_disease_details", "when": {"chronic_disease": 1}},
    {"field": "medication_details", "when": {"currently_on_medication": 1}},
    {"field": "feeding_type_other", "when": {"feeding_type": "Other"}}
  ],
  "sections": [
    {
      "key": "basic_info",
      "label": "Basic Info",
      "fields": [
        {"name": "status", "label": "Status", "type": "Select", "options": ["Draft", "Waiting Practitioner", "In Consultation", "Converted to Visit", "Closed"], "default": "Draft"},
        {"name": "priority", "label": "Priority", "type": "Select", "options": ["Low", "Normal", "Urgent", "Emergency"], "default": "Normal"},
        {"name": "case_sheet_date", "label": "Case Sheet Date", "type": "Datetime", "default": "Now"},
        {"name": "vet_visit", "label": "Vet Visit", "type": "Link", "options": "Vet Visit", "read_only": true},
        {"name": "appointment", "label": "Appointment", "type": "Link", "options": "Appointment"},
        {"name": "guardian", "label": "Guardian", "type": "Link", "options": "Guardian"},
        {"name": "customer", "label": "Customer", "type": "Link", "options": "Customer", "read_only": true},
        {"name": "phone_number", "label": "Phone Number", "type": "Data"},
        {"name": "animal_patient", "label": "Pet", "type": "Link", "options": "Pet"},
        {"name": "species", "label": "Species", "type": "Data", "read_only": true},
        {"name": "breed", "label": "Breed", "type": "Data", "read_only": true},
        {"name": "sex", "label": "Sex", "type": "Select", "options": ["Male", "Female", "Unknown"]},
        {"name": "age_text", "label": "Age", "type": "Data", "read_only": true},
        {"name": "weight", "label": "Weight", "type": "Float"}
      ]
    },
    {
      "key": "intake",
      "label": "Intake",
      "fields": [
        {"name": "chief_complaint", "label": "Chief Complaint", "type": "Select", "options": ["Checkup", "Vomiting", "Diarrhea", "Loss of Appetite", "Vaccination", "Injury", "Skin Problem", "Coughing", "Limping", "Eye Problem", "Ear Problem", "Follow-up", "Other"]},
        {"name": "complaint_other", "label": "Complaint Details", "type": "Small Text", "show_when": {"chief_complaint": "Other"}},
        {"name": "symptom_duration", "label": "Symptom Duration", "type": "Select", "options": ["Less than 24 hours", "1-3 days", "4-7 days", "More than 1 week", "Unknown"]}
      ]
    },
    {
      "key": "symptoms",
      "label": "Symptoms",
      "fields": [
        {"name": "has_vomiting", "label": "Vomiting", "type": "Check"},
        {"name": "has_diarrhea", "label": "Diarrhea", "type": "Check"},
        {"name": "has_cough", "label": "Cough", "type": "Check"},
        {"name": "has_sneezing", "label": "Sneezing", "type": "Check"},
        {"name": "has_loss_of_appetite", "label": "Loss of Appetite", "type": "Check"},
        {"name": "has_lethargy", "label": "Lethargy", "type": "Check"},
        {"name": "has_itching", "label": "Itching", "type": "Check"},
        {"name": "has_wound", "label": "Wound", "type": "Check"},
        {"name": "has_limping", "label": "Limping", "type": "Check"},
        {"name": "has_breathing_issue", "label": "Breathing Issue", "type": "Check"},
        {"name": "has_eye_discharge", "label": "Eye Discharge", "type": "Check"},
        {"name": "has_ear_discharge", "label": "Ear Discharge", "type": "Check"},
        {"name": "has_fever_flag", "label": "Fever Flag", "type": "Check"}
      ]
    },
    {
      "key": "history",
      "label": "Background / History",
      "fields": [
        {"name": "vaccination_status", "label": "Vaccination Status", "type": "Select", "options": ["Up to date", "Partially vaccinated", "Not vaccinated", "Unknown"]},
        {"name": "last_vaccine_date", "label": "Last Vaccine Date", "type": "Date"},
        {"name": "neutered_spayed", "label": "Neutered / Spayed", "type": "Select", "options": ["Yes", "No", "Unknown"]},
        {"name": "previous_vet_visit", "label": "Previous Vet Visit", "type": "Check"},
        {"name": "previous_vet_details", "label": "Previous Vet Details", "type": "Small Text", "show_when": {"previous_vet_visit": 1}},
        {"name": "chronic_disease", "label": "Chronic Disease", "type": "Check"},
        {"name": "chronic_disease_details", "label": "Chronic Disease Details", "type": "Small Text", "show_when": {"chronic_disease": 1}},
        {"name": "currently_on_medication", "label": "Currently on Medication", "type": "Check"},
        {"name": "medication_details", "label": "Medication Details", "type": "Small Text", "show_when": {"currently_on_medication": 1}}
      ]
    },
    {
      "key": "daily_condition",
      "label": "Daily Condition",
      "fields": [
        {"name": "appetite_status", "label": "Appetite Status", "type": "Select", "options": ["Normal", "Reduced", "Absent", "Unknown"]},
        {"name": "water_intake_status", "label": "Water Intake Status", "type": "Select", "options": ["Normal", "Reduced", "Increased", "Unknown"]},
        {"name": "urination_status", "label": "Urination Status", "type": "Select", "options": ["Normal", "Reduced", "Painful", "Frequent", "Unknown"]},
        {"name": "defecation_status", "label": "Defecation Status", "type": "Select", "options": ["Normal", "Constipated", "Diarrhea", "Blood Seen", "Unknown"]},
        {"name": "activity_status", "label": "Activity Status", "type": "Select", "options": ["Normal", "Less Active", "Very Weak", "Unknown"]}
      ]
    },
    {
      "key": "notes",
      "label": "Notes",
      "fields": [
        {"name": "feeding_type", "label": "Feeding Type", "type": "Select", "options": ["Dry Food", "Wet Food", "Home Food", "Mixed", "Other"]},
        {"name": "feeding_type_other", "label": "Feeding Type Other", "type": "Data", "show_when": {"feeding_type": "Other"}},
        {"name": "housing_environment", "label": "Housing / Living Environment", "type": "Select", "options": ["Indoor", "Outdoor", "Mixed", "Farm", "Unknown"]},
        {"name": "contact_with_other_animals", "label": "Contact with Other Animals", "type": "Check"},
        {"name": "intake_notes", "label": "Intake Notes", "type": "Small Text"}
      ]
    }
  ]
}
```

## Convert To Visit

```js
frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Case Sheet",
    name: caseSheetId,
    action: "convert_to_visit",
    payload: {
      doctor: practitionerId,
      priority: "Normal",
      visit_type: "Consultation",
      doctor_case_choice: "wellness"
    }
  }
})
```

`doctor_case_choice` can be `wellness`, `continue_case`, or `new_case` when the doctor already selected the case path.

## Optional Dynamic Template Fields

Case sheet templates add extra configurable intake questions beyond the core DocType fields.

```js
frappe.call({
  method: "pet_app.api.case_sheet_templates.list_templates",
  args: { visit_type: "General Consultation", species: "Cat", active: 1 }
})

frappe.call({
  method: "pet_app.api.case_sheet_templates.apply_template_to_case_sheet",
  args: { case_sheet: caseSheetId, template: templateId }
})

frappe.call({
  method: "pet_app.api.case_sheet_templates.save_case_sheet_responses",
  args: {
    case_sheet: caseSheetId,
    template: templateId,
    responses: [
      {"field_key": "triage_note", "label": "Triage Note", "value": "Bright, alert, responsive"}
    ]
  }
})
```

Template item shape:

```json
{
  "section": "Triage",
  "label": "Triage Note",
  "field_key": "triage_note",
  "fieldtype": "Small Text",
  "required": 1,
  "options": null,
  "link_doctype": null,
  "default_value": null,
  "sort_order": 10,
  "help_text": null
}
```

## Frontend Rules

- Show `Start Visit` / `Convert to Visit` only when `vet_visit` is empty and status is `Draft`, `Waiting Practitioner`, or `In Consultation`.
- Show `Open Visit` when `vet_visit` is set, or when `get_record` returns a visit aggregate with `focus_source`.
- Backend validates that the selected `guardian` is linked to `animal_patient`.
- Backend fills `customer`, `species`, `breed`, `sex`, `age_text`, and sometimes `weight` from pet/guardian data.
- Use `case_sheet_templates` only for extra dynamic questions. Use the core DocType fields above for the fixed intake UI.
