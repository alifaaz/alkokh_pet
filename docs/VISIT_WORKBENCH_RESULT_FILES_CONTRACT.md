# Visit Workbench Result Files Contract

This document is the current backend contract for embedding Lab and Imaging result attachments in visit workbench linked records.

Generated from the current codebase on 2026-07-12. It is descriptive, not aspirational.

## Response Shape

Methods:

- `pet_app.api.visit_workbench.get_visit_workbench`
- `pet_app.api.workspace.get_record` when returning a `Vet Visit` aggregate

Both methods keep the standard response envelope:

```json
{
  "ok": true,
  "data": {},
  "meta": {},
  "errors": []
}
```

For every Lab or Imaging entry in `data.linked_records[]`, backend emits `result_files`.

```json
{
  "source_type": "Lab",
  "source_doctype": "Lab",
  "name": "LAB-2026-00001",
  "status": "Released",
  "order_id": "VVT-2026-00001-ORD-LAB1",
  "title": "CBC",
  "modified": "2026-07-12 09:15:00",
  "result_files": [
    {
      "name": "FILE-00001",
      "file_url": "/private/files/cbc.pdf",
      "file_name": "cbc.pdf",
      "is_image": false
    }
  ]
}
```

## Always Emit The Key

`result_files` is always present for Lab and Imaging linked records.

- If files exist, `result_files` is an array of file payloads.
- If no result file exists yet, `result_files` is `[]`.
- Backend never omits the key and never returns `null`.

The frontend uses presence of `result_files` as the signal that the backend shipped attachments and that the old per-order fallback fetch should stay disabled.

## Source Fields

The committed DocType JSON and live metadata currently define:

| Doctype | Field | Type | Contract |
| --- | --- | --- | --- |
| `Lab` | `result` | Small Text | Narrative lab result text only. It is not a file field. |
| `Imaging` | `report` | Small Text | Narrative imaging report text only. It is not a file field. |
| `Imaging` | `image` | Attach Image | The only direct diagnostic Attach field in the current schema. Included in `result_files` and deduped against matching File rows by `file_url`. |

There is no committed or live `Lab.result_file` field. There is no committed or live `Imaging.report_file` field. Those names are legacy frontend assumptions and are not part of the current backend contract.

Current source-of-truth rules:

- Lab result files come from the `File` table only.
- Imaging report files come from the `File` table only.
- Imaging images come from the `File` table and, when populated, the direct `Imaging.image` Attach Image field.
- `result_files` dedupes by `file_url`, so an `Imaging.image` value and its matching `File` row produce exactly one row.

For both Lab and Imaging, backend bulk-loads matching `File` rows:

```python
frappe.get_all(
    "File",
    filters={"attached_to_doctype": doctype, "attached_to_name": ["in", names]},
    fields=["name", "file_url", "file_name", "attached_to_name"],
    ignore_permissions=True,
)
```

The File query uses `ignore_permissions=True` because visit access has already been authorized before the aggregate is built. Without this, Doctor/Coordinator users can see the visit but silently lose result files.

## Dedupe And Image Detection

Backend dedupes by `file_url`. This prevents duplicate frontend buttons when a direct Attach field and its `File` row point to the same URL.

`is_image` is computed from the lowercased URL extension:

```text
.png .jpg .jpeg .gif .webp .bmp .svg
```

`File.is_image` is not read because Frappe's File DocType does not provide that column.

## Sample Response

```json
{
  "ok": true,
  "data": {
    "visit": {
      "name": "VVT-2026-00001"
    },
    "linked_records": [
      {
        "source_type": "Lab",
        "source_doctype": "Lab",
        "name": "LAB-2026-00001",
        "status": "Released",
        "order_id": "VVT-2026-00001-ORD-LAB-CBC",
        "title": "CBC",
        "modified": "2026-07-12 09:15:00",
        "result_files": [
          {
            "name": "FILE-00001",
            "file_url": "/private/files/cbc.pdf",
            "file_name": "cbc.pdf",
            "is_image": false
          }
        ]
      },
      {
        "source_type": "Radiology",
        "source_doctype": "Imaging",
        "name": "IMG-2026-00001",
        "status": "Released",
        "order_id": "VVT-2026-00001-ORD-XRAY",
        "title": "Chest X-Ray",
        "modified": "2026-07-12 09:20:00",
        "result_files": [
          {
            "name": "FILE-00002",
            "file_url": "/private/files/chest-xray.png",
            "file_name": "chest-xray.png",
            "is_image": true
          },
          {
            "name": "FILE-00003",
            "file_url": "/private/files/chest-xray-report.pdf",
            "file_name": "chest-xray-report.pdf",
            "is_image": false
          }
        ]
      },
      {
        "source_type": "Lab",
        "source_doctype": "Lab",
        "name": "LAB-2026-00002",
        "status": "Ordered",
        "order_id": "VVT-2026-00001-ORD-LAB-CHEM",
        "title": "Chemistry Panel",
        "modified": "2026-07-12 09:25:00",
        "result_files": []
      }
    ]
  },
  "meta": {},
  "errors": []
}
```
