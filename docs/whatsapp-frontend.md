# WhatsApp Inbox, Actions, and Ratings Frontend Contract

## Purpose

This document is the frontend contract for the Pet App WhatsApp system.

It covers:

- WhatsApp conversations and messages.
- The 24-hour customer-service window.
- Text and media replies.
- Dynamic message templates and variables.
- Configurable response buttons, lists, and typed aliases.
- Staff review and approval actions.
- The default PetCareService rating flow.
- Linked records, unread state, notifications, and delivery failures.

The ready-made Frappe Desk screen is available at:

```txt
/app/whatsapp-inbox
```

Production URL:

```txt
https://admin.kokh-vet.com/app/whatsapp-inbox
```

## Important Meta Rules

> ### ⚠ REQUIRED SETUP — webhook field subscription
>
> The WABA **must be subscribed to the `message_template_status_update` webhook field**, in
> the Meta App dashboard under **WhatsApp → Configuration → Webhook fields**. Tick it
> alongside `messages`.
>
> Nothing in this repository can declare, verify, or repair that subscription — it lives only
> in the Meta dashboard. If it is not ticked, template approvals and rejections **arrive
> nowhere**: the mirror keeps showing the status it last saw, and there is no error anywhere
> to tell you why.
>
> `sync_meta_templates` is the fallback path and always works, because it reads the templates
> from Meta directly. The webhook is the fast path, not the only one. If approvals seem
> frozen, check this box first, then run a sync.
>
> See [Meta Template Mirror](#meta-template-mirror).

### Phone Number Format

The backend accepts Iraqi local or international input:

```txt
07716940612
+9647716940612
9647716940612
```

It stores and sends the normalized Meta value without `+`:

```txt
9647716940612
```

Frontend forms may accept all three formats. Always display the backend-provided
`normalized_phone` in inbox data instead of normalizing it again in JavaScript.

### The 24-Hour Window

An inbound guardian message opens the conversation for 24 hours.

When `conversation.session_open` is `true`:

- Staff may send free-form text.
- Staff may send files.
- App-styled messages may include reply buttons or a list.

When `conversation.session_open` is `false`:

- Disable the free-form composer.
- An approved Meta template is required to contact the guardian.
- A Hybrid action sends its configured Meta fallback invitation first.
- The app-styled interactive prompt is sent after the guardian replies and opens the window.

Do not offer a frontend override for this restriction.

## Authentication and Response Envelope

All admin endpoints require an authenticated Frappe session and the relevant DocType permission.

Frappe Desk can use `frappe.xcall`:

```js
async function petAppCall(method, args = {}) {
  const response = await frappe.xcall(method, args);

  if (!response.ok) {
    throw new Error(
      response.errors?.[0]?.message || "Request failed"
    );
  }

  return response.data;
}
```

Direct HTTP requests use:

```txt
/api/method/{method-path}
```

Frappe wraps the returned Pet App envelope in `message` for direct HTTP calls:

```json
{
  "message": {
    "ok": true,
    "data": {},
    "meta": {},
    "errors": []
  }
}
```

Normalize it in an external frontend:

```js
function unwrapFrappeResponse(httpBody) {
  const result = httpBody.message ?? httpBody;
  if (!result.ok) {
    throw new Error(result.errors?.[0]?.message || "Request failed");
  }
  return result.data;
}
```

POST requests should use JSON and the normal Frappe CSRF/session handling used by the rest of the admin frontend.

## Permissions

The generated WhatsApp DocTypes grant normal access to:

- `System Manager`
- `Healthcare Administrator`
- The role configured in `whatsapp_full_access_role`

The APIs still check DocType permissions. Do not hide an API permission error behind an empty state.

## Inbox Data Model

### Conversation

Important fields:

| Field | Type | Frontend use |
|---|---|---|
| `name` | string | Conversation id, for example `WA-CONV-2026-00001`. |
| `display_name` | string | Guardian name, or normalized phone when unmatched. |
| `normalized_phone` | string | Meta-format number without `+`. |
| `guardian` | Link | Linked Guardian when resolved by phone. |
| `customer` | Link | Linked Customer when available. |
| `status` | enum | `Open`, `Closed`, or `Blocked`. |
| `last_inbound_at` | datetime | Latest guardian message. |
| `last_outbound_at` | datetime | Latest staff/system message. |
| `session_expires_at` | datetime | End of the 24-hour window. |
| `session_open` | boolean | Authoritative composer state. |
| `unread_count` | integer | Number shown in the conversation list. |
| `last_message_preview` | string | Conversation-list preview. |
| `last_message_direction` | enum | `Inbound` or `Outbound`. |

`status: "Blocked"` means the guardian sent STOP or another supported opt-out keyword. Do not show send controls.

### Message

Important fields:

| Field | Type | Frontend use |
|---|---|---|
| `name` | string | Message id. |
| `direction` | enum | `Inbound` or `Outbound`. |
| `message_type` | enum | Text, media, location, contacts, interactive, or unsupported. |
| `body` | text | Main message content. |
| `caption` | text | Media caption. |
| `file` | Link | Downloaded private File record. |
| `status` | enum | `Received`, `Queued`, `Sent`, `Delivered`, `Read`, or `Failed`. |
| `message_at` | datetime | Message time. |
| `read_at` | datetime | Staff-read time for inbound or Meta-read time for outbound. |
| `action_request` | Link | Related action card. |
| `source_doctype` | Link | Linked business record type. |
| `source_name` | Dynamic Link | Linked business record id. |

Render `source_doctype` and `source_name` as a record link when both exist.

### Action Request

Important fields returned to the inbox:

| Field | Type | Frontend use |
|---|---|---|
| `name` | string | Action request id. |
| `rule` | Link | Configured action rule. |
| `status` | enum | Action lifecycle state. |
| `delivery_stage` | enum | Delivery/prompt stage. |
| `source_doctype` | Link | Record affected by the action. |
| `source_name` | Dynamic Link | Record id. |
| `response_key` | string | Matched option key. |
| `response_value` | text | Value passed to the executor. |
| `response_options` | array | Choices shown when an ambiguous reply needs review. |
| `result_doctype` | Link | Created result such as `Rating` or `ToDo`. |
| `result_name` | Dynamic Link | Created result id. |
| `error_message` | text | Failure detail for staff. |

Actions waiting for a session are returned even when no outbound message exists yet. Render unlinked action cards in the thread as well as message-linked action cards.

## Conversation APIs

### List Conversations

```txt
GET pet_app.api.notifications.list_whatsapp_conversations
```

Parameters:

| Parameter | Required | Notes |
|---|---:|---|
| `status` | No | Filter by `Open`, `Closed`, or `Blocked`. |
| `limit` | No | Default `50`. Desk inbox uses `100`. |

Frappe Desk example:

```js
const data = await petAppCall(
  "pet_app.api.notifications.list_whatsapp_conversations",
  { limit: 100 }
);

const conversations = data.conversations;
```

Example data:

```json
{
  "conversations": [
    {
      "name": "WA-CONV-2026-00001",
      "display_name": "Ali Faiz Ali",
      "normalized_phone": "9647716940612",
      "guardian": "GUARDIAN-00320",
      "customer": "CUST-00020",
      "status": "Open",
      "session_expires_at": "2026-07-21 10:00:00",
      "session_open": true,
      "unread_count": 2,
      "last_message_preview": "5",
      "last_message_direction": "Inbound"
    }
  ]
}
```

### Get Conversation Thread

```txt
GET pet_app.api.notifications.get_whatsapp_conversation
```

Parameters:

```json
{
  "conversation": "WA-CONV-2026-00001",
  "limit": 200
}
```

Response data:

```json
{
  "conversation": {
    "name": "WA-CONV-2026-00001",
    "display_name": "Ali Faiz Ali",
    "normalized_phone": "9647716940612",
    "status": "Open",
    "unread_count": 1,
    "session_open": true
  },
  "messages": [
    {
      "name": "WA-MSG-2026-00001",
      "direction": "Inbound",
      "message_type": "Text",
      "body": "5",
      "status": "Received",
      "message_at": "2026-07-20 10:00:00",
      "action_request": "WA-ACTION-2026-00001",
      "source_doctype": null,
      "source_name": null
    }
  ],
  "actions": [
    {
      "name": "WA-ACTION-2026-00001",
      "rule": "PetCareService Completed Rating",
      "status": "Completed",
      "delivery_stage": "Complete",
      "source_doctype": "PetCareService",
      "source_name": "PetCareService-00001",
      "response_key": "5",
      "response_value": "5",
      "result_doctype": "Rating",
      "result_name": "RATING-2026-00001",
      "response_options": []
    }
  ]
}
```

### Send Text or File Reply

```txt
POST pet_app.api.notifications.reply_whatsapp_conversation
```

Text payload:

```json
{
  "conversation": "WA-CONV-2026-00001",
  "message": "Thank you. We received your request."
}
```

File payload after uploading a Frappe `File`:

```json
{
  "conversation": "WA-CONV-2026-00001",
  "message": "Your document is attached.",
  "file": "FILE-DOC-NAME"
}
```

Supported outbound file families:

- Images: JPG, JPEG, PNG, WEBP.
- Video: MP4, 3GP.
- Audio: AAC, AMR, MP3, M4A, OGG, OPUS.
- Other uploaded files are sent as documents.

The API rejects replies when:

- The 24-hour window is closed.
- The conversation is blocked.
- Consent rules block the recipient.
- The user cannot read the selected File.
- Meta rejects the message.

### Mark Conversation Read

```txt
POST pet_app.api.notifications.mark_whatsapp_conversation_read
```

Payload:

```json
{
  "conversation": "WA-CONV-2026-00001"
}
```

This sets the conversation unread count to zero and marks unread inbound messages with `read_at`.

### Download Inbound Media

```txt
GET pet_app.api.notifications.download_whatsapp_media
```

Parameters:

```json
{
  "message": "WA-MSG-2026-00001"
}
```

Response data:

```json
{
  "file": {
    "name": "FILE-DOC-NAME",
    "file_name": "guardian-photo.jpg",
    "file_url": "/private/files/guardian-photo.jpg",
    "is_private": 1
  }
}
```

Open `file_url` using the authenticated session. Do not expose private URLs in a public/guardian page.

## Action Status UI

### Action Statuses

| Status | UI treatment | Staff controls |
|---|---|---|
| `Waiting Reply` | Neutral waiting state. | Reject may be offered in an admin detail view. |
| `Matched` | Reply was matched but not yet executed. | Approve/reject. |
| `Pending Review` | Risk or rule requires staff approval. | Approve/reject. |
| `Needs Review` | Reply was ambiguous. | Require response choice, then approve; or reject. |
| `Completed` | Success state with result link when available. | No mutation controls. |
| `Rejected` | Final rejected state. | No mutation controls. |
| `Expired` | Final expired state. | No mutation controls. |
| `Failed` | Error state showing `error_message`. | Link to queue/action details; no direct execution. |

### Delivery Stages

| Stage | Meaning |
|---|---|
| `Queued` | Request created; send job not completed. |
| `Awaiting Session` | Meta invitation sent, or app-styled message is waiting for an inbound reply. |
| `Interactive Sent` | The actionable prompt has been sent. This also covers typed-reply prompts. |
| `Awaiting Comment` | Rating was created and an optional comment is expected. |
| `Complete` | The action is finished. |

### List Pending Actions

```txt
GET pet_app.api.notifications.list_pending_whatsapp_actions
```

Parameters:

```json
{
  "status": "Pending Review",
  "limit": 50
}
```

Omit `status` to return all active waiting/review states.

### Approve Action

```txt
POST pet_app.api.notifications.approve_whatsapp_action
```

Normal approval:

```json
{
  "action_request": "WA-ACTION-2026-00001",
  "note": "Confirmed by reception"
}
```

Ambiguous reply approval:

```json
{
  "action_request": "WA-ACTION-2026-00001",
  "response_key": "confirm",
  "note": "Guardian clarified by phone"
}
```

For `Needs Review`, require `response_key` from the action's `response_options` before enabling Approve.

### Reject Action

```txt
POST pet_app.api.notifications.reject_whatsapp_action
```

Payload:

```json
{
  "action_request": "WA-ACTION-2026-00001",
  "note": "Duplicate guardian reply"
}
```

Only pending actions may be rejected.

## Sending an Action Manually

```txt
POST pet_app.api.notifications.send_actionable_whatsapp_message
```

Use a rule name:

```json
{
  "action_rule": "PetCareService Completed Rating",
  "source_doctype": "PetCareService",
  "source_name": "PetCareService-00001"
}
```

Or resolve the latest active rule by template:

```json
{
  "template_key": "pet_service_rating_request",
  "source_doctype": "PetCareService",
  "source_name": "PetCareService-00001"
}
```

Optional manual recipient override:

```json
{
  "action_rule": "PetCareService Completed Rating",
  "source_doctype": "PetCareService",
  "source_name": "PetCareService-00001",
  "recipient": "07716940612",
  "context": {
    "clinic": {
      "branch": "Main Branch"
    }
  }
}
```

Do not expose unrestricted manual-recipient sending to normal clinic users.

## Template Frontend

### Template Delivery Modes

| Mode | Meaning |
|---|---|
| `Meta Template` | Always sends the configured approved Meta template. |
| `App Styled` | Sends free-form styled content only while the 24-hour window is open. |
| `Hybrid` | Uses app-styled content in-window and the configured Meta fallback outside it. |

For most dynamic action/rating flows, use `Hybrid`.

### Supported Formatting

WhatsApp formatting entered in `body_preview` is sent as text:

```txt
*bold*
_italic_
~strikethrough~
`code`
```

Newlines, links, and emoji are preserved.

### List Template Variables

```txt
GET pet_app.api.notifications.list_whatsapp_template_variables
```

Example request:

```json
{
  "source_doctype": "PetCareService"
}
```

Example response data:

```json
{
  "variables": [
    {
      "key": "guardian.full_name",
      "token": "{{ guardian.full_name }}",
      "namespace": "guardian",
      "fieldname": "full_name"
    },
    {
      "key": "pet.pet_name",
      "token": "{{ pet.pet_name }}",
      "namespace": "pet",
      "fieldname": "pet_name"
    }
  ]
}
```

Supported namespaces:

- `guardian`
- `pet`
- `invoice`
- `death_record`
- `pet_service`
- `clinic`

Only allowlisted fields are returned. In particular, internal death-record notes are not exposed.

### Existing Template APIs

```txt
GET  pet_app.api.notifications.list_templates
GET  pet_app.api.notifications.get_template
POST pet_app.api.notifications.save_template
POST pet_app.api.notifications.preview_template
```

Preview example:

```json
{
  "template_key": "pet_service_rating_request",
  "context": {
    "guardian": { "full_name": "Ali" },
    "pet": { "pet_name": "Leo" },
    "pet_service": { "pet_service_name": "Grooming" },
    "clinic": { "business_name": "Alkokh Pet Store" }
  }
}
```

## Meta Template Mirror

Meta's own message templates are mirrored into a local doctype. The tab reads the mirror;
only sync and the three write methods talk to Graph.

> **Setup reminder:** the WABA must be subscribed to `message_template_status_update` under
> **WhatsApp → Configuration → Webhook fields** in the Meta App dashboard, or approval results
> never arrive. Sync is the fallback path. See the callout in [Important Meta Rules](#important-meta-rules).

### Why a separate doctype

`Pet App WhatsApp Meta Template` is a **mirror of Meta's copy**. It is not an extension of
`Pet App WhatsApp Template`, and the local template doctype was not modified to add it.

The two objects have different owners. Meta owns approval state, rejection reason and the
component structure. This app owns the event binding, the delivery mode and the body preview.
Merging them would mean a sync job writing into rows a human is editing, and a template Meta
has never seen would look identical to one it approved.

The mirror points at the local row through `local_template`. The local row does not point
back.

### `Pet App WhatsApp Meta Template`

Docname is `meta_template_id` — Meta's own ID — so a re-sync is a stable upsert.

| Field | Type | Notes |
|---|---|---|
| `meta_template_id` | Data | Meta's ID. The docname. |
| `template_name` | Data | Meta's name. Exposed to the frontend as `name`. |
| `language` | Data | Meta locale code (`en_US`, `ar`). |
| `category` | Data | Verbatim from Meta. |
| `status` | Data | Verbatim from Meta. |
| `rejected_reason` | Small Text | Verbatim from Meta. See the `NONE` note below. |
| `components_json` | Code (JSON) | Meta's `components`, never reshaped. |
| `raw_json` | Code (JSON) | The entire Meta template object, verbatim. |
| `provider_account` | Link | `Pet App WhatsApp Account` |
| `local_template` | Link | `Pet App WhatsApp Template`, nullable. The binding. |
| `binding_state` | Data | `bound` / `unbound` / `ambiguous` |
| `last_synced_at` | Datetime | |
| `missing_on_meta` | Check | Set when Meta stops returning a row we hold. |

#### `status`, `category` and `language` are `Data`, and must stay that way

They are **not** Select fields, and turning any of them into one is a regression.

Meta extends these vocabularies on its own schedule. A `Select` is a hardcoded list living
in a patch: a value outside it either throws on save or is silently dropped, and either way
the mirror stops reflecting reality until somebody ships a patch. Nothing in this app
validates these values, maps them, or changes their casing — whatever Meta sent is what is
stored.

This is verified behaviour, not an aspiration: a webhook carrying the status
`SOME_FUTURE_STATE_2031` lands in the mirror unchanged.

The same rule applies to `language`. It holds Meta's locale code as text, not a curated list.

#### `raw_json` is the anti-obsolescence field

Every upsert writes the complete Meta object verbatim, including keys this app does not
model. That is not theoretical — the live WABA already returns three:

```txt
disable_ios_autofill
is_primary_device_delivery_only
parameter_format
```

None of these has a column. All three survive every sync in `raw_json`. Adopting one later is
a read, not a schema migration and a backfill.

(`parameter_format` is the key that will eventually settle whether a template expects named or
positional body parameters. Nothing reads it yet.)

#### `rejected_reason` can read `NONE`

Meta sends `"reason": "NONE"` on approval webhooks, and the value is stored verbatim like
every other Meta value. The `GET` API omits the field entirely, so the same row reads blank
after a sync.

**`NONE` means "not rejected".** Treat `NONE` and empty as the same state when displaying;
do not print it as a rejection reason. This is a display concern and is deliberately not
normalised in the database.

### `Pet App WhatsApp Meta Category Map`

Local category → Meta category translation, held as **data rather than code**, so a new
category is a row somebody adds instead of a patch somebody writes.

| Field | Type | Notes |
|---|---|---|
| `local_category` | Data | The docname. |
| `meta_category` | Data | May be blank, meaning "no Meta equivalent". |
| `notes` | Small Text | |

Seeded rows:

| `local_category` | `meta_category` |
|---|---|
| `Authentication` | `AUTHENTICATION` |
| `Utility` | `UTILITY` |
| `Marketing` | `MARKETING` |
| `Service` | *(blank)* |

**`Service` seeds blank on purpose — Meta has no Service category.** Submitting a template in
that category throws until somebody fills the row:

```txt
Local category Service has no Meta category. Open Pet App WhatsApp Meta Category Map
Service and set meta_category to the Meta category it should be submitted as.
```

The fix is to open that row and set `meta_category` (usually `UTILITY`). The backend will not
guess and will not fall back.

**To add a category:** create a `Pet App WhatsApp Meta Category Map` row with
`local_category` set to the value your side uses and `meta_category` set to Meta's. No code
change, no deployment.

Resolution accepts either side of the mapping — a local category resolves through its row,
and a value that is already some row's `meta_category` (e.g. sending `UTILITY` directly)
passes through. Both answers come out of the table. An unmapped value throws and names the
row to create.

### The Six Methods

```txt
GET  pet_app.api.notifications.list_meta_templates
GET  pet_app.api.notifications.get_meta_template
POST pet_app.api.notifications.create_meta_template
POST pet_app.api.notifications.edit_meta_template
POST pet_app.api.notifications.delete_meta_template
POST pet_app.api.notifications.sync_meta_templates
```

All use the standard response envelope. `data` is exactly the shape below.

#### `MetaTemplate`

```json
{
  "meta_template_id": "1171002175046193",
  "name": "appointment_reminder",
  "language": "en_US",
  "category": "UTILITY",
  "status": "APPROVED",
  "rejected_reason": null,
  "components": [ { "type": "BODY", "text": "..." } ],
  "last_synced_at": "2026-08-24 01:54:41",
  "local_template_key": "meta_feedback"
}
```

`name` is the mirror's `template_name`. `local_template_key` is the `local_template` link, or
`null` when unbound.

#### `list_meta_templates`

Params: `status?`, `language?`, `limit`, `after?`

```json
{ "templates": [ /* MetaTemplate */ ], "paging": { "after": "WyJib2FyZGluZ19jaGVja2luIiwgIjIwNzkwNjY1NjYwNTU2NTAiXQ==" } }
```

**Reads the mirror, never Meta.** The tab keeps working while Graph is unreachable, and the
list is a local query rather than a round trip.

Paging is **cursor-based, not offset**. `paging.after` is an opaque keyset cursor over
`(template_name, meta_template_id)`; pass it back as `after` for the next page. It is absent
on the last page. A template inserted by a concurrent sync cannot shift a page boundary and
hide a row. Do not attempt to construct or parse a cursor.

`status` and `language` filter on exact stored values.

#### `get_meta_template`

Params: `meta_template_id`

Returns a single `MetaTemplate`. Throws if the ID is not in the mirror, naming sync as the
fix. Also reads the mirror only.

#### `create_meta_template`

Params: `name`, `language`, `category`, `components`

```json
{ "meta_template_id": "1234567890", "status": "PENDING" }
```

`name` is validated against Meta's naming rule before the round trip — **lowercase letters,
digits and underscores only.** No spaces, no capitals, no other characters. This is a format
rule; a rejected name never reaches Meta.

`category` is translated through `Pet App WhatsApp Meta Category Map`.

`components` is Meta's structure and is passed through unchanged. **`example.body_text` is an
array of arrays** — one inner array per example set. A flat array is rejected by Meta with an
unhelpful error:

```json
{
  "type": "BODY",
  "text": "Hello {{1}}, your appointment is {{2}}.",
  "example": { "body_text": [ ["Ali", "Tuesday 10:00"] ] }
}
```

The mirror row is created immediately from Meta's response, so the list reflects the new
template without waiting for a sync. Meta's create response is not a full template object, so
`raw_json` holds that response verbatim until the next sync replaces it with the real thing.

#### `edit_meta_template`

Params: `meta_template_id`, `components`

```json
{ "meta_template_id": "1234567890", "status": "APPROVED" }
```

Only components are editable on this endpoint.

**The returned `status` is the status the mirror already held.** Meta's edit response carries
no status and the backend will not invent one. An edited template is normally re-reviewed, so
expect the real status to change shortly afterwards — it arrives via the
`message_template_status_update` webhook, or on the next sync. Do not treat the returned
status as the post-edit state.

#### `delete_meta_template`

Params: `name`

```json
{ "ok": true }
```

Meta deletes **every language variant** sharing the name; there is no per-language delete on
this edge.

**The mirror rows are not deleted** — they are flagged `missing_on_meta = 1`. Deleting one
would destroy its `local_template` binding, which is this app's data and cannot be rebuilt
from Meta. Filter them out of the list UI or show them greyed.

#### `sync_meta_templates`

No params.

```json
{
  "ok": true,
  "data": { "synced": 15, "last_synced_at": "2026-08-24 01:54:41" },
  "meta": {
    "synced": 15, "created": 15, "updated": 0,
    "bound": 1, "unbound": 14, "ambiguous": 0,
    "missing_on_meta": 0, "pages": 1, "truncated": 0
  }
}
```

Pages the full list from Meta and upserts by `meta_template_id`. Rows Meta no longer returns
are flagged `missing_on_meta`, never deleted.

`truncated: 1` means the paging safety limit was reached and the sync did **not** cover
everything. It is never silent.

### Sending a template: the `parameters` contract

A template's parameter shape is read from **the mirror row and nothing else** — which
components take values, how many, and whether they are positional. It is never inferred from
the local template row, and never guessed from the braces in the message text.

Supply the values as an **ordered list under a reserved `parameters` key** in the context:

```json
{
  "event_key": "manual.boarding",
  "recipient_type": "Manual",
  "to_phone": "9647...",
  "conversation": "WA-CONV-2026-00017",
  "template_source": "meta",
  "meta_template": "1910912170205041",
  "context": { "parameters": ["Ali", "Leo", "K3", "2026-09-01"] }
}
```

The context stays an ordinary object; the list lives inside it under `parameters`. Nothing
else in the context is read as a parameter value.

**Order is the order the variables appear in the message.** For `boarding_checkinn`, whose
body reads `مرحباً {{1}}، تم تسجيل دخول {{2}} … الغرفة: {{3}} … موعد الخروج المتوقع: {{4}}`,
that is guardian, pet, room, check-out. Meta's `example.body_text[0]` on the mirror row names
each slot in order — use it to label the composer's inputs. When a template takes values in
more than one component, the single flat list is consumed in the declared component order
(header before body), because Meta numbers each component's placeholders from 1 separately.

Rules the backend enforces before any Graph call:

| rule | refusal |
|---|---|
| the count must match **exactly**, in both directions | `META_TEMPLATE_PARAM_COUNT` |
| four declared and three supplied | `META_TEMPLATE_PARAM_COUNT` |
| **zero declared and four supplied** — values an operator typed are never silently dropped | `META_TEMPLATE_PARAM_COUNT` |
| values must be text or numbers; objects, arrays, `null` and booleans are refused rather than serialized | `META_TEMPLATE_PARAM_INVALID` |
| `parameters` must be a list, not a string or object | `META_TEMPLATE_PARAM_INVALID` |
| the template numbers its variables with a gap (`{{1}}` `{{3}}`) | `META_TEMPLATE_PARAM_INVALID` |
| the template takes variables outside its body or header | `META_TEMPLATE_PARAM_INVALID` |
| the template uses named rather than positional parameters | `META_TEMPLATE_PARAM_INVALID` |

Zero declared and zero supplied is the normal case for a static template: the `components`
key is omitted entirely.

Both codes are **structural** — the send is refused locally, no Graph call is made, and the
queue row is **not** retried. Show `errors[0].message`; it names the template and the counts.

Numbers are coerced to text. Everything else is a caller error: a nested object would put a
raw JSON blob into a customer's message, so it is refused instead.

**A template that declares no `parameter_format` is treated as positional.** That is Meta's
historical default and every template on this WABA declares it explicitly; refusing on an
absent value would block static templates that would have sent fine.

**Unconfirmed, and deliberately not enforced:** Meta is widely reported to reject parameter
values containing newlines, tab characters, or more than four consecutive spaces, with
`Param text cannot have new-line/tab characters or more than 4 consecutive spaces`, and to
cap values at 1024 characters. The primary Meta documentation could not be reached to confirm
this, so **no such validation exists in the backend** — a value like that will be rejected by
Meta at send time rather than refused locally. Avoid multi-line values in the composer until
this is confirmed against a real error response.

### Binding to local templates

On every synced row, the mirror is matched to a local `Pet App WhatsApp Template` by
**exact `(template_name, language)`**.

| Matches | `local_template` | `binding_state` |
|---|---|---|
| exactly one | set | `bound` |
| none | empty | `unbound` |
| more than one | empty | `ambiguous` |

There is no fuzzy matching, no normalisation, no slugifying, and no case folding. The
comparison is re-checked byte-for-byte in Python because the database collation is
case-insensitive and would otherwise bind `Feedback` to `feedback`.

A high `unbound` count is normal, not a defect. A local template only binds if a human gave it
a `template_name` and `language` that exactly match an approved Meta template — for example a
local row with `language = "Arabic"` never binds, because Meta's locale code is `ar`.

### The `template_status` webhook event

Route is unchanged: `pet_app.api.whatsapp.webhook`.

The receiver now reads `change.field`. A `message_template_status_update` change becomes a
distinct event of type `template_status` instead of falling into the `raw` catch-all, where
it was previously stored and dropped.

It writes to the matched mirror row:

- `status` — from Meta's `event` key, **verbatim**
- `rejected_reason` — from Meta's `reason` key, verbatim (may be `NONE`)
- `last_synced_at`

Matching is by `meta_template_id` first, falling back to exact `template_name` + `language`
with the same byte-for-byte re-check. An ambiguous fallback matches nothing.

**No matching mirror row is not an error.** Nothing is created and nothing throws — a template
can be approved on Meta before this site has ever synced it, and a row invented from a status
payload would have no components and no `raw_json`. Instead the reason is written to
`processing_error` on the stored `Pet App WhatsApp Webhook Event` row:

```txt
No Pet App WhatsApp Meta Template row matched template_id=999999999999 name=never_synced
language=en_US; status APPROVED not applied. Run sync_meta_templates to pull the template in.
```

Filter the webhook event list on `processing_error` to find these. Running a sync resolves
them.

Webhook changes this app does not handle (for example `template_category_update`) still reach
the `raw` catch-all, but now leave a note in `processing_error` naming the field, rather than
vanishing.

Template-status events are **not deduplicated** — they carry no message ID to key on. Meta
re-delivering one writes the same values again, which is harmless, but produces a second row
in the webhook event log.

### What the send path deliberately does not know

None of the above is wired into sending. This was out of scope by decision, not oversight.

- **Nothing validates that a template is `APPROVED` before sending it.** `engine.py` resolves
  a local `Pet App WhatsApp Template` and sends `template_name` to Meta. It does not consult
  the mirror, does not check `status`, and does not check that the template exists on Meta at
  all. A local template whose name Meta has never seen fails at send time with a Graph error,
  exactly as it did before this work.
- **`delivery_mode` behaviour is unchanged.** The `Meta Template` / `App Styled` / `Hybrid`
  fork in `engine._send_queue_message` is untouched.
- **`Pet App WhatsApp Template` was not modified.** No `meta_*` fields were added to it. The
  mirror is the only place Meta state lives.
- **The local `category` field is still local-only.** It gates marketing sends and consent. It
  is not sent to Meta by the send path, and it is unrelated to the mirror's `category`, which
  is Meta's.
- **Binding is informational.** `local_template` records which local row corresponds to which
  Meta template. Nothing in the send path reads it.

## Action Rule Builder

Build the rule editor in this order:

```txt
WHEN -> IF -> SEND TO -> MESSAGE -> WAIT FOR REPLY -> THEN -> TEST
```

Every select, field, operator, recipient path, writable value, template, and executor must come from the designer schema. Do not query Frappe's full DocType list for this UI.

### Designer Schema

```txt
GET pet_app.api.notifications.get_whatsapp_designer_schema
GET pet_app.api.notifications.get_whatsapp_designer_schema?source_doctype=PetCareService
```

The first call returns shared registries and the explicit source-table list. The source-specific call also returns:

- Allowlisted fields and operators.
- Select/Check values and real Frappe field types.
- Eligible recipient Link paths.
- Writable fields and their allowed values.
- Compatible enabled templates.
- Executors permitted for that source.

Current registered sources are `PetCareService`, `Pet`, `Sales Invoice`, `Pet Death Record`, and `Guardian`. Treat `SOURCE_NOT_ALLOWED` as a configuration error rather than an empty result.

### Canonical Rule APIs

```txt
GET  pet_app.api.notifications.list_whatsapp_action_rules
POST pet_app.api.notifications.save_whatsapp_action_rule
POST pet_app.api.notifications.validate_whatsapp_action_rule
POST pet_app.api.notifications.simulate_whatsapp_action_rule
```

List filters:

```json
{
  "source_doctype": "PetCareService",
  "enabled": 1
}
```

List and save use parsed objects named `condition`, `response_config`, and `executor_config`. New frontend code must not encode these objects or use `condition_json`, `response_config_json`, or `executor_config_json`.

```json
{
  "rule_name": "Request rating after service completion",
  "enabled": 1,
  "source_doctype": "PetCareService",
  "trigger_event": "On Update",
  "condition": {
    "all": [
      {
        "field": "status",
        "operator": "changed_to",
        "value": ["Completed"]
      }
    ]
  },
  "recipient_type": "Guardian",
  "recipient_field": "guardian_id",
  "template_key": "pet_service_rating_request",
  "response_type": "List",
  "response_config": {
    "button_text": "Choose a rating",
    "section_title": "Rating",
    "options": [
      { "key": "5", "label": "5 / 5", "value": 5, "aliases": ["5"] }
    ]
  },
  "expiry_hours": 48,
  "duplicate_policy": "Once Per Source",
  "executor": "Create Rating",
  "executor_config": {
    "rating_scale": 5,
    "request_comment": 1,
    "comment_prompt": "Would you like to add a comment?"
  },
  "risk_level": "Low",
  "requires_approval": 0
}
```

Legacy malformed rows remain visible with `parse_errors`. Disable normal Save until the user explicitly replaces or resets every broken section.

### Builder Fields

| Field | Frontend control |
|---|---|
| `rule_name` | Required text input. |
| `enabled` | Toggle. |
| `source_doctype` | Source table select from `schema.source_tables`. |
| `trigger_event` | Select from `selected_source.trigger_events`. |
| `condition` | Structured groups built from `selected_source.fields`. |
| `recipient_type` | Select from recipient types attached to eligible fields. |
| `recipient_field` | Eligible Link field from the selected source. |
| `template_key` | Select from `selected_source.templates`. |
| `response_type` | Select from `schema.interaction_types`. |
| `response_config` | Ordered option and alias builder. |
| `expiry_hours` | Positive integer. |
| `duplicate_policy` | Select from `schema.duplicate_policies`. |
| `executor` | Select from `selected_source.executors`. |
| `executor_config` | Build controls from the executor's `config_fields`. |
| `risk_level` | Low, Medium, High, Sensitive. |
| `requires_approval` | Toggle that may be forced on by normalized validation. |

Never provide fields for Python expressions, method paths, or arbitrary document updates.

### Validation

Validate after meaningful edits and before Save:

```json
{
  "rule": { "...": "canonical rule" }
}
```

Render `validation.issues` beside their `when`, `if`, `send`, `message`, `reply`, `then`, or `test` section. Replace local state with `normalized_rule` when validation succeeds because backend policy can raise risk or force `requires_approval`.

Warnings do not block Save. Errors do.

### Test Simulation

```json
{
  "rule": { "...": "canonical rule" },
  "source_name": "PetCareService-00001"
}
```

Render the condition results, masked recipient, message preview, interaction options, executor preview, and warnings. Simulation is read-only and never sends or queues a message.

### Conditions

All conditions:

```json
{
  "all": [
    {
      "field": "status",
      "operator": "changed_to",
      "value": ["completed", "Completed"]
    }
  ]
}
```

Any condition:

```json
{
  "any": [
    { "field": "status", "operator": "equals", "value": "completed" },
    { "field": "status", "operator": "equals", "value": "Completed" }
  ]
}
```

Supported operators:

- `equals`
- `not_equals`
- `in`
- `not_in`
- `is_set`
- `is_not_set`
- `changed`
- `changed_to`

### Buttons

WhatsApp supports at most three reply buttons.

```json
{
  "options": [
    {
      "key": "confirm",
      "label": "Confirm",
      "value": "confirmed",
      "aliases": ["yes", "y", "1", "تمام", "نعم"]
    },
    {
      "key": "cancel",
      "label": "Cancel",
      "value": "cancelled",
      "aliases": ["no", "n", "2", "الغاء", "إلغاء"]
    }
  ]
}
```

### Lists

WhatsApp supports at most ten rows.

```json
{
  "button_text": "Choose a rating",
  "section_title": "Rating",
  "footer": "Your feedback helps us improve.",
  "options": [
    { "key": "1", "label": "1 / 5", "value": 1, "aliases": ["1"] },
    { "key": "2", "label": "2 / 5", "value": 2, "aliases": ["2"] },
    { "key": "3", "label": "3 / 5", "value": 3, "aliases": ["3"] },
    { "key": "4", "label": "4 / 5", "value": 4, "aliases": ["4"] },
    { "key": "5", "label": "5 / 5", "value": 5, "aliases": ["5"] }
  ]
}
```

Typed matching is Unicode-normalized and case-insensitive. Interactive button/list ids take priority over typed aliases.

### Safe Executors

#### Create Rating

```json
{
  "rating_scale": 5,
  "questionnaire": null,
  "request_comment": 1,
  "comment_prompt": "Thank you. Would you like to add a comment?"
}
```

Creates one linked `Rating`. A comment reply is stored in `Rating.notes`.

#### Update Allowed Field

```json
{
  "field": "status",
  "values": {
    "confirm": "Completed",
    "cancel": "Cancelled"
  }
}
```

The backend rejects fields and values outside its allowlist. Frontend validation is helpful but is not the security boundary.

#### Create Staff Task

```json
{
  "allocated_to": "reception@example.com",
  "description": "Guardian requested an appointment reschedule."
}
```

Creates a linked `ToDo` instead of changing a sensitive record automatically.

#### Record Acknowledgement

```json
{}
```

Stores acknowledgement in the action request and audit events.

#### Record Intent

```json
{}
```

Records invoice/payment/help intent. It does not mark an invoice paid.

#### Require Staff Review

```json
{}
```

Always holds the matched reply for staff approval.

Any non-Low risk level or `requires_approval: 1` also requires staff approval.

## Default PetCareService Rating Flow

Installed rule:

```txt
PetCareService Completed Rating
```

Behavior:

1. A PetCareService changes to `completed` or `Completed`.
2. The rule resolves `guardian_id` and the guardian phone.
3. One action request is created per source service.
4. Inside the 24-hour window, the app sends the styled 1-5 list.
5. Outside the window, the configured `meta_feedback` invitation is sent first.
6. Interactive list replies or typed values `1` through `5` are matched.
7. One `Rating` is created with:
   - `reference_doctype: "PetCareService"`
   - `reference_name: <service name>`
   - `overall_rating: 1..5`
8. When comments are enabled, the next text reply is saved to `Rating.notes`.

The rule, template, options, expiry, aliases, and comment setting are editable without code changes.

## Notifications

The backend creates Frappe `Notification Log` records for:

- New inbound WhatsApp messages.
- Actions that need review.
- Completed actions.
- Failed actions.

The recipients come from `Pet App Notification Settings.whatsapp_inbox_notification_users`.
When blank, notifications go to `Administrator`.

Frontend behavior:

- Use the standard Frappe notification feed for Desk.
- Route notification links to the related `Pet App WhatsApp Conversation`.
- Refresh the active thread after approval/rejection.
- The provided Desk inbox polls every 30 seconds.

## Recommended Inbox UI

### Conversation List

Show:

- Guardian/display name.
- Normalized phone.
- Last message preview.
- Unread badge.
- Open/closed service-window state.

Sort using backend order. Do not resort solely by `last_message_at`, because the API currently orders by document modification time.

### Thread

Show:

- Inbound and outbound alignment.
- Message time and delivery status.
- Failed messages visibly marked.
- Attachments.
- Linked source record.
- Action state and result record.
- Review choice for `Needs Review`.
- Approve/reject controls only in supported states.

### Composer

- Enable only when `session_open` is true and status is not Blocked.
- Support text, Enter-to-send, Shift+Enter newline, and one uploaded attachment.
- Refresh the conversation after sending.
- Show the backend error text from `errors[0].message`.

## Error Handling

Example Pet App error envelope:

```json
{
  "ok": false,
  "data": {},
  "meta": {
    "code": "HTTPError"
  },
  "errors": [
    {
      "message": "401 Client Error: Unauthorized for url: ...",
      "details": {}
    }
  ]
}
```

Use `meta.code` for programmatic handling and `errors[0].message` for staff-facing feedback.

Common cases:

| Condition | Frontend behavior |
|---|---|
| `PERMISSION_ERROR` | Show permission error; do not render an empty inbox. |
| Closed 24-hour window | Disable composer and show closed state. |
| Blocked/STOP | Disable all send controls. |
| Expired action | Refresh action card and remove approval controls. |
| Ambiguous reply | Show response chooser and review controls. |
| Meta 401 | Show credential failure to administrators. Do not auto-loop retries from the UI. |
| Failed media download | Keep message visible and show attachment unavailable/error state. |

## Deployment State

As of 2026-08-23 — Meta template mirror:

- `Pet App WhatsApp Meta Template` and `Pet App WhatsApp Meta Category Map` are created by
  `pet_app.patches.p1_12_meta_template_mirror`. The four category-map rows are seeded, with
  `Service` deliberately blank.
- The six `*_meta_template*` methods are live. `list` and `get` read the mirror; `sync` and
  the three write methods call Graph.
- The account's `whatsapp_business_account_id` and `graph_api_version` are both set and are
  the only source of those values. No API version literal exists in the code.
- The access token was verified to carry `whatsapp_business_management`; template management
  needs it in addition to `whatsapp_business_messaging`.
- A verification sync read 15 templates from the WABA: 1 bound, 14 unbound, 0 ambiguous. The
  unbound rows are expected — the legacy local templates are being retired and recreated
  through the new tab.
- **Confirm the `message_template_status_update` webhook field is subscribed in the Meta App
  dashboard.** Without it, approvals never reach the mirror. See the callout at the top.
- The send path is unchanged. Nothing validates that a template is `APPROVED` before sending.

As of 2026-07-20:

- Backend implementation, Desk inbox, action preview, linked records, tests, assets, scheduler, and workers are deployed.
- The local Meta template mapping is `meta_feedback` -> Meta template `feedback` (`en_US`).
- A production verification request reached Meta but returned HTTP 401, so no guardian message was delivered.
- The failed verification queue was cancelled and appears in the inbox as a failed outbound message.
- The Meta access token must be replaced with a valid permanent System User token.
- The Meta App Secret is empty. Webhook signature checking activates automatically after it is configured.

Do not treat a queued message as delivered. Use the returned message/queue status and subsequent Meta webhook statuses.

## Frontend Acceptance Checklist

- Conversation list loads and displays unread/session state.
- Thread displays inbound, outbound, failed, and media messages.
- Closed or blocked conversations cannot send free-form replies.
- Mark-read clears unread count.
- Source and result records open from action cards.
- Waiting actions display even before an outbound message exists.
- `Needs Review` requires a selected response before approval.
- Pending Review supports approve and reject.
- Completed, Rejected, Expired, and Failed actions have no execution controls.
- Template variable picker only uses backend-provided variables.
- Action preview renders buttons, list rows, typed choices, and WhatsApp formatting.
- Rule editor builds structured JSON and never accepts executable code.
- The default completed-service flow creates one request and one linked Rating.
- API errors display `errors[0].message`.
- A valid permanent Meta token and App Secret are configured before production sign-off.
- The Meta templates tab lists from the mirror and still renders when Graph is unreachable.
- Paging uses the returned `paging.after` cursor only; no offset paging, no cursor built client-side.
- `rejected_reason` of `NONE` is displayed as "not rejected", never as a rejection reason.
- `missing_on_meta` rows are filtered out or visibly marked, not shown as live templates.
- Template creation rejects names that are not lowercase/digits/underscore before submitting.
- `example.body_text` is sent as an array of arrays.
- The status returned by `edit_meta_template` is not shown as the post-edit state.
- `message_template_status_update` is subscribed in the Meta App dashboard.
