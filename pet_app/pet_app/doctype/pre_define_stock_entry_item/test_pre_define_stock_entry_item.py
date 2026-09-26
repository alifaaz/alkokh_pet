# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

# No tests of its own: rows are exercised through test_pre_define_stock_entry. This
# module exists so the test-record walk that starts from the child table skips the
# ERPNext masters (their test modules bootstrap data at import time, which fails on a
# populated site).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Item", "Item Group", "UOM", "Warehouse", "Company", "Stock Entry", "Stock Entry Type"]
