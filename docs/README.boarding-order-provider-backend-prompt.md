# Boarding Order Provider Backend Prompt

Add **`provider`** — who is doing the work — to `pet_app.api.healthcare.boarding.create_order`,
for `kind` in `service` · `procedure` (and `lab` · `radiology` if those are assignable).

**There is a live UI defect riding on this.** The boarding Add Order dialog **already shows an
"Assigned to" picker**, the operator already picks a practitioner, and the value is **discarded in
the client before the request is built**. The order is created unassigned, and the Service Provider
page shows it with nobody on it. From the desk it looks like the assignment was accepted and then
lost. Until this parameter exists the client cannot fix that except by removing the control.

---

## 1. There is nowhere to put it today — verified, not assumed

**`create_order` declares no provider parameter and takes no `**kwargs`.** The signature per
[`BOARDING_CREATE_ORDER_API.md`](BOARDING_CREATE_ORDER_API.md) §2 is `boarding_id`, `kind`, `pet`,
`template_id`, `care_service_id`, `item_code`, `priority`, `note`, `dose_option`, `warehouse`,
`dosage`, `frequency`, `duration_days`, `scheduled_datetime`. Fourteen parameters, none of them a
practitioner, a user, or an assignee. Grepping the contract for `provider` / `practitioner` /
`assigned_to` returns nothing.

Per §2 of that document, a name outside the table is filtered out of `form_dict` by Frappe before
the call — **silently, with no error and no effect**. So sending `provider` today would change
nothing while looking like it worked.

**The billable row has no field for it either.**

## 2. The visit already has this. Boarding is the surface that lacks it

`create_orders` on the visit path accepts `provider` on both assignable kinds — see
[`visit-orders.md`](visit-orders.md):

| kind | visit `create_orders` | boarding `create_order` |
| ----------- | --------------------- | ----------------------- |
| `service` | `provider` ✅ | — nothing |
| `procedure` | `provider` ✅ | — nothing |
| `lab` / `radiology` | — | — nothing |

`orderActionPayload` in `visitWorkbenchApi.ts` sends it on both. So the **same dialog**, opened from
a visit, assigns correctly; opened from boarding, the assignment evaporates. Same control, same
value, two different outcomes — which is exactly how this was reported.

## 3. What we are asking for

| Name | Type | Required | Notes |
| ---------- | ------------------------------- | :------: | ---------------------------------------------------- |
| `provider` | string | no | Who performs the work. Ignored for `kind=medication` |

**Optional, and absence stays valid.** An unassigned order is the ordinary "whoever is free" case
and must keep working exactly as it does now. Please do **not** default it to the calling user — a
defaulted assignee is indistinguishable from a real one, and the desk would lose the ability to see
what still needs assigning. Same rule `scheduled_datetime` got, for the same reason.

**⚠️ Please confirm WHICH IDENTIFIER `provider` expects, because the client may already be sending
the wrong one on the visit path.** [`visit-orders.md`](visit-orders.md) shows
`provider: "provider@example.com"` — a **User email** — while the order dialog's picker is a
`Healthcare Practitioner` autocomplete and submits `option.id`, which is the **Practitioner docname**
(`HLC-PRAC-…`). If the field is a Link to User, visit-path assignment may be failing quietly too.
Name the target doctype in the reply and the client will send that; the picker already requests
`user_id` alongside the docname, so either is available without another lookup.

## 4. Echo it back on the billable row

Please return `provider` on the `billable_item` in the `create_order` response and on
`billable_items[]` in `get_boarding_detail`, alongside `scheduled_datetime`.

**Read back, never assumed.** The row is the only proof the assignment stored, so an assignment
that silently failed reads as unassigned rather than as the name still sitting in the form. Same
pattern as the schedule fields.

A display name (`provider_name`) beside the id would save the orders panel a lookup — the Service
Provider page already renders `providerName || provider || user`, so it is the shape that surface
expects.

## 5. What the client will do once it lands

- Send `provider` from the boarding host (`onOrdersAdded` in `src/pages/healthcare/boarding/[id].vue`),
  which currently sends nine fields and drops the dialog's `provider`.
- Show the assignee on the boarding orders panel, so a boarding order reads as assigned in the same
  place its schedule does.

## 6. What we are NOT asking for

- **No billing effect.** Who performs the work must not change what is charged.
- **No validation that the practitioner is available, rostered, or qualified.** If the assignment is
  wrong that is a desk decision, not an API refusal.
- **Not asking for the visit side to change**, beyond the identifier question in §3.
- **No new doctype.** A field on the existing row is enough.
