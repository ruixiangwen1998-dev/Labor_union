"""B3 end-to-end coverage for Taishin client subsidy-return imports."""

from __future__ import annotations

from decimal import Decimal
import json

import pandas as pd
import pytest

from scripts.imports import import_finance_excel as importer
from scripts.imports.finance_formats.taishin import TAISHIN_HEADERS
from tests._finance_alert_mock_support import AutocommitOff, handle_finance_alert_sql


ACCOUNT = "0012345678901234"


class StatefulCursor:
    """Small in-memory MySQL-shaped store used only at the database boundary."""

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
            self.current = self.state["identity_rows"]
        elif compact.startswith("SELECT sba.staff_id"):
            self.current = []
        elif compact.startswith("INSERT INTO finance_import_batches"):
            self.lastrowid = len(self.state["batches"]) + 1
            self.state["batches"].append({"id": self.lastrowid, "status": "staged"})
        elif compact.startswith("SELECT id, classification_type, reconciliation_status FROM finance_import_rows"):
            fingerprint = params[0]
            row = self.state["rows_by_fingerprint"].get(fingerprint)
            self.current = None if row is None else {
                key: row[key] for key in ("id", "classification_type", "reconciliation_status")
            }
        elif compact.startswith("INSERT INTO finance_import_rows"):
            row_id = len(self.state["rows"]) + 1
            keys = (
                "dedup_fingerprint", "batch_id", "format_id", "source_file",
                "source_bank_account", "sheet_name", "source_row", "source_reference",
                "transaction_date", "transaction_time", "posting_date", "value_date",
                "debit", "credit", "direction", "balance", "currency", "summary",
                "memo", "counterparty_name", "counterparty_account", "cancellation_code",
                "bank_references", "warnings", "raw_payload",
            )
            row = dict(zip(keys, params, strict=True))
            row.update({"id": row_id, "reconciliation_status": "pending"})
            self.state["rows"].append(row)
            self.state["rows_by_fingerprint"][row["dedup_fingerprint"]] = row
            self.lastrowid = row_id
        elif compact.startswith("INSERT INTO finance_import_occurrences"):
            self.state["occurrences"].append(params)
        elif compact.startswith("UPDATE finance_import_rows SET classification_type"):
            row = self._row(params[-1])
            row.update({
                "classification_type": params[0],
                "matched_identity_ids": params[1],
                "resolved_counterparty_account": params[2],
                "classification_reason": params[3],
            })
        elif compact.startswith("SELECT id, classification_type, matched_identity_ids,"):
            row = self._row(params[0])
            self.current = {key: row.get(key) for key in (
                "id", "classification_type", "matched_identity_ids",
                "resolved_counterparty_account", "debit",
            )}
        elif compact.startswith("SELECT fir.id AS finance_import_row_id"):
            row = self._row(params[1])
            payment = self.state["payment"]
            self.current = {**row, "finance_import_row_id": row["id"], **payment}
        elif compact.startswith("SELECT id, client_payment_id, case_no, finance_import_row_id, stage,"):
            reference, row_id = params
            self.current = [
                transaction for transaction in self.state["transactions"]
                if transaction["external_reference"] == reference
                or transaction["finance_import_row_id"] == row_id
            ]
        elif compact.startswith("SELECT id, transaction_type, transaction_status, amount, occurred_at,"):
            self.current = [
                transaction for transaction in self.state["transactions"]
                if transaction["client_payment_id"] == params[0]
                and transaction["stage"] == "subsidy_return"
            ]
        elif compact.startswith("INSERT INTO client_payment_transactions"):
            transaction_id = len(self.state["transactions"]) + 100
            self.lastrowid = transaction_id
            self.state["transactions"].append({
                "id": transaction_id,
                "client_payment_id": params[0], "case_no": params[1],
                "stage": "subsidy_return", "transaction_type": "refund",
                "transaction_status": "succeeded", "amount": params[2],
                "occurred_at": params[3], "external_reference": params[4],
                "finance_import_row_id": params[5], "reversal_of_transaction_id": None,
            })
        elif compact.startswith("UPDATE client_payments SET subsidy_return_refunded"):
            self.state["payment"].update({"subsidy_return_refunded": params[0], "subsidy_return_at": params[1]})
        elif compact.startswith("UPDATE finance_import_rows SET reconciliation_status='reconciled'"):
            row = self._row(params[1])
            if row["reconciliation_status"] != "pending":
                self.rowcount = 0
            else:
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
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return StatefulCursor(self.state)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def _state(*, account_ids=(9,), transactions=()):
    if not transactions:
        transactions = ({
            "id": 1, "client_payment_id": 9, "case_no": "CASE-9",
            "stage": "subsidy_return", "transaction_type": "refund",
            "transaction_status": "succeeded", "amount": Decimal("250"),
            "occurred_at": "2026-07-01", "external_reference": "manual:1",
            "finance_import_row_id": None, "reversal_of_transaction_id": None,
        },)
    return {
        "identity_rows": [
            {"client_payment_id": client_id, "refund_account_no": ACCOUNT}
            for client_id in account_ids
        ],
        "payment": {
            "id": 9, "case_no": "CASE-9", "subsidy_return_receivable": Decimal("1500"),
            "subsidy_return_refunded": Decimal("250"), "subsidy_return_at": None,
        },
        "transactions": list(transactions), "rows": [], "rows_by_fingerprint": {},
        "occurrences": [], "batches": [],
    }


def _write_taishin_fixture(tmp_path, debit):
    path = tmp_path / "taishin-client-return.xlsx"
    data = [["statement"], list(TAISHIN_HEADERS), [
        "0001", "2026/07/15", "09:08:07", "2026/07/15", "transfer",
        str(debit), "", "9,000.00", f"client return {ACCOUNT}",
    ]]
    pd.DataFrame(data).to_excel(path, sheet_name="Taishin", index=False, header=False)
    return path


def _import(monkeypatch, path, state):
    connection = StatefulConnection(state)
    monkeypatch.setattr(importer, "get_connection", lambda: connection)
    return importer.import_finance_workbook(str(path)), connection


def test_taishin_exact_return_runs_normalization_staging_classifier_and_reconciliation(monkeypatch, tmp_path):
    state = _state()
    path = _write_taishin_fixture(tmp_path, "1250.00")

    result, connection = _import(monkeypatch, path, state)

    assert {key: result[key] for key in (
        "batch_id", "inserted_rows", "skipped_existing",
        "reconciled_counts", "pending_rows",
    )} == {
        "batch_id": 1, "inserted_rows": 1, "skipped_existing": 0,
        "reconciled_counts": {"client_subsidy_return": 1}, "pending_rows": [],
    }
    row = state["rows"][0]
    assert row["format_id"] == "taishin"
    assert row["direction"] == "outgoing"
    assert row["classification_type"] == "client_subsidy_return"
    assert json.loads(row["matched_identity_ids"]) == [9]
    assert row["reconciliation_status"] == "reconciled"
    assert state["payment"]["subsidy_return_refunded"] == Decimal("1500")
    assert state["payment"]["subsidy_return_at"] == "2026-07-15"
    assert state["transactions"][-1]["external_reference"] == f"fp:{row['dedup_fingerprint']}"
    assert connection.commits == 1 and connection.rollbacks == 0

    rerun, _ = _import(monkeypatch, path, state)
    assert rerun["inserted_rows"] == 0
    assert rerun["skipped_existing"] == 1
    assert len(state["transactions"]) == 2
    assert len(state["occurrences"]) == 2


@pytest.mark.parametrize("debit", ["1249.99", "1250.01"])
def test_taishin_return_amount_boundaries_remain_pending(monkeypatch, tmp_path, debit):
    state = _state()
    result, _ = _import(monkeypatch, _write_taishin_fixture(tmp_path, debit), state)

    assert result["pending_rows"] == [1]
    assert result["reconciled_counts"] == {}
    assert state["rows"][0]["classification_type"] == "client_subsidy_return"
    assert state["rows"][0]["reconciliation_status"] == "pending"
    assert len(state["transactions"]) == 1
    assert state["payment"]["subsidy_return_refunded"] == Decimal("250")


def test_taishin_ambiguous_client_return_account_stays_non_business_review(monkeypatch, tmp_path):
    state = _state(account_ids=(9, 10))
    result, _ = _import(monkeypatch, _write_taishin_fixture(tmp_path, "1250"), state)

    assert result["pending_rows"] == [1]
    assert state["rows"][0]["classification_type"] == "non_business_review"
    assert json.loads(state["rows"][0]["matched_identity_ids"]) == []
    assert len(state["transactions"]) == 1


def test_taishin_return_uses_failed_and_reversal_history_when_matching_remaining(monkeypatch, tmp_path):
    transactions = (
        {"id": 1, "client_payment_id": 9, "case_no": "CASE-9", "stage": "subsidy_return", "transaction_type": "refund", "transaction_status": "succeeded", "amount": Decimal("500"), "occurred_at": "2026-07-01", "external_reference": "manual:1", "finance_import_row_id": None, "reversal_of_transaction_id": None},
        {"id": 2, "client_payment_id": 9, "case_no": "CASE-9", "stage": "subsidy_return", "transaction_type": "refund", "transaction_status": "failed", "amount": Decimal("999"), "occurred_at": "2026-07-02", "external_reference": "manual:2", "finance_import_row_id": None, "reversal_of_transaction_id": None},
        {"id": 3, "client_payment_id": 9, "case_no": "CASE-9", "stage": "subsidy_return", "transaction_type": "reversal", "transaction_status": "succeeded", "amount": Decimal("250"), "occurred_at": "2026-07-03", "external_reference": "manual:3", "finance_import_row_id": None, "reversal_of_transaction_id": 1},
    )
    state = _state(transactions=transactions)
    result, _ = _import(monkeypatch, _write_taishin_fixture(tmp_path, "1250"), state)

    assert result["reconciled_counts"] == {"client_subsidy_return": 1}
    assert state["payment"]["subsidy_return_refunded"] == Decimal("1500")
    assert len(state["transactions"]) == 4
