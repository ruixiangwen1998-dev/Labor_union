from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

import services.caregiver_availability_lock_cancellation_service as service


class FakeCursor:
    def __init__(self, staff_count: int = 1, *, fail_on_update: str | None = None) -> None:
        self.staff_ids = list(range(1, staff_count + 1))
        self.fail_on_update = fail_on_update
        self.rowcount = 0
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.order_status = "洽談中"
        self.cancel_reason: str | None = None
        self.lock_status = "active"
        self.lock_is_active: int | None = 1
        self.released_by: str | None = None
        self.released_at: object | None = None
        self.event: dict[str, Any] | None = None
        self._one: Any = None
        self._many: Any = None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self.rowcount = 0
        self._one = None
        self._many = None
        if self.fail_on_update and self.fail_on_update in normalized:
            raise RuntimeError("injected failure")
        if "FROM orders WHERE case_no = %s FOR UPDATE" in normalized:
            self._one = {
                "case_no": "CASE-1",
                "status": self.order_status,
                "cancel_reason": self.cancel_reason,
            }
        elif "FROM orders WHERE case_no = %s" in normalized:
            self._one = {
                "case_no": "CASE-1",
                "status": self.order_status,
                "cancel_reason": self.cancel_reason,
            }
        elif "FROM caregiver_availability_lock_events WHERE event_key = %s" in normalized:
            self._one = None if self.event is None else dict(self.event)
        elif "JOIN caregiver_matching_plans p" in normalized:
            self._one = {
                "lock_id": 20,
                "plan_id": 10,
                "plan_id_check": 10,
                "case_no": "CASE-1",
                "plan_status": "accepted",
                "plan_is_active": 1,
                "plan_start_date": date(2026, 1, 1),
                "plan_end_date": date(2026, 1, len(self.staff_ids)),
            }
        elif "FROM caregiver_matching_plans WHERE id = %s FOR UPDATE" in normalized:
            self._one = {
                "id": 10,
                "case_no": "CASE-1",
                "status": "accepted",
                "is_active": 1,
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, len(self.staff_ids)),
            }
        elif "FROM caregiver_matching_plan_segments" in normalized:
            self._many = [
                {
                    "id": 100 + index,
                    "plan_id": 10,
                    "segment_order": index,
                    "staff_id": staff_id,
                    "assigned_start_date": date(2026, 1, index),
                    "assigned_end_date": date(2026, 1, index),
                }
                for index, staff_id in enumerate(self.staff_ids, 1)
            ]
        elif "FROM caregiver_availability_locks" in normalized:
            self._one = {
                "lock_id": 20,
                "plan_id": 10,
                "lock_status": self.lock_status,
                "lock_is_active": self.lock_is_active,
                "released_by": self.released_by,
                "released_at": self.released_at,
            }
        elif "FROM caregiver_availability_lock_days" in normalized:
            active_only = "active_marker = 1" in normalized
            self._many = [
                {
                    "segment_id": 100 + index,
                    "staff_id": staff_id,
                    "lock_date": date(2026, 1, index),
                    "active_marker": 1 if self.lock_is_active == 1 else None,
                    "released_by": self.released_by,
                    "released_at": self.released_at,
                }
                for index, staff_id in enumerate(self.staff_ids, 1)
            ]
        elif normalized.startswith("UPDATE orders"):
            self.order_status = "訂單取消"
            self.cancel_reason = params[1]
            self.rowcount = 1
        elif normalized.startswith("UPDATE caregiver_availability_lock_days"):
            self.released_by = params[0]
            self.released_at = object()
            self.rowcount = len(self.staff_ids)
        elif normalized.startswith("UPDATE caregiver_availability_locks"):
            self.lock_status = "cancelled"
            self.lock_is_active = None
            self.released_by = params[0]
            self.released_at = object()
            self.rowcount = 1
        elif normalized.startswith("INSERT INTO caregiver_availability_lock_events"):
            self.event = {
                "id": 30,
                "lock_id": params[0],
                "event_type": params[1],
                "event_key": params[2],
                "actor": params[3],
                "reason": params[4],
                "payload": params[5],
            }
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> Any:
        return self._many

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def install_fakes(monkeypatch: pytest.MonkeyPatch, cursor: FakeCursor) -> FakeConnection:
    connection = FakeConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    def mutex(actual_cursor: FakeCursor, staff_ids: list[int]) -> list[int]:
        assert actual_cursor is cursor
        assert staff_ids == sorted(cursor.staff_ids)
        return list(staff_ids)

    monkeypatch.setattr(service, "lock_staff_occupancy_mutex", mutex)
    return connection


@pytest.mark.parametrize("staff_count", [1, 2, 3, 4])
def test_cancel_one_to_four_staff_is_atomic(monkeypatch: pytest.MonkeyPatch, staff_count: int) -> None:
    cursor = FakeCursor(staff_count)
    connection = install_fakes(monkeypatch, cursor)

    result = service.cancel_caregiver_availability_lock_for_order(
        "CASE-1", "cancel-1", "admin", "customer cancelled"
    )

    assert result["result"] == "cancelled"
    assert result["case_no"] == "CASE-1"
    assert result["plan_id"] == 10
    assert result["lock_id"] == 20
    assert len(result["lock_rows"]) == staff_count
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert cursor.closed and connection.closed
    assert json.loads(cursor.event["payload"])["staff_ids"] == cursor.staff_ids


def test_exact_replay_is_reachable_and_has_no_second_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakeCursor(2)
    connection = install_fakes(monkeypatch, cursor)
    first = service.cancel_caregiver_availability_lock_for_order(
        "CASE-1", "cancel-1", "admin", "customer cancelled"
    )
    cursor.closed = False
    connection.closed = False

    second = service.cancel_caregiver_availability_lock_for_order(
        "CASE-1", "cancel-1", "admin", "customer cancelled"
    )

    assert first["result"] == "cancelled"
    assert second == {**first, "result": "existing"}
    assert connection.commits == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", "other-admin"),
        ("reason", "different reason"),
        ("event_type", "lock_released"),
    ],
)
def test_adversarial_event_reuse_fails_closed(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    cursor = FakeCursor()
    install_fakes(monkeypatch, cursor)
    service.cancel_caregiver_availability_lock_for_order(
        "CASE-1", "cancel-1", "admin", "customer cancelled"
    )
    cursor.event[field] = value

    with pytest.raises(ValueError):
        service.cancel_caregiver_availability_lock_for_order(
            "CASE-1", "cancel-1", "admin", "customer cancelled"
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE orders",
        "UPDATE caregiver_availability_lock_days",
        "UPDATE caregiver_availability_locks",
        "INSERT INTO caregiver_availability_lock_events",
    ],
)
def test_each_write_failure_rolls_back_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    cursor = FakeCursor(2, fail_on_update=statement)
    connection = install_fakes(monkeypatch, cursor)

    with pytest.raises(RuntimeError, match="injected failure"):
        service.cancel_caregiver_availability_lock_for_order(
            "CASE-1", "cancel-1", "admin", "customer cancelled"
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert cursor.closed and connection.closed
