# Migration note: guardian pet pickers move to `get_guardian_pets`

**Status:** live on `frappe.localhost` (migrated, hook registered, restart pending).
**Applies to:** the six call sites that load a guardian's animals with a raw
`/api/resource/PetGuardian?filters=[["guardian_id","=",X]]&fields=[...,"pet_id.*"]` query.

## Why this changes

The guardian-scoped picker was the one selection surface with no method behind it. The
client issued a raw resource query and assembled the list itself, so none of the
deceased-pet filtering applied to the other selection endpoints could reach it — that is
how PET-00191, a deceased animal, was offered for boarding and only refused at submit.

A rule that lives in a method binds only the callers that call it. So two things shipped
together: the method below, and a `permission_query_conditions` hook on PetGuardian so the
raw path can no longer return **more** than the method would.

## 1. Replace the raw query

```
GET/POST /api/method/pet_app.api.pet.get_guardian_pets
```

| Param | Default | Meaning |
|---|---|---|
| `guardians` | — | One guardian id, a JSON array, or a comma-separated string. All three encodings work, so the one-guardian case needs no request body. |
| `include_deceased` | `false` | Opt in to deceased pets. Accepts `true`/`1`/`false`/`0` as strings — a GET query param is always a string, and that is handled. |
| `include_archived` | `false` | Opt in to `pet_status = "Archived"`. |
| `search` | — | Server-side `pet_name` match. Do not filter client-side; it would fight the paging. |
| `page`, `page_size` | `1`, `0` | `page_size=0` (the default) returns everything unpaged. Guardians hold 1.4 pets on average and 11 at most on this site, so the default saves each caller a loop. |

Response is the usual `standardize_response` envelope; the payload is:

```json
{
  "guardians": ["GUARDIAN-00107"],
  "page": 1, "page_size": 0, "total": 3, "total_pages": 1,
  "has_next": false, "has_prev": false,
  "data": [
    {
      "pet_id": "PET-00128", "name": "PET-00128", "pet_name": "Luna",
      "pet_image": "...", "animal_species": "Mammal", "animal_type": "Dog",
      "breed": "...", "gender": "...", "weight": 12.0, "hight": null,
      "birth_date": "...", "color": "...", "blood_type": "...",
      "status": "...", "pet_status": "...",
      "is_deceased": 0, "death_date": null, "death_record": null,
      "link": "<PetGuardian row name>", "role": "primary_owner",
      "guardian_id": "GUARDIAN-00107",
      "links": [{"link": "...", "guardian_id": "...", "role": "..."}]
    }
  ]
}
```

Three things to note when you map it:

- **One row per PET, not per link row.** `pet_id` is unique on PetGuardian only by
  convention — nothing constrains it — so asking for several guardians who share an animal
  no longer puts that animal in the picker twice. The naming link is chosen
  primary-owner-first; every matching link stays in `links`.
- **The death markers ride along even in the default living-only response.** A picker that
  can see a pet is deceased can grey the row and say why. One that simply never receives
  the pet leaves the operator wondering where the animal went.
- `name` and `pet_id` are the same value, both present so existing mappers keyed on either
  keep working.

## 2. The mortality picker passes `include_deceased=True`

That picker, the guardian's own memorial view, and medical history are the reason this is
an opt-in rather than a filter nobody can turn off. Everything that creates a record —
boarding, visits, appointments — takes the default and gets living animals only.

## 3. Two related endpoints changed

- `pet_app.api.pet.list_pets` gained `include_deceased` (default `0`). This is the list
  every clinical and billable flow picks from, so the dead are out by default here too.
  Pass `include_deceased=1` for medical history, the death record, mortality reporting.
- `pet_app.api.mobile.pets.list_pets` gained `exclude_deceased` (default `0` — deceased
  pets are **kept**). This is a guardian looking at their own animals, not a clinician
  picking one to bill; hiding a pet that died would delete it from its owner's view of
  their own family, and the payload carries `is_deceased`/`death_date` so the app can
  render a memorial state. Pass `exclude_deceased=1` in the in-app flows that DO create
  records — booking or requesting an appointment.

## 4. Raw PetGuardian reads are now row-scoped — read this even if you migrate everything

A `permission_query_conditions` hook is now registered for PetGuardian. For any request
that still reaches the resource API:

- Full-access roles (`Administrator`, `System Manager`, `Pet App Admin`,
  `Healthcare Administrator`) — unchanged, everything.
- A user linked to a `Guardian` record — **only their own links.** A guardian portal user
  who asks for another guardian's id now gets an empty list, not that owner's pets.
- Everyone else — unchanged, whatever their role grants.

Verified on this site: a guardian-linked user with PetGuardian read now sees 11 of 2279
link rows and nothing belonging to another owner.

The practical consequence for the frontend: **any code path that read across guardians
from a guardian session now returns fewer rows, silently and correctly.** If a screen
looked like it worked because it was quietly reading rows it should never have had, it
will now look empty. That is the fix, not a regression — but it is worth checking those
six call sites for it rather than discovering it in support.

Clinic staff who also happen to have a Guardian record of their own (a vet whose dog is on
file) are matched by the full-access check first, so they are not narrowed to their own
animal and locked out of their job.

## What is deliberately NOT closed

Staff with no Guardian record of their own are unscoped: a receptionist at one clinic can
still list every guardian link on the site. This is narrower than what shipped before —
previously a guardian could read any other guardian's pets, which is the disclosure that
actually mattered — and it is left open on purpose rather than half-designed. Closing it
needs a `branch` column on PetGuardian or Pet (neither has one), a backfill patch before
the filter goes on, and an answer for the pet treated at more than one clinic. The
reasoning is recorded in `pet_app/permissions/petguardian.py`.

No frontend change is needed for that gap today.
