# Branch Selection Modal — Build Spec

**Status:** not built. This is the missing piece that is currently breaking record creation for the Administrator account.

**Audience:** whoever builds the Vue side (`alkhokh_pet_store_front`, branch `development`).

**Relationship to [`frontend-branch-separation.md`](./frontend-branch-separation.md):** that document is the general handoff and defines the contract. This one is the concrete build order for the modal specifically — the decisions that document leaves open (when to open it, what to do about the 7 doctypes vs. the generic `frappe.client.insert` call, what to cache, how to test). Read section 2 and 3 of that doc first; nothing here contradicts it.

---

## 1. The problem in one paragraph

`main` used to be the only Branch on this site, so the backend could always infer which clinic a new record belonged to. A second Branch (`hotel`) was created on 2026-08-12. From that moment, any user who is **not** pinned to exactly one branch can no longer create records: the backend refuses to guess and throws, and the frontend has no control that lets the user answer. This is not a design gap in the backend — the answer channel exists and is documented. It has simply never been consumed.

**This has already caused a real failure.** On 2026-08-12 at 14:16 site time, five consecutive attempts to create a Vet Case Sheet for a live patient (`PET-00129`, guardian `GUARDIAN-00107`, complaint "Checkup") failed for the `Administrator` account, via `POST /api/method/frappe.client.insert`. The intake was redone 26 minutes later by a branch-assigned colleague.

---

## 2. Where it throws (backend, do not change)

`pet_app/utils/branch.py:208` — `stamp_branch_on_insert`, a `before_insert` hook.

Resolution order:

1. `:234-238` — **branch supplied by the client** → validated, then stamped. *This is the path the modal feeds.*
2. `:240-243` — else the user's own branch, from their Branch User Permission.
3. `:245-256` — else, if the user is unrestricted: use the only Branch on site if there is exactly one; **otherwise throw**.

```text
Select a branch for this Vet Case Sheet. Your user is not restricted to one clinic,
so the branch cannot be inferred.
```

Exception type is `frappe.MandatoryError`.

The relevant condition is at `:283-285`:

```python
def _sole_branch() -> str | None:
    rows = frappe.get_all(BRANCH_DOCTYPE, pluck="name", limit_page_length=2)
    return rows[0] if len(rows) == 1 else None
```

Two Branches ⇒ `None` ⇒ throw. Nothing else about branch changed on 2026-08-12; only the *count* did.

---

## 3. Which doctypes need the modal

The seven in `utils/branch.py:18-42` (`SCOPED_DOCTYPES`):

| Doctype | Hook site |
|---|---|
| Vet Visit | `hooks.py:321` |
| Appointment | `hooks.py:330` |
| Vet Case Sheet | `hooks.py:337` |
| Pet Queue Ticket | `hooks.py:341` |
| PetCareService | `hooks.py:352` |
| Pet Procedure | `hooks.py:358` |
| Sales Invoice | `hooks.py:374` |

**Pet Boarding is not in this list and must not get the modal.** Boarding is deliberately global — one facility serves both clinics — and its branch is set from a setting (`Pet Boarding Settings.boarding_branch`) by the `stamp_boarding_branch` hook, not from the operator. Adding a picker there would re-introduce the misattribution this fixes.

> **History.** That stamp was reverted once, and between the revert and 2026-08-13 this section described behaviour that was not live. Stamping the facility made checkout raise an invoice carrying a branch the operator had no claim to, and Frappe refused the insert. It is now re-wired: `check_out_boarding` authorises the caller and passes `branch_authorised`, which covers both `stamp_branch_on_insert`'s `assert_can_write_to_branch` and Frappe's own user-permission check on `Sales Invoice.branch`. See `utils/branch.py::BRANCH_AUTHORISED_FLAG`.

---

## 4. The endpoint

```http
GET /api/method/pet_app.api.branch.get_branch_context
```

```ts
type BranchContext = {
  current: string | null;                                  // the user's branch, or null
  available: { name: string; branch: string }[];           // what they may file into
  sees_all_branches: boolean;                              // true = unrestricted
  must_select: boolean;                                    // true = SHOW THE MODAL
};
```

`must_select` is computed at `pet_app/api/branch.py:63` as `len(available) > 1 && !current`.

**Live values on this site right now** — verified, not hypothetical:

| User | `current` | `available` | `must_select` |
|---|---|---|---|
| `Administrator` | `null` | `["main", "hotel"]` | **`true`** |
| `atthar@app.com` | `null` | `["main", "hotel"]` | **`true`** |
| `farah@app.com` | `"hotel"` | `["hotel"]` | `false` |
| `dr.ayman@petapp.com` | `"main"` | `["main"]` | `false` |

Only 2 real accounts have `must_select: true`. The other 18 unrestricted accounts are `@example.com` test fixtures that have never been logged into. **The modal will be seen by almost nobody — but the accounts that see it are blocked without it.**

---

## 5. What to build

### 5.1 Fetch and cache

Call once on app boot, next to the existing access-snapshot call. Cache in a store (`branch.store.ts` or similar). It changes only when an admin reassigns the user, so a per-session cache is correct. Do **not** call it per create.

### 5.2 The rule

```ts
// must_select === false  →  omit `branch` entirely. The backend stamps it.
//                           Do NOT send null, "" , or a guessed value.
// must_select === true   →  the user must choose; send the chosen value as `branch`.
```

Sending `null` or `""` when `must_select` is true is the same as sending nothing — `cstr(...).strip()` at `utils/branch.py:234` treats them identically and you get the throw.

### 5.3 When the modal opens

Open it **before** the create request, not in response to the error. Two acceptable patterns — pick one and use it consistently:

- **Per-create (recommended).** On submit of any create form for the seven doctypes, if `must_select` is true and no branch has been chosen for this session, open the modal, then continue the submit with the chosen value. One decision per session, not per record.
- **Session-scoped.** Ask once on first create after login, store the choice for the session, show it in the header with a way to change it.

Do not implement it as a retry-on-`MandatoryError`: the error arrives after the user has filled in an entire clinical form, and Frappe's rollback means their work is discarded.

### 5.4 Modal content

- Title: something like "Which clinic is this for?"
- A required single-select over `available`, using `branch` as the display label and `name` as the submitted value. On this site both are the same string, but do not assume that — they are separate fields.
- No default selection. Do not pre-pick the first option; guessing is the bug being fixed.
- Cancel must abort the create, not submit without a branch.
- Copy should say *clinic*, not *branch*, to match how staff talk about it. The Arabic term already in use elsewhere is «الفرع».

### 5.5 Sending it

```ts
const ctx = useBranchStore();

const payload = {
  doctype: "Vet Case Sheet",
  animal_patient: petId,
  guardian: guardianId,
  // ...clinical fields...
  ...(ctx.must_select ? { branch: chosenBranch } : {}),
};
```

**Watch for the generic insert path.** The failure on 2026-08-12 came through `frappe.client.insert` with a raw doctype payload, not through a typed API function. If the app has a shared `insert()` wrapper, put the branch injection **there**, keyed on a set of the seven doctype names — that covers every current and future create site at once and is far safer than patching seven forms individually.

---

## 6. One backend gap the modal does not cover

`create_sales_invoice_for_guardian` (`pet_app/api/sales.py:143-154`) calls `get_or_create_open_invoice` **without passing a branch**, so it falls through to the same unresolved-branch throw for an unrestricted user. The modal cannot fix this from the client unless the endpoint is given a `branch` parameter to accept.

For contrast, the two callers that are already safe, and why:

| Caller | Passes | Safe? |
|---|---|---|
| `doctype/vet_visit/vet_visit.py:822-828` | `branch=visit.get("branch")` | yes — the visit already carries one |
| `api/healthcare/boarding.py` (checkout) | `branch=boarding.get("branch")` plus `branch_authorised=True` | yes — the stay carries its own stamped branch, and the elevation lets it be a branch the operator has no claim to |
| `api/sales.py:143-154` | *nothing* | **no — throws** |

Decide separately whether that endpoint should take a branch argument or derive one. It is out of scope for the modal but will look like the same bug when it is reported.

---

## 7. Acceptance criteria

1. `dr.ayman@petapp.com` (main) creates a Vet Case Sheet → **no modal**, record lands with `branch = "main"`.
2. `farah@app.com` (hotel) creates a PetCareService → **no modal**, record lands with `branch = "hotel"`.
3. `Administrator` creates a Vet Case Sheet → **modal appears**, offers `main` and `hotel`, no default; choosing `main` lands the record with `branch = "main"`.
4. `Administrator` cancels the modal → no request is sent, no partial record, form state preserved.
5. All seven doctypes in §3 exercise the same path, including creates made through the generic `frappe.client.insert` wrapper.
6. Pet Boarding create → **no modal**, and no `branch` key in the payload.
7. Regression: with the modal shipped, `Error Log` gains no new `Select a branch` entries. There are exactly 5 today, all from 2026-08-12 14:16, all `Administrator`, all Vet Case Sheet.

---

## 8. If you need it working before the frontend ships

Give `Administrator` Branch User Permissions for **both** `main` and `hotel`. That makes `get_current_branch()` resolve to the first, so inserts stop throwing, while keeping both branches visible. Give `atthar@app.com` a single `main` permission. Two records, no deploy, fully reversible — and it does not remove the need for the modal, it just stops the bleeding.

Do **not** fix this by deleting `hotel`. The boarding revenue attribution landed on 2026-08-12 now depends on that Branch existing (`Pet Boarding Settings.boarding_branch = "hotel"`).
