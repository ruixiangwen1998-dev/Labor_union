import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from services import caregiver_availability_lock_release_service as service
from services.caregiver_availability_lock_acquisition_helpers import normalize_plan_snapshot


def _segments(count: int):
    start = date(2026, 8, 1)
    return [
        {
            "id": 11 + index,
            "plan_id": 7,
            "segment_order": index + 1,
            "staff_id": 3 + index,
            "assigned_start_date": start + timedelta(days=index),
            "assigned_end_date": start + timedelta(days=index),
        }
        for index in range(count)
    ]


def _snapshot(count: int, plan_status: str = "accepted") -> dict[str, object]:
    return normalize_plan_snapshot(
        "C-1",
        7,
        {
            "id": 7,
            "case_no": "C-1",
            "status": plan_status,
            "is_active": 1,
            "start_date": date(2026, 8, 1),
            "end_date": date(2026, 8, count),
        },
        _segments(count),
    )


class _Cursor:
    def __init__(
        self,
        *,
        count: int = 1,
        lock_status: str = "active",
        lock_is_active: int | None = 1,
        lock_released_by: str | None = None,
        lock_released_at: str | None = None,
        lock_row_plan_id: int = 7,
        lock_row_id: int = 88,
        lock_days_released: bool = False,
        lock_actor: str = "admin",
        lock_reason: str = "release lock",
        plan_status: str = "accepted",
        plan_is_active: int = 1,
        existing_event: bool = False,
        existing_event_lock_id: int = 88,
        existing_event_type: str = "lock_released",
        existing_event_actor: str = "admin",
        existing_event_reason: str = "release lock",
        event_payload: dict | None = None,
        plan_id: int = 7,
        transactions: list[dict] | None = None,
        summary_received: str = "0",
        summary_receivable: str = "0",
        fail_execute_at: int | None = None,
        fail_fetch_kind: str | None = None,
        fail_cursor_rowcount_days: int | None = None,
        fail_cursor_rowcount_lock: int | None = None,
        fail_cursor_rowcount_plan: int | None = None,
    ):
        if count < 1:
            raise ValueError("count must be positive")
        self.segments = _segments(count)
        self.snapshot = _snapshot(count, plan_status=plan_status)

        self.lock_status = lock_status
        self.lock_is_active = lock_is_active
        self.lock_released_by = lock_released_by
        self.lock_released_at = lock_released_at
        self.lock_row_plan_id = lock_row_plan_id
        self.lock_row_id = lock_row_id
        self.lock_days_released = lock_days_released
        self.lock_actor = lock_actor
        self.lock_reason = lock_reason
        self.plan_status = plan_status
        self.plan_is_active = plan_is_active
        self.existing_event = existing_event
        self.existing_event_lock_id = existing_event_lock_id
        self.existing_event_type = existing_event_type
        self.existing_event_actor = existing_event_actor
        self.existing_event_reason = existing_event_reason
        self.plan_id = plan_id
        self.transactions = transactions or [
            {
                "id": 31,
                "transaction_type": "receipt",
                "transaction_status": "failed",
                "stage": "deposit",
                "amount": Decimal("100"),
                "occurred_at": date(2026, 8, 1),
                "external_reference": "tx-31",
                "reversal_of_transaction_id": None,
            }
        ]
        self.summary_received = Decimal(summary_received)
        self.summary_receivable = Decimal(summary_receivable)
        self.fail_execute_at = fail_execute_at
        self.fail_fetch_kind = fail_fetch_kind

        request = {
            "case_no": "C-1",
            "plan_id": 7,
            "lock_id": lock_row_id,
            "event_key": "event-1",
            "actor": "admin",
            "reason": "release lock",
        }
        if event_payload is None:
            event_payload = service._build_release_event_payload(request, self.snapshot)
        self.event_payload = event_payload

        self.lock_days = [
            {
                "id": index + 501,
                "segment_id": row["segment_id"],
                "staff_id": row["staff_id"],
                "lock_date": date.fromisoformat(row["lock_date"]),
                "active_marker": None if lock_days_released else 1,
                "released_by": self.lock_actor if lock_days_released else None,
                "released_at": date(2026, 8, 1) if lock_days_released else None,
            }
            for index, row in enumerate(self.snapshot["lock_rows"])
        ]

        self.executed = []
        self.rowcount = 0
        self.closed = False
        self.fail_execute_counter = 0
        self.fail_cursor_rowcount_days = fail_cursor_rowcount_days
        self.fail_cursor_rowcount_lock = fail_cursor_rowcount_lock
        self.fail_cursor_rowcount_plan = fail_cursor_rowcount_plan

    def execute(self, sql: str, params=()):
        self.executed.append((sql, params))
        self.fail_execute_counter += 1
        if self.fail_execute_counter == self.fail_execute_at:
            raise RuntimeError("execute failed")

        normalized = " ".join(sql.split())
        if normalized.startswith("UPDATE caregiver_availability_lock_days"):
            self.rowcount = (
                self.fail_cursor_rowcount_days
                if self.fail_cursor_rowcount_days is not None
                else len(self.lock_days)
            )
        elif normalized.startswith("UPDATE caregiver_availability_locks"):
            self.rowcount = (
                self.fail_cursor_rowcount_lock
                if self.fail_cursor_rowcount_lock is not None
                else 1
            )
        elif normalized.startswith("UPDATE caregiver_matching_plans"):
            self.rowcount = (
                self.fail_cursor_rowcount_plan
                if self.fail_cursor_rowcount_plan is not None
                else 1
            )
        elif normalized.startswith("INSERT INTO caregiver_availability_lock_events"):
            self.rowcount = 1
        else:
            self.rowcount = 0

    def fetchone(self):
        if self.fail_fetch_kind == "one":
            raise RuntimeError("fetchone failed")

        if "FROM caregiver_matching_plans" in self.executed[-1][0]:
            return {
                "id": self.plan_id,
                "case_no": "C-1",
                "status": self.plan_status,
                "is_active": self.plan_is_active,
                "start_date": date(2026, 8, 1),
                "end_date": date(2026, 8, len(self.segments)),
            }

        if "FROM caregiver_availability_lock_events" in self.executed[-1][0]:
            if not self.existing_event:
                return None
            return {
                "id": 99,
                "lock_id": self.existing_event_lock_id,
                "event_type": self.existing_event_type,
                "event_key": "event-1",
                "actor": self.existing_event_actor,
                "reason": self.existing_event_reason,
                "payload": json.dumps(self.event_payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True),
            }

        if "FROM caregiver_availability_locks" in self.executed[-1][0]:
            return {
                "id": self.lock_row_id,
                "plan_id": self.lock_row_plan_id,
                "status": self.lock_status,
                "is_active": self.lock_is_active,
                "released_by": self.lock_released_by,
                "released_at": self.lock_released_at,
            }

        if "FROM orders" in self.executed[-1][0]:
            return {
                "case_no": "C-1",
                "status": "洽談中",
            }

        if "FROM client_payment_transactions" in self.executed[-1][0]:
            return None

        if "FROM client_payments" in self.executed[-1][0]:
            return {
                "case_no": "C-1",
                "deposit_receivable": self.summary_receivable,
                "deposit_received": self.summary_received,
            }

        raise AssertionError(self.executed[-1][0])

    def fetchall(self):
        if self.fail_fetch_kind == "all":
            raise RuntimeError("fetchall failed")

        if "FROM caregiver_matching_plan_segments" in self.executed[-1][0]:
            return [dict(row) for row in self.segments]

        if "FROM caregiver_availability_lock_days" in self.executed[-1][0]:
            return [dict(row) for row in self.lock_days]

        if "FROM client_payment_transactions" in self.executed[-1][0]:
            return [dict(row) for row in self.transactions]

        raise AssertionError(self.executed[-1][0])

    def close(self):
        self.closed = True


class _Connection:
    def __init__(
        self,
        cursor: _Cursor,
        *,
        fail_cursor: bool = False,
        fail_commit: bool = False,
        fail_rollback: bool = False,
        fail_close: bool = False,
    ):
        self.cursor_value = cursor
        self.fail_cursor = fail_cursor
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.fail_close = fail_close
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        if self.fail_cursor:
            raise RuntimeError("cursor failed")
        return self.cursor_value

    def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("commit failed")

    def rollback(self):
        self.rollbacks += 1
        if self.fail_rollback:
            raise RuntimeError("rollback failed")

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


def _install(monkeypatch, **cursor_kwargs):
    cursor = _Cursor(**cursor_kwargs)
    connection = _Connection(cursor)
    calls = []

    monkeypatch.setattr(service, "get_connection", lambda: connection)

    def mutex(cursor_obj, ids):
        calls.append((cursor_obj, ids))
        return sorted(ids)

    monkeypatch.setattr(service, "lock_staff_occupancy_mutex", mutex)
    return connection, cursor, calls


def _default_request(**overrides):
    data = {
        "case_no": "C-1",
        "plan_id": 7,
        "lock_id": 88,
        "event_key": "event-1",
        "actor": "admin",
        "reason": "release lock",
    }
    data.update(overrides)
    return data


def _default_payload(request: dict[str, object], count: int):
    return service._build_release_event_payload(request, _snapshot(count))


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_created_for_one_through_four_segments(monkeypatch, count):
    connection, cursor, mutex_calls = _install(monkeypatch, count=count)

    result = service.release_caregiver_availability_lock(**_default_request())

    assert result["result"] == "created"
    assert result["lock_rows"] == cursor.snapshot["lock_rows"]
    assert cursor.lock_days[0]["active_marker"] == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert mutex_calls == [(cursor, [3, 4, 5, 6][:count])]
    writes = [(sql, params) for sql, params in cursor.executed if sql.startswith(("UPDATE", "INSERT"))]
    assert writes[0][0].startswith("UPDATE caregiver_availability_lock_days")
    assert writes[-1][0].startswith("INSERT INTO caregiver_availability_lock_events")


def test_exact_replay_returns_existing_for_released_lock(monkeypatch):
    connection, cursor, mutex_calls = _install(
        monkeypatch,
        count=2,
        lock_status="released",
        lock_is_active=None,
        lock_days_released=True,
        plan_status="proposed",
        lock_released_by="admin",
        lock_released_at="2026-01-01",
        existing_event=True,
    )

    result = service.release_caregiver_availability_lock(**_default_request())

    assert result["result"] == "existing"
    assert result["lock_rows"] == cursor.snapshot["lock_rows"]
    assert connection.commits == 0
    assert not [sql for sql, _ in cursor.executed if sql.startswith(("UPDATE", "INSERT"))]
    assert mutex_calls == [(cursor, [3, 4])]


def test_existing_event_mismatch_lock_id_fails_closed(monkeypatch):
    connection, _, _ = _install(monkeypatch, existing_event=True, existing_event_lock_id=99)
    with pytest.raises(ValueError, match="event key already used"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection.rollbacks == 1


def test_existing_event_actor_mismatch_fails_closed(monkeypatch):
    connection, _, _ = _install(monkeypatch, existing_event=True, existing_event_actor="other-admin")
    with pytest.raises(ValueError, match="event actor mismatch"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection.rollbacks == 1


def test_non_accepted_plan_fails_without_writes(monkeypatch):
    connection, _, _ = _install(monkeypatch, plan_status="proposed")
    with pytest.raises(ValueError, match="plan is not accepted"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection.commits == 0
    assert not [sql for sql, _ in connection.cursor_value.executed if sql.startswith(("UPDATE", "INSERT"))]


def test_released_lock_requires_existing_event_or_rejects(monkeypatch):
    connection, cursor, _ = _install(monkeypatch, lock_status="released", lock_is_active=None)
    with pytest.raises(ValueError, match="lock is not active"):
        service.release_caregiver_availability_lock(**_default_request())
    assert cursor.lock_days[0]["active_marker"] == 1
    assert connection.rollbacks == 1


def test_existing_deposit_rows_must_balance_zero(monkeypatch):
    connection, _, _ = _install(monkeypatch, summary_received="1")
    with pytest.raises(ValueError, match="deposit summary is inconsistent"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection.rollbacks == 1


def test_reversal_transaction_in_deposit_stream_fails_closed(monkeypatch):
    connection, _, _ = _install(
        monkeypatch,
        transactions=[
            {
                "id": 31,
                "transaction_type": "reversal",
                "transaction_status": "succeeded",
                "stage": "deposit",
                "amount": Decimal("100"),
                "occurred_at": date(2026, 8, 1),
                "external_reference": "tx-31",
                "reversal_of_transaction_id": 29,
            }
        ],
    )
    with pytest.raises(ValueError, match="reversal rows invalidate"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection.rollbacks == 1


def test_mismatch_lock_rows_between_lock_and_plan_fails_closed(monkeypatch):
    connection, cursor, _ = _install(monkeypatch, count=2)
    cursor.lock_days = cursor.lock_days[:1]
    with pytest.raises(ValueError, match="lock rows must match"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection.rollbacks == 1

def test_mutex_mismatch_or_exception_fails_closed(monkeypatch):
    connection, _, _ = _install(monkeypatch)
    monkeypatch.setattr(service, "lock_staff_occupancy_mutex", lambda *_: [])
    with pytest.raises(ValueError, match="mutex result does not match lock staff"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection.rollbacks == 1

    connection2, _, _ = _install(monkeypatch)
    monkeypatch.setattr(
        service,
        "lock_staff_occupancy_mutex",
        lambda *_: (_ for _ in ()).throw(RuntimeError("mutex failed")),
    )
    with pytest.raises(RuntimeError, match="mutex failed"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection2.rollbacks == 1


def test_execute_and_fetch_failures_rollback_and_close(monkeypatch):
    for fail_at in range(1, 15):
        connection, cursor, _ = _install(monkeypatch, fail_execute_at=fail_at)
        with pytest.raises(RuntimeError, match="execute failed"):
            service.release_caregiver_availability_lock(**_default_request())
        assert connection.rollbacks == 1
        assert connection.commits == 0
        assert cursor.closed and connection.closed

    connection, cursor, _ = _install(monkeypatch, fail_fetch_kind="one")
    with pytest.raises(RuntimeError, match="fetchone failed"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection.rollbacks == 1
    assert cursor.closed and connection.closed

    connection, cursor, _ = _install(monkeypatch, fail_fetch_kind="all")
    with pytest.raises(RuntimeError, match="fetchall failed"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection.rollbacks == 1
    assert cursor.closed and connection.closed


def test_update_rowcount_guard_raises(monkeypatch):
    connection, cursor, _ = _install(
        monkeypatch,
        fail_cursor_rowcount_days=0,
    )
    with pytest.raises(ValueError, match="lock day update rowcount mismatch"):
        service.release_caregiver_availability_lock(**_default_request())
    assert cursor.rowcount == 0

    connection2, cursor2, _ = _install(
        monkeypatch,
        fail_cursor_rowcount_lock=0,
    )
    with pytest.raises(ValueError, match="lock header update rowcount mismatch"):
        service.release_caregiver_availability_lock(**_default_request())
    assert cursor2.rowcount == 0

    connection3, cursor3, _ = _install(
        monkeypatch,
        fail_cursor_rowcount_plan=0,
    )
    with pytest.raises(ValueError, match="plan lifecycle update failed"):
        service.release_caregiver_availability_lock(**_default_request())
    assert cursor3.rowcount == 0


def test_connection_cursor_cleanup_failures(monkeypatch):
    monkeypatch.setattr(service, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("connect failed")))
    with pytest.raises(RuntimeError, match="connect failed"):
        service.release_caregiver_availability_lock(**_default_request())

    connection, _, _ = _install(monkeypatch)
    connection.fail_cursor = True
    with pytest.raises(RuntimeError, match="cursor failed"):
        service.release_caregiver_availability_lock(**_default_request())

    connection2, cursor2, _ = _install(monkeypatch)
    connection2.fail_commit = connection2.fail_rollback = connection2.fail_close = True
    cursor2.close = lambda: (_ for _ in ()).throw(RuntimeError("cursor close"))
    with pytest.raises(RuntimeError, match="commit failed"):
        service.release_caregiver_availability_lock(**_default_request())
    assert connection2.commits == 1
    assert connection2.rollbacks == 1


