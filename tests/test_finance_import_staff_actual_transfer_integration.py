"""B5 end-to-end coverage for actual staff-transfer imports."""

from __future__ import annotations

from decimal import Decimal
import json

import pandas as pd

from scripts.imports import import_finance_excel as importer
from scripts.imports.finance_formats.sinopac import SINOPAC_HEADERS
from scripts.imports.finance_formats.taishin import TAISHIN_HEADERS
from tests._finance_alert_mock_support import AutocommitOff, handle_finance_alert_sql


STAFF = 7
ACCOUNT_A = "1234567890123456"
ACCOUNT_B = "6543210987654321"


class Cursor:
    """Stateful MySQL-shaped boundary; normalization and dispatch remain real."""

    rowcount = 1

    def __init__(self, state):
        self.state, self.current, self.lastrowid = state, None, None
        self.connection = AutocommitOff()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        q = " ".join(sql.split())
        self.rowcount = 1
        if q.startswith("SELECT cp.id AS client_payment_id"):
            self.current = []
        elif q.startswith("SELECT sba.staff_id"):
            self.current = self.state["accounts"]
        elif q.startswith("INSERT INTO finance_import_batches"):
            self.lastrowid = len(self.state["batches"]) + 1
            self.state["batches"].append({"id": self.lastrowid, "status": "staged"})
        elif q.startswith("SELECT id, classification_type, reconciliation_status FROM finance_import_rows"):
            row = self.state["by_fp"].get(params[0])
            self.current = None if row is None else {key: row.get(key) for key in ("id", "classification_type", "reconciliation_status")}
        elif q.startswith("INSERT INTO finance_import_rows"):
            keys = ("dedup_fingerprint", "batch_id", "format_id", "source_file", "source_bank_account", "sheet_name", "source_row", "source_reference", "transaction_date", "transaction_time", "posting_date", "value_date", "debit", "credit", "direction", "balance", "currency", "summary", "memo", "counterparty_name", "counterparty_account", "cancellation_code", "bank_references", "warnings", "raw_payload")
            row = dict(zip(keys, params, strict=True))
            row.update(id=len(self.state["rows"]) + 1, reconciliation_status="pending")
            self.state["rows"].append(row); self.state["by_fp"][row["dedup_fingerprint"]] = row; self.lastrowid = row["id"]
        elif q.startswith("INSERT INTO finance_import_occurrences"):
            self.state["occurrences"].append(params)
        elif q.startswith("UPDATE finance_import_rows SET classification_type"):
            self._row(params[-1]).update(classification_type=params[0], matched_identity_ids=params[1], resolved_counterparty_account=params[2], classification_reason=params[3])
        elif q.startswith("SELECT id, classification_type, matched_identity_ids"):
            row = self._row(params[0]); self.current = {key: row.get(key) for key in ("id", "classification_type", "matched_identity_ids", "resolved_counterparty_account", "debit")}
        elif q.startswith("SELECT id, staff_id FROM staff_monthly_settlements"):
            self.current = [{"id": s["id"], "staff_id": s["staff_id"]} for s in self.state["settlements"] if s["staff_id"] == params[0] and s["status"] in {"finalized", "partially_paid"}]
        elif q.startswith("SELECT id AS settlement_detail_id"):
            self.current = [{"settlement_detail_id": d["id"], **{k: d[k] for k in ("service_salary", "legacy_subsidy_payable", "floor_fee_amount", "adjustment_amount", "legacy_subsidy_status", "review_required")}} for d in self.state["details"] if d["settlement_id"] == params[0] and d["staff_id"] == params[1]]
        elif q.startswith("SELECT sta.settlement_detail_id"):
            settlement_id = params[0]
            detail_ids = {d["id"] for d in self.state["details"] if d["settlement_id"] == settlement_id}
            self.current = [{"settlement_detail_id": a["settlement_detail_id"], "component_type": a["component_type"], "allocated_amount": a["allocated_amount"], "transaction_type": next(t for t in self.state["transfers"] if t["id"] == a["transfer_id"])["transaction_type"]} for a in self.state["allocations"] if a["settlement_detail_id"] in detail_ids]
        elif q.startswith("SELECT fir.id AS finance_import_row_id"):
            settlement = next(s for s in self.state["settlements"] if s["id"] == params[0]); self.current = {**self._row(params[1]), "finance_import_row_id": params[1], "settlement_id": settlement["id"], "staff_id": settlement["staff_id"], "settlement_month": settlement["settlement_month"], "total_payable": settlement["total_payable"], "total_paid": settlement["total_paid"], "settlement_status": settlement["status"]}
        elif q.startswith("SELECT id, staff_id, account_no FROM staff_bank_accounts"):
            self.current = [a for a in self.state["accounts"] if a["account_no"] == params[0]]
        elif q.startswith("SELECT id, settlement_id, staff_id, payment_phase"):
            self.current = [t for t in self.state["transfers"] if t["external_reference"] == params[0] or t["raw_import_reference"] == params[1]]
        elif q.startswith("SELECT id, settlement_id, staff_id, service_salary"):
            self.current = [d.copy() for d in self.state["details"] if d["settlement_id"] == params[0]]
        elif q.startswith("INSERT INTO staff_actual_transfers"):
            self.lastrowid = len(self.state["transfers"]) + 1
            fields = ("settlement_id", "staff_id", "payment_phase", "amount", "occurred_at", "source_bank", "source_account", "counterparty_account", "external_reference", "raw_import_reference")
            item = dict(zip(fields, params, strict=True)); item.update(id=self.lastrowid, transaction_type="transfer", transaction_status="succeeded", reversal_of_transfer_id=None, review_status="confirmed")
            self.state["transfers"].append(item)
        elif q.startswith("INSERT INTO staff_transfer_allocations"):
            self.state["allocations"].append({"transfer_id": params[0], "settlement_detail_id": params[1], "allocated_amount": params[2], "component_type": params[3], "allocation_method": "explicit", "review_status": "approved", "reversal_of_allocation_id": None})
        elif q.startswith("UPDATE staff_monthly_settlements SET total_paid"):
            settlement = next(s for s in self.state["settlements"] if s["id"] == params[2]); settlement.update(total_paid=params[0], status=params[1])
        elif q.startswith("UPDATE finance_import_rows SET reconciliation_status='reconciled'"):
            self._row(params[1]).update(reconciliation_status="reconciled", reconciliation_reference=params[0])
        elif q.startswith("UPDATE finance_import_batches SET status='completed'"):
            self.state["batches"][params[0] - 1]["status"] = "completed"
        elif handle_finance_alert_sql(self.state, q, params, self):
            pass
        else:
            raise AssertionError(f"unexpected SQL: {q}")

    def fetchone(self): return self.current
    def fetchall(self): return list(self.current or [])
    def _row(self, row_id): return next(row for row in self.state["rows"] if row["id"] == row_id)


class Connection:
    def __init__(self, state): self.state, self.commits, self.rollbacks = state, 0, 0
    def cursor(self): return Cursor(self.state)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): pass


def _state(*, accounts=None, settlements=None, details=None):
    accounts = accounts or [{"id": 1, "staff_id": STAFF, "account_no": ACCOUNT_A}, {"id": 2, "staff_id": STAFF, "account_no": ACCOUNT_B}]
    settlements = settlements or [{"id": 20, "staff_id": STAFF, "settlement_month": "2026-06", "total_payable": Decimal("450"), "total_paid": Decimal("0"), "status": "finalized"}]
    details = details or [{"id": 11, "settlement_id": 20, "staff_id": STAFF, "service_salary": Decimal("400"), "legacy_subsidy_payable": Decimal("0"), "floor_fee_amount": Decimal("50"), "adjustment_amount": Decimal("0"), "payable_amount": Decimal("450"), "legacy_subsidy_status": "confirmed", "review_required": False}]
    return {"accounts": accounts, "settlements": settlements, "details": details, "rows": [], "by_fp": {}, "occurrences": [], "batches": [], "transfers": [], "allocations": []}


def _sinopac(tmp_path, accounts, amounts, name="salary.xlsx"):
    rows = [list(SINOPAC_HEADERS)]
    for number, (account, amount) in enumerate(zip(accounts, amounts, strict=True), 1):
        rows.append(["BANK", "2026/07/15 09:00:00", "2026/07/15", "2026/07/15", "轉帳", "TWD", str(amount), "", "9000", "", "", f"REF-{number}", f"薪資 {account}", "", ""])
    path = tmp_path / name; pd.DataFrame(rows).to_excel(path, sheet_name="銀行流水明細", index=False, header=False); return path


def _taishin(tmp_path, account, amount, name="legacy.xlsx"):
    rows = [["statement"], list(TAISHIN_HEADERS), ["1", "2026/07/15", "09:00:00", "2026/07/15", "轉帳", str(amount), "", "9000", f"舊制補助 {account}"]]
    path = tmp_path / name; pd.DataFrame(rows).to_excel(path, sheet_name="Taishin", index=False, header=False); return path


def _import(monkeypatch, path, state):
    conn = Connection(state); monkeypatch.setattr(importer, "get_connection", lambda: conn); return importer.import_finance_workbook(str(path)), conn


def _immutable_snapshot(state):
    return {
        "settlements": [{key: settlement[key] for key in ("settlement_month", "total_payable")} for settlement in state["settlements"]],
        "details": [{key: detail[key] for key in ("id", "service_salary", "legacy_subsidy_payable", "floor_fee_amount", "adjustment_amount", "payable_amount")} for detail in state["details"]],
    }


def _pending_snapshot(state):
    return {
        "settlements": [settlement.copy() for settlement in state["settlements"]],
        "details": [detail.copy() for detail in state["details"]],
    }


def _assert_audit(state, *, batches, rows, occurrences):
    assert len(state["batches"]) == batches and len(state["rows"]) == rows
    assert len(state["occurrences"]) == occurrences
    for row in state["rows"]:
        assert row["dedup_fingerprint"] and row["raw_payload"] and row["transaction_date"] == "2026-07-15"
        assert row["memo"] or json.loads(row["bank_references"]).get("存摺備註")


def test_sinopac_salary_two_registered_accounts_reconcile_once_and_rerun(monkeypatch, tmp_path):
    state = _state()
    immutable = _immutable_snapshot(state)
    path = _sinopac(tmp_path, [ACCOUNT_A], ["450"])
    result, _ = _import(monkeypatch, path, state)
    assert result["reconciled_counts"] == {"staff_salary": 1}
    assert len(state["transfers"]) == 1 and len(state["allocations"]) == 2
    assert {a["component_type"] for a in state["allocations"]} == {"regular_salary", "floor_fee"}
    assert state["settlements"][0]["total_paid"] == Decimal("450") and state["settlements"][0]["status"] == "paid"
    assert state["rows"][0]["resolved_counterparty_account"] == ACCOUNT_A
    assert state["rows"][0]["transaction_date"] == "2026-07-15"
    assert _immutable_snapshot(state) == immutable
    rerun, _ = _import(monkeypatch, path, state)
    assert rerun["skipped_existing"] == 1 and len(state["transfers"]) == 1 and len(state["allocations"]) == 2
    _assert_audit(state, batches=2, rows=1, occurrences=2)

    second = _state(settlements=[{"id": 21, "staff_id": STAFF, "settlement_month": "2026-07", "total_payable": Decimal("450"), "total_paid": Decimal("0"), "status": "finalized"}], details=[{**state["details"][0], "settlement_id": 21}])
    second_immutable = _immutable_snapshot(second)
    result, _ = _import(monkeypatch, _sinopac(tmp_path, [ACCOUNT_B], ["450"], "other-account.xlsx"), second)
    assert result["reconciled_counts"] == {"staff_salary": 1}
    assert second["rows"][0]["resolved_counterparty_account"] == ACCOUNT_B and second["transfers"][0]["staff_id"] == STAFF
    assert second["rows"][0]["transaction_date"] == "2026-07-15" and _immutable_snapshot(second) == second_immutable
    rerun, _ = _import(monkeypatch, _sinopac(tmp_path, [ACCOUNT_B], ["450"], "other-account.xlsx"), second)
    assert rerun["skipped_existing"] == 1 and len(second["transfers"]) == 1 and len(second["allocations"]) == 2
    _assert_audit(second, batches=2, rows=1, occurrences=2)


def test_salary_ambiguity_and_month_candidates_stay_pending_and_rerun(monkeypatch, tmp_path):
    shared = _state(accounts=[{"id": 1, "staff_id": STAFF, "account_no": ACCOUNT_A}, {"id": 2, "staff_id": 8, "account_no": ACCOUNT_A}])
    simultaneous = _state()
    months = _state(settlements=[{"id": 20, "staff_id": STAFF, "settlement_month": "2026-05", "total_payable": Decimal("450"), "total_paid": Decimal("0"), "status": "finalized"}, {"id": 21, "staff_id": STAFF, "settlement_month": "2026-06", "total_payable": Decimal("450"), "total_paid": Decimal("0"), "status": "finalized"}], details=[{**_state()["details"][0], "settlement_id": 20}, {**_state()["details"][0], "id": 12, "settlement_id": 21}])
    unmatched = _state()
    cases = [(shared, _sinopac(tmp_path, [ACCOUNT_A], ["450"], "shared.xlsx")), (simultaneous, _sinopac(tmp_path, [ACCOUNT_A], ["450"], "zero-settlement.xlsx")), (months, _sinopac(tmp_path, [ACCOUNT_A], ["450"], "months.xlsx")), (_state(), _sinopac(tmp_path, [ACCOUNT_A], ["450"], "multi-account.xlsx")), (unmatched, _sinopac(tmp_path, ["1111222233334444"], ["450"], "unmatched.xlsx"))]
    for index, (state, path) in enumerate(cases):
        if index == 1: state["settlements"] = []
        if index == 3: path = _sinopac(tmp_path, [ACCOUNT_A], ["450"], "multi-account.xlsx"); pd.DataFrame([list(SINOPAC_HEADERS), ["BANK", "2026/07/15 09:00:00", "2026/07/15", "2026/07/15", "轉帳", "TWD", "450", "", "9000", "", "", "REF", f"{ACCOUNT_A} {ACCOUNT_B}", "", ""]]).to_excel(path, sheet_name="銀行流水明細", index=False, header=False)
        immutable = _pending_snapshot(state)
        result, _ = _import(monkeypatch, path, state)
        assert result["pending_rows"] == [1] and not state["transfers"] and not state["allocations"]
        assert _pending_snapshot(state) == immutable
        rerun, _ = _import(monkeypatch, path, state)
        assert rerun["skipped_existing"] == 1 and not state["transfers"]
        assert _pending_snapshot(state) == immutable
        _assert_audit(state, batches=2, rows=1, occurrences=2)


def test_taishin_legacy_exact_and_invalid_components_are_deterministic(monkeypatch, tmp_path):
    detail = {**_state()["details"][0], "service_salary": Decimal("0"), "floor_fee_amount": Decimal("0"), "legacy_subsidy_payable": Decimal("300"), "payable_amount": Decimal("300")}
    success = _state(settlements=[{"id": 20, "staff_id": STAFF, "settlement_month": "2026-06", "total_payable": Decimal("300"), "total_paid": Decimal("0"), "status": "finalized"}], details=[detail])
    immutable = _immutable_snapshot(success)
    path = _taishin(tmp_path, ACCOUNT_A, "300")
    result, _ = _import(monkeypatch, path, success)
    assert result["reconciled_counts"] == {"staff_legacy_subsidy": 1}
    assert success["transfers"][0]["payment_phase"] == "second_subsidy" and success["allocations"][0]["component_type"] == "legacy_subsidy"
    assert success["rows"][0]["transaction_date"] == "2026-07-15" and _immutable_snapshot(success) == immutable
    rerun, _ = _import(monkeypatch, path, success)
    assert rerun["skipped_existing"] == 1 and len(success["transfers"]) == len(success["allocations"]) == 1
    _assert_audit(success, batches=2, rows=1, occurrences=2)

    for changed, amount in (({"legacy_subsidy_status": "pending"}, "300"), ({"review_required": True}, "300"), ({}, "299")):
        state = _state(settlements=[{"id": 20, "staff_id": STAFF, "settlement_month": "2026-06", "total_payable": Decimal("300"), "total_paid": Decimal("0"), "status": "finalized"}], details=[{**detail, **changed}])
        immutable = _pending_snapshot(state)
        invalid = _taishin(tmp_path, ACCOUNT_A, amount, f"invalid-{amount}-{len(changed)}.xlsx")
        result, _ = _import(monkeypatch, invalid, state)
        assert result["pending_rows"] == [1] and not state["transfers"] and not state["allocations"]
        assert _pending_snapshot(state) == immutable
        rerun, _ = _import(monkeypatch, invalid, state)
        assert rerun["skipped_existing"] == 1 and not state["transfers"]
        assert _pending_snapshot(state) == immutable
        _assert_audit(state, batches=2, rows=1, occurrences=2)
