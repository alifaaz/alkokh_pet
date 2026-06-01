# Healthcare Workspace API

Backend source of truth for `/profile` daily work is `pet_app.api.workspace`.

## Queue

`pet_app.api.workspace.get_my_workspace`

Input:

```json
{
  "mode": "doctor | service | coordinator | diagnostics | accounting | management | all",
  "search": "optional text",
  "priority": "Low | Normal | Urgent | Emergency",
  "status": "optional exact status",
  "limit": 50,
  "cursor": 0
}
```

Output:

```json
{
  "mode": "doctor",
  "metrics": {
    "total": 2,
    "overdue": 0,
    "urgent": 1,
    "by_source": {"Visit": 2},
    "by_status": {"In Progress": 1}
  },
  "items": [
    {
      "id": "Visit:VVT-2026-00002",
      "name": "VVT-2026-00002",
      "source_type": "Visit",
      "source_doctype": "Vet Visit",
      "title": "Max - Gastrointestinal - VVT-2026-00002",
      "status": "In Progress",
      "priority": "Normal",
      "pet": {},
      "guardian": {},
      "assignee": {},
      "next_task": "Complete case"
    }
  ],
  "next_cursor": null
}
```

## Aggregate Record

`pet_app.api.workspace.get_record`

Input:

```json
{"source_type": "Visit | Case Sheet | Appointment | Service | Procedure | Lab | Radiology | Invoice", "name": "record-name"}
```

Output returns a single aggregate:

```json
{
  "summary": {},
  "pet": {},
  "guardian": {},
  "assignee": {},
	  "clinical": {},
	  "follow_up": {
	    "required": 1,
	    "reason": "Recheck appetite",
	    "preferred_date": "2026-05-10",
	    "date": "2026-05-10",
	    "appointment_id": "APMT-Omar Hassan-0001",
	    "visit_id": "VVT-2026-00003",
	    "of_visit_id": null,
	    "status": "Seen"
	  },
	  "consult_requests": [
	    {
	      "name": "child-row-name",
	      "requested_doctor": "DOC-00002",
	      "requested_doctor_label": "Dr. Example",
	      "requested_by": "doctor@example.com",
	      "reason": "Dermatology opinion",
	      "status": "Requested",
	      "consult_note": null,
	      "requested_at": "2026-05-02 09:00:00",
	      "completed_at": null
	    }
	  ],
	  "diagnoses": [],
  "orders": [],
  "linked_records": [],
  "notes": [],
  "attachments": [],
  "billing": {},
  "timeline": []
}
```

`Radiology` maps to the existing `Imaging` DocType. `Procedure` maps to `Pet Procedure` and returns the parent Visit aggregate with `focus_source.detail` containing procedure documentation, checklist, and timing fields.

## Actions

`pet_app.api.workspace.perform_action`

Supported actions include:

- Doctor: `start_consultation`, `save_clinical_note`, `save_diagnoses`, `create_orders`, `request_follow_up`, `request_consult`, `complete_consult`, `complete_case`
- Service: `start_service`, `finish_service`, `close_service`
- Procedure: `start_procedure`, `save_procedure_note`, `complete_procedure`, `close_procedure`, `cancel_procedure`
- Coordinator: `assign`, `reassign`, `convert_to_visit`, `convert_follow_up_to_visit`, `convert_to_service`
- Diagnostics: `start_test`, `save_result`, `release`
- Accounting: `submit_invoice`, `mark_follow_up`

All actions return the updated aggregate record.

Follow-up behavior:

- `request_follow_up` creates or reuses one linked `Appointment`; when `scheduled_time` is omitted it uses `09:00` on `follow_up_preferred_date`.
- `convert_follow_up_to_visit` converts that Appointment into a new `Vet Visit` with `visit_type = Follow-up` and `follow_up_of_visit_id` set to the original visit.
- Follow-up and consult actions do not add billable consultation rows. Orders created during the follow-up visit still use the normal billable order flow.

Procedure behavior:

- `create_orders` accepts `kind = "procedure"` with `procedure_template`, optional `provider`, `scheduled_at`, `priority`, and `note`.
- The backend creates a linked `Pet Procedure`, copies its template checklist, links the Visit Order, and creates one `Procedure` billable row from `Procedure Template.billing_care_service`.
- Procedure documentation actions update the same record and billable row; they do not create extra charges.
- Cancelling a procedure cancels the unbilled procedure billable row.

Notes and files:

- `pet_app.api.workspace.add_note`
- `pet_app.api.workspace.attach_file`
