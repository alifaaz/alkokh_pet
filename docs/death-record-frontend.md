# Pet Death Record Frontend Contract

Backend module: `pet_app.api.mortality`

Frappe HTTP responses wrap return values in `message`. Examples below show the HTTP body shape.

## Summary

Use two DocTypes for mortality:

- `Pet Death Reason`: master data for selectable death reasons.
- `Pet Death Record`: the actual death report for a pet.

Frontend should normally load active reasons through `list_death_reasons`, then create records through `report_pet_death`. Do not create `Pet Death Record` directly from the UI unless an admin backend screen is intentionally editing the DocType.

## Shared Dropdowns

### Death Categories

Used by:

- `Pet Death Reason.category`
- `Pet Death Record.death_reason_category`

Options:

- `Natural Death`
- `Disease / Illness`
- `Emergency / Critical Case`
- `Accident / Trauma`
- `Procedure Complication`
- `Anesthesia Complication`
- `Boarding Incident`
- `Euthanasia`
- `Unknown / Found Dead`
- `External / Reported By Guardian`
- `Other`

Manager review is automatically required when the final category is one of:

- `Procedure Complication`
- `Anesthesia Complication`
- `Boarding Incident`
- `Unknown / Found Dead`

## Pet Death Reason

DocType: `Pet Death Reason`

Autoname: `reason_name`

Title field: `reason_name`

### Fields

| Field | Label | Type | Required | Options / Link | Frontend notes |
|---|---|---:|---:|---|---|
| `reason_name` | Reason Name | Data | Yes | Unique | Display name and document id. Use as the value sent in `death_reason`. |
| `reason_code` | Reason Code | Data | No | Unique | Optional short code for integrations/reporting. |
| `category` | Category | Select | Yes | Death Categories | Drives default category on the death record. |
| `species` | Species | Data | No | Free text | Blank means the reason applies to all species. |
| `description` | Description | Small Text | No | - | Internal/explanatory text. |
| `active` | Active | Check | No | Default `1` | Only active reasons should appear in normal forms. |
| `requires_doctor_confirmation` | Requires Practitioner Confirmation | Check | No | - | If checked, new death records start as `Pending Confirmation`. |
| `requires_manager_review` | Requires Manager Review | Check | No | - | Forces manager review before finalizing. |
| `requires_incident_report` | Requires Incident Report | Check | No | - | Frontend can use this to show an incident-report prompt. |
| `guardian_visible_label` | Guardian Visible Label | Data | No | - | Used as default `guardian_visible_summary` if the user does not enter one. |
| `sort_order` | Sort Order | Int | No | - | Used for ordering reason dropdowns. |

### List Endpoint

`GET /api/method/pet_app.api.mortality.list_death_reasons`

Params:

| Param | Required | Description |
|---|---:|---|
| `species` | No | Filters to reasons where `species` is blank or exact-match to this species, case-insensitive. |
| `category` | No | Filters by death category. |
| `active` | No | Defaults to active-only behavior. Send `0` only for admin/master-data screens. |

Response:

```json
{
  "message": {
    "ok": true,
    "data": {
      "reasons": [
        {
          "name": "Disease / Illness",
          "reason_name": "Disease / Illness",
          "reason_code": "ILLNESS",
          "category": "Disease / Illness",
          "species": "",
          "description": "Optional description",
          "active": 1,
          "requires_doctor_confirmation": 0,
          "requires_manager_review": 0,
          "requires_incident_report": 0,
          "guardian_visible_label": "Passed away due to illness",
          "sort_order": 10
        }
      ]
    },
    "meta": { "total": 1 },
    "errors": []
  }
}
```

## Pet Death Record

DocType: `Pet Death Record`

Autoname: `PDR-.YYYY.-.#####`

Title field: `pet`

### Fields

| Field | Label | Type | Required | Options / Link | Frontend notes |
|---|---|---:|---:|---|---|
| `name` | Name | Auto | System | `PDR-.YYYY.-.#####` | Read-only record id. |
| `pet` | Pet | Link | Yes | `Pet` | Required by API unless it can be resolved from source. |
| `guardian` | Guardian | Link | No | `Guardian` | API resolves from pet if omitted. |
| `customer` | Customer | Link | No | `Customer` | API resolves from guardian if omitted. |
| `death_datetime` | Death Datetime | Datetime | Yes | - | API defaults to current datetime if omitted, but UI should ask for it. |
| `reported_datetime` | Reported Datetime | Datetime | System | - | Set by API. |
| `reported_by` | Reported By | Link | System | `User` | Current session user. |
| `confirmed_by` | Confirmed By | Link | System | `User` | Set when confirmed/finalized. |
| `confirmed_at` | Confirmed At | Datetime | System | - | Set when confirmed/finalized. |
| `death_reason` | Death Reason | Link | No | `Pet Death Reason` | Use selected `Pet Death Reason.name`. Recommended for normal UI. |
| `death_reason_category` | Death Reason Category | Select | No | Death Categories | API uses selected reason category if this is omitted. |
| `cause_of_death_text` | Cause Of Death Text | Small Text | No | - | Free-text cause/details. |
| `clinical_summary` | Clinical Summary | Small Text | No | - | Staff-facing clinical summary. |
| `guardian_visible_summary` | Guardian Visible Summary | Small Text | No | - | Safe text shown to guardian; defaults from reason label or cause text. |
| `internal_note` | Internal Note | Small Text | No | - | Staff-only. Do not show to guardians. |
| `source_doctype` | Source Doctype | Link | No | `DocType` | API allow-list below. |
| `source_name` | Source Name | Dynamic Link | No | `source_doctype` | Source record id. Must belong to the same pet. |
| `source_title` | Source Title | Data | No | - | API defaults to `{source_doctype} {source_name}`. |
| `death_location` | Death Location | Select | No | Death Location options | Optional. |
| `death_location_detail` | Death Location Detail | Small Text | No | - | Optional details/address/room. |
| `was_under_clinic_care` | Was Under Clinic Care | Check | No | - | Defaults to `1` when source exists, otherwise `0`. |
| `was_unexpected` | Was Unexpected | Check | No | - | Informational flag for staff review/reporting. |
| `requires_manager_review` | Requires Manager Review | Check | System/override | - | Computed from reason/category/payload. |
| `manager_review_status` | Manager Review Status | Select | System | Review Status options | API sets `Pending` or `Not Required`. |
| `manager_reviewed_by` | Manager Reviewed By | Link | System | `User` | Set by manager review endpoint. |
| `manager_reviewed_at` | Manager Reviewed At | Datetime | System | - | Set by manager review endpoint. |
| `manager_review_note` | Manager Review Note | Small Text | No | - | Manager note; can be sent during review/finalize. |
| `body_handling_option` | Body Handling Option | Select | No | Body Handling options | Defaults to `Pending Guardian Decision`. |
| `body_released_to` | Body Released To | Data | No | - | Person/facility receiving body. |
| `body_released_at` | Body Released At | Datetime | No | - | Release datetime. |
| `body_release_note` | Body Release Note | Small Text | No | - | Optional release notes. |
| `necropsy_requested` | Necropsy Requested | Check | No | - | If checked, API defaults status to `Pending`. |
| `necropsy_status` | Necropsy Status | Select | No | Necropsy Status options | Defaults to `Not Requested` or `Pending`. |
| `necropsy_result` | Necropsy Result | Small Text | No | - | Optional result summary. |
| `certificate_issued` | Certificate Issued | Check | System | - | Set by certificate endpoint. |
| `certificate_no` | Certificate No | Data | System | - | Defaults to `DC-{death_record}` if omitted on issue. |
| `certificate_issued_at` | Certificate Issued At | Datetime | System | - | Set by certificate endpoint. |
| `certificate_file` | Certificate File | Attach | No | File path | Set by certificate endpoint. |
| `status` | Status | Select | System | Status options | API controls lifecycle. |
| `amended` | Amended | Check | No | - | Admin/audit use. |
| `amendment_reason` | Amendment Reason | Small Text | No | - | Admin/audit use. |
| `cancelled_reason` | Cancelled Reason | Small Text | No | - | Set when cancelled. |

### Pet Death Record Dropdowns

`death_location` options:

- `Clinic`
- `Boarding`
- `Home`
- `External Facility`
- `Unknown`
- `Other`

`body_handling_option` options:

- `Released To Guardian`
- `Clinic Disposal`
- `Burial Arrangement`
- `Cremation`
- `Transferred To External Facility`
- `Pending Guardian Decision`
- `Other`

`necropsy_status` options:

- `Not Requested`
- `Pending`
- `Completed`
- `Declined`

`manager_review_status` options:

- `Not Required`
- `Pending`
- `Approved`
- `Rejected`

`status` options:

- `Draft`
- `Reported`
- `Pending Confirmation`
- `Confirmed`
- `Finalized`
- `Cancelled`

`source_doctype` API allow-list:

- `Vet Visit`
- `Pet Procedure`
- `Pet Boarding`
- `PetCareService`
- `Appointment`
- `Lab`
- `Imaging`

## Recommended Report Form

Show these fields for creating a death record:

| UI field | Payload field | Required | Notes |
|---|---|---:|---|
| Pet | `pet` | Yes, unless source resolves pet | Link/search `Pet`. |
| Death date/time | `death_datetime` | Recommended | Send Frappe datetime string. |
| Death reason | `death_reason` | Recommended | Value is `Pet Death Reason.name`. |
| Category | `death_reason_category` | Auto/optional | Auto-fill from reason; allow manual only if needed. |
| Cause/details | `cause_of_death_text` | No | Free text. |
| Guardian-visible summary | `guardian_visible_summary` | No | If blank, API uses reason label or cause text. |
| Clinical summary | `clinical_summary` | No | Staff-facing. |
| Internal note | `internal_note` | No | Staff-only. |
| Source type | `source_doctype` | No | Use allow-list above. |
| Source record | `source_name` | Required if source type selected | Dynamic link id. |
| Location | `death_location` | No | Dropdown. |
| Location detail | `death_location_detail` | No | Free text. |
| Under clinic care | `was_under_clinic_care` | No | Defaults from source. |
| Unexpected | `was_unexpected` | No | Boolean. |
| Body handling | `body_handling_option` | No | Defaults to `Pending Guardian Decision`. |
| Necropsy requested | `necropsy_requested` | No | Boolean. |

Hide these on the create form unless building an admin/debug screen:

- `reported_datetime`
- `reported_by`
- `confirmed_by`
- `confirmed_at`
- `requires_manager_review`
- `manager_review_status`
- `manager_reviewed_by`
- `manager_reviewed_at`
- `certificate_issued`
- `certificate_no`
- `certificate_issued_at`
- `status`
- `amended`
- `amendment_reason`
- `cancelled_reason`

## Create Death Record

`POST /api/method/pet_app.api.mortality.report_pet_death`

Payload:

```json
{
  "pet": "PET-.00001",
  "guardian": "GUARDIAN-.00001",
  "death_datetime": "2026-05-08 13:30:00",
  "death_reason": "Disease / Illness",
  "cause_of_death_text": "Cardiorespiratory arrest after critical illness",
  "clinical_summary": "Presented in critical condition and did not respond to treatment.",
  "guardian_visible_summary": "Passed away after a critical illness.",
  "internal_note": "Staff-only note",
  "source_doctype": "Vet Visit",
  "source_name": "VISIT-.00001",
  "death_location": "Clinic",
  "death_location_detail": "Exam room 2",
  "was_under_clinic_care": 1,
  "was_unexpected": 0,
  "body_handling_option": "Pending Guardian Decision",
  "necropsy_requested": 0
}
```

Response:

```json
{
  "message": {
    "ok": true,
    "data": {
      "death_record": {
        "name": "PDR-2026-00001",
        "pet": "PET-.00001",
        "guardian": "GUARDIAN-.00001",
        "death_datetime": "2026-05-08 13:30:00",
        "death_reason": "Disease / Illness",
        "death_reason_category": "Disease / Illness",
        "guardian_visible_summary": "Passed away after a critical illness.",
        "status": "Reported",
        "certificate_issued": 0,
        "certificate_no": null,
        "certificate_issued_at": null,
        "certificate_file": null,
        "body_handling_option": "Pending Guardian Decision",
        "customer": "CUST-.00001",
        "reported_datetime": "2026-05-08 13:31:00",
        "reported_by": "user@example.com",
        "confirmed_by": null,
        "confirmed_at": null,
        "cause_of_death_text": "Cardiorespiratory arrest after critical illness",
        "clinical_summary": "Presented in critical condition and did not respond to treatment.",
        "internal_note": "Staff-only note",
        "source_doctype": "Vet Visit",
        "source_name": "VISIT-.00001",
        "source_title": "Vet Visit VISIT-.00001",
        "death_location": "Clinic",
        "death_location_detail": "Exam room 2",
        "was_under_clinic_care": 1,
        "was_unexpected": 0,
        "requires_manager_review": 0,
        "manager_review_status": "Not Required",
        "manager_reviewed_by": null,
        "manager_reviewed_at": null,
        "manager_review_note": null,
        "body_released_to": null,
        "body_released_at": null,
        "body_release_note": null,
        "necropsy_requested": 0,
        "necropsy_status": "Not Requested",
        "necropsy_result": null,
        "amended": 0,
        "amendment_reason": null,
        "cancelled_reason": null
      }
    },
    "meta": {},
    "errors": []
  }
}
```

## Actions

### Get Death Record

`GET /api/method/pet_app.api.mortality.get_pet_death_record`

Params:

- `death_record` or `name`: specific `Pet Death Record.name`
- `pet`: fetch the active non-cancelled death record for this pet

Guardian users receive a limited safe payload:

- `name`
- `pet`
- `guardian`
- `death_datetime`
- `death_reason`
- `death_reason_category`
- `guardian_visible_summary`
- `status`
- `certificate_issued`
- `certificate_no`
- `certificate_issued_at`
- `certificate_file`
- `body_handling_option`

### Confirm Death

`POST /api/method/pet_app.api.mortality.confirm_pet_death`

Payload:

```json
{
  "death_record": "PDR-2026-00001",
  "clinical_summary": "Optional updated clinical summary",
  "guardian_visible_summary": "Optional updated guardian-safe summary"
}
```

Sets:

- `status = Confirmed`
- `confirmed_by = current user`
- `confirmed_at = now`

### Manager Review

`POST /api/method/pet_app.api.mortality.manager_review_pet_death`

Payload:

```json
{
  "death_record": "PDR-2026-00001",
  "status": "Approved",
  "note": "Reviewed and approved."
}
```

Allowed `status` values should be `Approved` or `Rejected`.

### Finalize Death

`POST /api/method/pet_app.api.mortality.finalize_pet_death`

Payload:

```json
{
  "death_record": "PDR-2026-00001",
  "manager_review_note": "Optional final manager note"
}
```

Finalizing:

- Sets `status = Finalized`.
- Marks the pet as deceased.
- Sets pet `death_date` and `death_record`.
- Cancels future appointments for the pet.
- Updates source record outcome fields when applicable.

If manager review is required, `manager_review_status` must be `Approved` before finalizing.

### Issue Certificate

`POST /api/method/pet_app.api.mortality.issue_death_certificate`

Payload:

```json
{
  "death_record": "PDR-2026-00001",
  "certificate_no": "DC-2026-00001",
  "certificate_file": "/files/death-certificate.pdf"
}
```

Requires death record status `Confirmed` or `Finalized`.

### Cancel Death Record

`POST /api/method/pet_app.api.mortality.cancel_pet_death_record`

Payload:

```json
{
  "death_record": "PDR-2026-00001",
  "reason": "Created by mistake"
}
```

Finalized death records cannot be cancelled.

## Backend Rules To Respect

- A pet can have only one active death record. Active means status is not `Cancelled`.
- If `source_doctype` or `source_name` is sent, both must be valid and the source record must belong to the same pet.
- If `death_reason` has `requires_doctor_confirmation = 1`, the initial status is `Pending Confirmation`; otherwise it is `Reported`.
- Manager review is required when the reason requires it, payload requests it, or the category is one of the manager-review categories listed above.
- Guardian users must not see staff-only fields such as `internal_note`, `clinical_summary`, manager review details, or cancellation/amendment reasons.
