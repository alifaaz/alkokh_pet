# Clinic Branch Separation — Backend Contract

Canonical contract. Implemented 2026-08-11.

Multiple clinics run on one Frappe site. A practitioner in one clinic should not see
another clinic's **worklists**. Everything else — the patient record, diagnostics and
money — is deliberately shared.

## The model

Branch is ERPNext **`Branch`**. There is no custom branch DocType. `Branch` was already
wired into `pet_app.api.permissions` (`RESTRICTION_DOCTYPE_MAP["branch"] = "Branch"`) and
into the Vue `restrictions.branch` contract, so it needed no new plumbing.

Assignment is a standard Frappe **`User Permission`** row (`allow="Branch"`). That works
for every user type — practitioners, reception, cashiers — and is admin-editable in Desk.

**Data is dynamic; rules are code.** Which branches exist and who belongs to them is
data an admin edits. Which doctypes are scoped is a code-level allow-list. Do not build a
UI that configures the rules — see the "Rejected alternative" section.

### What is scoped

Seven doctypes, listed in `SCOPED_DOCTYPES` in `pet_app/utils/branch.py`:

| DocType | Branch source |
| --- | --- |
| Vet Visit | acting user |
| Vet Case Sheet | acting user |
| Appointment | acting user |
| Pet Queue Ticket | acting user |
| PetCareService | linked visit, else acting user |
| Pet Procedure | linked visit, else acting user |
| Sales Invoice | **the linked visit**, not the acting user |

The common thread: everything here appears in a **workspace worklist**
(`_visit_items`, `_case_sheet_items`, `_service_items`, `_procedure_items` in
`pet_app/api/workspace.py`, and `list_queue` in `pet_app/api/queue.py`).

### Billing: the document is scoped, the money is not

This is the subtle one. An invoice **belongs to the clinic that raised it**, so the
invoice *list* is branch scoped. But the customer and the ledger are **not**:

| Scoped | Global |
| --- | --- |
| `Sales Invoice` (the document) | `Customer`, `Payment Entry`, `GL Entry`, `Journal Entry` |

Why the split: there is one Company (`Kokh-vet`) and one receivable account
(`411100 - العملاء`), so a customer's balance is a single GL figure. Scoping the balance
would make a customer look settled at one clinic while owing money at another, and would
break collecting a Clinic A debt at Clinic B. Scoping the invoice document gives each
clinic its own sales list without touching the books.

`Sales Invoice.branch` is inherited from the linked `Vet Visit`, not the acting user, so
a central back-office biller cannot accidentally reassign an invoice to their own clinic.
Stamped in `pet_app/pet_app/doctype/vet_visit/vet_visit.py` on the visit-billing path and
by the generic `before_insert` hook elsewhere.

### What is NOT scoped, deliberately

| Area | DocTypes | Why |
| --- | --- | --- |
| **Patient record** | Pet, Guardian, PetGuardian, Pet Medical Profile, Pet Care Episode, vaccination/deworming | Pets and guardians are shared. A vet must see prior treatment wherever it happened — hiding it is a clinical safety problem. |
| **Diagnostics** | Lab, Imaging | One lab/radiology operation serves all clinics. Product decision. |
| **The money** | Customer, Payment Entry, GL Entry, POS Profile | One Company, one receivable ledger — see above. |
| **Boarding** | Pet Boarding, Service Room | One boarding facility serves every clinic. The 37 Service Rooms are a single shared pool with no branch, so scoping the booking would let one clinic reserve a room another clinic could then neither see nor check out. |

`SCOPED_DOCTYPES` is an **allow-list, not a deny-list**, and that is load-bearing:
ERPNext already puts a `branch` custom field on Sales Order, Payment Entry, Stock Entry
and POS Profile as a reporting dimension. A deny-list would have silently scoped payments
and stock along with invoices. A regression test asserts Customer, Payment Entry, GL Entry
and Journal Entry stay unfiltered.

### Worklist vs history

A `Vet Visit` is two things, and they get opposite treatment:

| Reading | Question | Scoped | Code |
| --- | --- | --- | --- |
| **Worklist** | "what is on my plate" | **Yes** | `_visit_items`, `api/workspace.py` |
| **History** | "what happened to this pet" | **No** | `_latest_visits`, `api/medical_profile.py` |

These are already separate functions with separate filters, so scoping one leaves the
other alone. Paths that must keep returning cross-clinic data: `api/medical_profile.py`,
`api/medical_file.py`, `api/visit_workbench.py`, `api/guardian_portal.py`, and any
single-record `get_record` fetch.

## NULL is the most permissive value

The single most important thing to understand.

Frappe's User Permission filter (`frappe/model/db_query.py`, `add_user_permissions`) is:

```sql
ifnull(`tabVet Visit`.`branch`, '') = '' or `tabVet Visit`.`branch` in ('main')
```

A record with **no branch is visible to every clinic**, not to none. `apply_strict_user_permissions`
would remove that clause, but it is a global System Settings flag that would also change
existing warehouse / POS Profile / practitioner restrictions — so it is **not** enabled.

Consequences:

- Backfill is a correctness requirement, not cleanup.
- Writing a NULL branch is a leak. `stamp_branch_on_insert` never does.
- A **user** with no Branch User Permission is likewise unrestricted and sees everything.

## API

`pet_app/utils/branch.py`:

| Function | Purpose |
| --- | --- |
| `get_current_branch(user)` | Resolve the user's branch: User Permission first, `Healthcare Practitioner.clinic_branch` as fallback. `None` if unresolved. |
| `get_user_branches(user)` | All branches the user may see. Wraps `get_restriction_values`, so nested-set descendants expand. |
| `require_current_branch(user)` | Same, but throws. For write paths. |
| `user_sees_all_branches(user)` | Administrator, or any user with no Branch User Permission. |
| `apply_branch_filter(filters, doctype, user)` | Add a branch condition to a `get_all` filters dict. No-op unless the doctype is in `SCOPED_DOCTYPES`. |
| `assert_can_write_to_branch(branch, user)` | Reject filing a record into another clinic. |
| `stamp_branch_on_insert(doc)` | `before_insert` hook. |
| `sync_practitioner_branch(user)` | Mirror the branch onto the practitioner record. |

`pet_app/api/branch.py` exposes `get_branch_context()` for the frontend — see
`docs/frontend-branch-separation.md`.

### Stamping rules

`stamp_branch_on_insert`, wired in `hooks.py` `doc_events`, in order:

1. **Caller supplied a branch** — honoured only after `assert_can_write_to_branch`. A
   restricted user cannot file into another clinic whatever they post.
2. **User has one branch** — stamped automatically.
3. **User is unrestricted** (Administrator, back-office) — no branch to infer. With one
   Branch on site it is used; with several the insert **throws**, asking them to choose.
4. **Restricted but unresolved** — a misconfiguration. Throws rather than write a NULL.

Background work (`in_install`, `in_migrate`, `in_patch`, `in_test`) takes the default
branch, so jobs and installers are never blocked.

Wired via `doc_events` rather than at each call site because `Vet Visit` alone has five
non-test insert paths; a missed one would write a NULL.

### The practitioner mirror

`Healthcare Practitioner.clinic_branch` is a **read-only mirror** of the User Permission,
kept current by `on_user_permission_change` on `after_delete` — not `on_trash`, because
`on_trash` fires before the row leaves the table (`frappe/model/delete_doc.py`), so the
mirror would recompute against the row being deleted.

User Permission is the source of truth. Do not edit the mirror directly.

## Enforcement limits — read this

Filtering is applied **endpoint by endpoint in the API layer**, not by Frappe's permission
engine. `frappe.get_all` does not check permissions (`frappe/__init__.py`: *"Will **not**
check for permissions"*), and this app has ~331 `get_all` calls, ~845
`ignore_permissions=True`, and ~111 raw `frappe.db.sql` against ~2 `get_list`.

So User Permission covers Desk and `/api/resource`; the Vue API surface does not go
through it. Currently filtered: `_visit_items`, `_case_sheet_items` (`api/workspace.py`)
and `list_queue` (`api/queue.py`).

**This is worklist separation, not tenant isolation.** An endpoint not on that list still
returns cross-clinic data to a direct caller. `AGENTS.md` — "Backend APIs remain the
authority even when the frontend hides UI" — still applies. State this plainly to
stakeholders rather than implying the site is multi-tenant.

## Adding a clinic

1. Create a `Branch` record.
2. Assign users: `POST pet_app.api.permissions.update_user_restrictions`
   with `{"user": "...", "restrictions": {"branch": ["Clinic B"]}}`, or a User Permission
   row in Desk. The practitioner mirror and access cache update automatically.
3. Existing records stay on their current branch — reassign them deliberately.

Administrator is intentionally left unassigned so they see every clinic.

## Rejected alternative

The original proposal was rules + child doctypes hanging off a custom branch DocType, to
configure access dynamically from the frontend. Not built, because:

- `AGENTS.md` bans a second permission platform, naming five permission DocTypes already
  deleted for this reason (`Pet App Permission Rule`, `Pet App Workflow Rule`, …).
- `api/permissions.py` documents a production failure of role-name-based access:
  role names drifted per site and a doctor with valid DocPerms was refused.

Configurable access rules drift from actual enforcement, and you discover it when
somebody cannot do their job.

## Verification

```bash
bench --site <site> run-tests --app pet_app --module pet_app.tests.test_branch_scope
```

12 tests. Several assert things stay **global** — if a later change scopes pet history,
diagnostics or billing, the failing test names the decision instead of looking like a bug.

Backfill check, must be `0` for every scoped doctype:

```sql
SELECT COUNT(*) FROM `tabVet Visit` WHERE ifnull(branch,'')='';
```

Note: `bench console` imports fresh from disk and proves nothing about running workers.
Per `AGENTS.md`, restart with
`sudo supervisorctl restart frappe-bench-web:frappe-bench-frappe-web` and verify over HTTP.

## Patches

| Patch | Does |
| --- | --- |
| `clinic_branch_foundation` | Creates the first Branch, assigns users, backfills practitioners, drops the legacy `clinic branch` DocType |
| `vet_visit_branch_scope` | `Vet Visit.branch` + backfill |
| `worklist_branch_scope` | Same for the other four; converts `Pet Queue Ticket.branch` from free-text Data to Link |
| `branch_field_selectable` | Makes the field selectable so unrestricted users can choose |
| `sales_invoice_branch_scope` | `Sales Invoice.branch` backfill from the linked visit/boarding |
| `service_procedure_branch_scope` | `PetCareService` and `Pet Procedure` + backfill |
| `boarding_practitioner_not_branch` | Unscopes boarding; adds the optional `Pet Boarding.practitioner` field |
| `backfill_branch_orphans` | Re-runnable sweep for any record left NULL |

## Known pre-existing issues (not addressed here)

Found while investigating; recorded so they are not lost:

- `result_entered_by` / `released_by` overwrite unconditionally in `api/diagnostics.py`
  but are first-wins in `api/workspace.py` — same fields, divergent audit semantics.
- `_validate_release_integrity` exists on `Lab` with no `Imaging` counterpart.
- `_assign_record` (`api/workspace.py`) writes `Lab/Imaging.doctor`, which `STRICT_MODE`
  in `utils/visit_billing.py` then rejects for any visit-linked record.
- `_diagnostic_items` (`api/workspace.py`) accepts `user`/`roles` and uses neither. It is
  unscoped **by decision** — do not "fix" it without reversing the diagnostics decision.
