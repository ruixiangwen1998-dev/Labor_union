"""Atomic acquisition of a waiting-for-deposit caregiver availability lock."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from services.caregiver_availability_lock_acquisition_helpers import (
    build_acquired_event_payload,
    normalize_conflicts,
    normalize_lock_acquisition_request,
    normalize_plan_snapshot,
)
from services.db_service import get_connection
from services.staff_occupancy_mutex_service import lock_staff_occupancy_mutex


def _close_once(resource: Any, closed: dict[str, bool]) -> None:
    """Close a DB resource without allowing cleanup to mask the primary error."""
    if resource is not None and not closed["value"]:
        closed["value"] = True
        try:
            resource.close()
        except BaseException:  # noqa: BLE001 - cleanup is deliberately best effort
            pass


def _one(cursor: Any, message: str) -> dict[str, Any]:
    row = cursor.fetchone()
    if not isinstance(row, dict):
        raise ValueError(message)
    return row


def _rows(cursor: Any, message: str) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(message)
    return rows


def _canonical_snapshot(
    case_no: str,
    plan_id: int,
    order_row: dict[str, Any],
    plan_row: dict[str, Any],
    segment_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if set(order_row) != {"case_no", "status", "start_date", "end_date"}:
        raise ValueError("invalid order row")
    if order_row["case_no"] != case_no:
        raise ValueError("order case_no does not match request")
    if order_row["status"] != "洽談中":
        raise ValueError("case is not in negotiation stage")
    if set(plan_row) != {"id", "case_no", "status", "is_active", "start_date", "end_date"}:
        raise ValueError("invalid matching plan row")
    if plan_row["start_date"] != order_row["start_date"] or plan_row["end_date"] != order_row["end_date"]:
        raise ValueError("plan dates do not match order")
    return normalize_plan_snapshot(case_no, plan_id, plan_row, segment_rows)


def _load_prelock_snapshot(cursor: Any, case_no: str, plan_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read only the plan data needed to choose the shared staff mutex."""
    cursor.execute(
        "SELECT id, case_no, status, is_active, start_date, end_date "
        "FROM caregiver_matching_plans WHERE id = %s AND case_no = %s",
        (plan_id, case_no),
    )
    plan_row = _one(cursor, "matching plan not found")
    cursor.execute(
        "SELECT id, plan_id, segment_order, staff_id, assigned_start_date, assigned_end_date "
        "FROM caregiver_matching_plan_segments WHERE plan_id = %s ORDER BY segment_order",
        (plan_id,),
    )
    return plan_row, _rows(cursor, "invalid matching plan segments")


def _lock_snapshot(cursor: Any, case_no: str, plan_id: int) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    cursor.execute(
        "SELECT case_no, status, start_date, end_date FROM orders "
        "WHERE case_no = %s FOR UPDATE",
        (case_no,),
    )
    order_row = _one(cursor, "case not found")
    cursor.execute(
        "SELECT id, case_no, status, is_active, start_date, end_date "
        "FROM caregiver_matching_plans WHERE id = %s AND case_no = %s FOR UPDATE",
        (plan_id, case_no),
    )
    plan_row = _one(cursor, "matching plan not found")
    cursor.execute(
        "SELECT id, plan_id, segment_order, staff_id, assigned_start_date, assigned_end_date "
        "FROM caregiver_matching_plan_segments WHERE plan_id = %s ORDER BY segment_order FOR UPDATE",
        (plan_id,),
    )
    return order_row, plan_row, _rows(cursor, "invalid matching plan segments")


def _occupancy_conflicts(cursor: Any, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Read every occupancy owner under the mutex; each result is row-level."""
    staff_ids = tuple(snapshot["staff_ids"])
    dates = tuple(row["lock_date"] for row in snapshot["lock_rows"])
    staff_placeholders = ", ".join(["%s"] * len(staff_ids))
    date_placeholders = ", ".join(["%s"] * len(dates))
    params = staff_ids + dates
    conflicts: list[dict[str, Any]] = []
    cursor.execute(
        "SELECT id AS source_id, staff_id, assigned_start_date, assigned_end_date "
        "FROM case_staff_assignments WHERE staff_id IN (" + staff_placeholders + ") "
        "AND status <> 'cancelled' FOR UPDATE",
        staff_ids,
    )
    for row in _rows(cursor, "invalid assignment occupancy row"):
        if set(row) != {"source_id", "staff_id", "assigned_start_date", "assigned_end_date"}:
            raise ValueError("invalid assignment occupancy row")
        if (
            isinstance(row["source_id"], bool)
            or not isinstance(row["source_id"], int)
            or row["source_id"] <= 0
            or isinstance(row["staff_id"], bool)
            or not isinstance(row["staff_id"], int)
            or row["staff_id"] <= 0
            or row["assigned_start_date"].__class__ is not date
            or row["assigned_end_date"].__class__ is not date
            or row["assigned_start_date"] > row["assigned_end_date"]
        ):
            raise ValueError("invalid assignment occupancy row")
        for wanted in snapshot["lock_rows"]:
            wanted_date = date.fromisoformat(wanted["lock_date"])
            if wanted["staff_id"] == row["staff_id"] and row["assigned_start_date"] <= wanted_date <= row["assigned_end_date"]:
                conflicts.append({"staff_id": wanted["staff_id"], "lock_date": wanted_date, "source_type": "assignment", "source_id": row["source_id"]})
    cursor.execute(
        "SELECT id AS source_id, staff_id, work_date, assignment_id FROM staff_schedule "
        "WHERE staff_id IN (" + staff_placeholders + ") AND work_date IN (" + date_placeholders + ") FOR UPDATE",
        params,
    )
    for row in _rows(cursor, "invalid schedule occupancy row"):
        if set(row) != {"source_id", "staff_id", "work_date", "assignment_id"}:
            raise ValueError("invalid schedule occupancy row")
        if (
            isinstance(row["source_id"], bool)
            or not isinstance(row["source_id"], int)
            or row["source_id"] <= 0
            or isinstance(row["staff_id"], bool)
            or not isinstance(row["staff_id"], int)
            or row["staff_id"] <= 0
            or row["work_date"].__class__ is not date
        ):
            raise ValueError("invalid schedule occupancy row")
        if row["assignment_id"] is not None and (isinstance(row["assignment_id"], bool) or not isinstance(row["assignment_id"], int) or row["assignment_id"] <= 0):
            raise ValueError("invalid schedule assignment_id")
        # A NULL assignment_id is deliberately not exempt: its ownership is
        # unknown, so it is reported using the canonical schedule source type.
        conflicts.append({"staff_id": row["staff_id"], "lock_date": row["work_date"], "source_type": "schedule", "source_id": row["source_id"]})
    cursor.execute(
        "SELECT l.id FROM caregiver_availability_locks l "
        "INNER JOIN caregiver_availability_lock_days d ON d.lock_id = l.id "
        "WHERE l.status = 'active' AND l.is_active = 1 AND d.active_marker = 1 "
        "AND d.staff_id IN (" + staff_placeholders + ") AND d.lock_date IN (" + date_placeholders + ") "
        "ORDER BY l.id, d.id FOR UPDATE",
        params,
    )
    active_headers = _rows(cursor, "invalid active lock header rows")
    header_ids: list[int] = []
    for row in active_headers:
        if (
            set(row) != {"id"}
            or isinstance(row["id"], bool)
            or not isinstance(row["id"], int)
            or row["id"] <= 0
        ):
            raise ValueError("invalid active lock header rows")
        if row["id"] not in header_ids:
            header_ids.append(row["id"])
    cursor.execute(
        "SELECT d.id AS source_id, d.lock_id, d.staff_id, d.lock_date FROM caregiver_availability_lock_days d "
        "INNER JOIN caregiver_availability_locks l ON l.id = d.lock_id "
        "WHERE d.staff_id IN (" + staff_placeholders + ") AND d.lock_date IN (" + date_placeholders + ") "
        "AND l.status = 'active' AND l.is_active = 1 AND d.active_marker = 1 FOR UPDATE",
        params,
    )
    for row in _rows(cursor, "invalid active lock occupancy row"):
        if set(row) != {"source_id", "lock_id", "staff_id", "lock_date"}:
            raise ValueError("invalid active lock occupancy row")
        if (
            isinstance(row["source_id"], bool)
            or not isinstance(row["source_id"], int)
            or row["source_id"] <= 0
            or isinstance(row["lock_id"], bool)
            or not isinstance(row["lock_id"], int)
            or row["lock_id"] <= 0
            or row["lock_id"] not in header_ids
            or isinstance(row["staff_id"], bool)
            or not isinstance(row["staff_id"], int)
            or row["staff_id"] <= 0
            or row["lock_date"].__class__ is not date
        ):
            raise ValueError("invalid active lock occupancy row")
        conflicts.append({"staff_id": row["staff_id"], "lock_date": row["lock_date"], "source_type": "active_lock", "source_id": row["source_id"]})
    return normalize_conflicts(conflicts)


def _existing_result(
    cursor: Any,
    event_row: dict[str, Any],
    request: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Validate an exact event-key replay without repairing any stored state."""
    expected_keys = {"id", "lock_id", "event_type", "event_key", "actor", "reason", "payload"}
    if set(event_row) != expected_keys:
        raise ValueError("invalid availability lock event row")
    lock_id = event_row["lock_id"]
    if isinstance(lock_id, bool) or not isinstance(lock_id, int) or lock_id <= 0:
        raise ValueError("invalid availability lock event lock_id")
    if event_row["event_type"] != "lock_acquired" or event_row["event_key"] != request["event_key"] or event_row["actor"] != request["actor"] or event_row["reason"] is not None:
        raise ValueError("event_key has already been used")
    cursor.execute(
        "SELECT id, plan_id, status, is_active FROM caregiver_availability_locks WHERE id = %s FOR UPDATE",
        (lock_id,),
    )
    header = _one(cursor, "availability lock event has no lock header")
    if set(header) != {"id", "plan_id", "status", "is_active"} or header != {"id": lock_id, "plan_id": request["plan_id"], "status": "active", "is_active": 1}:
        raise ValueError("event_key has inconsistent active lock")
    cursor.execute(
        "SELECT segment_id, staff_id, lock_date FROM caregiver_availability_lock_days "
        "WHERE lock_id = %s AND active_marker = 1 ORDER BY segment_id, lock_date FOR UPDATE",
        (lock_id,),
    )
    days = _rows(cursor, "invalid availability lock day rows")
    if any(set(day) != {"segment_id", "staff_id", "lock_date"} for day in days):
        raise ValueError("invalid availability lock day rows")
    canonical_days: list[dict[str, Any]] = []
    for day in days:
        if (
            isinstance(day["segment_id"], bool)
            or not isinstance(day["segment_id"], int)
            or day["segment_id"] <= 0
            or isinstance(day["staff_id"], bool)
            or not isinstance(day["staff_id"], int)
            or day["staff_id"] <= 0
            or day["lock_date"].__class__ is not date
        ):
            raise ValueError("invalid availability lock day rows")
        canonical_days.append(
            {
                "segment_id": day["segment_id"],
                "staff_id": day["staff_id"],
                "lock_date": day["lock_date"].isoformat(),
            }
        )
    canonical_days.sort(key=lambda row: (row["lock_date"], row["segment_id"], row["staff_id"]))
    if canonical_days != snapshot["lock_rows"]:
        raise ValueError("event_key has inconsistent lock days")
    payload = event_row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid availability lock event payload") from exc
    expected_payload = build_acquired_event_payload({**request, "lock_id": lock_id}, snapshot)
    if payload != expected_payload:
        raise ValueError("event_key has inconsistent payload")
    return {"result": "existing", "lock_id": lock_id, "plan_id": request["plan_id"], "case_no": request["case_no"], "lock_rows": snapshot["lock_rows"]}


def acquire_caregiver_availability_lock(case_no: Any, plan_id: Any, event_key: Any, actor: Any) -> dict[str, Any]:
    """Atomically reserve all dates in a confirmed proposed matching plan."""
    request = normalize_lock_acquisition_request(case_no, plan_id, event_key, actor, 1)
    connection = cursor = None
    cursor_closed, connection_closed = {"value": False}, {"value": False}
    try:
        connection = get_connection()
        cursor = connection.cursor()
        preliminary_plan, preliminary_segments = _load_prelock_snapshot(cursor, request["case_no"], request["plan_id"])
        preliminary_snapshot = normalize_plan_snapshot(request["case_no"], request["plan_id"], preliminary_plan, preliminary_segments)
        locked_ids = lock_staff_occupancy_mutex(cursor, list(preliminary_snapshot["staff_ids"]))
        if locked_ids != preliminary_snapshot["staff_ids"]:
            raise ValueError("staff mutex result does not match matching plan")
        order_row, locked_plan, locked_segments = _lock_snapshot(cursor, request["case_no"], request["plan_id"])
        locked_snapshot = _canonical_snapshot(request["case_no"], request["plan_id"], order_row, locked_plan, locked_segments)
        if locked_snapshot != preliminary_snapshot:
            raise ValueError("matching plan changed while acquiring lock")
        conflicts = _occupancy_conflicts(cursor, locked_snapshot)
        cursor.execute(
            "SELECT id, lock_id, event_type, event_key, actor, reason, payload "
            "FROM caregiver_availability_lock_events WHERE event_key = %s FOR UPDATE",
            (request["event_key"],),
        )
        existing = cursor.fetchone()
        if existing is not None:
            if locked_plan["status"] != "accepted" or locked_plan["is_active"] != 1:
                raise ValueError("event_key has already been used")
            return _existing_result(cursor, existing, request, locked_snapshot)
        if locked_plan["status"] != "proposed" or locked_plan["is_active"] != 1:
            raise ValueError("matching plan is not an active proposed plan")
        if conflicts:
            raise ValueError(json.dumps({"conflicts": conflicts}, ensure_ascii=False, sort_keys=True))
        cursor.execute(
            "INSERT INTO caregiver_availability_locks (plan_id, status, is_active, created_by) "
            "VALUES (%s, 'active', 1, %s)",
            (request["plan_id"], request["actor"]),
        )
        lock_id = cursor.lastrowid
        if isinstance(lock_id, bool) or not isinstance(lock_id, int) or lock_id <= 0:
            raise ValueError("lock insert did not return a valid id")
        for row in locked_snapshot["lock_rows"]:
            cursor.execute(
                "INSERT INTO caregiver_availability_lock_days "
                "(lock_id, segment_id, staff_id, lock_date, active_marker) VALUES (%s, %s, %s, %s, 1)",
                (lock_id, row["segment_id"], row["staff_id"], row["lock_date"]),
            )
        cursor.execute(
            "UPDATE caregiver_matching_plans SET status = 'accepted', is_active = 1 "
            "WHERE id = %s AND case_no = %s AND status = 'proposed' AND is_active = 1",
            (request["plan_id"], request["case_no"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("matching plan lifecycle update failed")
        payload = build_acquired_event_payload({**request, "lock_id": lock_id}, locked_snapshot)
        cursor.execute(
            "INSERT INTO caregiver_availability_lock_events "
            "(lock_id, event_type, event_key, actor, reason, payload) VALUES (%s, 'lock_acquired', %s, %s, NULL, %s)",
            (lock_id, request["event_key"], request["actor"], json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        connection.commit()
        return {"result": "created", "lock_id": lock_id, "plan_id": request["plan_id"], "case_no": request["case_no"], "lock_rows": locked_snapshot["lock_rows"]}
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except BaseException:
                pass
        raise
    finally:
        _close_once(cursor, cursor_closed)
        _close_once(connection, connection_closed)
