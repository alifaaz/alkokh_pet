# Clinic Branch Separation — Frontend Handoff

Backend contract: `docs/CLINIC_BRANCH_SEPARATION.md` (authoritative — this doc is a consumer).

## TL;DR for the frontend

**Filtering is server-side. Do not filter lists in Vue.**

When a user belongs to Clinic A, the worklist endpoints already return only Clinic A's
records. No query parameter, no client-side filter, no changes to existing list screens.

There are exactly **two** things to build:

1. An **admin screen** to assign users to branches.
2. A **branch picker on create**, shown only when the backend says one is needed.

Plus optionally showing the current branch in the header.

---

## 1. What changed on existing screens

Nothing you must do. Concretely:

| Screen | Change |
| --- | --- |
| Visits worklist | Returns only the user's branch. Same shape, fewer rows. |
| Case sheets worklist | Same. |
| Queue | Same. |
| Services / procedures worklist | Same. |
| Pet medical profile / history | **Unchanged — still shows every clinic's visits.** |
| Labs / Radiology | **Unchanged — global, all clinics.** |
| **Sales invoice list** | Returns only the user's branch. |
| Customer balance / outstanding / payments / POS | **Unchanged — global.** |
| Boarding rooms / records | **Unchanged — global, all clinics.** |

The history and diagnostics exclusions are deliberate, not oversights. A vet opening a pet
must see prior treatment from other clinics. Do not "helpfully" filter these client-side.

**Billing is split, and the split matters.** The invoice *list* is per branch — Clinic A
does not see Clinic B's invoices. But the customer's **balance is global**, because there
is one company and one receivable ledger. So:

- An invoice list shows only this clinic's invoices.
- A customer's outstanding total still reflects **every** clinic.
- A Clinic A debt is still payable at Clinic B.

If you show "customer owes X", that figure is company-wide and must not be filtered by
branch. Showing a branch-filtered total would tell a cashier a customer is settled when
they are not.

---

## 2. Branch context endpoint

```http
GET /api/method/pet_app.api.branch.get_branch_context
```

```jsonc
{
  "message": {
    "ok": true,
    "data": {
      "current": "main",                                  // string | null
      "available": [{ "name": "main", "branch": "main" }],
      "sees_all_branches": false,                         // true for Administrator
      "must_select": false                                // true => show the picker
    },
    "meta": {},
    "errors": []
  }
}
```

```ts
type BranchContext = {
  current: string | null;
  available: { name: string; branch: string }[];
  sees_all_branches: boolean;
  must_select: boolean;
};
```

Call it once on app boot, alongside `get_current_access`. Cache it; it changes only when
an admin reassigns the user.

**`must_select` is the only flag that drives UI.** It is `true` when the user covers more
than one branch, so the backend cannot infer where a new record belongs.

The current branch is also present in the existing access snapshot as
`get_current_access().restrictions.branch` (an array). `get_branch_context` is preferred
for the picker because it also returns display names and the `must_select` decision.

---

## 3. Branch picker on create

Applies to: **Vet Visit, Vet Case Sheet, Appointment, Pet Queue Ticket, PetCareService,
Pet Procedure** (and Sales Invoice, which inherits from its visit).

**Not** Pet Boarding — see section 5.

Rules:

- `must_select === false` → **omit `branch` entirely** from the create payload. The backend
  stamps it. Do not send `null` or `""`.
- `must_select === true` → show a required select from `available`, and send the chosen
  value as `branch`.

```ts
const ctx = await getBranchContext();

const payload = {
  animal_patient: petId,
  customer: customerId,
  doctor: doctorId,
  ...(ctx.must_select ? { branch: selectedBranch } : {}),
};
```

If `must_select` is true and you omit `branch`, the create fails with:

```text
Select a branch for this Vet Visit. Your user is not restricted to one clinic,
so the branch cannot be inferred.
```

That is a `MandatoryError` — surface it on the branch field, not as a generic toast.

In practice this affects Administrator and back-office accounts. A normal practitioner
always has `must_select: false` and never sees the picker.

Sending a branch the user has no claim to returns a `PermissionError`:

```text
You are not permitted to create records for branch <b>Clinic B</b>.
```

The picker should make that unreachable — it is a server-side backstop, not a UX path.

---

## 4. Admin screen — assigning users to branches

Both endpoints already exist and are admin-gated.

**Read:**

```http
GET /api/method/pet_app.api.permissions.get_user_restrictions?user=doctor@clinic.com
```

```jsonc
{ "message": { "ok": true, "data": {
  "warehouse": [], "cashier_profile": [], "practitioner": [],
  "branch": ["main"], "doctor": []
} } }
```

**Write:**

```http
POST /api/method/pet_app.api.permissions.update_user_restrictions
Content-Type: application/json
```

```json
{ "user": "doctor@clinic.com", "restrictions": { "branch": ["Clinic B"] } }
```

`restrictions` replaces the whole set for that user, so send the other keys unchanged if
you are only editing `branch` — read first, merge, then write.

**Branch list** for the dropdown:

```http
GET /api/resource/Branch?fields=["name"]&limit_page_length=0
```

**User list** — reuse the existing endpoint, which already does search and pagination:

```http
GET /api/method/pet_app.api.users.get_users_with_role_profile?filters=[["full_name","like","%ali%"]]&limit_start=0&limit_page_length=10
```

### Screen shape

A user picker plus a branch select. Suggested placement: next to the existing role-profile
/ permissions admin, since it writes the same `User Permission` records as warehouse and
cashier-profile restrictions.

Worth showing the practitioner name beside the user in the picker — admins think in terms
of "Dr. Ayman", not an email address. Resolve via `Healthcare Practitioner.user_id`.

### Two rules to surface in the UI

1. **Leave Administrator unassigned.** Assigning Administrator a branch would *filter*
   what they see and break "only the administrator sees every clinic". If an admin tries,
   warn them.
2. **A user with no branch sees everything.** That is Frappe's own behaviour (no
   restriction rows means no filtering). So an unassigned back-office user is not
   locked out — they are unrestricted. Show unassigned users clearly, because "no branch"
   reads as restrictive and means the opposite.

Changes take effect immediately — the access-snapshot cache is invalidated on write.

---

## 5. Pet Boarding — global, with a new optional field

Boarding is **not** branch separated. One facility with a single shared pool of 37
Service Rooms serves every clinic, so any clinic can see, book, check in and check out
any stay. Do not send `branch` on boarding, and do not filter boarding lists by branch.

There is one new field for you to surface:

```ts
type PetBoarding = {
  practitioner?: string | null;   // Link -> Healthcare Practitioner
};
```

- **Label:** "Responsible Practitioner"
- **Optional.** Leave it out and the record saves fine.
- **Frontend-owned.** The backend never fills it in and never infers it, so whatever you
  send is what is stored. It is currently empty on all existing records.
- It records *who is accountable* for the animal, not *who may see the record* — every
  clinic sees every boarding regardless of this value.

Populate it from a Healthcare Practitioner picker on the boarding form:

```http
GET /api/resource/Healthcare Practitioner?filters=[["disabled","=",0]]&fields=["name","practitioner_name"]&limit_page_length=0
```

---

## 6. Optional: current branch in the header

Show `ctx.current` so users know which clinic they are working in. For
`sees_all_branches` users show something like "All clinics" rather than a branch name.

There is no "switch branch" action. A user's branch is set by an admin, not chosen at
runtime. If you want per-session switching for admins, that is a new backend feature —
ask first.

---

## 7. Testing checklist

| Case | Expected |
| --- | --- |
| Practitioner in Clinic A opens visits worklist | Only Clinic A visits |
| Practitioner in Clinic B opens visits worklist | Only Clinic B visits |
| Either opens a pet treated at both | **Full history from both clinics** |
| Either opens labs / radiology | **All clinics' diagnostics** |
| Practitioner opens the invoice list | Only their own clinic's invoices |
| Either opens boarding rooms / records | **All clinics' boarding** |
| Either views a customer's outstanding balance | **Company-wide total, both clinics** |
| Clinic B cashier collects a Clinic A debt | **Works** |
| Administrator opens visits worklist | Every clinic |
| Administrator creates a visit, 2+ branches | Picker required |
| Practitioner creates a visit | No picker, branch stamped automatically |

The middle three are regression checks. If history, diagnostics or billing ever start
filtering by branch, that is a bug — report it rather than working around it in Vue.
