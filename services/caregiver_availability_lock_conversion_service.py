"""Convert a paid caregiver availability lock into formal assignments atomically."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from services.caregiver_availability_lock_acquisition_helpers import normalize_plan_snapshot
from services.db_service import get_connection
from services.multi_caregiver_assignment_rules import validate_assignment_plan_transition
from services.multi_caregiver_schedule_generation import generate_assignment_schedule_in_transaction
from services.staff_occupancy_mutex_service import lock_staff_occupancy_mutex


def _strict_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-empty string without surrounding whitespace")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _exact_date(value: Any, field_name: str) -> date:
    if value.__class__ is not date:
        raise ValueError(f"{field_name} must be a date")
    return value


def _decimal(value: Any, field_name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{field_name} must be {'positive' if positive else 'non-negative'}")
    if value.as_tuple().exponent < -2:
        raise ValueError(f"{field_name} must have at most two decimal places")
    return value.quantize(Decimal("0.01"))


def _database_decimal(value: Any, field_name: str, *, positive: bool = False) -> Decimal:
    """Accept Decimal or integral DB values while rejecting float coercion."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must be a non-float database number")
    if isinstance(value, int):
        value = Decimal(value)
    return _decimal(value, field_name, positive=positive)


def _row(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} has unexpected keys")
    return value


def _rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _close(resource: Any) -> None:
    if resource is not None:
        try:
            resource.close()
        except BaseException:  # noqa: BLE001 - never mask the transaction error.
            pass


def _normalize_terms(value: Any) -> dict[int, dict[str, Decimal]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise ValueError("assignment_terms must be a list containing one to four terms")
    result: dict[int, dict[str, Decimal]] = {}
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"segment_id", "hourly_rate", "floor_fee_allocated"}:
            raise ValueError(f"assignment_terms[{index}] has unexpected keys")
        segment_id = _positive_int(item["segment_id"], f"assignment_terms[{index}].segment_id")
        if segment_id in result:
            raise ValueError("assignment_terms contains duplicate segment_id")
        result[segment_id] = {
            "hourly_rate": _decimal(item["hourly_rate"], f"assignment_terms[{index}].hourly_rate", positive=True),
            "floor_fee_allocated": _decimal(item["floor_fee_allocated"], f"assignment_terms[{index}].floor_fee_allocated"),
        }
    return result


def _normalize_request(
    case_no: Any,
    lock_id: Any,
    event_key: Any,
    actor: Any,
    reason: Any,
    assignment_terms: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    if kwargs:
        raise ValueError(f"unknown request fields: {', '.join(sorted(kwargs))}")
    return {
        "case_no": _strict_string(case_no, "case_no"),
        "lock_id": _positive_int(lock_id, "lock_id"),
        "event_key": _strict_string(event_key, "event_key"),
        "actor": _strict_string(actor, "actor"),
        "reason": _strict_string(reason, "reason"),
        "terms": _normalize_terms(assignment_terms),
    }


def _load_preflight_lock_days(
    cursor: Any,
    lock_id: int,
    *,
    active_only: bool = True,
    for_update: bool = True,
) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT id, segment_id, staff_id, lock_date, active_marker, released_by, released_at "
        "FROM caregiver_availability_lock_days WHERE lock_id = %s ORDER BY lock_date, segment_id, staff_id"
        + (" FOR UPDATE" if for_update else ""),
        (lock_id,),
    )
    rows = _rows(cursor.fetchall(), "preflight_lock_days")
    if not rows:
        raise ValueError("lock has no days")
    normalized = []
    for index, value in enumerate(rows):
        value = _row(value, frozenset({"id", "segment_id", "staff_id", "lock_date", "active_marker", "released_by", "released_at"}), f"preflight_lock_days[{index}]")
        if active_only and (value["active_marker"] != 1 or value["released_by"] is not None or value["released_at"] is not None):
            raise ValueError("lock day is not active")
        normalized.append({
            "id": _positive_int(value["id"], "lock_day.id"),
            "segment_id": _positive_int(value["segment_id"], "lock_day.segment_id"),
            "staff_id": _positive_int(value["staff_id"], "lock_day.staff_id"),
            "lock_date": _exact_date(value["lock_date"], "lock_day.lock_date"),
            "active_marker": value["active_marker"],
            "released_by": value["released_by"],
            "released_at": value["released_at"],
        })
    return normalized


def _validate_deposit_transactions(transaction_rows: list[dict[str, Any]], received: Decimal) -> None:
    net = Decimal("0.00")
    for index, tx in enumerate(transaction_rows):
        tx = _row(
            tx,
            frozenset(
                {
                    "transaction_type",
                    "transaction_status",
                    "stage",
                    "amount",
                    "reversal_of_transaction_id",
                }
            ),
            f"deposit_transactions[{index}]",
        )
        if tx["stage"] != "deposit" or tx["transaction_status"] not in {"succeeded", "failed", "reversed"}:
            raise ValueError("deposit transaction is invalid")
        amount = _database_decimal(tx["amount"], f"deposit_transactions[{index}].amount", positive=True)
        if tx["transaction_status"] != "succeeded":
            continue
        if tx["transaction_type"] == "receipt":
            if tx["reversal_of_transaction_id"] is not None:
                raise ValueError("deposit receipt cannot reverse another transaction")
            net += amount
        elif tx["transaction_type"] == "refund":
            _positive_int(
                tx["reversal_of_transaction_id"],
                f"deposit_transactions[{index}].reversal_of_transaction_id",
            )
            net -= amount
        else:
            raise ValueError("deposit transaction type is invalid")
    if net != received:
        raise ValueError("deposit ledger is inconsistent")


def _load_state(cursor: Any, request: dict[str, Any]) -> dict[str, Any]:
    cursor.execute("SELECT CURRENT_DATE() AS current_date FOR UPDATE")
    current = _row(cursor.fetchone(), frozenset({"current_date"}), "database_current_date")
    current_date = _exact_date(current["current_date"], "database_current_date.current_date")

    cursor.execute(
        "SELECT case_no, status, start_date, end_date, service_days, service_hours_per_day, floor_fee "
        "FROM orders WHERE case_no = %s FOR UPDATE",
        (request["case_no"],),
    )
    order = _row(
        cursor.fetchone(),
        frozenset(
            {
                "case_no",
                "status",
                "start_date",
                "end_date",
                "service_days",
                "service_hours_per_day",
                "floor_fee",
            }
        ),
        "order",
    )
    if order["case_no"] != request["case_no"] or order["status"] != "洽談中":
        raise ValueError("case is not eligible for conversion")
    case_start = _exact_date(order["start_date"], "order.start_date")
    case_end = _exact_date(order["end_date"], "order.end_date")
    service_days = _positive_int(order["service_days"], "order.service_days")
    daily_hours = _database_decimal(order["service_hours_per_day"], "order.service_hours_per_day", positive=True)
    floor_fee = _database_decimal(order["floor_fee"], "order.floor_fee")
    target_hours = daily_hours * service_days

    cursor.execute(
        "SELECT case_no, deposit_receivable, deposit_received "
        "FROM client_payments WHERE case_no = %s FOR UPDATE",
        (request["case_no"],),
    )
    payment = _row(cursor.fetchone(), frozenset({"case_no", "deposit_receivable", "deposit_received"}), "client_payment")
    receivable = _database_decimal(payment["deposit_receivable"], "client_payment.deposit_receivable", positive=True)
    received = _database_decimal(payment["deposit_received"], "client_payment.deposit_received", positive=True)
    if payment["case_no"] != request["case_no"] or received != receivable:
        raise ValueError("deposit is not fully confirmed")

    cursor.execute(
        "SELECT transaction_type, transaction_status, stage, amount, reversal_of_transaction_id "
        "FROM client_payment_transactions WHERE case_no = %s AND stage = 'deposit' FOR UPDATE",
        (request["case_no"],),
    )
    transaction_rows = _rows(cursor.fetchall(), "deposit_transactions")
    _validate_deposit_transactions(transaction_rows, received)

    cursor.execute(
        "SELECT id, plan_id, status, is_active, released_by, released_at "
        "FROM caregiver_availability_locks WHERE id = %s FOR UPDATE",
        (request["lock_id"],),
    )
    lock = _row(cursor.fetchone(), frozenset({"id", "plan_id", "status", "is_active", "released_by", "released_at"}), "lock")
    if (
        lock["id"] != request["lock_id"]
        or lock["status"] != "active"
        or lock["is_active"] != 1
        or lock["released_by"] is not None
        or lock["released_at"] is not None
    ):
        raise ValueError("lock is not active")
    plan_id = _positive_int(lock["plan_id"], "lock.plan_id")

    cursor.execute(
        "SELECT id, case_no, status, is_active, start_date, end_date "
        "FROM caregiver_matching_plans WHERE id = %s AND case_no = %s FOR UPDATE",
        (plan_id, request["case_no"]),
    )
    plan = _row(cursor.fetchone(), frozenset({"id", "case_no", "status", "is_active", "start_date", "end_date"}), "plan")
    cursor.execute(
        "SELECT id, plan_id, segment_order, staff_id, assigned_start_date, assigned_end_date "
        "FROM caregiver_matching_plan_segments WHERE plan_id = %s ORDER BY segment_order FOR UPDATE",
        (plan_id,),
    )
    segments = _rows(cursor.fetchall(), "plan_segments")
    snapshot = normalize_plan_snapshot(request["case_no"], plan_id, plan, segments)
    if plan["status"] != "accepted" or plan["is_active"] != 1:
        raise ValueError("plan is not accepted")
    if case_start != snapshot["start_date"] or case_end != snapshot["end_date"]:
        raise ValueError("order and plan dates differ")
    if set(request["terms"]) != {segment["segment_id"] for segment in snapshot["segments"]}:
        raise ValueError("assignment_terms must exactly match plan segments")

    lock_days = _load_preflight_lock_days(cursor, request["lock_id"])
    expected_lock_days = {(item["segment_id"], item["staff_id"], item["lock_date"]) for item in lock_days}
    snapshot_days = {(item["segment_id"], item["staff_id"], date.fromisoformat(item["lock_date"])) for item in snapshot["lock_rows"]}
    if expected_lock_days != snapshot_days:
        raise ValueError("lock days do not match accepted plan")
    if any(item["lock_date"] <= current_date for item in lock_days):
        raise ValueError("historical or current lock dates cannot be converted")

    cursor.execute(
        "SELECT id, case_no, staff_id, assignment_sequence, assigned_start_date, assigned_end_date, status "
        "FROM case_staff_assignments WHERE case_no = %s FOR UPDATE",
        (request["case_no"],),
    )
    existing_assignments = _rows(cursor.fetchall(), "existing_assignments")
    if existing_assignments:
        raise ValueError("case already has assignments; partial conversion is forbidden")
    return {
        "current_date": current_date,
        "order": {
            "start_date": case_start,
            "end_date": case_end,
            "service_days": service_days,
            "daily_hours": daily_hours,
            "target_hours": target_hours,
            "floor_fee": floor_fee,
        },
        "plan_id": plan_id,
        "snapshot": snapshot,
        "lock_days": lock_days,
    }


def _payload(request: dict[str, Any], state: dict[str, Any], assignments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_no": request["case_no"],
        "lock_id": request["lock_id"],
        "plan_id": state["plan_id"],
        "terms": [
            {"segment_id": segment_id, **{key: str(value) for key, value in request["terms"][segment_id].items()}}
            for segment_id in sorted(request["terms"])
        ],
        "assignments": assignments,
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    if value.__class__ is date:
        return value.isoformat()
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _validate_existing_conversion(
    cursor: Any,
    request: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    if set(payload) != {"case_no", "lock_id", "plan_id", "terms", "assignments"}:
        raise ValueError("existing event payload has unexpected keys")
    plan_id = _positive_int(payload["plan_id"], "existing payload plan_id")
    assignments = _rows(payload["assignments"], "existing payload assignments")
    if not assignments:
        raise ValueError("existing conversion has no assignments")

    cursor.execute(
        "SELECT id, plan_id, status, is_active, released_by, released_at "
        "FROM caregiver_availability_locks WHERE id = %s FOR UPDATE",
        (request["lock_id"],),
    )
    lock = _row(
        cursor.fetchone(),
        frozenset({"id", "plan_id", "status", "is_active", "released_by", "released_at"}),
        "existing converted lock",
    )
    if (
        lock["id"] != request["lock_id"]
        or lock["plan_id"] != plan_id
        or lock["status"] != "converted"
        or lock["is_active"] is not None
        or lock["released_by"] != request["actor"]
        or lock["released_at"] is None
    ):
        raise ValueError("existing converted lock lifecycle is inconsistent")

    cursor.execute(
        "SELECT status FROM orders WHERE case_no = %s FOR UPDATE",
        (request["case_no"],),
    )
    order = _row(
        cursor.fetchone(),
        frozenset({"status"}),
        "existing converted order",
    )
    if order["status"] != "訂單成立":
        raise ValueError("existing converted order lifecycle is inconsistent")

    lock_days = _load_preflight_lock_days(cursor, request["lock_id"], active_only=False)
    if any(
        day["active_marker"] is not None
        or day["released_by"] != request["actor"]
        or day["released_at"] is None
        for day in lock_days
    ):
        raise ValueError("existing converted lock days are inconsistent")

    expected_assignment_keys = frozenset(
        {
            "assignment_id",
            "segment_id",
            "segment_order",
            "staff_id",
            "assigned_start_date",
            "assigned_end_date",
            "planned_hours",
            "actual_hours",
            "hourly_rate",
            "floor_fee_allocated",
            "schedule",
        }
    )
    payload_by_id: dict[int, dict[str, Any]] = {}
    for index, assignment in enumerate(assignments):
        assignment = _row(assignment, expected_assignment_keys, f"existing payload assignments[{index}]")
        assignment_id = _positive_int(assignment["assignment_id"], "existing assignment_id")
        if assignment_id in payload_by_id:
            raise ValueError("existing event contains duplicate assignment_id")
        payload_by_id[assignment_id] = assignment

    cursor.execute(
        "SELECT id, case_no, staff_id, assignment_sequence, assigned_start_date, assigned_end_date, "
        "planned_hours, actual_hours, hourly_rate, floor_fee_allocated, status "
        "FROM case_staff_assignments WHERE case_no = %s ORDER BY assignment_sequence FOR UPDATE",
        (request["case_no"],),
    )
    stored_assignments = _rows(cursor.fetchall(), "existing stored assignments")
    if len(stored_assignments) != len(payload_by_id):
        raise ValueError("existing assignments do not match conversion event")
    for index, stored in enumerate(stored_assignments):
        stored = _row(
            stored,
            frozenset(
                {
                    "id",
                    "case_no",
                    "staff_id",
                    "assignment_sequence",
                    "assigned_start_date",
                    "assigned_end_date",
                    "planned_hours",
                    "actual_hours",
                    "hourly_rate",
                    "floor_fee_allocated",
                    "status",
                }
            ),
            f"existing stored assignments[{index}]",
        )
        expected = payload_by_id.get(stored["id"])
        if expected is None or stored["case_no"] != request["case_no"] or stored["status"] != "planned":
            raise ValueError("existing assignment ownership is inconsistent")
        comparisons = {
            "staff_id": stored["staff_id"],
            "segment_order": stored["assignment_sequence"],
            "assigned_start_date": _json_value(stored["assigned_start_date"]),
            "assigned_end_date": _json_value(stored["assigned_end_date"]),
            "planned_hours": _json_value(stored["planned_hours"]),
            "actual_hours": _json_value(stored["actual_hours"]),
            "hourly_rate": _json_value(stored["hourly_rate"]),
            "floor_fee_allocated": _json_value(stored["floor_fee_allocated"]),
        }
        if any(expected[key] != value for key, value in comparisons.items()):
            raise ValueError("existing assignment snapshot is inconsistent")

    assignment_ids = sorted(payload_by_id)
    placeholders = ", ".join(["%s"] * len(assignment_ids))
    cursor.execute(
        "SELECT assignment_id, work_date, is_work_day, is_double_pay, notes "
        f"FROM staff_schedule WHERE assignment_id IN ({placeholders}) "
        "ORDER BY assignment_id, work_date FOR UPDATE",
        tuple(assignment_ids),
    )
    stored_schedules = _rows(cursor.fetchall(), "existing stored schedules")
    expected_schedules = []
    for assignment_id in assignment_ids:
        schedule = _rows(payload_by_id[assignment_id]["schedule"], "existing payload schedule")
        for row in schedule:
            if not isinstance(row, dict):
                raise ValueError("existing payload schedule row must be a mapping")
            expected_schedules.append({"assignment_id": assignment_id, **row})
    if _json_value(stored_schedules) != _json_value(expected_schedules):
        raise ValueError("existing assignment schedules do not match conversion event")
    return assignments


def _existing_result(cursor: Any, request: dict[str, Any]) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT lock_id, event_type, event_key, actor, reason, payload "
        "FROM caregiver_availability_lock_events WHERE event_key = %s FOR UPDATE",
        (request["event_key"],),
    )
    event = cursor.fetchone()
    if event is None:
        return None
    event = _row(event, frozenset({"lock_id", "event_type", "event_key", "actor", "reason", "payload"}), "existing_event")
    if event["lock_id"] != request["lock_id"] or event["event_type"] != "lock_converted":
        raise ValueError("event key is already used by another operation")
    if event["actor"] != request["actor"] or event["reason"] != request["reason"]:
        raise ValueError("replay request does not match event")
    try:
        payload = json.loads(event["payload"]) if isinstance(event["payload"], str) else event["payload"]
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("existing event payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("existing event payload is invalid")
    expected_terms = [
        {"segment_id": segment_id, **{key: str(value) for key, value in request["terms"][segment_id].items()}}
        for segment_id in sorted(request["terms"])
    ]
    if payload.get("case_no") != request["case_no"] or payload.get("lock_id") != request["lock_id"] or payload.get("terms") != expected_terms:
        raise ValueError("replay request does not match event payload")
    assignments = _validate_existing_conversion(cursor, request, payload)
    return {
        "result": "existing",
        "case_no": request["case_no"],
        "lock_id": request["lock_id"],
        "plan_id": payload["plan_id"],
        "assignments": assignments,
    }


def convert_availability_lock_to_assignments(
    case_no: Any,
    lock_id: Any,
    event_key: Any,
    actor: Any,
    reason: Any,
    assignment_terms: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Convert one paid active lock into independent assignment-owned schedules."""
    request = _normalize_request(case_no, lock_id, event_key, actor, reason, assignment_terms, **kwargs)
    connection = cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor()
        replay_days = _load_preflight_lock_days(
            cursor,
            request["lock_id"],
            active_only=False,
            for_update=False,
        )
        replay_staff_ids = sorted({row["staff_id"] for row in replay_days})
        if lock_staff_occupancy_mutex(cursor, replay_staff_ids) != replay_staff_ids:
            raise ValueError("staff occupancy mutex result mismatch")
        locked_days = _load_preflight_lock_days(cursor, request["lock_id"], active_only=False)
        if locked_days != replay_days:
            raise ValueError("lock days changed while acquiring staff occupancy mutex")
        existing = _existing_result(cursor, request)
        if existing is not None:
            connection.commit()
            return existing

        state = _load_state(cursor, request)

        proposed = [
            {
                "id": f"conversion-{segment['segment_id']}", "case_no": request["case_no"],
                "staff_id": segment["staff_id"], "status": "planned",
                "assigned_start_date": segment["assigned_start_date"], "assigned_end_date": segment["assigned_end_date"],
                "kind": "formal", "original_assignment_id": None, "substitution_work_date": None,
            }
            for segment in state["snapshot"]["segments"]
        ]
        validate_assignment_plan_transition(
            case_no=request["case_no"], database_current_date=state["current_date"],
            effective_date=state["order"]["start_date"], case_start_date=state["order"]["start_date"],
            case_end_date=state["order"]["end_date"], operation_kind="segment_reconfigure",
            current_assignments=[], proposed_assignments=proposed,
        )

        assignments: list[dict[str, Any]] = []
        planned_total = Decimal("0.00")
        actual_total = Decimal("0.00")
        floor_total = Decimal("0.00")
        for segment in state["snapshot"]["segments"]:
            term = request["terms"][segment["segment_id"]]
            days = (segment["assigned_end_date"] - segment["assigned_start_date"]).days + 1
            planned_hours = state["order"]["daily_hours"] * days
            cursor.execute(
                "INSERT INTO case_staff_assignments "
                "(case_no, staff_id, assignment_sequence, assigned_start_date, assigned_end_date, planned_hours, hourly_rate, floor_fee_allocated, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'planned')",
                (request["case_no"], segment["staff_id"], segment["segment_order"], segment["assigned_start_date"], segment["assigned_end_date"], planned_hours, term["hourly_rate"], term["floor_fee_allocated"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("assignment insert rowcount mismatch")
            assignment_id = _positive_int(getattr(cursor, "lastrowid", None), "created assignment id")
            schedule = generate_assignment_schedule_in_transaction(cursor, assignment_id)
            if not isinstance(schedule, dict) or set(schedule) != {"assignment_schedule", "actual_hours"}:
                raise ValueError("schedule generator returned invalid result")
            actual_hours = _decimal(schedule["actual_hours"], "generated actual_hours")
            assignment = {
                "assignment_id": assignment_id, "segment_id": segment["segment_id"], "segment_order": segment["segment_order"],
                "staff_id": segment["staff_id"], "assigned_start_date": segment["assigned_start_date"].isoformat(),
                "assigned_end_date": segment["assigned_end_date"].isoformat(), "planned_hours": str(planned_hours),
                "actual_hours": str(actual_hours), "hourly_rate": str(term["hourly_rate"]),
                "floor_fee_allocated": str(term["floor_fee_allocated"]), "schedule": schedule["assignment_schedule"],
            }
            assignments.append(assignment)
            planned_total += planned_hours
            actual_total += actual_hours
            floor_total += term["floor_fee_allocated"]
        if (
            floor_total != state["order"]["floor_fee"]
            or planned_total != state["order"]["daily_hours"] * len(state["lock_days"])
            or actual_total != state["order"]["target_hours"]
        ):
            raise ValueError("assignment accounting totals do not reconcile")

        cursor.execute(
            "UPDATE caregiver_availability_lock_days SET active_marker = NULL, released_by = %s, released_at = CURRENT_TIMESTAMP "
            "WHERE lock_id = %s AND active_marker = 1",
            (request["actor"], request["lock_id"]),
        )
        if cursor.rowcount != len(state["lock_days"]):
            raise ValueError("lock day update rowcount mismatch")
        cursor.execute(
            "UPDATE caregiver_availability_locks SET status = 'converted', is_active = NULL, released_by = %s, released_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND status = 'active' AND is_active = 1",
            (request["actor"], request["lock_id"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("lock conversion rowcount mismatch")
        cursor.execute(
            "UPDATE orders SET status = '訂單成立' "
            "WHERE case_no = %s AND status = '洽談中'",
            (request["case_no"],),
        )
        if cursor.rowcount != 1:
            raise ValueError("order promotion rowcount mismatch")
        payload = _payload(request, state, assignments)
        cursor.execute(
            "INSERT INTO caregiver_availability_lock_events (lock_id, event_type, event_key, actor, reason, payload) "
            "VALUES (%s, 'lock_converted', %s, %s, %s, %s)",
            (request["lock_id"], request["event_key"], request["actor"], request["reason"], json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        if cursor.rowcount != 1:
            raise ValueError("conversion event insert rowcount mismatch")
        connection.commit()
        return {"result": "created", "case_no": request["case_no"], "lock_id": request["lock_id"], "plan_id": state["plan_id"], "assignments": assignments, "planned_hours": planned_total, "actual_hours": actual_total, "floor_fee_allocated": floor_total}
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except BaseException:  # noqa: BLE001
                pass
        raise
    finally:
        _close(cursor)
        _close(connection)
