"""Shared finance_alerts/finance_alert_events mock-SQL handling.

Reused by the hand-rolled Cursor mocks in the finance-import integration
tests (test_finance_import_{client_subsidy_return,government_subsidy,
staff_actual_transfer}_integration.py) so each mock can stay focused on its
own domain tables while still exercising the real SQL shape issued by
services.finance_alert_detection.create_or_get_finance_alert() through
services.finance_alert_wiring.maybe_alert_pending().
"""

from __future__ import annotations


class AutocommitOff:
    """Minimal cursor.connection stand-in satisfying create_or_get_finance_alert()'s
    _require_transaction() check, which just needs a callable get_autocommit()."""

    def get_autocommit(self) -> bool:
        return False


def handle_finance_alert_sql(state: dict, compact: str, params, cursor) -> bool:
    """Handle one SAVEPOINT/finance_alerts/finance_alert_events statement.

    Sets cursor.current/cursor.lastrowid as needed. Returns True if the
    statement was recognized and handled; False means the caller's own
    execute() should keep looking (or raise for a truly unexpected query).
    """
    alerts = state.setdefault("alerts", {})
    events = state.setdefault("alert_events", {})

    if (
        compact.startswith("SAVEPOINT ")
        or compact.startswith("RELEASE SAVEPOINT")
        or compact.startswith("ROLLBACK TO SAVEPOINT")
    ):
        cursor.current = None
        return True
    if compact.startswith("SELECT id FROM finance_import_rows WHERE id=%s"):
        row_id = params[0]
        exists = any(row["id"] == row_id for row in state.get("rows", []))
        cursor.current = {"id": row_id} if exists else None
        return True
    if compact.startswith("SELECT id, credit, debit, transaction_date, bank_references FROM finance_import_rows WHERE id=%s"):
        row_id = params[0]
        row = next((row for row in state.get("rows", []) if row["id"] == row_id), None)
        cursor.current = (
            {key: row.get(key) for key in ("id", "credit", "debit", "transaction_date", "bank_references")}
            if row
            else None
        )
        return True
    if compact.startswith("SELECT matched_identity_ids FROM finance_import_rows WHERE id=%s"):
        row_id = params[0]
        row = next((row for row in state.get("rows", []) if row["id"] == row_id), None)
        cursor.current = {"matched_identity_ids": row.get("matched_identity_ids")} if row else None
        return True
    if compact.startswith("SELECT case_no FROM client_payments WHERE id=%s"):
        payment = state.get("payment")
        matches = payment is not None and payment.get("id") == params[0]
        cursor.current = {"case_no": payment.get("case_no")} if matches else None
        return True
    if compact.startswith("SELECT id, alert_key, alert_code, source_domain, source_type, source_id"):
        cursor.current = alerts.get(params[0])
        return True
    if compact.startswith("INSERT INTO finance_alerts"):
        keys = (
            "alert_key", "alert_code", "source_domain", "source_type", "source_id",
            "finance_import_row_id", "finance_import_batch_id", "reason",
            "expected_amount", "actual_amount", "difference_amount", "candidate_snapshot",
        )
        row = dict(zip(keys, params, strict=True))
        alert_id = len(alerts) + 1
        row.update(
            id=alert_id, status="open", claimed_by=None, claimed_at=None,
            resolved_by=None, resolved_at=None, resolution_reason=None,
            created_at=None, updated_at=None,
        )
        alerts[row["alert_key"]] = row
        cursor.lastrowid = alert_id
        return True
    if compact.startswith("SELECT id, alert_id, event_key"):
        cursor.current = events.get(params[0])
        return True
    if compact.startswith("INSERT INTO finance_alert_events"):
        keys = (
            "alert_id", "event_key", "source_domain", "source_type",
            "source_id", "reason", "event_snapshot", "occurred_at",
        )
        row = dict(zip(keys, params, strict=True))
        row.update(event_type="detected", actor=None)
        events[row["event_key"]] = row
        return True
    return False
