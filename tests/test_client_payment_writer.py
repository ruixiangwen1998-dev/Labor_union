from decimal import Decimal

import pytest

from services import client_payment_writer as writer
from services.client_payment_writer import build_client_summary_update


class FakeCursor:
    def __init__(self, *, payment=None, conflicts=None, transactions=None, active_lock=None):
        self.payment = payment or {
            "id": 1,
            "case_no": "115000001",
            "deposit_receivable": Decimal("1000"),
            "first_payment_receivable": Decimal("0"),
            "second_payment_receivable": Decimal("0"),
        }
        self.conflicts = conflicts or []
        self.transactions = transactions or []
        self.active_lock = active_lock
        self.executed = []
        self.current = None
        self.lastrowid = 81

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.executed.append((compact, params))
        if "FROM client_payments WHERE id" in compact:
            self.current = self.payment
        elif "WHERE external_reference = %s" in compact:
            self.current = self.conflicts
        elif "WHERE client_payment_id = %s" in compact:
            self.current = self.transactions
        elif "FROM caregiver_availability_locks l" in compact:
            self.current = self.active_lock
        else:
            self.current = None

    def fetchone(self):
        return self.current

    def fetchall(self):
        return list(self.current or [])


class FakeConnection:
    def __init__(self, cursor=None):
        self.cursor_instance = cursor or FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _candidate(**updates):
    transaction = {
        "stage": "deposit",
        "transaction_type": "receipt",
        "transaction_status": "succeeded",
        "amount": Decimal("1000"),
        "occurred_at": "2026-07-14",
        "external_reference": "fp:bank-1",
        "finance_import_row_id": 31,
        "notes": None,
    }
    transaction.update(updates)
    return transaction


def _existing(**updates):
    transaction = {
        "id": 81,
        "client_payment_id": 1,
        "case_no": "115000001",
        **_candidate(),
    }
    transaction.update(updates)
    return transaction


def test_summary_updates_only_active_receipt_stages():
    result = build_client_summary_update(
        {"deposit": 1000, "first_payment": 0, "second_payment": 0},
        [
            {
                "external_reference": "in",
                "stage": "deposit",
                "transaction_type": "receipt",
                "transaction_status": "succeeded",
                "amount": 1000,
            },
        ],
        "2026-07-14",
    )
    assert result["amount_received"] == 1000.0
    assert result["deposit_received_at"] == "2026-07-14"
    assert result["first_payment_received_at"] is None


def test_first_settlement_sets_second_due_date_from_actual_receipt_date():
    result = build_client_summary_update(
        {"deposit": 1000, "first_payment": 2000, "second_payment": 3000},
        [
            {"external_reference": "deposit", "stage": "deposit", "transaction_type": "receipt", "transaction_status": "succeeded", "amount": 1000, "occurred_at": "2026-05-01"},
            {"external_reference": "first-part", "stage": "first_payment", "transaction_type": "receipt", "transaction_status": "succeeded", "amount": 1000, "occurred_at": "2026-05-03"},
            {"external_reference": "first-rest", "stage": "first_payment", "transaction_type": "receipt", "transaction_status": "succeeded", "amount": 1000, "occurred_at": "2026-05-06"},
        ],
        "2026-05-06",
    )

    assert result["first_payment_received_at"] == "2026-05-06"
    assert result["second_payment_due_date"] == "2026-05-21"


def test_zero_receivable_stage_never_gets_a_settlement_date():
    result = build_client_summary_update(
        {"deposit": 1000, "first_payment": 0, "second_payment": 0},
        [{"external_reference": "deposit", "stage": "deposit", "transaction_type": "receipt", "transaction_status": "succeeded", "amount": 1000}],
        "2026-07-14",
    )

    assert result["first_payment_received_at"] is None
    assert result["second_payment_due_date"] is None


def test_cursor_core_inserts_bank_link_recalculates_summary_and_returns_id(monkeypatch):
    cursor = FakeCursor()
    monkeypatch.setattr(
        writer,
        "_activate_subsidy_return_obligation_after_full_receipt",
        lambda _cursor, _payment: None,
    )

    result = writer.record_client_payment_transaction_with_cursor(cursor, 1, _candidate())

    statements = [statement for statement, _ in cursor.executed]
    insert_params = next(params for statement, params in cursor.executed if statement.startswith("INSERT"))
    assert result["transaction_id"] == 81
    assert result["deposit_received_at"] == "2026-07-14"
    assert insert_params[-2] == 31
    assert any("second_payment_due_date=%s WHERE id=%s" in statement for statement in statements)
    assert any("SET status = '訂單成立'" in statement for statement in statements)


def test_failed_transaction_is_stored_without_changing_summary_or_settlement_dates():
    cursor = FakeCursor()

    result = writer.record_client_payment_transaction_with_cursor(
        cursor,
        1,
        _candidate(transaction_status="failed", amount=Decimal("500")),
    )

    assert result["deposit_received"] == 0.0
    assert result["amount_received"] == 0.0
    assert result["deposit_received_at"] is None
    assert any(statement.startswith("INSERT") for statement, _ in cursor.executed)
    assert not any(statement.startswith("UPDATE") for statement, _ in cursor.executed)


def test_deposit_with_active_lock_waits_for_atomic_assignment_conversion(monkeypatch):
    cursor = FakeCursor(active_lock={"id": 77})
    monkeypatch.setattr(
        writer,
        "_activate_subsidy_return_obligation_after_full_receipt",
        lambda _cursor, _payment: None,
    )

    result = writer.record_client_payment_transaction_with_cursor(
        cursor, 1, _candidate()
    )

    assert result["awaiting_availability_lock_conversion"] is True
    assert not any(
        "SET status = '訂單成立'" in statement
        for statement, _ in cursor.executed
    )


def test_same_finance_import_row_can_be_allocated_across_multiple_stages(monkeypatch):
    settled_deposit = _existing()
    cursor = FakeCursor(
        transactions=[settled_deposit],
        payment={
            "id": 1,
            "case_no": "115000001",
            "deposit_receivable": Decimal("1000"),
            "first_payment_receivable": Decimal("500"),
            "second_payment_receivable": Decimal("0"),
        },
    )
    monkeypatch.setattr(
        writer,
        "_activate_subsidy_return_obligation_after_full_receipt",
        lambda _cursor, _payment: None,
    )

    result = writer.record_client_payment_transaction_with_cursor(
        cursor,
        1,
        _candidate(
            stage="first_payment",
            amount=Decimal("500"),
            external_reference="fp:bank-1:first_payment",
        ),
    )

    conflict_query = next(
        statement for statement, _ in cursor.executed
        if "WHERE external_reference = %s" in statement
    )
    assert "finance_import_row_id = %s" not in conflict_query
    assert result["amount_received"] == 1500.0
    assert any(statement.startswith("INSERT") for statement, _ in cursor.executed)


def test_later_transaction_does_not_repeat_deposit_promotion():
    settled_deposit = _existing()
    cursor = FakeCursor(
        transactions=[settled_deposit],
        payment={
            "id": 1,
            "case_no": "115000001",
            "deposit_receivable": Decimal("1000"),
            "first_payment_receivable": Decimal("500"),
            "second_payment_receivable": Decimal("0"),
        },
    )

    writer.record_client_payment_transaction_with_cursor(
        cursor,
        1,
        _candidate(
            stage="first_payment",
            transaction_status="failed",
            amount=Decimal("500"),
            external_reference="fp:bank-2",
            finance_import_row_id=32,
        ),
    )

    assert not any("UPDATE orders" in statement for statement, _ in cursor.executed)


def test_exact_retry_returns_existing_id_without_writes():
    existing = _existing()
    cursor = FakeCursor(conflicts=[existing], transactions=[existing])

    result = writer.record_client_payment_transaction_with_cursor(cursor, 1, _candidate())

    assert result["transaction_id"] == 81
    assert result["amount_received"] == 1000.0
    assert not any(
        statement.startswith("INSERT") or statement.startswith("UPDATE")
        for statement, _ in cursor.executed
    )


@pytest.mark.parametrize(
    "conflict",
    [
        _existing(amount=Decimal("999")),
        _existing(finance_import_row_id=32),
        _existing(external_reference="fp:different"),
    ],
)
def test_partial_or_conflicting_retry_is_rejected_without_writes(conflict):
    cursor = FakeCursor(conflicts=[conflict], transactions=[conflict])

    with pytest.raises(ValueError, match="conflicts"):
        writer.record_client_payment_transaction_with_cursor(cursor, 1, _candidate())

    assert not any(
        statement.startswith("INSERT") or statement.startswith("UPDATE")
        for statement, _ in cursor.executed
    )


def test_overpayment_is_rejected_before_any_insert_or_update():
    cursor = FakeCursor()

    with pytest.raises(ValueError, match="outside the receivable range"):
        writer.record_client_payment_transaction_with_cursor(
            cursor,
            1,
            _candidate(amount=Decimal("1001")),
        )

    assert not any(
        statement.startswith("INSERT") or statement.startswith("UPDATE")
        for statement, _ in cursor.executed
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"stage": "subsidy_refund"},
        {"stage": "subsidy_return"},
        {"transaction_type": "refund"},
    ],
)
def test_subsidy_and_refund_flows_are_not_written(updates):
    cursor = FakeCursor()

    with pytest.raises(ValueError):
        writer.record_client_payment_transaction_with_cursor(cursor, 1, _candidate(**updates))

    assert cursor.executed == []


def test_bank_fingerprint_reference_requires_finance_import_row_id():
    cursor = FakeCursor()

    with pytest.raises(ValueError, match="require finance_import_row_id"):
        writer.record_client_payment_transaction_with_cursor(
            cursor,
            1,
            _candidate(finance_import_row_id=None),
        )

    assert cursor.executed == []


def test_compatibility_wrapper_owns_commit_and_close(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(writer, "get_connection", lambda: connection)
    monkeypatch.setattr(
        writer,
        "_activate_subsidy_return_obligation_after_full_receipt",
        lambda _cursor, _payment: None,
    )

    result = writer.record_client_payment_transaction(
        1,
        "deposit",
        "receipt",
        "succeeded",
        1000,
        "2026-07-14",
        "fp:bank-1",
        finance_import_row_id=31,
    )

    assert result["transaction_id"] == 81
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True


def test_full_payment_triggers_subsidy_return_activation(monkeypatch):
    cursor = FakeCursor(
        payment={
            "id": 1,
            "case_no": "115000001",
            "deposit_receivable": Decimal("1000"),
            "first_payment_receivable": Decimal("0"),
            "second_payment_receivable": Decimal("0"),
            "amount_receivable": Decimal("1000"),
        }
    )
    monkeypatch.setattr(
        writer,
        "_activate_subsidy_return_obligation_after_full_receipt",
        lambda cur, pay: {"obligation": {"subsidy_return_receivable": 12000}, "result": "activated"},
    )

    result = writer.record_client_payment_transaction_with_cursor(cursor, 1, _candidate(amount=Decimal("1000")))

    assert result["amount_received"] == 1000.0
    assert result["subsidy_return_activation"]["result"] == "activated"
    assert result["subsidy_return_review_status"] is None
    assert any(
        "subsidy_return_review_status=NULL" in statement
        for statement, _ in cursor.executed
    )


def test_reversal_when_obligation_active_persists_review_required():
    existing_receipt = _existing()
    cursor = FakeCursor(
        payment={
            "id": 1,
            "case_no": "115000001",
            "deposit_receivable": Decimal("1000"),
            "first_payment_receivable": Decimal("0"),
            "second_payment_receivable": Decimal("0"),
            "amount_receivable": Decimal("1000"),
            "subsidy_return_receivable": Decimal("12000"),
        },
        transactions=[existing_receipt],
    )

    result = writer.record_client_payment_transaction_with_cursor(
        cursor,
        1,
        _candidate(
            transaction_type="reversal",
            amount=Decimal("1000"),
            external_reference="fp:rev-1",
        ),
    )

    assert result["amount_received"] == 0.0
    assert result["subsidy_return_review_status"] == "review_required"
    review_write = next(
        (statement, params)
        for statement, params in cursor.executed
        if "subsidy_return_review_status=%s" in statement
    )
    assert review_write[1] == (
        "review_required",
        "client_receipt_reversal_below_receivable",
        1,
    )
    assert "subsidy_return_receivable" not in review_write[0]
    assert "subsidy_return_due_date" not in review_write[0]


def test_activation_uses_projection_and_obligation_with_same_cursor(monkeypatch):
    cursor = FakeCursor()
    projected = {
        "order": {
            "service_days": 20,
            "service_hours_per_day": 8,
            "floor_fee": 0,
            "start_date": "2026-06-01",
            "actual_start_date": "2026-06-02",
            "actual_end_date": "2026-06-28",
        },
        "client": {"identity_status": "一般市民"},
        "collection_schedule": {
            "deposit_service_days": 5,
            "deposit_due_date": "2026-05-15",
        },
    }
    calls = {}

    def fake_projection(seen_cursor, case_no):
        calls["projection"] = (seen_cursor, case_no)
        return projected

    def fake_calculator(order_terms, assignments, collection_schedule):
        calls["calculator"] = (order_terms, assignments, collection_schedule)
        return {"client_ledger_plan": {"subsidy_return_amount": 12000}}

    def fake_activate(seen_cursor, payment_id, amount, due_date):
        calls["activation"] = (seen_cursor, payment_id, amount, due_date)
        return {"obligation": {"subsidy_return_receivable": amount}, "result": "activated"}

    monkeypatch.setattr(writer, "load_case_accounting_source_with_cursor", fake_projection)
    monkeypatch.setattr(writer, "calculate_order_amounts", fake_calculator)
    monkeypatch.setattr(writer, "activate_subsidy_return_obligation", fake_activate)

    result = writer._activate_subsidy_return_obligation_after_full_receipt(
        cursor, {"id": 1, "case_no": "115000001"}
    )

    assert calls["projection"] == (cursor, "115000001")
    assert calls["calculator"][0]["identity_status"] == "一般市民"
    assert calls["calculator"][0]["service_start_date"] == "2026-06-02"
    assert calls["calculator"][1] == []
    assert calls["calculator"][2] is projected["collection_schedule"]
    assert calls["activation"] == (
        cursor,
        1,
        Decimal("12000"),
        "2026-07-05",
    )
    assert result["result"] == "activated"


def test_projection_failure_rolls_back_entire_wrapper_transaction(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(writer, "get_connection", lambda: connection)
    monkeypatch.setattr(
        writer,
        "load_case_accounting_source_with_cursor",
        lambda _cursor, _case_no: (_ for _ in ()).throw(RuntimeError("projection failed")),
    )

    with pytest.raises(RuntimeError, match="projection failed"):
        writer.record_client_payment_transaction(
            1,
            "deposit",
            "receipt",
            "succeeded",
            1000,
            "2026-07-14",
            "fp:bank-1",
            finance_import_row_id=31,
        )

    assert connection.committed is False
    assert connection.rolled_back is True
    assert connection.closed is True


def test_activation_review_result_is_persisted(monkeypatch):
    cursor = FakeCursor()
    monkeypatch.setattr(
        writer,
        "_activate_subsidy_return_obligation_after_full_receipt",
        lambda _cursor, _payment: {"obligation": None, "result": "review_required"},
    )

    result = writer.record_client_payment_transaction_with_cursor(
        cursor, 1, _candidate()
    )

    assert result["subsidy_return_review_status"] == "review_required"
    assert result["subsidy_return_review_reason"] == "subsidy_return_obligation_requires_review"
    assert any(
        params == (
            "review_required",
            "subsidy_return_obligation_requires_review",
            1,
        )
        for statement, params in cursor.executed
        if "subsidy_return_review_status=%s" in statement
    )
