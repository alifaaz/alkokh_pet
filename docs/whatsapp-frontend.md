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
