from pathlib import Path

from scripts.imports import import_finance_excel as importer
from services.finance_transaction_classifier import classify_finance_transaction
from services.finance_transaction_fingerprint import build_dedup_fingerprint


SAMPLE = (
    Path("document")
    / "資料庫、資料處理"
    / "歷史對帳單.xlsx"
)


class Cursor:
    rowcount = 1

    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))


class Connection:
    def __init__(self):
        self._cursor = Cursor()
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def test_exact_historical_statement_runs_full_dry_run_and_rolls_back(monkeypatch):
    connection = Connection()

    def stage_rows(cursor, normalized, identity_maps):
        assert normalized["format_id"] == "legacy"
        assert normalized["sheet_name"] == "永豐3131(虛擬)"
        assert normalized["header_row"] == 3
        assert len(normalized["normalized_rows"]) == 1
        row = normalized["normalized_rows"][0]
        classification = classify_finance_transaction(row, {}, {})
        assert classification == {
            "classification_type": "non_business_review",
            "matched_identity_ids": [],
            "resolved_counterparty_account": None,
            "reason": "sinopac_staff_account_no_match",
        }
        return {
            "batch_id": 999,
            "staged_rows": [
                {
                    "row_id": 1001,
                    "dedup_fingerprint": build_dedup_fingerprint(row),
                    "classification_type": classification["classification_type"],
                    "result": "inserted",
                }
            ],
        }

    monkeypatch.setattr(importer, "get_connection", lambda: connection)
    monkeypatch.setattr(importer, "load_finance_identity_maps", lambda cursor: {})
    monkeypatch.setattr(importer, "stage_finance_rows", stage_rows)
    monkeypatch.setattr(
        importer,
        "_dispatch_inserted_row",
        lambda cursor, row: {
            "result": "pending",
            "reason": "sinopac_staff_account_no_match",
        },
    )
    monkeypatch.setattr(importer, "maybe_alert_pending", lambda *args, **kwargs: None)

    result = importer.import_finance_workbook(str(SAMPLE), dry_run=True)

    assert result["mode"] == "dry_run"
    assert result["source_path"] == str(SAMPLE.resolve())
    assert result["format_manifest"] == {
        "format_id": "legacy",
        "sheet_name": "永豐3131(虛擬)",
        "header_row": 3,
        "normalized_row_count": 1,
    }
    assert result["batch_id"] is None
    assert result["inserted_rows"] == 1
    assert result["skipped_existing"] == 0
    assert result["reconciled_counts"] == {}
    assert result["pending_rows"] == []
    assert result["row_results"] == [
        {
            "dedup_fingerprint": "1295ac604d12e08b1ef03e79a8ebbcaabda47b70aa2fe9be15ce99f5253c5bde",
            "classification_type": "non_business_review",
            "staging_result": "inserted",
            "dispatch_result": "pending",
            "reason": "sinopac_staff_account_no_match",
        }
    ]
    assert result["transaction_outcome"] == "rolled_back"
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closes == 1
    assert any("SET status='completed'" in sql for sql, _ in connection._cursor.executed)
