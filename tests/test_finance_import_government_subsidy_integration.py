"""B4 end-to-end coverage for Taishin government-subsidy imports."""

from __future__ import annotations

from decimal import Decimal
import json

import pandas as pd

from scripts.imports import import_finance_excel as importer
from scripts.imports.finance_formats.taishin import TAISHIN_HEADERS
from tests._finance_alert_mock_support import AutocommitOff, handle_finance_alert_sql


class StatefulCursor:
    """Small in-memory MySQL-shaped store at the importer's DB boundary."""

    rowcount = 1

    def __init__(self, state):
        self.state = state
        self.current = None
        self.lastrowid = None
        self.connection = AutocommitOff()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.rowcount = 1
        if compact.startswith("SELECT cp.id AS client_payment_id"):
            self.current = []
        elif compact.startswith("SELECT sba.staff_id"):
            self.current = []
        elif compact.startswith("INSERT INTO finance_import_batches"):
            self.lastrowid = len(self.state["batches"]) + 1
            self.state["batches"].append({"id": self.lastrowid, "status": "staged"})
        elif compact.startswith("SELECT id, classification_type, reconciliation_status FROM finance_import_rows"):
            row = self.state["rows_by_fingerprint"].get(params[0])
            self.current = None if row is None else {
                key: row[key] for key in ("id", "classification_type", "reconciliation_status")
            }
        elif compact.startswith("INSERT INTO finance_import_rows"):
            keys = (
                "dedup_fingerprint", "batch_id", "format_id", "source_file", "source_bank_account",
                "sheet_name", "source_row", "source_reference", "transaction_date", "transaction_time",
                "posting_date", "value_date", "debit", "credit", "direction", "balance", "currency",
                "summary", "memo", "counterparty_name", "counterparty_account", "cancellation_code",
                "bank_references", "warnings", "raw_payload",
            )
            row = dict(zip(keys, params, strict=True))
            row.update({"id": len(self.state["rows"]) + 1, "reconciliation_status": "pending"})
            self.state["rows"].append(row)
            self.state["rows_by_fingerprint"][row["dedup_fingerprint"]] = row
            self.lastrowid = row["id"]
        elif compact.startswith("INSERT INTO finance_import_occurrences"):
            self.state["occurrences"].append(params)
        elif compact.startswith("UPDATE finance_import_rows SET classification_type"):
            row = self._row(params[-1])
            row.update({"classification_type": params[0], "matched_identity_ids": params[1],
                        "resolved_counterparty_account": params[2], "classification_reason": params[3]})
        elif compact.startswith("SELECT id, dedup_fingerprint, format_id, transaction_date"):
            self.current = self._row(params[0])
        elif compact.startswith("SELECT id, claim_batch_id, finance_import_row_id, amount, external_reference FROM government_subsidy_transactions WHERE finance_import_row_id"):
            self.current = next((item for item in self.state["transactions"] if item["finance_import_row_id"] == params[0]), None)
        elif compact.startswith("SELECT id, claim_batch_id, finance_import_row_id, amount, external_reference FROM government_subsidy_transactions WHERE external_reference"):
            self.current = next((item for item in self.state["transactions"] if item["external_reference"] == params[0]), None)
        elif compact.startswith("SELECT * FROM subsidy_claim_batches"):
            if "WHERE id = %s" in compact:
                batch_id, amount = params
                self.current = [batch for batch in self.state["claim_batches"] if batch["id"] == batch_id and batch["status"] == "approved" and batch["paid_amount"] == 0 and batch["approved_amount"] == amount]
            else:
                amount = params[0]
                self.current = [batch for batch in self.state["claim_batches"] if batch["status"] == "approved" and batch["paid_amount"] == 0 and batch["approved_amount"] == amount]
        elif compact.startswith("SELECT id, batch_id, approved_amount, paid_amount FROM subsidy_claim_batch_items"):
            self.current = [item for item in self.state["items"] if item["batch_id"] == params[0]]
        elif compact.startswith("INSERT INTO government_subsidy_transactions"):
            transaction = {"id": len(self.state["transactions"]) + 1, "claim_batch_id": params[0],
                           "finance_import_row_id": params[1], "amount": params[2], "occurred_at": params[3],
                           "external_reference": params[4]}
            self.state["transactions"].append(transaction)
            self.lastrowid = transaction["id"]
        elif compact.startswith("INSERT INTO government_subsidy_allocations"):
            self.state["allocations"].append({"transaction_id": params[0], "claim_batch_id": params[1],
                                               "claim_item_id": params[2], "allocated_amount": params[3]})
        elif compact.startswith("UPDATE subsidy_claim_batch_items SET paid_amount"):
            item = next(item for item in self.state["items"] if item["id"] == params[0])
            item["paid_amount"] = item["approved_amount"]
        elif compact.startswith("UPDATE subsidy_claim_batches SET paid_amount"):
            batch = next(batch for batch in self.state["claim_batches"] if batch["id"] == params[0])
            batch.update({"paid_amount": batch["approved_amount"], "status": "paid"})
        elif compact.startswith("UPDATE finance_import_rows SET reconciliation_status = 'reconciled'"):
            row = self._row(params[1])
            row.update({"reconciliation_status": "reconciled", "reconciliation_reference": params[0]})
        elif compact.startswith("UPDATE finance_import_batches SET status='completed'"):
            self.state["batches"][params[0] - 1]["status"] = "completed"
        elif handle_finance_alert_sql(self.state, compact, params, self):
            pass
        else:
            raise AssertionError(f"unexpected SQL: {compact}")

    def fetchone(self):
        return self.current

    def fetchall(self):
        return list(self.current or [])

    def _row(self, row_id):
        return next(row for row in self.state["rows"] if row["id"] == row_id)


class StatefulConnection:
    def __init__(self, state):
        self.state = state
        self.commits = self.rollbacks = self.closes = 0

    def cursor(self):
        return StatefulCursor(self.state)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def _state(batch_amounts=(Decimal("1000"),)):
    batches = [{"id": index + 1, "status": "approved", "requested_amount": amount,
                "approved_amount": amount, "paid_amount": Decimal("0")}
               for index, amount in enumerate(batch_amounts)]
    items = []
    for batch in batches:
        amounts = (Decimal("400"), Decimal("600")) if batch["approved_amount"] == 1000 else (batch["approved_amount"],)
        for amount in amounts:
            items.append({"id": len(items) + 1, "batch_id": batch["id"], "requested_amount": amount,
                          "approved_amount": amount, "paid_amount": Decimal("0")})
    return {"batches": [], "rows": [], "rows_by_fingerprint": {}, "occurrences": [],
            "claim_batches": batches, "items": items, "transactions": [], "allocations": []}


def _write_taishin_fixture(tmp_path, amounts, name="government-subsidy.xlsx"):
    rows = [["statement"], list(TAISHIN_HEADERS)]
    for index, amount in enumerate(amounts, start=1):
        rows.append([str(index), "2026/07/15", f"09:08:{index:02d}", "2026/07/15", "transfer",
                     "", str(amount), "9,000.00", "新竹市政府 補助撥款"])
    path = tmp_path / name
    pd.DataFrame(rows).to_excel(path, sheet_name="Taishin", index=False, header=False)
    return path


def _import(monkeypatch, path, state):
    connection = StatefulConnection(state)
    monkeypatch.setattr(importer, "get_connection", lambda: connection)
    return importer.import_finance_workbook(str(path)), connection


def _assert_raw_staging(state, batches, occurrences):
    assert len(state["batches"]) == batches
    assert len(state["rows"]) >= 1
    assert len(state["occurrences"]) == occurrences
    for row in state["rows"]:
        assert row["format_id"] == "taishin"
        assert row["transaction_date"] == "2026-07-15"
        assert row["dedup_fingerprint"]
        assert json.loads(row["raw_payload"])["備註"] == "新竹市政府 補助撥款"


def test_exact_unique_government_subsidy_reconciles_and_rerun_only_adds_occurrence(monkeypatch, tmp_path):
    state = _state()
    path = _write_taishin_fixture(tmp_path, ["1000"])

    result, connection = _import(monkeypatch, path, state)

    assert {key: result[key] for key in (
        "batch_id", "inserted_rows", "skipped_existing",
        "reconciled_counts", "pending_rows",
    )} == {"batch_id": 1, "inserted_rows": 1, "skipped_existing": 0,
          "reconciled_counts": {"government_subsidy": 1}, "pending_rows": []}
    row, batch = state["rows"][0], state["claim_batches"][0]
    assert row["classification_type"] == "government_subsidy"
    assert row["reconciliation_status"] == "reconciled"
    assert len(state["transactions"]) == 1 and state["transactions"][0]["amount"] == Decimal("1000")
    assert state["transactions"][0]["external_reference"] == f"fp:{row['dedup_fingerprint']}"
    assert sum(item["allocated_amount"] for item in state["allocations"]) == Decimal("1000")
    assert [item["paid_amount"] for item in state["items"]] == [Decimal("400"), Decimal("600")]
    assert batch["paid_amount"] == Decimal("1000") and batch["status"] == "paid"
    assert batch["requested_amount"] == batch["approved_amount"] == Decimal("1000")
    assert connection.commits == 1 and connection.rollbacks == 0

    rerun, _ = _import(monkeypatch, path, state)
    assert rerun["inserted_rows"] == 0 and rerun["skipped_existing"] == 1
    assert len(state["transactions"]) == 1 and len(state["allocations"]) == 2
    _assert_raw_staging(state, 2, 2)


def test_short_over_and_split_bank_receipts_stay_pending_without_formal_writes(monkeypatch, tmp_path):
    for index, amounts in enumerate((["900"], ["1100"], ["600", "400"]), start=1):
        state = _state()
        path = _write_taishin_fixture(tmp_path, amounts, f"boundary-{index}.xlsx")
        result, _ = _import(monkeypatch, path, state)
        assert result["reconciled_counts"] == {}
        assert result["pending_rows"] == list(range(1, len(amounts) + 1))
        assert len(state["transactions"]) == 0 and len(state["allocations"]) == 0
        assert all(row["classification_type"] == "government_subsidy" and row["reconciliation_status"] == "pending" for row in state["rows"])
        assert state["claim_batches"][0]["status"] == "approved"
        assert state["claim_batches"][0]["paid_amount"] == Decimal("0")
        assert all(item["paid_amount"] == Decimal("0") for item in state["items"])
        _assert_raw_staging(state, 1, len(amounts))

        rerun, _ = _import(monkeypatch, path, state)
        assert rerun["inserted_rows"] == 0
        assert rerun["skipped_existing"] == len(amounts)
        assert rerun["reconciled_counts"] == {} and rerun["pending_rows"] == []
        assert len(state["transactions"]) == len(state["allocations"]) == 0
        assert all(batch["paid_amount"] == 0 and batch["status"] == "approved" for batch in state["claim_batches"])
        _assert_raw_staging(state, 2, len(amounts) * 2)


def test_same_amount_multiple_batches_and_cross_batch_total_stay_pending(monkeypatch, tmp_path):
    same_amount = _state((Decimal("1000"), Decimal("1000")))
    same_path = _write_taishin_fixture(tmp_path, ["1000"], "ambiguous.xlsx")
    result, _ = _import(monkeypatch, same_path, same_amount)
    assert result["pending_rows"] == [1]
    assert len(same_amount["transactions"]) == len(same_amount["allocations"]) == 0
    assert all(batch["status"] == "approved" and batch["paid_amount"] == 0 for batch in same_amount["claim_batches"])
    rerun, _ = _import(monkeypatch, same_path, same_amount)
    assert rerun["inserted_rows"] == 0 and rerun["skipped_existing"] == 1
    assert len(same_amount["transactions"]) == len(same_amount["allocations"]) == 0
    _assert_raw_staging(same_amount, 2, 2)

    cross_batch = _state((Decimal("400"), Decimal("600")))
    cross_path = _write_taishin_fixture(tmp_path, ["1000"], "cross-batch.xlsx")
    result, _ = _import(monkeypatch, cross_path, cross_batch)
    assert result["pending_rows"] == [1]
    assert len(cross_batch["transactions"]) == len(cross_batch["allocations"]) == 0
    assert all(batch["paid_amount"] == 0 for batch in cross_batch["claim_batches"])
    rerun, _ = _import(monkeypatch, cross_path, cross_batch)
    assert rerun["inserted_rows"] == 0 and rerun["skipped_existing"] == 1
    assert len(cross_batch["transactions"]) == len(cross_batch["allocations"]) == 0
    _assert_raw_staging(cross_batch, 2, 2)
