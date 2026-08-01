"""Atomic cancellation of waiting-for-deposit caregiver availability locks."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from services.caregiver_availability_lock_acquisition_helpers import normalize_plan_snapshot
from services.db_service import get_connection
from services.staff_occupancy_mutex_service import lock_staff_occupancy_mutex


_ORDER_STATUS_NEGOTIATING = "洽談中"
_ORDER_STATUS_CANCELLED = "訂單取消"
_EVENT_CANCELLED = "lock_cancelled"


def _close_once(resource: Any, closed: dict[str, bool]) -> None:
    if resource is not None and not closed["value"]:
        closed["value"] = True
        try:
            resource.close()
        except BaseException:  # noqa: BLE001 - cleanup best effort
            pass


def _strict_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if value.strip() != value:
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _as_date(value: Any, field_name: str) -> date:
    if value.__class__ is not date:
        raise ValueError(f"{field_name} must be date")
    return value


def _exact_keys(row: Any, expected_keys: frozenset[str], field_name: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{field_name} must be a dict")
    if set(row) != expected_keys:
        raise ValueError(f"{field_name} has unexpected keys")
    return row


def _row_list(values: Any, field_name: str) -> list[Any]:
    if not isinstance(values, list):
        raise ValueError(f"{field_name} must be a list")
    return values


def _normalize_request(case_no: Any, event_key: Any, actor: Any, cancel_reason: Any, **kwargs: Any) -> dict[str, Any]:
    if set(kwargs):
        unknown = ", ".join(sorted(kwargs.keys()))
        raise ValueError(f"unexpected request fields: {unknown}")
    return {
        "case_no": _strict_str(case_no, "case_no"),
        "event_key": _strict_str(event_key, "event_key"),
        "actor": _strict_str(actor, "actor"),
        "cancel_reason": _strict_str(cancel_reason, "cancel_reason"),
    }


def _normalize_lock_day_rows(rows: list[dict[str, Any]], active_only: bool, field_name: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        row = _exact_keys(
            row,
            frozenset({"segment_id", "staff_id", "lock_date", "active_marker", "released_by", "released_at"}),
            f"{field_name}[{index}]",
        )
        segment_id = _positive_int(row["segment_id"], f"{field_name}[{index}].segment_id")
        staff_id = _positive_int(row["staff_id"], f"{field_name}[{index}].staff_id")
        lock_date = _as_date(row["lock_date"], f"{field_name}[{index}].lock_date")
        active_marker = row["active_marker"]
        if active_only and active_marker != 1:
            raise ValueError(f"{field_name}[{index}] must be active lock row")
        if active_marker is not None and (
            isinstance(active_marker, bool)
            or not isinstance(active_marker, int)
            or active_marker != 1
        ):
            raise ValueError(f"{field_name}[{index}].active_marker must be null or 1")

        normalized.append(
            {
                "segment_id": segment_id,
                "staff_id": staff_id,
                "lock_date": lock_date,
                "active_marker": active_marker,
                "released_by": row["released_by"],
                "released_at": row["released_at"],
            }
        )

    return sorted(
        [
            {
                "segment_id": row["segment_id"],
                "staff_id": row["staff_id"],
                "lock_date": row["lock_date"].isoformat(),
                "active_marker": row["active_marker"],
                "released_by": row["released_by"],
                "released_at": row["released_at"],
            }
            for row in normalized
        ],
        key=lambda item: (item["lock_date"], item["segment_id"], item["staff_id"]),
    )


def _normalize_plan_snapshot(plan_row: dict[str, Any], segment_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return normalize_plan_snapshot(
        plan_row["case_no"],
        plan_row["id"],
        {
            "id": plan_row["id"],
            "case_no": plan_row["case_no"],
            "status": plan_row["status"],
            "is_active": plan_row["is_active"],
            "start_date": plan_row["start_date"],
            "end_date": plan_row["end_date"],
        },
        segment_rows,
    )


def _load_case_row(cursor: Any, case_no: str) -> dict[str, Any]:
    cursor.execute(
        "SELECT case_no, status, cancel_reason FROM orders WHERE case_no = %s",
        (case_no,),
    )
    return _exact_keys(cursor.fetchone(), frozenset({"case_no", "status", "cancel_reason"}), "order_row")


def _load_preflight_active_lock(cursor: Any, case_no: str) -> dict[str, Any]:
    cursor.execute(
        "SELECT l.id AS lock_id, l.plan_id, p.id AS plan_id_check, p.case_no, p.status AS plan_status, "
        "p.is_active AS plan_is_active, p.start_date AS plan_start_date, p.end_date AS plan_end_date "
        "FROM caregiver_availability_locks l "
        "JOIN caregiver_matching_plans p ON p.id = l.plan_id "
        "WHERE p.case_no = %s AND l.status = 'active' AND l.is_active = 1 "
        "ORDER BY l.id DESC LIMIT 1",
        (case_no,),
    )
    pre_lock = _exact_keys(
        cursor.fetchone(),
        frozenset(
            {
                "lock_id",
                "plan_id",
                "plan_id_check",
                "case_no",
                "plan_status",
                "plan_is_active",
                "plan_start_date",
                "plan_end_date",
            }
        ),
        "active_lock_prelock_row",
    )
    if pre_lock["plan_id"] != pre_lock["plan_id_check"]:
        raise ValueError("plan id mismatch while loading preflight lock")
    if pre_lock["case_no"] != case_no:
        raise ValueError("case_no mismatch while loading preflight lock")
    _strict_str(pre_lock["plan_status"], "plan_status")
    _positive_int(pre_lock["plan_id"], "plan_id")
    _as_date(pre_lock["plan_start_date"], "plan_start_date")
    _as_date(pre_lock["plan_end_date"], "plan_end_date")
    if pre_lock["plan_start_date"] > pre_lock["plan_end_date"]:
        raise ValueError("plan period is invalid while loading preflight lock")
    if pre_lock["plan_is_active"] != 1:
        raise ValueError("preflight matching plan is not active")

    cursor.execute(
        "SELECT id, plan_id, segment_order, staff_id, assigned_start_date, assigned_end_date "
        "FROM caregiver_matching_plan_segments "
        "WHERE plan_id = %s ORDER BY segment_order",
        (pre_lock["plan_id"],),
    )
    segment_rows = _row_list(cursor.fetchall(), "preflight_plan_segments")
    if not segment_rows:
        raise ValueError("no plan segments for active lock")

    normalized_segments: list[dict[str, Any]] = []
    for index, row in enumerate(segment_rows):
        row = _exact_keys(
            row,
            frozenset({"id", "plan_id", "segment_order", "staff_id", "assigned_start_date", "assigned_end_date"}),
            f"preflight_plan_segments[{index}]",
        )
        normalized_segments.append(
            {
                "id": _positive_int(row["id"], f"preflight_plan_segments[{index}].id"),
                "plan_id": _positive_int(row["plan_id"], f"preflight_plan_segments[{index}].plan_id"),
                "segment_order": _positive_int(row["segment_order"], f"preflight_plan_segments[{index}].segment_order"),
                "staff_id": _positive_int(row["staff_id"], f"preflight_plan_segments[{index}].staff_id"),
                "assigned_start_date": _as_date(row["assigned_start_date"], f"preflight_plan_segments[{index}].assigned_start_date"),
                "assigned_end_date": _as_date(row["assigned_end_date"], f"preflight_plan_segments[{index}].assigned_end_date"),
            }
        )

    cursor.execute(
        "SELECT segment_id, staff_id, lock_date, active_marker, released_by, released_at "
        "FROM caregiver_availability_lock_days "
        "WHERE lock_id = %s AND active_marker = 1 "
        "ORDER BY lock_date, segment_id, staff_id",
        (pre_lock["lock_id"],),
    )
    lock_day_rows = _normalize_lock_day_rows(
        _row_list(cursor.fetchall(), "preflight_active_lock_days"),
        active_only=True,
        field_name="preflight_active_lock_days",
    )
    if not lock_day_rows:
        raise ValueError("active lock has no active days")

    snapshot_plan = _normalize_plan_snapshot(
        {
            "id": pre_lock["plan_id"],
            "case_no": case_no,
            "status": pre_lock["plan_status"],
            "is_active": pre_lock["plan_is_active"],
            "start_date": pre_lock["plan_start_date"],
            "end_date": pre_lock["plan_end_date"],
        },
        normalized_segments,
    )
    snapshot_plan["lock_rows"] = lock_day_rows

    return {
        "lock_id": pre_lock["lock_id"],
        "plan_id": pre_lock["plan_id"],
        "plan_status": pre_lock["plan_status"],
        "plan_row": {
            "id": pre_lock["plan_id"],
            "case_no": case_no,
            "status": pre_lock["plan_status"],
            "is_active": pre_lock["plan_is_active"],
            "start_date": pre_lock["plan_start_date"],
            "end_date": pre_lock["plan_end_date"],
        },
        "segment_rows": normalized_segments,
        "active_lock_rows": lock_day_rows,
        "plan_snapshot": snapshot_plan,
        "lock_rows": lock_day_rows,
    }


def _load_event_for_key(cursor: Any, event_key: str, for_update: bool = False) -> dict[str, Any] | None:
    sql = (
        "SELECT id, lock_id, event_type, event_key, actor, reason, payload "
        "FROM caregiver_availability_lock_events WHERE event_key = %s"
    )
    if for_update:
        sql += " FOR UPDATE"
    cursor.execute(sql, (event_key,))
    event_row = cursor.fetchone()
    if event_row is None:
        return None
    return _exact_keys(
        event_row,
        frozenset({"id", "lock_id", "event_type", "event_key", "actor", "reason", "payload"}),
        "event_row",
    )


def _load_event_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("event payload must be JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("event payload must be JSON object")
    return _exact_keys(
        payload,
        frozenset(
            {
                "case_no",
                "plan_id",
                "lock_id",
                "actor",
                "cancel_reason",
                "order_status",
                "plan_status",
                "plan_is_active",
                "staff_ids",
                "segments",
                "lock_rows",
                "case_start_date",
                "case_end_date",
            }
        ),
        "event_payload",
    )


def _load_lock_state(
    cursor: Any,
    case_no: str,
    plan_id: int,
    lock_id: int,
) -> dict[str, Any]:
    cursor.execute(
        "SELECT case_no, status, cancel_reason FROM orders WHERE case_no = %s FOR UPDATE",
        (case_no,),
    )
    order_row = _exact_keys(cursor.fetchone(), frozenset({"case_no", "status", "cancel_reason"}), "order_row")
    if order_row["case_no"] != case_no:
        raise ValueError("order case_no mismatch")

    cursor.execute(
        "SELECT id, case_no, status, is_active, start_date, end_date "
        "FROM caregiver_matching_plans WHERE id = %s FOR UPDATE",
        (plan_id,),
    )
    plan_row = _exact_keys(
        cursor.fetchone(),
        frozenset({"id", "case_no", "status", "is_active", "start_date", "end_date"}),
        "plan_row",
    )
    if plan_row["id"] != plan_id or plan_row["case_no"] != case_no:
        raise ValueError("plan row mismatch")

    cursor.execute(
        "SELECT id, plan_id, segment_order, staff_id, assigned_start_date, assigned_end_date "
        "FROM caregiver_matching_plan_segments WHERE plan_id = %s ORDER BY segment_order FOR UPDATE",
        (plan_id,),
    )
    segment_rows = _row_list(cursor.fetchall(), "plan_segments")
    normalized_segments: list[dict[str, Any]] = []
    for index, row in enumerate(segment_rows):
        row = _exact_keys(
            row,
            frozenset({"id", "plan_id", "segment_order", "staff_id", "assigned_start_date", "assigned_end_date"}),
            f"plan_segments[{index}]",
        )
        normalized_segments.append(
            {
                "id": _positive_int(row["id"], f"plan_segments[{index}].id"),
                "plan_id": _positive_int(row["plan_id"], f"plan_segments[{index}].plan_id"),
                "segment_order": _positive_int(row["segment_order"], f"plan_segments[{index}].segment_order"),
                "staff_id": _positive_int(row["staff_id"], f"plan_segments[{index}].staff_id"),
                "assigned_start_date": _as_date(row["assigned_start_date"], f"plan_segments[{index}].assigned_start_date"),
                "assigned_end_date": _as_date(row["assigned_end_date"], f"plan_segments[{index}].assigned_end_date"),
            }
        )

    cursor.execute(
        "SELECT id AS lock_id, plan_id, status AS lock_status, is_active AS lock_is_active, "
        "released_by, released_at "
        "FROM caregiver_availability_locks "
        "WHERE id = %s AND plan_id = %s FOR UPDATE",
        (lock_id, plan_id),
    )
    lock_row = _exact_keys(
        cursor.fetchone(),
        frozenset({"lock_id", "plan_id", "lock_status", "lock_is_active", "released_by", "released_at"}),
        "lock_row",
    )
    if lock_row["lock_id"] != lock_id or lock_row["plan_id"] != plan_id:
        raise ValueError("lock row mismatch")

    cursor.execute(
        "SELECT segment_id, staff_id, lock_date, active_marker, released_by, released_at "
        "FROM caregiver_availability_lock_days WHERE lock_id = %s ORDER BY lock_date, segment_id, staff_id FOR UPDATE",
        (lock_id,),
    )
    day_rows = _normalize_lock_day_rows(
        _row_list(cursor.fetchall(), "lock_days"),
        active_only=False,
        field_name="lock_days",
    )
    if not day_rows:
        raise ValueError("lock has no days")

    normalized = _normalize_plan_snapshot(
        {
            "id": plan_row["id"],
            "case_no": case_no,
            "status": plan_row["status"],
            "is_active": _positive_int(plan_row["is_active"], "plan_row.is_active")
            if plan_row["is_active"] is not None
            else 0,
            "start_date": _as_date(plan_row["start_date"], "plan_row.start_date"),
            "end_date": _as_date(plan_row["end_date"], "plan_row.end_date"),
        },
        normalized_segments,
    )
    normalized["lock_rows"] = day_rows
    return {
        "order_row": {
            "case_no": _strict_str(order_row["case_no"], "order_row.case_no"),
            "status": _strict_str(order_row["status"], "order_row.status"),
            "cancel_reason": order_row["cancel_reason"],
        },
        "lock_row": {
            "id": lock_row["lock_id"],
            "plan_id": plan_id,
            "status": _strict_str(lock_row["lock_status"], "lock_row.status"),
            "is_active": lock_row["lock_is_active"],
            "released_by": lock_row["released_by"],
            "released_at": lock_row["released_at"],
        },
        "plan_row": {
            "id": _positive_int(plan_row["id"], "plan_row.id"),
            "case_no": _strict_str(plan_row["case_no"], "plan_row.case_no"),
            "status": _strict_str(plan_row["status"], "plan_row.status"),
            "is_active": plan_row["is_active"],
            "start_date": _as_date(plan_row["start_date"], "plan_row.start_date"),
            "end_date": _as_date(plan_row["end_date"], "plan_row.end_date"),
        },
        "segment_rows": normalized_segments,
        "lock_days": day_rows,
        "plan_snapshot": normalized,
    }


def _build_cancelled_event_payload(
    case_no: str,
    plan_id: int,
    lock_id: int,
    actor: str,
    cancel_reason: str,
    order_status: str,
    plan_row: dict[str, Any],
    plan_snapshot: dict[str, Any],
    lock_day_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "case_no": case_no,
        "plan_id": plan_id,
        "lock_id": lock_id,
        "actor": actor,
        "cancel_reason": cancel_reason,
        "order_status": order_status,
        "plan_status": plan_row["status"],
        "plan_is_active": plan_row["is_active"],
        "staff_ids": list(plan_snapshot["staff_ids"]),
        "segments": [dict(row) for row in plan_snapshot["segments"]],
        "lock_rows": [dict(row) for row in lock_day_rows],
        "case_start_date": plan_snapshot["case_start_date"],
        "case_end_date": plan_snapshot["case_end_date"],
    }


def _assert_cancel_payload_match(request: dict[str, Any], state: dict[str, Any], event_payload: dict[str, Any], event_row: dict[str, Any]) -> None:
    if event_row["event_type"] != _EVENT_CANCELLED:
        raise ValueError("event_key has already been used for different lifecycle event")
    if event_row["actor"] != request["actor"]:
        raise ValueError("event actor mismatch for existing cancellation")
    if _strict_str(event_row["reason"], "event_row.reason") != request["cancel_reason"]:
        raise ValueError("event reason mismatch for existing cancellation")
    payload_lock_rows = _row_list(event_payload["lock_rows"], "event_payload.lock_rows")
    canonical_payload_lock_rows: list[dict[str, Any]] = []
    for index, row in enumerate(payload_lock_rows):
        row = _exact_keys(
            row,
            frozenset({"segment_id", "staff_id", "lock_date", "active_marker", "released_by", "released_at"}),
            f"event_payload.lock_rows[{index}]",
        )
        if row["active_marker"] != 1 or row["released_by"] is not None or row["released_at"] is not None:
            raise ValueError("event payload lock row is not the immutable active snapshot")
        canonical_payload_lock_rows.append(dict(row))
    payload_identities = [
        (row["segment_id"], row["staff_id"], row["lock_date"])
        for row in canonical_payload_lock_rows
    ]
    state_identities = [
        (row["segment_id"], row["staff_id"], row["lock_date"])
        for row in state["lock_days"]
    ]
    if payload_identities != state_identities:
        raise ValueError("event payload lock row identity mismatch")
    expected_payload = _build_cancelled_event_payload(
        request["case_no"],
        state["plan_row"]["id"],
        state["lock_row"]["id"],
        request["actor"],
        request["cancel_reason"],
        _ORDER_STATUS_CANCELLED,
        state["plan_row"],
        state["plan_snapshot"],
        canonical_payload_lock_rows,
    )
    if _strict_str(event_payload["case_no"], "event_payload.case_no") != expected_payload["case_no"]:
        raise ValueError("event payload case mismatch")
    if event_payload != expected_payload:
        raise ValueError("event payload mismatch for existing cancellation")


def _assert_exact_state_match(preflight_plan_snapshot: dict[str, Any], locked_plan_snapshot: dict[str, Any]) -> None:
    if preflight_plan_snapshot["case_no"] != locked_plan_snapshot["case_no"]:
        raise ValueError("case_no mismatch")
    if preflight_plan_snapshot["plan_id"] != locked_plan_snapshot["plan_id"]:
        raise ValueError("plan_id mismatch")
    if preflight_plan_snapshot["staff_ids"] != locked_plan_snapshot["staff_ids"]:
        raise ValueError("staff_ids mismatch")
    if preflight_plan_snapshot["lock_rows"] != locked_plan_snapshot["lock_rows"]:
        raise ValueError("lock snapshot mismatch")


def _existing_result(
    request: dict[str, Any],
    state: dict[str, Any],
    event_row: dict[str, Any],
) -> dict[str, Any]:
    if state["order_row"]["status"] != _ORDER_STATUS_CANCELLED:
        raise ValueError("existing cancellation event but order is not cancelled")
    if _strict_str(state["order_row"]["cancel_reason"], "order_row.cancel_reason") != request["cancel_reason"]:
        raise ValueError("existing cancellation event but cancel reason mismatched")
    if state["lock_row"]["status"] != "cancelled":
        raise ValueError("existing cancellation event but lock not cancelled")
    if state["lock_row"]["is_active"] is not None:
        raise ValueError("existing cancellation event but lock_is_active is not null")
    if state["lock_row"]["released_by"] != request["actor"]:
        raise ValueError("existing cancellation event actor mismatch")
    if state["lock_row"]["released_at"] is None:
        raise ValueError("existing cancellation event missing lock released_at")
    if state["plan_row"]["status"] != "accepted":
        raise ValueError("existing cancellation event plan status mismatch")

    for row in state["lock_days"]:
        if row["active_marker"] is not None:
            raise ValueError("existing cancellation event but some lock day is still active")
        if row["released_by"] != request["actor"]:
            raise ValueError("existing cancellation event actor mismatch on lock day")
        if row["released_at"] is None:
            raise ValueError("existing cancellation event missing lock day released_at")

    payload = _load_event_payload(event_row["payload"])
    _assert_cancel_payload_match(request=request, state=state, event_payload=payload, event_row=event_row)
    return {
        "result": "existing",
        "case_no": request["case_no"],
        "plan_id": state["plan_row"]["id"],
        "lock_id": state["lock_row"]["id"],
        "order_status": _ORDER_STATUS_CANCELLED,
        "cancel_reason": request["cancel_reason"],
        "lock_rows": payload["lock_rows"],
    }


def cancel_caregiver_availability_lock_for_order(
    case_no: Any,
    event_key: Any,
    actor: Any,
    cancel_reason: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    request = _normalize_request(case_no, event_key, actor, cancel_reason, **kwargs)
    connection = cursor = None
    cursor_closed = {"value": False}
    connection_closed = {"value": False}

    try:
        connection = get_connection()
        cursor = connection.cursor()

        order_row = _load_case_row(cursor, request["case_no"])
        existing_event = _load_event_for_key(cursor, request["event_key"])
        if existing_event is not None:
            payload = _load_event_payload(existing_event["payload"])
            payload_plan_id = _positive_int(payload["plan_id"], "event_payload.plan_id")
            payload_lock_id = _positive_int(payload["lock_id"], "event_payload.lock_id")
            existing_lock_staff_ids = [
                _positive_int(value, "event_payload.staff_ids") for value in _row_list(payload["staff_ids"], "event_payload.staff_ids")
            ]
            if len(existing_lock_staff_ids) != len(set(existing_lock_staff_ids)):
                raise ValueError("event payload contains duplicate staff_ids")
            locked_staff_ids = lock_staff_occupancy_mutex(cursor, sorted(existing_lock_staff_ids))
            if locked_staff_ids != sorted(existing_lock_staff_ids):
                raise ValueError("mutex result mismatch for existing event")
            existing_state = _load_lock_state(
                cursor,
                request["case_no"],
                payload_plan_id,
                payload_lock_id,
            )
            locked_event = _load_event_for_key(cursor, request["event_key"], for_update=True)
            if locked_event is None or locked_event != existing_event:
                raise ValueError("event changed while acquiring cancellation locks")
            return _existing_result(request, existing_state, locked_event)

        if order_row["status"] != _ORDER_STATUS_NEGOTIATING:
            raise ValueError("case is not in negotiation")
        preflight = _load_preflight_active_lock(cursor, request["case_no"])
        locked_staff_ids = lock_staff_occupancy_mutex(cursor, preflight["plan_snapshot"]["staff_ids"])
        if locked_staff_ids != preflight["plan_snapshot"]["staff_ids"]:
            raise ValueError("mutex result does not match plan staff")

        state = _load_lock_state(
            cursor,
            request["case_no"],
            preflight["plan_id"],
            preflight["lock_id"],
        )
        _assert_exact_state_match(preflight["plan_snapshot"], state["plan_snapshot"])

        locked_event = _load_event_for_key(cursor, request["event_key"], for_update=True)
        if locked_event is not None:
            return _existing_result(request, state, locked_event)

        if state["order_row"]["status"] != _ORDER_STATUS_NEGOTIATING:
            raise ValueError("case is not in negotiation after locking")
        if state["plan_row"]["status"] != "accepted" or state["plan_row"]["is_active"] != 1:
            raise ValueError("plan is not accepted for lock cancellation")
        if state["lock_row"]["status"] != "active" or state["lock_row"]["is_active"] != 1:
            raise ValueError("lock is not active")

        active_days = [row for row in state["lock_days"] if row["active_marker"] == 1]
        if len(active_days) != len(preflight["active_lock_rows"]):
            raise ValueError("active lock rows mismatch before cancellation")

        payload = _build_cancelled_event_payload(
            request["case_no"],
            state["plan_row"]["id"],
            state["lock_row"]["id"],
            request["actor"],
            request["cancel_reason"],
            _ORDER_STATUS_CANCELLED,
            state["plan_row"],
            state["plan_snapshot"],
            preflight["lock_rows"],
        )

        cursor.execute(
            "UPDATE orders SET status = %s, cancel_reason = %s WHERE case_no = %s",
            (_ORDER_STATUS_CANCELLED, request["cancel_reason"], request["case_no"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("order update rowcount mismatch")

        cursor.execute(
            "UPDATE caregiver_availability_lock_days "
            "SET active_marker = NULL, released_by = %s, released_at = CURRENT_TIMESTAMP "
            "WHERE lock_id = %s AND active_marker = 1",
            (request["actor"], state["lock_row"]["id"]),
        )
        if cursor.rowcount != len(active_days):
            raise ValueError("lock day update rowcount mismatch")

        cursor.execute(
            "UPDATE caregiver_availability_locks "
            "SET status = 'cancelled', is_active = NULL, released_by = %s, released_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND status = 'active' AND is_active = 1",
            (request["actor"], state["lock_row"]["id"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("lock header update rowcount mismatch")

        cursor.execute(
            "INSERT INTO caregiver_availability_lock_events "
            "(lock_id, event_type, event_key, actor, reason, payload) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                state["lock_row"]["id"],
                _EVENT_CANCELLED,
                request["event_key"],
                request["actor"],
                request["cancel_reason"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )
        connection.commit()
        return {
            "result": "cancelled",
            "case_no": request["case_no"],
            "plan_id": state["plan_row"]["id"],
            "lock_id": state["lock_row"]["id"],
            "order_status": _ORDER_STATUS_CANCELLED,
            "cancel_reason": request["cancel_reason"],
            "lock_rows": preflight["lock_rows"],
        }
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except BaseException:  # noqa: BLE001
                pass
        raise
    finally:
        _close_once(cursor, cursor_closed)
        _close_once(connection, connection_closed)
