# WhatsApp Notification Engine — Frontend Integration Guide

How the frontend talks to the `pet_app` WhatsApp engine: configure the account, build & send messages (instant or scheduled), manage reminders/follow-ups, and monitor delivery.

> **Backend:** Meta WhatsApp Cloud API (`graph.facebook.com`). All calls go through Frappe whitelisted methods at `POST/GET /api/method/<dotted.path>`.

---

## 0. Conventions

- **Base URL:** `/api/method/`
- **Auth:** standard Frappe session cookie or `Authorization: token <api_key>:<api_secret>`.
- **POST body:** send JSON. Most endpoints accept either a flat body OR a `{"data": {...}}` wrapper — flat is simplest.
- **Response shape** (from `api_success` / `api_error`):
  ```json
  { "ok": true,  "data": { ... }, "meta": { ... } }
  { "ok": false, "error": { "message": "...", "code": "VALIDATION_ERROR" } }
  ```
  Always branch on `ok`.

All method paths below are under `pet_app.api.notifications.*` unless noted.

---

## 1. Configuration screen (Admin)

This is what an admin fills in **once** so the engine can send. Requires role `System Manager` or `Healthcare Administrator`.

### 1.1 Read / write global settings

**GET** `get_notification_settings` → `data.settings`
**POST** `update_notification_settings` with any subset of fields.

Fields the frontend should expose as a **Settings** form:

| Field | Type | Meaning | Safe default |
|---|---|---|---|
| `enabled` | bool | Master switch. If off → everything is `Skipped`. | `1` |
| `dry_run` | bool | **If on, NOTHING is actually sent to Meta** (payload built only). Turn OFF to go live. | ships `1` |
| `default_channel` | select | WhatsApp / SMS / Email / In App | `WhatsApp` |
| `default_country_code` | data | Prepended to local numbers | `964` (Iraq) |
| `default_language` | data | Template language fallback | `en` |
| `allow_marketing_messages` | bool | Gate for `Marketing`-category templates | `0` |
| `require_opt_in` | bool | Block send if guardian hasn't opted in (Auth bypasses) | `0` |
| `respect_quiet_hours` | bool | Delay non-urgent sends during quiet window | `0` |
| `quiet_hours_start` / `quiet_hours_end` | time | e.g. `21:00:00` → `08:00:00` | |
| `default_whatsapp_account` | link | Which account to send from | |
| `max_retries` / `retry_after_minutes` | int | Auto-retry policy | `3` / `5` |
| `otp_template` | data | Template key used for OTP | `auth_otp` |
| `mask_sensitive_values` | bool | Mask OTP/token in logs | `1` |

> ⚠️ **Go-live checklist to surface in the UI:** `enabled=1` **and** `dry_run=0` **and** a `default_whatsapp_account` set, otherwise show a "not sending for real" banner.

### 1.2 WhatsApp Account (credentials)

The account holds Meta credentials. There is **no dedicated create/list API** for the account in `notifications.py` — manage it through the **Frappe desk / `frappe.db` REST resource API** for DocType `Pet App WhatsApp Account`, or build a thin admin form using the generic resource endpoints:

- **List:** `GET /api/resource/Pet App WhatsApp Account`
- **Create/Update:** `POST/PUT /api/resource/Pet App WhatsApp Account`
- **Test/health:** `GET /api/method/pet_app.api.notifications.test_whatsapp_account?account=<name>` → stamps `last_health_check_at`.

Account fields to collect:

| Field | Notes |
|---|---|
| `account_name` | unique, required (the record name) |
| `provider` | **must be `Meta Cloud API`** to actually send (else Dummy) |
| `is_default` / `enabled` | pick the sending account |
| `whatsapp_business_account_id` | from Meta |
| `phone_number_id` | **from Meta — used in the send URL** |
| `phone_number` / `display_phone_number` | human display |
| `graph_api_version` | default `v20.0` |
| `access_token` | **password field** — Meta permanent token |
| `app_secret` | **password field** |
| `verify_token` | **password field** — used by the webhook handshake |
| `default_language` | `en` |

> Password fields are write-only; never rendered back. UI should show "•••• set" vs "not set".

### 1.3 Webhook (receipts + opt-out) — set up in Meta

Register this URL in the Meta App → WhatsApp → Configuration:

- **Callback URL:** `https://<your-site>/api/method/pet_app.api.whatsapp.webhook`
- **Verify token:** must equal the account's `verify_token`.

Meta will GET it once (handshake) then POST delivery/read/failed receipts and inbound messages. Inbound STOP words (`STOP`, `ايقاف`, `الغاء`, `إلغاء`) auto opt-out the sender. No frontend work needed beyond showing the URL to copy.

---

## 2. Templates (what you're allowed to send)

WhatsApp requires **pre-approved templates** for business-initiated messages. Each Meta template maps to one `Pet App WhatsApp Template` row keyed by `template_key`.

| Method | Path | Use |
|---|---|---|
| GET | `list_templates?event_key=&enabled=1` | template picker |
| GET | `get_template?template_key=<key>` | load one |
| POST | `save_template` | create/update (body = template fields) |
| POST | `preview_template` | render body with sample `context` |

**Template picker → preview flow (frontend):**
```js
// 1. load enabled templates
const t = await api("list_templates", { enabled: 1 });   // GET
// 2. when user picks one + fills variables, preview it
const p = await api.post("preview_template", {
  template_key: "invoice_due",
  context: { amount: "45,000 IQD", pet_name: "Rex" }
});
// p.data.preview  → show WYSIWYG bubble
```

Key template fields: `template_key`, `template_name` (the exact Meta name), `language`, `category` (Authentication/Utility/Marketing/Service), `event_key`, `recipient_type`, `body_preview`, `components_json`, `buttons_json`, `requires_opt_in`, `allow_during_quiet_hours`, `priority`.

---

## 3. Sending messages

The whole engine is **one queue**. Timing is controlled by two things: whether you pass `send_after`, and whether you call the "process now" variant.

### 3.1 Send **instantly** (button: "Send WhatsApp now")

**POST** `send_manual_notification` (queues **and** fires immediately when `process_now` truthy — default 1).

```js
await api.post("send_manual_notification", {
  event_key: "death_certificate.ready",
  recipient_type: "Guardian",          // Guardian | Customer | User | Doctor | Manual
  recipient_name: "GUARD-0001",        // the linked record name
  template_key: "death_certificate_ready",
  context: { pet_name: "Rex", guardian: "Sara" },
  source_doctype: "Pet Death Record",  // optional trace-back
  source_name: "PDR-0007",
  channel: "WhatsApp",
  process_now: 1
});
// → data.queue.status === "Sent" (or Failed with error)
```

For **Manual** recipients (no linked record) pass `to_phone` directly instead of `recipient_name`.

**Deduplication contract.** `send_manual_notification` sets `manual=true`, and a manual send is treated as a deliberate action: **every click dispatches a fresh message** — it is *not* de-duplicated against a prior send. (Automated rule triggers and any call that passes an explicit `idempotency_key` still de-dupe: a repeat returns `ok` with `meta.duplicate === true` and does **not** re-dispatch.) If you want a specific manual button to be click-safe (guard against accidental double-submits), pass your own stable `idempotency_key`; then a repeat returns the existing row with `meta.duplicate === true` instead of sending again.

> ⚠️ **Always read `data.queue.status`, not just `ok`.** `ok: true` only means the request was accepted. The message was actually delivered only when `data.queue.status === "Sent"`. If you ever pass an `idempotency_key`, also check `meta.duplicate` — a `true` means "already sent, not re-sent" and the UI should say so rather than showing a fresh success toast.

### 3.2 **Schedule** for later (invoice due, follow-up next week)

**POST** `queue_notification` with `send_after` (ISO datetime) and **do not** process now. The scheduler sends it when due.

```js
await api.post("queue_notification", {
  event_key: "invoice.due_reminder",
  recipient_type: "Customer",
  recipient_name: "CUST-0002",
  template_key: "invoice_due",
  context: { amount: "45,000 IQD" },
  source_doctype: "Sales Invoice",
  source_name: "SINV-0009",
  channel: "WhatsApp",
  send_after: "2026-07-25 09:00:00",
  manual: true
});
// → data.queue.status === "Queued", scheduled for send_after
```

### 3.3 Timing model (mental map)

```
queue_notification({ send_after })
        │
        ├─ send_after empty/past  + process_now  → sends NOW
        └─ send_after in future                  → sits Queued → scheduler fires at that time
```

Global overrides that can still delay/skip a send: `enabled=0` (Skipped), `dry_run=1` (no real send), quiet hours (non-urgent delayed), consent/marketing gates. Use `priority: "urgent"` to bypass quiet hours.

---

## 4. Reminders & follow-ups (recommended for scheduled sends)

For anything recurring or date-driven (follow-up after a visit, vaccination due, invoice due), create a **Pet App Reminder** instead of hand-scheduling a queue row. The scheduler converts it into a WhatsApp send at `send_at`.

| Method | Path | Use |
|---|---|---|
| POST | `create_reminder` | schedule a reminder/follow-up |
| GET | `list_reminders?status=Scheduled` | list |
| POST | `cancel_reminder` | cancel one (`{reminder: "<name>"}`) |
| POST | `enqueue_due_reminders` | force-run the enqueuer (admin/debug) |

```js
await api.post("create_reminder", {
  reminder_type: "Follow-up Due",   // see full list below
  guardian: "GUARD-0001",
  pet: "PET-0003",
  channel: "WhatsApp",
  template_key: "followup_checkin",
  send_at: "2026-07-22 10:00:00",   // if omitted → due_datetime or now
  context_json: JSON.stringify({ pet_name: "Rex" }),
  source_doctype: "Vet Visit",
  source_name: "VISIT-0012"
});
```

`reminder_type` options: `Appointment Reminder`, `Follow-up Due`, `Vaccination Due`, `Deworming Due`, `Medication Refill`, `Boarding Checkout`, `Lab Result Released`, `Invoice Due`, `Food Reorder`, `Death Certificate Ready`.

---

## 5. Monitoring dashboard

| Method | Path | Returns |
|---|---|---|
| GET | `get_notification_stats` | counts grouped by status → KPI tiles |
| GET | `list_notification_queue?status=&limit=50` | live queue table |
| GET | `get_notification_queue?queue=<name>` | single message detail |
| GET | `list_notification_logs?queue=<name>&limit=50` | audit trail (masked phone) |
| POST | `retry_notification` | `{queue: "<name>"}` → re-send a failed one |
| POST | `cancel_notification` | `{queue: "<name>"}` → cancel a pending one |

**Status lifecycle** to render as a badge/timeline:
```
Queued → Processing → Sent → Delivered → Read
                    ↘ Failed → (auto-retry) → Retry Scheduled
Cancelled / Skipped  (terminal, no send)
```
`delivered_at` / `read_at` are filled by the Meta webhook. If they stay empty long after `sent_at`, the webhook isn't wired.

**Suggested dashboard layout**
- KPI row from `get_notification_stats` (Sent / Delivered / Read / Failed / Queued).
- Queue table (poll `list_notification_queue` every ~15s), row → detail drawer with `provider_message_id`, error code/message, timeline of `*_at` timestamps, retry/cancel actions.
- A "Reminders" tab from `list_reminders`.

---

## 6. Consent / opt-out (compliance)

Consent lives in `Pet App Communication Consent` (per party + channel + phone). There's no dedicated notifications API for it here — manage via the resource API (`/api/resource/Pet App Communication Consent`) or desk. Fields: `opt_in`, `marketing_allowed`, `utility_allowed`, `authentication_allowed`, `opt_out_at`, `opt_out_reason`. Inbound STOP words flip `opt_in` off automatically via the webhook. Surface an opt-in/out toggle on the guardian profile.

---

## 7. Minimal client helper

```js
const BASE = "/api/method/pet_app.api.notifications.";
async function api(method, params = {}) {   // GET
  const q = new URLSearchParams(params).toString();
  const r = await fetch(`${BASE}${method}?${q}`, { credentials: "include" });
  return unwrap(await r.json());
}
api.post = async (method, body = {}) => {
  const r = await fetch(`${BASE}${method}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": window.csrf_token },
    body: JSON.stringify(body)
  });
  return unwrap(await r.json());
};
function unwrap(json) {
  const m = json.message ?? json;         // Frappe wraps in { message: ... }
  if (m && m.ok === false) throw new Error(m.error?.message || "Request failed");
  return m;
}
```

---

## 8. Backend gaps — what's missing / what the frontend cannot rely on yet

Be honest with these when planning the UI. The engine is real and production-shaped, but these pieces are **not implemented** on the backend:

1. **Auto-trigger on business events is wired via `Pet App WhatsApp Action Rule` (not `Pet App Notification Rule`).** `hooks.py` registers wildcard `doc_events` (`after_insert` / `on_update` / `on_submit`) → `pet_app.notifications.actions.evaluate_document_rules`, which reads enabled **`Pet App WhatsApp Action Rule`** rows for the source doctype, checks `condition_json`, and queues through the engine. So saving a **Pet Death Record** (Link fields `pet` → Pet, `guardian` → Guardian) with a matching enabled rule **does** dispatch automatically, and `build_document_context` populates `pet.*` / `guardian.*` (incl. `display_name`) for the template. The older **`Pet App Notification Rule`** DocType is the one with no evaluator — don't build automation on it. → For one-off sends the frontend still calls `send_manual_notification` / `create_reminder`; for config-driven automation, manage `Pet App WhatsApp Action Rule` rows.

2. **Rate limiting is a stub.** `assert_rate_limit_allowed` always returns True. `max_messages_per_minute` / `max_messages_per_day` settings exist but are **not enforced**. Don't present them as active guarantees.

3. **Only Meta + Dummy channels actually send.** `SMS`, `Email`, `In App`, `Twilio`, `360dialog`, `Custom` are enum values with **no sending implementation**. `fallback_to_sms` / `fallback_to_email` settings do nothing. UI should hide/disable non-WhatsApp channels (or label them "coming soon").

4. **No account CRUD API in `notifications.py`.** Account and Consent records must be managed via the generic Frappe resource API or desk — there's no `save_account` / `list_accounts` whitelisted method. Either build on `/api/resource/...` or ask backend for dedicated endpoints.

5. **Two parallel reminder systems.** The real engine uses `Pet App Reminder`; a legacy `Pet Reminder` + `send_manual_reminder` path also exists and only writes an in-app log (except when `channel="WhatsApp"`, which reroutes to the engine). Build the frontend on `Pet App Reminder` (`create_reminder` / `list_reminders`), not the legacy path.

6. **Template sync is manual.** Creating a `Pet App WhatsApp Template` row does **not** create/approve the template in Meta. Templates must be approved in Meta separately, and `template_name` must match exactly. No "sync from Meta" endpoint exists.

7. **No realtime push to the frontend.** Delivery/read updates land via the Meta webhook into the DB; the frontend must **poll** `list_notification_queue` — there's no websocket event emitted for status changes yet.

### If you want "send on death record / invoice" to be config-driven
This already exists through **`Pet App WhatsApp Action Rule`** (see gap #1): `doc_events` in `hooks.py` fan out to `actions.evaluate_document_rules`, which evaluates enabled rules (`source_doctype`, `trigger_event`, `condition_json`, `template_key`, recipient resolution) and queues via the engine. Build the "Automation Rules" UI on this doctype. The legacy `Pet App Notification Rule` doctype (`send_timing`, `delay_minutes`, …) has **no** evaluator — don't target it.
```
