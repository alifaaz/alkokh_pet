# Customer Account Reset

Desk page: `/desk/customer-account-reset` (search **Customer Account Reset**).

Select an existing Guardian or Customer, enter the reset password, preview the records,
then type the exact Customer ID to execute. If both selectors are filled they must match
`Guardian.customer_id`. Selecting a guardian resets the whole linked Customer account,
including any other guardians linked to that same customer.

Only the `Administrator` user or a user with the native `Administrator` role can load
the page or call either endpoint. `System Manager` and `Accounts Manager` alone are not
authorization. The password is checked on both calls, including for Administrator.
It is stored as a salted PBKDF2 hash in the site's
`customer_account_reset_password_hash` configuration, never in page JavaScript or this repo.
An unconfigured site refuses all reset requests. The page does not change this password. An incorrect reset password returns a
validation error (HTTP 417), not a login error (HTTP 401), so the existing site
session and cookies remain intact.

## Scope

- Cancels submitted Sales Invoices, Payment Entries and customer Journal Entries using
  ERPNext controllers, then deletes the drafts/cancelled vouchers and their child tables.
- Deletes **complete voucher groups** from GL Entry, Payment Ledger Entry,
  Advance Payment Ledger Entry and Stock Ledger Entry, including both accounting sides.
  Submitted stock invoices are cancelled normally first; any remaining active stock
  ledger entry aborts the reset. Historical independent Material Issues and medication
  dispense records are retained.
- Keeps Customer, Guardian, Pet and clinical history. Resets invoice/payment links,
  billing flags, rates/prices, paid/balance totals and boarding discounts on the selected
  medical records. Marks existing Pet Billable Item charges Cancelled with zero price
  and amount, retaining dose/dispense/stock information. Clinical statuses are unchanged.
- Finds clinical records by explicit customer/guardian, visit/boarding source, invoice
  backlinks and invoice-line provenance. Never treats shared pet ownership as proof
  that another guardian's charges belong to the selected account.
- Preserves linked Pet Notification Log rows, including message, recipient and sent
  status. Backs them up and records the original voucher in an Info comment before
  clearing their obsolete dynamic links. Preview includes `notification_log_count`.
- Cancels invoice reminders and clears their obsolete references. Reattaches voucher
  files to the Customer so document deletion cannot destroy their physical files.
- Retained medical records can be billed again through subsequent authorized clinical
  workflows. This reset clears current accounting; it does not disable the customer,
  close active treatment, or permanently exempt future services from billing.

Shared-party journals/payments/ledgers, foreign voucher references, missing/unsupported
ledger sources, POS consolidation, driver orders, Sales Orders and Delivery Notes are
refused. Unhandled native links also abort the entire operation; the service never
force-deletes through a link check. These workflows need individual resolution first.

## API

Both methods are authenticated **POST only** under
`pet_app.api.accounting.account_reset`:

- `preview(password, guardian=None, customer=None)` returns customer identity, all linked
  guardian IDs, voucher/medical/ledger counts, reminder/attachment counts, content
  fingerprint (SHA-256 of the actual snapshot), balance and blockers.
- `execute(password, fingerprint, confirm_customer, guardian=None, customer=None)`
  re-authenticates, locks the customer and selected records, rebuilds the snapshot,
  rejects stale previews, writes a private backup, applies the reset and checks that no
  target accounting records remain. Returns a reset ID, counts and zero net balance.

Success uses the standard `{ok, data, meta, errors}` envelope. Errors raise Frappe
exceptions; they are not swallowed into success-status responses. Preview is limited
to 10 calls / 5 minutes / IP and execution to 5 / 5 minutes / IP.

All database mutations use one transaction. Exceptions roll back to the reset
savepoint; unexpected commits inside controller hooks abort. The HTTP framework commits
only after success. In console scripts, the caller must commit explicitly on success.
Locks and current reads protect snapshot comparison; stale or failed requests require
a new preview.

## Recovery and deployment

Each attempt that reaches execution writes an fsynced JSON snapshot under
`sites/<site>/private/account-reset-backups/<reset-id>.json`, directory mode 0700,
file mode 0600. It contains original vouchers/children, affected clinical records,
ledger rows, notification logs, reminder/file metadata, actor and timestamp. The Customer receives an
Info comment with the successful reset ID and counts in the same database transaction.
A backup file by itself is not proof of success: failed/rolled-back attempts retain
their pre-change snapshot. Snapshots are for administrator-assisted recovery, not an
automatic substitute for restoring a complete site backup.

Install just this new Page (no unrelated schema migration is required):

```bash
bench --site <site> reload-doc pet_app page customer_account_reset
bench --site <site> clear-cache
```

Provision the password hash privately in site configuration using
`passlib.hash.pbkdf2_sha256.hash(...)`. Do not put a plaintext password in fixtures,
source control, frontend assets, API URLs or documentation. Existing live processes
must import the new module; verify deployment through actual HTTP workers.

## Verification

```bash
env/bin/python -m unittest pet_app.tests.test_account_reset -v
node --check apps/pet_app/pet_app/pet_app/page/customer_account_reset/customer_account_reset.js
```

The initial installation was additionally verified with a complete live-data reset
inside a transaction followed by rollback and exact snapshot comparison, then HTTP
checks for admin access, incorrect password, non-admin access with the correct password,
guest page access and GET rejection. The requested real reset was executed through the
HTTP endpoint and checked for zero balances and unchanged unrelated general ledger.
