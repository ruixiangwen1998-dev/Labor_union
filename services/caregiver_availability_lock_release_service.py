"""Atomic release of waiting-for-deposit caregiver availability lock to unbound state."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from services.caregiver_availability_lock_acquisition_helpers import normalize_plan_snapshot
from services.db_service import get_connection
from services.staff_occupancy_mutex_service import lock_staff_occupancy_mutex


def _close_once(resource: Any, state: dict[str, bool]) -> None:
    if resource is not None and not state["closed"]:
        state["closed"] = True
        try:
            resource.close()
        except BaseException:  # noqa: BLE001 - cleanup should stay best effort.
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
        raise ValueError(f"{field_name} must be a date")
    return value


def _as_decimal(value: Any, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a decimal amount")
    try:
        amount = Decimal(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal amount") from exc
    if amount < 0:
        raise ValueError(f"{field_name} must not be negative")
    return amount


def _exact_row(value: Any, expected_keys: frozenset[str], field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a dict")
    if set(value) != expected_keys:
        raise ValueError(f"{field_name} has unexpected keys")
    return value


def _assert_row_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return value


def _normalize_request(
    case_no: Any,
    plan_id: Any,
    lock_id: Any,
    event_key: Any,
    actor: Any,
    reason: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    expected = {"case_no", "plan_id", "lock_id", "event_key", "actor", "reason"}
    if set(kwargs):
        unknown = ", ".join(sorted(kwargs.keys()))
        raise ValueError(f"unexpected request fields: {unknown}")

    return {
        "case_no": _strict_str(case_no, "case_no"),
        "plan_id": _positive_int(plan_id, "plan_id"),
        "lock_id": _positive_int(lock_id, "lock_id"),
        "event_key": _strict_str(event_key, "event_key"),
        "actor": _strict_str(actor, "actor"),
        "reason": _strict_str(reason, "reason"),
    }


def _normalize_lock_day_rows(value: Any, required_active_only: bool = True) -> list[dict[str, Any]]:
    rows = _assert_row_list(value, "lock_row_rows")
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        row = _exact_row(
            row,
            frozenset(
                {
                    "segment_id",
                    "staff_id",
                    "lock_date",
                    "active_marker",
                    "released_by",
                    "released_at",
                }
            ),
            f"lock_row_rows[{index}]",
        )
        segment_id = _positive_int(row["segment_id"], f"lock_row_rows[{index}].segment_id")
        staff_id = _positive_int(row["staff_id"], f"lock_row_rows[{index}].staff_id")
        lock_date = _as_date(row["lock_date"], f"lock_row_rows[{index}].lock_date")
        active_marker = row["active_marker"]
        released_by = row["released_by"]
        released_at = row["released_at"]
        if required_active_only:
            if active_marker != 1:
                raise ValueError("lock day must be active during release preflight")
            if released_by is not None or released_at is not None:
                raise ValueError("active lock day cannot include release metadata")
        normalized.append(
            {
                "segment_id": segment_id,
                "staff_id": staff_id,
                "lock_date": lock_date,
                "active_marker": active_marker,
                "released_by": released_by,
                "released_at": released_at,
            }
        )

    normalized.sort(key=lambda item: (item["lock_date"], item["segment_id"], item["staff_id"]))
    return normalized


def _snapshot_staff_ids(lock_days: list[dict[str, Any]]) -> list[int]:
    staff_ids = sorted({row["staff_id"] for row in lock_days})
    if not 1 <= len(staff_ids) <= 4:
        raise ValueError("lock must reference one to four staff")
    return staff_ids


def _snapshot_lock_rows(lock_days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in lock_days:
        rows.append(
            {
                "segment_id": row["segment_id"],
                "staff_id": row["staff_id"],
                "lock_date": row["lock_date"].isoformat(),
            }
        )
    rows.sort(key=lambda item: (item["lock_date"], item["segment_id"], item["staff_id"]))
    return rows


def _assert_lock_rows_match(expected: list[dict[str, Any]], actual: list[dict[str, Any]], field_name: str) -> None:
    if _snapshot_lock_rows(actual) != expected:
        raise ValueError(f"{field_name} must match plan snapshot")


def _assert_lock_days_active(lock_days: list[dict[str, Any]]) -> None:
    for index, row in enumerate(lock_days):
        if row["active_marker"] != 1:
            raise ValueError(f"lock_row_rows[{index}] must be active")
        if row["released_by"] is not None or row["released_at"] is not None:
            raise ValueError(f"lock_row_rows[{index}] cannot contain release metadata")


def _assert_lock_days_released(lock_days: list[dict[str, Any]], actor: str) -> None:
    for index, row in enumerate(lock_days):
        if row["active_marker"] is not None:
            raise ValueError(f"lock_row_rows[{index}] must be released")
        if row["released_by"] != actor:
            raise ValueError(f"lock_row_rows[{index}] release actor mismatch")
        if row["released_at"] is None:
            raise ValueError(f"lock_row_rows[{index}] release timestamp missing")


def _build_release_event_payload(
    request: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "case_no": request["case_no"],
        "plan_id": request["plan_id"],
        "lock_id": request["lock_id"],
        "actor": request["actor"],
        "reason": request["reason"],
        "plan_status": "proposed",
        "lock_status": "released",
        "staff_ids": list(snapshot["staff_ids"]),
        "segments": [dict(segment) for segment in snapshot["segments"]],
        "lock_rows": [dict(row) for row in snapshot["lock_rows"]],
        "case_start_date": snapshot["case_start_date"],
        "case_end_date": snapshot["case_end_date"],
    }


def _load_prelocked_rows(
    cursor: Any,
    lock_id: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cursor.execute(
        "SELECT id, plan_id, status, is_active, released_by, released_at "
        "FROM caregiver_availability_locks WHERE id = %s",
        (lock_id,),
    )
    lock_row = _exact_row(
        cursor.fetchone(),
        frozenset({"id", "plan_id", "status", "is_active", "released_by", "released_at"}),
        "lock_row",
    )
    if _positive_int(lock_row["id"], "lock_row.id") != lock_id:
        raise ValueError("lock id mismatch")
    _positive_int(lock_row["plan_id"], "lock_row.plan_id")

    cursor.execute(
        "SELECT segment_id, staff_id, lock_date, active_marker, released_by, released_at "
        "FROM caregiver_availability_lock_days WHERE lock_id = %s ORDER BY lock_date, segment_id, staff_id",
        (lock_id,),
    )
    lock_days = _normalize_lock_day_rows(cursor.fetchall(), required_active_only=False)
    if not lock_days:
        raise ValueError("lock has no days")
    if lock_row["status"] == "active":
        if lock_row["is_active"] != 1:
            raise ValueError("active lock header lifecycle mismatch")
        if lock_row["released_by"] is not None or lock_row["released_at"] is not None:
            raise ValueError("active lock header cannot include release metadata")
        _assert_lock_days_active(lock_days)
    elif lock_row["status"] == "released":
        if lock_row["is_active"] is not None:
            raise ValueError("released lock header lifecycle mismatch")
        released_by = _strict_str(lock_row["released_by"], "lock_row.released_by")
        if lock_row["released_at"] is None:
            raise ValueError("released lock timestamp missing")
        _assert_lock_days_released(lock_days, released_by)
    else:
        raise ValueError("lock is neither active nor released")
    return lock_row, lock_days


def _assert_order_row(order_row: Any, case_no: str) -> dict[str, Any]:
    order_row = _exact_row(order_row, frozenset({"case_no", "status"}), "order_row")
    _strict_str(order_row["case_no"], "order_row.case_no")
    if order_row["case_no"] != case_no:
        raise ValueError("order case_no mismatch")
    if order_row["status"] != "洽談中":
        raise ValueError("case is not in negotiation")
    return order_row


def _assert_plan_row(plan_row: Any, case_no: str, plan_id: int) -> dict[str, Any]:
    plan_row = _exact_row(
        plan_row,
        frozenset({"id", "case_no", "status", "is_active", "start_date", "end_date"}),
        "plan_row",
    )
    if plan_row["id"] != plan_id:
        raise ValueError("plan id mismatch")
    if plan_row["case_no"] != case_no:
        raise ValueError("plan case_no mismatch")
    _strict_str(plan_row["case_no"], "plan_row.case_no")
    if _as_date(plan_row["start_date"], "plan_row.start_date") > _as_date(plan_row["end_date"], "plan_row.end_date"):
        raise ValueError("plan start_date cannot be after end_date")
    return plan_row


def _load_plan_snapshot(cursor: Any, case_no: str, plan_id: int) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    cursor.execute(
        "SELECT id, case_no, status, is_active, start_date, end_date "
        "FROM caregiver_matching_plans WHERE id = %s AND case_no = %s FOR UPDATE",
        (plan_id, case_no),
    )
    plan_row = _assert_plan_row(cursor.fetchone(), case_no, plan_id)

    cursor.execute(
        "SELECT id, plan_id, segment_order, staff_id, assigned_start_date, assigned_end_date "
        "FROM caregiver_matching_plan_segments WHERE plan_id = %s ORDER BY segment_order FOR UPDATE",
        (plan_id,),
    )
    segment_rows = _assert_row_list(cursor.fetchall(), "plan_segment_rows")
    normalized_segments: list[dict[str, Any]] = []
    for index, row in enumerate(segment_rows):
        row = _exact_row(
            row,
            frozenset({"id", "plan_id", "segment_order", "staff_id", "assigned_start_date", "assigned_end_date"}),
            f"plan_segment_rows[{index}]",
        )
        normalized_segments.append(
            {
                "id": _positive_int(row["id"], f"plan_segment_rows[{index}].id"),
                "plan_id": _positive_int(row["plan_id"], f"plan_segment_rows[{index}].plan_id"),
                "segment_order": _positive_int(
                    row["segment_order"],
                    f"plan_segment_rows[{index}].segment_order",
                ),
                "staff_id": _positive_int(row["staff_id"], f"plan_segment_rows[{index}].staff_id"),
                "assigned_start_date": _as_date(row["assigned_start_date"], f"plan_segment_rows[{index}].assigned_start_date"),
                "assigned_end_date": _as_date(row["assigned_end_date"], f"plan_segment_rows[{index}].assigned_end_date"),
            }
        )
    snapshot = normalize_plan_snapshot(
        case_no,
        plan_id,
        {
            "id": plan_row["id"],
            "case_no": plan_row["case_no"],
            "status": plan_row["status"],
            "is_active": plan_row["is_active"],
            "start_date": plan_row["start_date"],
            "end_date": plan_row["end_date"],
        },
        normalized_segments,
    )
    return plan_row, segment_rows, snapshot


def _load_lock_rows_for_update(cursor: Any, lock_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cursor.execute(
        "SELECT id, plan_id, status, is_active, released_by, released_at "
        "FROM caregiver_availability_locks WHERE id = %s FOR UPDATE",
        (lock_id,),
    )
    lock_row = _exact_row(cursor.fetchone(), frozenset({"id", "plan_id", "status", "is_active", "released_by", "released_at"}), "lock_row")
    if _positive_int(lock_row["id"], "lock_row.id") != lock_id:
        raise ValueError("lock id mismatch")
    _positive_int(lock_row["plan_id"], "lock_row.plan_id")
    cursor.execute(
        "SELECT id, segment_id, staff_id, lock_date, active_marker, released_by, released_at "
        "FROM caregiver_availability_lock_days WHERE lock_id = %s ORDER BY lock_date, segment_id, staff_id FOR UPDATE",
        (lock_id,),
    )
    lock_days = _assert_row_list(cursor.fetchall(), "lock_days")
    normalized = []
    for index, row in enumerate(lock_days):
        row = _exact_row(
            row,
            frozenset({"id", "segment_id", "staff_id", "lock_date", "active_marker", "released_by", "released_at"}),
            f"lock_days[{index}]",
        )
        normalized.append(
            {
                "id": _positive_int(row["id"], f"lock_days[{index}].id"),
                "segment_id": _positive_int(row["segment_id"], f"lock_days[{index}].segment_id"),
                "staff_id": _positive_int(row["staff_id"], f"lock_days[{index}].staff_id"),
                "lock_date": _as_date(row["lock_date"], f"lock_days[{index}].lock_date"),
                "active_marker": row["active_marker"],
                "released_by": row["released_by"],
                "released_at": row["released_at"],
            }
        )
    if not normalized:
        raise ValueError("lock has no days")
    return lock_row, normalized


def _normalize_client_payment_summary(row: Any) -> dict[str, Any]:
    row = _exact_row(row, frozenset({"case_no", "deposit_receivable", "deposit_received"}), "client_payment_summary")
    _strict_str(row["case_no"], "client_payment_summary.case_no")
    deposit_receivable = _as_decimal(row["deposit_receivable"], "client_payment_summary.deposit_receivable")
    deposit_received = _as_decimal(row["deposit_received"], "client_payment_summary.deposit_received")
    if deposit_receivable < 0 or deposit_received < 0:
        raise ValueError("deposit summary values cannot be negative")
    if deposit_received % Decimal("0.01") != Decimal("0.00"):
        raise ValueError("deposit_received precision invalid")
    return {
        "case_no": row["case_no"],
        "deposit_receivable": deposit_receivable,
        "deposit_received": deposit_received,
    }


def _normalize_deposit_transactions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        row = _exact_row(
            row,
            frozenset({"id", "transaction_type", "transaction_status", "stage", "amount", "occurred_at", "external_reference", "reversal_of_transaction_id"}),
            f"deposit_transactions[{index}]",
        )
        tx_id = _positive_int(row["id"], f"deposit_transactions[{index}].id")
        transaction_type = row["transaction_type"]
        transaction_status = row["transaction_status"]
        stage = row["stage"]
        if stage != "deposit":
            raise ValueError(f"deposit_transactions[{index}].stage must be deposit")
        if transaction_type not in {"receipt", "refund", "reversal"}:
            raise ValueError(f"deposit_transactions[{index}].transaction_type invalid")
        if transaction_status not in {"succeeded", "failed", "reversed"}:
            raise ValueError(f"deposit_transactions[{index}].transaction_status invalid")
        normalized.append(
            {
                "id": tx_id,
                "transaction_type": transaction_type,
                "transaction_status": transaction_status,
                "stage": stage,
                "amount": _as_decimal(row["amount"], f"deposit_transactions[{index}].amount"),
                "occurred_at": row["occurred_at"],
                "external_reference": row["external_reference"],
                "reversal_of_transaction_id": row["reversal_of_transaction_id"],
            }
        )
    return normalized


def _assert_zero_deposit(summary: dict[str, Any], transactions: list[dict[str, Any]]) -> None:
    net = Decimal("0.00")
    for row in transactions:
        if row["transaction_status"] != "succeeded":
            continue
        amount = row["amount"]
        if row["transaction_type"] == "receipt":
            net += amount
        elif row["transaction_type"] == "refund":
            net -= amount
        else:
            raise ValueError("deposit transaction reversal rows invalidate zero-deposit proof")

    if net != summary["deposit_received"]:
        raise ValueError("deposit summary is inconsistent with deposit transactions")
    if net != Decimal("0.00"):
        raise ValueError("existing deposit net amount must be zero")


def _load_existing_event(cursor: Any, event_key: str, lock_id: int) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT id, lock_id, event_type, event_key, actor, reason, payload "
        "FROM caregiver_availability_lock_events WHERE event_key = %s FOR UPDATE",
        (event_key,),
    )
    event_row = cursor.fetchone()
    if event_row is None:
        return None
    event_row = _exact_row(event_row, frozenset({"id", "lock_id", "event_type", "event_key", "actor", "reason", "payload"}), "event_row")
    if event_row["lock_id"] != lock_id:
        raise ValueError("event key already used for another lock")
    if event_row["event_key"] != event_key:
        raise ValueError("event key lookup mismatch")
    return event_row


def _normalize_event_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("event payload must be JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("event payload must be JSON object")
    return payload


def _existing_result(
    request: dict[str, Any],
    event_row: dict[str, Any],
    snapshot: dict[str, Any],
    plan_row: dict[str, Any],
    lock_row: dict[str, Any],
    lock_days: list[dict[str, Any]],
) -> dict[str, Any]:
    if event_row["event_type"] != "lock_released":
        raise ValueError("event key already used for non-release event")
    if event_row["actor"] != request["actor"]:
        raise ValueError("event actor mismatch")
    if event_row["reason"] != request["reason"]:
        raise ValueError("event reason mismatch")
    payload = _normalize_event_payload(event_row["payload"])
    expected_payload = _build_release_event_payload(request, snapshot)
    if payload != expected_payload:
        raise ValueError("event payload mismatch")

    if plan_row["status"] != "proposed" or plan_row["is_active"] != 1:
        raise ValueError("plan status mismatch for existing release")
    if lock_row["status"] != "released" or lock_row["is_active"] is not None:
        raise ValueError("lock status mismatch for existing release")
    if not isinstance(lock_row["released_by"], str) or not lock_row["released_by"]:
        raise ValueError("lock released_by missing")
    if lock_row["released_at"] is None:
        raise ValueError("lock released_at missing")

    _assert_lock_days_released(lock_days, request["actor"])

    return {
        "result": "existing",
        "case_no": request["case_no"],
        "plan_id": request["plan_id"],
        "lock_id": request["lock_id"],
        "plan_status": "proposed",
        "lock_status": "released",
        "lock_rows": snapshot["lock_rows"],
    }


def release_caregiver_availability_lock(
    case_no: Any,
    plan_id: Any,
    lock_id: Any,
    event_key: Any,
    actor: Any,
    reason: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Release one active waiting-for-deposit lock and revert plan to proposed."""

    request = _normalize_request(
        case_no=case_no,
        plan_id=plan_id,
        lock_id=lock_id,
        event_key=event_key,
        actor=actor,
        reason=reason,
        **kwargs,
    )
    connection = None
    cursor = None
    cursor_closed = {"closed": False}
    connection_closed = {"closed": False}
    try:
        connection = get_connection()
        cursor = connection.cursor()

        pre_lock_row, pre_lock_rows = _load_lock_rows_for_update(cursor, request["lock_id"])
        staff_ids = _snapshot_staff_ids(pre_lock_rows)
        locked_staff = lock_staff_occupancy_mutex(cursor, staff_ids)
        if locked_staff != staff_ids:
            raise ValueError("mutex result does not match lock staff")

        cursor.execute(
            "SELECT case_no, status FROM orders WHERE case_no = %s FOR UPDATE",
            (request["case_no"],),
        )
        _assert_order_row(cursor.fetchone(), request["case_no"])

        cursor.execute(
            "SELECT case_no, deposit_receivable, deposit_received "
            "FROM client_payments WHERE case_no = %s FOR UPDATE",
            (request["case_no"],),
        )
        summary = _normalize_client_payment_summary(cursor.fetchone())
        if summary["case_no"] != request["case_no"]:
            raise ValueError("client payment summary case_no mismatch")

        cursor.execute(
            "SELECT id, transaction_type, transaction_status, stage, amount, occurred_at, external_reference, reversal_of_transaction_id "
            "FROM client_payment_transactions WHERE case_no = %s AND stage = 'deposit' FOR UPDATE",
            (request["case_no"],),
        )
        transactions = _normalize_deposit_transactions(cursor.fetchall())
        _assert_zero_deposit(summary, transactions)

        plan_row, _segment_rows, snapshot = _load_plan_snapshot(
            cursor, request["case_no"], request["plan_id"]
        )
        _assert_lock_rows_match(snapshot["lock_rows"], pre_lock_rows, "lock rows")
        if pre_lock_row["plan_id"] != request["plan_id"]:
            raise ValueError("lock plan_id mismatch")

        lock_row, lock_days = _load_lock_rows_for_update(cursor, request["lock_id"])
        if lock_row["plan_id"] != request["plan_id"]:
            raise ValueError("lock plan_id mismatch")
        _assert_lock_rows_match(_snapshot_lock_rows(pre_lock_rows), lock_days, "locked lock rows")
        if _snapshot_staff_ids(lock_days) != staff_ids:
            raise ValueError("locked lock staff mismatch")

        existing_event = _load_existing_event(cursor, request["event_key"], request["lock_id"])
        if existing_event is not None:
            return _existing_result(
                request=request,
                event_row=existing_event,
                snapshot=snapshot,
                plan_row=plan_row,
                lock_row=lock_row,
                lock_days=lock_days,
            )

        if lock_row["status"] != "active" or lock_row["is_active"] != 1:
            raise ValueError("lock is not active")
        if plan_row["status"] != "accepted" or plan_row["is_active"] != 1:
            raise ValueError("plan is not accepted")

        _assert_lock_rows_match(snapshot["lock_rows"], lock_days, "lock_rows")
        _assert_lock_days_active(lock_days)
        payload = _build_release_event_payload(request, snapshot)
        cursor.execute(
            "UPDATE caregiver_availability_lock_days "
            "SET active_marker = NULL, released_by = %s, released_at = CURRENT_TIMESTAMP "
            "WHERE lock_id = %s AND active_marker = 1",
            (request["actor"], request["lock_id"]),
        )
        if cursor.rowcount != len(snapshot["lock_rows"]):
            raise ValueError("lock day update rowcount mismatch")

        cursor.execute(
            "UPDATE caregiver_availability_locks "
            "SET status = 'released', is_active = NULL, released_by = %s, released_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND plan_id = %s AND status = 'active' AND is_active = 1",
            (request["actor"], request["lock_id"], request["plan_id"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("lock header update rowcount mismatch")

        cursor.execute(
            "UPDATE caregiver_matching_plans "
            "SET status = 'proposed' "
            "WHERE id = %s AND case_no = %s AND status = 'accepted' AND is_active = 1",
            (request["plan_id"], request["case_no"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("plan lifecycle update failed")

        cursor.execute(
            "INSERT INTO caregiver_availability_lock_events "
            "(lock_id, event_type, event_key, actor, reason, payload) "
            "VALUES (%s, 'lock_released', %s, %s, %s, %s)",
            (
                request["lock_id"],
                request["event_key"],
                request["actor"],
                request["reason"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("release event insert rowcount mismatch")
        connection.commit()

        return {
            "result": "created",
            "case_no": request["case_no"],
            "plan_id": request["plan_id"],
            "lock_id": request["lock_id"],
            "plan_status": "proposed",
            "lock_status": "released",
            "lock_rows": snapshot["lock_rows"],
        }
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except BaseException:  # noqa: BLE001
                pass
        raise
    finally:
        if cursor is not None:
            _close_once(cursor, cursor_closed)
        if connection is not None:
            _close_once(connection, connection_closed)






