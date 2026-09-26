# Cashier activity backend handoff

The canonical API contract, verified cash-account configuration, stock-relief answer,
and earlier-handoff status are in [backend/cashier-activity.md](backend/cashier-activity.md).

Read that contract before consuming expense totals: ambiguous mixed vouchers are reported
separately through `unresolved_cash_vouchers`, and `cash_total_complete` marks incomplete
cash totals. Cash and stock totals remain separate.
