# Visit Referral Contract

`Visit Referral` is the case-handoff audit trail for a single `Vet Visit`.

It is not a consult request. A consult asks another doctor for an opinion while the case stays with the requesting doctor. A referral transfers this visit to another doctor immediately.

## Storage

`Visit Referral` is a child table on `Vet Visit.referrals`.

| Field | Type | Options | Required | Notes |
| --- | --- | --- | --- | --- |
| `from_practitioner` | Link | `Healthcare Practitioner` | yes | The practitioner the visit moved from. For doctor-initiated transfers this is the session doctor; for admin/coordinator transfers this is the visit's current practitioner before reassignment. Never accepted from payload. |
| `to_practitioner` | Link | `Healthcare Practitioner` | yes | Receiving doctor. |
| `note` | Small Text | | yes | Referral reason. Whitespace-only values are rejected for everyone. |
| `referred_by` | Link | `User` | no | Read-only; the user who performed the transfer. This may be an admin/coordinator user rather than `from_practitioner`'s linked user. |
| `referred_at` | Datetime | | no | Read-only; set when the transfer is created. |

## Create Referral

Method:

```text
pet_app.api.case_assignment.create_visit_referral
```

POST payload:

```json
{
  "visit": "VVT-2026-00057",
  "to_practitioner": "HCP-00002",
  "note": "Please take over this case."
}
```

Who may call it:

- The visit's current Doctor user, when linked to an enabled `Healthcare Practitioner`.
- A direct-assignment admin/coordinator user with one of the direct assignment roles: `Coordinator`, `Visit Admin`, `Healthcare Administrator`, `Pet App Admin`, `System Manager`, or a full-access user.

Doctor users may refer only visits currently assigned to their own practitioner. Admin/coordinator users may refer any eligible visit, but still must provide a note.

Behavior:

- Requires `note`; no role is exempt.
- Performs the transfer immediately and atomically under a database savepoint.
- Sets `from_practitioner` to the visit's current practitioner before reassignment.
- Sets `referred_by` to `frappe.session.user`.
- Inserts the `Visit Referral` row with `referred_at = now`.
- Reassigns this visit through `set_visit_practitioner()`, which writes both `primary_practitioner` and `doctor`.
- Adds `to_practitioner` to the care episode team as `Treating Doctor` if missing.
- Sends a `Notification Log` to `to_practitioner`'s linked user. The notification subject includes the referral note text.
- For admin/coordinator-initiated transfers, also sends a `Notification Log` to `from_practitioner`'s linked user because that doctor did not initiate the handoff.

The transfer is immediate and atomic; the referrals table is the audit trail. A referral row without the visit transfer, or a visit transfer without the referral row, is a broken state and must be rolled back.

Error cases:

- `Visit is required.`
- `Visit <name> was not found.`
- `Visit <name> is not linked to a Care Episode.`
- `Vet Visit referral table is missing. Run migrations first.`
- `Completed visits cannot be referred.`
- `Visit is locked after billing.`
- `Referral note is required.`
- `Visit has no current practitioner.`
- `Only a linked Doctor user may create a referral.`
- `Only the current visit doctor may refer this visit.`
- `Receiving practitioner is required.`
- `Practitioner <name> was not found.`
- `Practitioner <name> is disabled.`
- `Practitioner <name> must be a Doctor.`
- `Cannot refer a visit to its current practitioner.`
- `Practitioner <name> does not have a linked user.`
- `Not permitted`

## Direct Doctor Assignment

`pet_app.api.case_assignment.assign_visit_doctor` remains only as legacy/admin tooling. It is not the UI path for anyone, including admins/coordinators, because UI handoffs must always carry a written reason and create a `Visit Referral` audit row.

Plain Doctor users are rejected from `assign_visit_doctor` with:

```text
Doctors must transfer visits using referral with a note.
```

## Workbench Shape

`pet_app.api.visit_workbench.get_visit_workbench` always emits `referrals`, even when empty. The rows are oldest-first so the frontend can render a transfer timeline without another request.

```json
{
  "referrals": [
    {
      "name": "child-row-name",
      "note": "Please take over this case.",
      "from_practitioner": "HCP-00001",
      "from_practitioner_name": "Dr. A",
      "to_practitioner": "HCP-00002",
      "to_practitioner_name": "Dr. B",
      "referred_by": "coordinator@example.com",
      "referred_by_name": "Coordinator Name",
      "referred_at": "2026-07-13 12:00:00"
    }
  ]
}
```

Empty state:

```json
{
  "referrals": []
}
```

The existing `permissions` object also includes:

```json
{
  "permissions": {
    "can_refer_visit": true
  }
}
```

`can_refer_visit` is true when the current user can call `create_visit_referral` for this visit: the current visit doctor, a direct-assignment admin/coordinator role, or a full-access user. It is false for unrelated doctors and locked/completed visits.

## Scope

A referral moves this visit's practitioner only. It does not retroactively change other visits in the same care episode. Multiple referrals over one visit's life are valid, for example `A -> B -> C`; the child table records that transfer history.

## Why Not Visit Consult Request

`Visit Consult Request` means "give me your opinion"; the requesting doctor retains ownership. It has its own lifecycle and consult note fields.

`Visit Referral` means "take this case"; ownership moves immediately from `from_practitioner` to `to_practitioner`. It has transfer side effects and an audit row, not a pending/accepted/declined lifecycle. Keeping this separate prevents consult reporting, referral history, and ownership changes from being mixed into one ambiguous child table.
