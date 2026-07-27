"""Read-only planning for multi-caregiver order and assignment changes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from services.accounting_source_projection import (
    load_case_accounting_source_with_cursor,
)
from services.client_subsidy_return_obligations import (
    activate_subsidy_return_obligation,
    calculate_subsidy_return_due_date,
)
from services.db_service import (
    get_connection,
    sync_client_payment_due_dates_for_case_no,
)
from services.multi_caregiver_assignment_rules import (
    validate_non_overlapping_assignment_interval,
)
from services.multi_caregiver_schedule_generation import (
    generate_assignment_schedule_in_transaction,
)
from services.order_amount_calculator import (
    SUBSIDY_HOURS_BY_IDENTITY_STATUS,
    calculate_order_amounts,
)


_WEEKDAY_NAMES = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return result


def _date(value: object, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO date") from exc
    raise ValueError(f"{field_name} is required")


def _weekly_rest_days(value: object) -> set[int]:
    if value in (None, ""):
        return {6}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("weekly_rest_days must be a JSON array") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("weekly_rest_days must be a JSON array")
    unknown = set(value) - set(_WEEKDAY_NAMES)
    if unknown:
        raise ValueError("weekly_rest_days contains an unsupported weekday")
    return {_WEEKDAY_NAMES[item] for item in value}


def _normalise_order_change(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("order_change must be an object")
    service_days = _decimal(value.get("service_days"), "service_days")
    daily_hours = _decimal(value.get("service_hours_per_day"), "service_hours_per_day")
    if service_days < 0 or daily_hours < 0:
        raise ValueError("order service hours cannot be negative")

    start_date = _date(value.get("start_date"), "start_date")
    end_date = _date(value.get("end_date"), "end_date")
    actual_start_date = _date(value.get("actual_start_date"), "actual_start_date")
    actual_end_date = _date(value.get("actual_end_date"), "actual_end_date")
    if start_date > end_date or actual_start_date > actual_end_date:
        raise ValueError("order dates must be chronological")
    return {
        "service_days": service_days,
        "service_hours_per_day": daily_hours,
        "start_date": start_date,
        "end_date": end_date,
        "actual_start_date": actual_start_date,
        "actual_end_date": actual_end_date,
    }


def _normalise_apply_order_change(value: object) -> dict[str, Any]:
    """Require the complete non-cancellation order target used by Apply."""
    if not isinstance(value, Mapping):
        raise ValueError("order_change must be an object")
    change = _normalise_order_change(value)
    allowed_change_fields = {
        "client_name", "service_days", "service_hours_per_day", "floor_fee",
        "deposit_date", "start_date", "end_date", "actual_start_date",
        "actual_end_date",
    }
    if set(value) - allowed_change_fields:
        raise ValueError("order_change contains unsupported fields")
    floor_fee = _decimal(value.get("floor_fee"), "floor_fee")
    if floor_fee < 0:
        raise ValueError("floor_fee cannot be negative")
    deposit_date_value = value.get("deposit_date")
    return {
        **change,
        "client_name": _required_text(value.get("client_name"), "client_name"),
        "floor_fee": floor_fee,
        "deposit_date": (
            None if deposit_date_value is None else _date(deposit_date_value, "deposit_date")
        ),
    }


def _normalise_assignment_plan(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("assignment_plan must be a list")

    normalised: list[dict[str, Any]] = []
    sequences: set[int] = set()
    assignment_ids: set[int] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("each assignment_plan item must be an object")
        staff_id = item.get("staff_id")
        sequence = item.get("assignment_sequence")
        if isinstance(staff_id, bool) or not isinstance(staff_id, int) or staff_id < 1:
            raise ValueError("staff_id must be a positive integer")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("assignment_sequence must be a positive integer")
        if sequence in sequences:
            raise ValueError("assignment_sequence must be unique")

        assignment_id = item.get("assignment_id")
        if assignment_id is not None:
            if isinstance(assignment_id, bool) or not isinstance(assignment_id, int) or assignment_id < 1:
                raise ValueError("assignment_id must be a positive integer")
            if assignment_id in assignment_ids:
                raise ValueError("assignment_id must be unique")
            assignment_ids.add(assignment_id)

        start_date = _date(item.get("assigned_start_date"), "assigned_start_date")
        end_date = _date(item.get("assigned_end_date"), "assigned_end_date")
        if start_date > end_date:
            raise ValueError("assigned_start_date must not be after assigned_end_date")
        normalised.append(
            {
                "assignment_id": assignment_id,
                "staff_id": staff_id,
                "assignment_sequence": sequence,
                "assigned_start_date": start_date,
                "assigned_end_date": end_date,
            }
        )
        sequences.add(sequence)
    return sorted(normalised, key=lambda item: item["assignment_sequence"])


def _fetchall(cursor: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor.execute(sql, params)
    return list(cursor.fetchall() or [])


def _blocking_assignments(cursor: Any, assignment_ids: list[int]) -> dict[int, set[str]]:
    blockers = {assignment_id: set() for assignment_id in assignment_ids}
    if not assignment_ids:
        return blockers
    placeholders = ", ".join(["%s"] * len(assignment_ids))
    queries = (
        (
            f"""SELECT assignment_id FROM staff_payments
                  WHERE assignment_id IN ({placeholders})
                    AND payment_status <> 'cancelled'""",
            "active_staff_payment",
        ),
        (
            f"""SELECT d.assignment_id
                  FROM staff_monthly_settlement_details d
                  JOIN staff_monthly_settlements s ON s.id = d.settlement_id
                 WHERE d.assignment_id IN ({placeholders})
                   AND s.status <> 'cancelled'""",
            "active_monthly_settlement",
        ),
        (
            f"""SELECT assignment_id FROM actual_hours_adjustments
                  WHERE assignment_id IN ({placeholders})""",
            "manual_actual_hours_adjustment",
        ),
    )
    for sql, reason in queries:
        for row in _fetchall(cursor, sql, tuple(assignment_ids)):
            assignment_id = row.get("assignment_id")
            if assignment_id in blockers:
                blockers[assignment_id].add(reason)
    return blockers


def _working_days(start_date: date, end_date: date, rest_days: set[int], holidays: set[date]) -> int:
    current = start_date
    total = 0
    while current <= end_date:
        if current.weekday() not in rest_days and current not in holidays:
            total += 1
        current += timedelta(days=1)
    return total


def _status_for(blocking_reasons: list[dict[str, Any]], target_hours: Decimal, proposed_hours: Decimal) -> str:
    if any(reason["code"] in {"active_staff_payment", "active_monthly_settlement"} for reason in blocking_reasons):
        return "locked"
    if blocking_reasons:
        return "requires_review"
    if proposed_hours != target_hours:
        return "requires_allocation"
    return "in_sync"


def preview_order_assignment_sync(
    case_no: str, order_change: dict[str, Any], assignment_plan: list[dict[str, Any]]
) -> dict[str, Any]:
    """Read order-change effects without writing or locking any business record."""
    normalised_case_no = _required_text(case_no, "case_no")
    change = _normalise_order_change(order_change)
    allowed_change_fields = {
        "client_name", "service_days", "service_hours_per_day", "floor_fee",
        "deposit_date", "start_date", "end_date", "actual_start_date",
        "actual_end_date",
    }
    unsupported_fields = set(order_change) - allowed_change_fields
    if unsupported_fields:
        raise ValueError("order_change contains unsupported fields")
    floor_fee = _decimal(order_change.get("floor_fee"), "floor_fee")
    if floor_fee < 0:
        raise ValueError("floor_fee cannot be negative")
    deposit_date_value = order_change.get("deposit_date")
    change.update(
        {
            "client_name": _required_text(order_change.get("client_name"), "client_name"),
            "floor_fee": floor_fee,
            "deposit_date": (
                None
                if deposit_date_value is None
                else _date(deposit_date_value, "deposit_date")
            ),
        }
    )
    plan = _normalise_assignment_plan(assignment_plan)
    target_hours = change["service_days"] * change["service_hours_per_day"]

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT o.case_no, o.service_days, o.service_hours_per_day,
                          o.start_date, o.end_date, o.actual_start_date, o.actual_end_date,
                          c.identity_status AS identity_status
                     FROM orders o
                     JOIN clients c ON c.case_no = o.case_no
                    WHERE o.case_no = %s""",
                (normalised_case_no,),
            )
            order = cursor.fetchone()
            if order is None:
                raise ValueError("case_no does not exist")
            _required_text(order.get("identity_status"), "identity_status")

            existing_assignments = _fetchall(
                cursor,
                """SELECT id, staff_id, assignment_sequence, assigned_start_date,
                          assigned_end_date, status, planned_hours, actual_hours
                     FROM case_staff_assignments WHERE case_no = %s ORDER BY assignment_sequence, id""",
                (normalised_case_no,),
            )
            existing_by_id = {row["id"]: row for row in existing_assignments}
            blocking_reasons: list[dict[str, Any]] = []

            if not plan:
                return {
                    "case_no": normalised_case_no,
                    "target_hours": target_hours,
                    "current_actual_hours": _current_actual_hours(existing_assignments),
                    "proposed_actual_hours": Decimal("0"),
                    "difference": target_hours,
                    "schedule_impact": [],
                    "blocking_reasons": [{"code": "assignment_plan_required"}],
                    "sync_status": "requires_allocation",
                }

            for item in plan:
                assignment_id = item["assignment_id"]
                if assignment_id is not None and assignment_id not in existing_by_id:
                    blocking_reasons.append(
                        {"code": "assignment_not_in_case", "assignment_id": assignment_id}
                    )

            plan_assignment_ids = [item["assignment_id"] for item in plan if item["assignment_id"] is not None]
            omitted_active_assignment_ids = [
                row["id"]
                for row in existing_assignments
                if row.get("status") != "cancelled" and row["id"] not in plan_assignment_ids
            ]
            affected_assignment_ids = sorted(
                {
                    assignment_id
                    for assignment_id in plan_assignment_ids + omitted_active_assignment_ids
                    if assignment_id in existing_by_id
                }
            )
            blockers = _blocking_assignments(cursor, affected_assignment_ids)
            for assignment_id, reasons in blockers.items():
                for reason in sorted(reasons):
                    blocking_reasons.append({"code": reason, "assignment_id": assignment_id})

            staff_ids = sorted({item["staff_id"] for item in plan})
            staff_rest_days: dict[int, set[int]] = {}
            for staff_id in staff_ids:
                cursor.execute("SELECT weekly_rest_days FROM staff WHERE id = %s", (staff_id,))
                staff = cursor.fetchone()
                if staff is None:
                    blocking_reasons.append({"code": "assignment_staff_not_found", "staff_id": staff_id})
                else:
                    staff_rest_days[staff_id] = _weekly_rest_days(staff.get("weekly_rest_days"))

            min_date = min(item["assigned_start_date"] for item in plan)
            max_date = max(item["assigned_end_date"] for item in plan)
            holidays = {
                _date(row["holiday_date"], "holiday_date")
                for row in _fetchall(
                    cursor,
                    "SELECT holiday_date FROM holidays WHERE holiday_date BETWEEN %s AND %s",
                    (min_date, max_date),
                )
            }

            schedule_impact: list[dict[str, Any]] = []
            proposed_hours = Decimal("0")
            candidate_rows: list[dict[str, Any]] = []
            for item in plan:
                assignment_id = item["assignment_id"]
                if assignment_id is not None and assignment_id in existing_by_id:
                    candidate_rows.append(existing_by_id[assignment_id])
                try:
                    validate_non_overlapping_assignment_interval(
                        item["assigned_start_date"],
                        item["assigned_end_date"],
                        candidate_rows,
                        candidate_assignment_id=assignment_id,
                    )
                except ValueError as exc:
                    blocking_reasons.append(
                        {"code": "assignment_interval_conflict", "assignment_id": assignment_id, "detail": str(exc)}
                    )
                candidate_rows.append(
                    {
                        "id": assignment_id,
                        "status": "planned",
                        "assigned_start_date": item["assigned_start_date"],
                        "assigned_end_date": item["assigned_end_date"],
                    }
                )

                work_days = _working_days(
                    item["assigned_start_date"],
                    item["assigned_end_date"],
                    staff_rest_days.get(item["staff_id"], {6}),
                    holidays,
                )
                predicted_hours = change["service_hours_per_day"] * work_days
                proposed_hours += predicted_hours
                schedule_rows = _fetchall(
                    cursor,
                    """SELECT id, case_no, assignment_id, work_date
                         FROM staff_schedule
                        WHERE staff_id = %s AND work_date BETWEEN %s AND %s""",
                    (item["staff_id"], item["assigned_start_date"], item["assigned_end_date"]),
                )
                for schedule in schedule_rows:
                    owner = schedule.get("assignment_id")
                    if owner is None:
                        code = "legacy_schedule_requires_review"
                    elif owner != assignment_id:
                        code = "schedule_conflict"
                    elif schedule.get("case_no") != normalised_case_no:
                        code = "schedule_case_mismatch"
                    else:
                        continue
                    blocking_reasons.append(
                        {"code": code, "schedule_id": schedule.get("id"), "assignment_id": assignment_id}
                    )
                schedule_impact.append(
                    {
                        "assignment_id": assignment_id,
                        "staff_id": item["staff_id"],
                        "assigned_start_date": item["assigned_start_date"],
                        "assigned_end_date": item["assigned_end_date"],
                        "estimated_work_days": work_days,
                        "estimated_actual_hours": predicted_hours,
                        "existing_schedule_rows": len(schedule_rows),
                    }
                )

            desired_by_assignment_id = {
                item["assignment_id"]: item
                for item in plan
                if item["assignment_id"] is not None and item["assignment_id"] in existing_by_id
            }
            required_schedule_removals: list[dict[str, Any]] = []
            if affected_assignment_ids:
                placeholders = ", ".join(["%s"] * len(affected_assignment_ids))
                existing_schedule_rows = _fetchall(
                    cursor,
                    f"""SELECT id, case_no, assignment_id, staff_id, work_date
                          FROM staff_schedule
                         WHERE case_no = %s
                           AND assignment_id IN ({placeholders})""",
                    (normalised_case_no, *affected_assignment_ids),
                )
                for schedule in existing_schedule_rows:
                    assignment_id = schedule.get("assignment_id")
                    existing_assignment = existing_by_id.get(assignment_id)
                    desired = desired_by_assignment_id.get(assignment_id)
                    if existing_assignment is None or existing_assignment.get("status") == "cancelled":
                        continue
                    if desired is None:
                        should_remove = True
                    else:
                        work_date = _date(schedule.get("work_date"), "work_date")
                        should_remove = (
                            existing_assignment.get("staff_id") != desired["staff_id"]
                            or work_date < desired["assigned_start_date"]
                            or work_date > desired["assigned_end_date"]
                        )
                    if should_remove:
                        required_schedule_removals.append(
                            {
                                "schedule_id": schedule.get("id"),
                                "assignment_id": assignment_id,
                                "work_date": schedule.get("work_date"),
                            }
                        )

            required_schedule_removals.sort(
                key=lambda item: (item["work_date"], item["schedule_id"])
            )

            blocking_reasons = _deduplicate_reasons(blocking_reasons)
            return {
                "case_no": normalised_case_no,
                "target_hours": target_hours,
                "current_actual_hours": _current_actual_hours(existing_assignments),
                "proposed_actual_hours": proposed_hours,
                "difference": target_hours - proposed_hours,
                "schedule_impact": schedule_impact,
                "required_schedule_removals": required_schedule_removals,
                "blocking_reasons": blocking_reasons,
                "sync_status": _status_for(blocking_reasons, target_hours, proposed_hours),
            }
    finally:
        connection.close()


def _current_actual_hours(assignments: list[Mapping[str, Any]]) -> Decimal | None:
    total = Decimal("0")
    for assignment in assignments:
        if assignment.get("status") == "cancelled":
            continue
        actual_hours = assignment.get("actual_hours")
        if actual_hours is None:
            return None
        hours = _decimal(actual_hours, "actual_hours")
        if hours < 0:
            return None
        total += hours
    return total


def _deduplicate_reasons(reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for reason in reasons:
        marker = tuple(sorted((key, str(value)) for key, value in reason.items()))
        if marker not in seen:
            unique.append(reason)
            seen.add(marker)
    return unique


def _normalise_schedule_change_plan(value: object) -> list[int] | None:
    if not isinstance(value, Mapping) or "remove_schedule_ids" not in value:
        return None
    schedule_ids = value["remove_schedule_ids"]
    if not isinstance(schedule_ids, list):
        raise ValueError("remove_schedule_ids must be a list")
    if any(isinstance(schedule_id, bool) or not isinstance(schedule_id, int) or schedule_id < 1 for schedule_id in schedule_ids):
        raise ValueError("remove_schedule_ids must contain positive integers")
    if len(schedule_ids) != len(set(schedule_ids)):
        raise ValueError("remove_schedule_ids must be unique")
    return sorted(schedule_ids)


def _snapshot(value: Mapping[str, Any]) -> str:
    return json.dumps(value, default=lambda item: item.isoformat() if isinstance(item, date) else str(item), sort_keys=True)


def apply_order_assignment_sync(
    case_no: str,
    order_change: dict[str, Any],
    assignment_plan: list[dict[str, Any]],
    schedule_change_plan: dict[str, Any],
    applied_by: str,
) -> dict[str, Any]:
    """Atomically apply one explicitly confirmed order and caregiver-plan change."""
    normalised_case_no = _required_text(case_no, "case_no")
    operator = _required_text(applied_by, "applied_by")
    change = _normalise_apply_order_change(order_change)
    plan = _normalise_assignment_plan(assignment_plan)
    removal_ids = _normalise_schedule_change_plan(schedule_change_plan)
    target_hours = change["service_days"] * change["service_hours_per_day"]

    if not plan or removal_ids is None:
        return {
            "case_no": normalised_case_no,
            "sync_status": "requires_allocation",
            "blocking_reasons": [{"code": "assignment_plan_required" if not plan else "schedule_change_plan_required"}],
        }

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT o.case_no, o.service_days, o.service_hours_per_day,
                          o.floor_fee, o.deposit_date, o.start_date, o.end_date,
                          o.actual_start_date, o.actual_end_date, c.name AS client_name,
                          c.identity_status AS identity_status
                     FROM orders o
                     JOIN clients c ON c.case_no = o.case_no
                    WHERE o.case_no = %s FOR UPDATE""",
                (normalised_case_no,),
            )
            order_before = cursor.fetchone()
            if order_before is None:
                raise ValueError("case_no does not exist")
            identity_status = _required_text(order_before.get("identity_status"), "identity_status")

            existing_assignments = _fetchall(
                cursor,
                """SELECT id, staff_id, assignment_sequence, assigned_start_date,
                          assigned_end_date, status, planned_hours, actual_hours
                     FROM case_staff_assignments
                    WHERE case_no = %s
                    ORDER BY assignment_sequence, id FOR UPDATE""",
                (normalised_case_no,),
            )
            existing_by_id = {row["id"]: row for row in existing_assignments}
            plan_assignment_ids = [item["assignment_id"] for item in plan if item["assignment_id"] is not None]
            unknown_assignment_ids = [
                assignment_id for assignment_id in plan_assignment_ids if assignment_id not in existing_by_id
            ]
            if unknown_assignment_ids:
                raise ValueError("assignment does not belong to this case")
            if any(existing_by_id[assignment_id].get("status") == "cancelled" for assignment_id in plan_assignment_ids):
                raise ValueError("cancelled assignment requires a new assignment-plan entry")

            omitted_active_assignment_ids = [
                row["id"]
                for row in existing_assignments
                if row.get("status") != "cancelled" and row["id"] not in plan_assignment_ids
            ]
            affected_assignment_ids = sorted(set(plan_assignment_ids + omitted_active_assignment_ids))
            if affected_assignment_ids:
                placeholders = ", ".join(["%s"] * len(affected_assignment_ids))
                locking_queries = (
                    (
                        f"""SELECT assignment_id FROM staff_payments
                              WHERE assignment_id IN ({placeholders})
                                AND payment_status <> 'cancelled' FOR UPDATE""",
                        "active_staff_payment",
                    ),
                    (
                        f"""SELECT d.assignment_id
                              FROM staff_monthly_settlement_details d
                              JOIN staff_monthly_settlements s ON s.id = d.settlement_id
                             WHERE d.assignment_id IN ({placeholders})
                               AND s.status <> 'cancelled' FOR UPDATE""",
                        "active_monthly_settlement",
                    ),
                    (
                        f"""SELECT assignment_id FROM actual_hours_adjustments
                              WHERE assignment_id IN ({placeholders}) FOR UPDATE""",
                        "manual_actual_hours_adjustment",
                    ),
                )
                blocking_reasons: list[dict[str, Any]] = []
                for sql, code in locking_queries:
                    for row in _fetchall(cursor, sql, tuple(affected_assignment_ids)):
                        blocking_reasons.append({"code": code, "assignment_id": row["assignment_id"]})
                if blocking_reasons:
                    connection.rollback()
                    return {
                        "case_no": normalised_case_no,
                        "sync_status": "locked" if any(reason["code"] != "manual_actual_hours_adjustment" for reason in blocking_reasons) else "requires_review",
                        "blocking_reasons": _deduplicate_reasons(blocking_reasons),
                    }

            staff_rest_days: dict[int, set[int]] = {}
            for staff_id in sorted({item["staff_id"] for item in plan}):
                cursor.execute("SELECT weekly_rest_days FROM staff WHERE id = %s FOR UPDATE", (staff_id,))
                staff = cursor.fetchone()
                if staff is None:
                    raise ValueError("assignment staff does not exist")
                staff_rest_days[staff_id] = _weekly_rest_days(staff.get("weekly_rest_days"))

            min_date = min(item["assigned_start_date"] for item in plan)
            max_date = max(item["assigned_end_date"] for item in plan)
            holidays = {
                _date(row["holiday_date"], "holiday_date")
                for row in _fetchall(
                    cursor,
                    "SELECT holiday_date FROM holidays WHERE holiday_date BETWEEN %s AND %s FOR UPDATE",
                    (min_date, max_date),
                )
            }

            candidate_rows: list[dict[str, Any]] = []
            plan_hours_by_sequence: dict[int, Decimal] = {}
            for item in plan:
                assignment_id = item["assignment_id"]
                if assignment_id is not None:
                    candidate_rows.append(existing_by_id[assignment_id])
                validate_non_overlapping_assignment_interval(
                    item["assigned_start_date"],
                    item["assigned_end_date"],
                    candidate_rows,
                    candidate_assignment_id=assignment_id,
                )
                candidate_rows.append(
                    {
                        "id": assignment_id,
                        "status": "planned",
                        "assigned_start_date": item["assigned_start_date"],
                        "assigned_end_date": item["assigned_end_date"],
                    }
                )
                plan_hours_by_sequence[item["assignment_sequence"]] = change["service_hours_per_day"] * _working_days(
                    item["assigned_start_date"],
                    item["assigned_end_date"],
                    staff_rest_days[item["staff_id"]],
                    holidays,
                )
            if sum(plan_hours_by_sequence.values(), Decimal("0")) != target_hours:
                raise ValueError("assignment plan does not exactly cover the order target hours")

            desired_by_assignment_id = {
                item["assignment_id"]: item for item in plan if item["assignment_id"] is not None
            }
            expected_removal_ids: set[int] = set()
            if affected_assignment_ids:
                placeholders = ", ".join(["%s"] * len(affected_assignment_ids))
                schedule_rows = _fetchall(
                    cursor,
                    f"""SELECT id, case_no, assignment_id, staff_id, work_date
                          FROM staff_schedule
                         WHERE case_no = %s
                           AND assignment_id IN ({placeholders}) FOR UPDATE""",
                    (normalised_case_no, *affected_assignment_ids),
                )
                for schedule in schedule_rows:
                    assignment_id = schedule["assignment_id"]
                    desired = desired_by_assignment_id.get(assignment_id)
                    existing_assignment = existing_by_id[assignment_id]
                    if desired is None:
                        expected_removal_ids.add(schedule["id"])
                    else:
                        work_date = _date(schedule["work_date"], "work_date")
                        if (
                            existing_assignment["staff_id"] != desired["staff_id"]
                            or work_date < desired["assigned_start_date"]
                            or work_date > desired["assigned_end_date"]
                        ):
                            expected_removal_ids.add(schedule["id"])
            if set(removal_ids) != expected_removal_ids:
                raise ValueError("remove_schedule_ids must exactly match required assignment-owned schedule removals")

            cursor.execute(
                """UPDATE orders
                       SET service_days = %s, service_hours_per_day = %s,
                           floor_fee = %s, deposit_date = %s,
                           start_date = %s, end_date = %s,
                           actual_start_date = %s, actual_end_date = %s,
                           status = '訂單成立'
                     WHERE case_no = %s""",
                (
                    change["service_days"], change["service_hours_per_day"],
                    change["floor_fee"], change["deposit_date"],
                    change["start_date"], change["end_date"],
                    change["actual_start_date"], change["actual_end_date"], normalised_case_no,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("order could not be updated")
            cursor.execute(
                "UPDATE clients SET name = %s WHERE case_no = %s",
                (change["client_name"], normalised_case_no),
            )
            if cursor.rowcount != 1:
                raise ValueError("client could not be updated")
            sync_client_payment_due_dates_for_case_no(normalised_case_no, cursor=cursor)

            if removal_ids:
                placeholders = ", ".join(["%s"] * len(removal_ids))
                cursor.execute(
                    f"DELETE FROM staff_schedule WHERE id IN ({placeholders}) AND assignment_id IS NOT NULL",
                    tuple(removal_ids),
                )
                if cursor.rowcount != len(removal_ids):
                    raise ValueError("required assignment-owned schedules could not all be removed")

            if omitted_active_assignment_ids:
                placeholders = ", ".join(["%s"] * len(omitted_active_assignment_ids))
                cursor.execute(
                    f"UPDATE case_staff_assignments SET status = 'cancelled' WHERE id IN ({placeholders}) AND case_no = %s AND status <> 'cancelled'",
                    (*omitted_active_assignment_ids, normalised_case_no),
                )
                if cursor.rowcount != len(omitted_active_assignment_ids):
                    raise ValueError("omitted assignment could not be cancelled")

            temporary_sequence_base = max(
                (int(row["assignment_sequence"]) for row in existing_assignments), default=0
            ) + len(plan) + 1
            for offset, assignment_id in enumerate(plan_assignment_ids):
                cursor.execute(
                    "UPDATE case_staff_assignments SET assignment_sequence = %s WHERE id = %s AND case_no = %s",
                    (temporary_sequence_base + offset, assignment_id, normalised_case_no),
                )
                if cursor.rowcount != 1:
                    raise ValueError("assignment could not be prepared for sequence update")

            resolved_plan: list[dict[str, Any]] = []
            for item in plan:
                assignment_id = item["assignment_id"]
                planned_hours = plan_hours_by_sequence[item["assignment_sequence"]]
                if assignment_id is None:
                    cursor.execute(
                        """INSERT INTO case_staff_assignments
                               (case_no, staff_id, assignment_sequence, assigned_start_date,
                                assigned_end_date, planned_hours, status)
                           VALUES (%s, %s, %s, %s, %s, %s, 'planned')""",
                        (
                            normalised_case_no, item["staff_id"], item["assignment_sequence"],
                            item["assigned_start_date"], item["assigned_end_date"], planned_hours,
                        ),
                    )
                    assignment_id = cursor.lastrowid
                else:
                    cursor.execute(
                        """UPDATE case_staff_assignments
                              SET staff_id = %s, assignment_sequence = %s,
                                  assigned_start_date = %s, assigned_end_date = %s,
                                  planned_hours = %s
                            WHERE id = %s AND case_no = %s AND status <> 'cancelled'""",
                        (
                            item["staff_id"], item["assignment_sequence"],
                            item["assigned_start_date"], item["assigned_end_date"], planned_hours,
                            assignment_id, normalised_case_no,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("assignment could not be updated")
                resolved_plan.append({**item, "assignment_id": assignment_id, "planned_hours": planned_hours})

            generated_schedules = [
                generate_assignment_schedule_in_transaction(cursor, item["assignment_id"])
                for item in resolved_plan
            ]
            cursor.execute(
                """SELECT id AS assignment_id, staff_id, actual_hours
                     FROM case_staff_assignments
                    WHERE case_no = %s AND status <> 'cancelled'
                    ORDER BY id FOR UPDATE""",
                (normalised_case_no,),
            )
            confirmation_assignments = list(cursor.fetchall() or [])
            actual_hours_total = Decimal("0")
            for assignment in confirmation_assignments:
                actual_hours = assignment.get("actual_hours")
                if actual_hours is None:
                    raise ValueError("assignment actual_hours is required for confirmation")
                actual_hours = _decimal(actual_hours, "actual_hours")
                if actual_hours < 0:
                    raise ValueError("assignment actual_hours cannot be negative")
                actual_hours_total += actual_hours
            if actual_hours_total != target_hours:
                raise ValueError("assignment actual-hours total does not match order target hours")
            confirmation = {
                "case_no": normalised_case_no,
                "target_hours": target_hours,
                "actual_hours_total": actual_hours_total,
                "difference": target_hours - actual_hours_total,
                "assignments": confirmation_assignments,
                "can_confirm": True,
            }

            subsidy_return_obligation_result = None
            if change["actual_end_date"] is not None:
                cursor.execute(
                    """SELECT id, amount_receivable, amount_received,
                              subsidy_return_receivable
                         FROM client_payments
                        WHERE case_no = %s FOR UPDATE""",
                    (normalised_case_no,),
                )
                payment_row = cursor.fetchone()
                if payment_row is not None:
                    payment_columns = (
                        "id", "amount_receivable", "amount_received",
                        "subsidy_return_receivable",
                    )
                    payment = (
                        dict(payment_row)
                        if isinstance(payment_row, Mapping)
                        else dict(zip(payment_columns, payment_row, strict=True))
                    )
                    amount_receivable = _decimal(
                        payment["amount_receivable"], "amount_receivable"
                    )
                    amount_received = _decimal(
                        payment["amount_received"], "amount_received"
                    )
                    current_return = payment.get("subsidy_return_receivable")
                    obligation_is_inactive = current_return is None or _decimal(
                        current_return, "subsidy_return_receivable"
                    ) == Decimal("0")

                    if amount_received == amount_receivable and obligation_is_inactive:
                        accounting_source = load_case_accounting_source_with_cursor(
                            cursor, normalised_case_no
                        )
                        missing_terms = list(accounting_source.get("missing_terms") or [])
                        if missing_terms:
                            raise ValueError(
                                "accounting source is incomplete: " + ", ".join(missing_terms)
                            )

                        projected_order = accounting_source["order"]
                        projected_client = accounting_source["client"]
                        projected_identity_status = str(
                            projected_client.get("identity_status") or ""
                        ).strip()
                        if projected_identity_status in SUBSIDY_HOURS_BY_IDENTITY_STATUS:
                            calculator_assignments = [
                                {
                                    "assignment_id": assignment.get("assignment_id"),
                                    "staff_id": assignment.get("staff_id"),
                                    "actual_hours": assignment.get("actual_hours"),
                                    "hourly_rate": assignment.get("hourly_rate"),
                                    "floor_fee_amount": assignment.get("floor_fee_allocated"),
                                }
                                for assignment in accounting_source["staff_assignments"]
                                if assignment.get("status") != "cancelled"
                            ]
                            calculated_plan = calculate_order_amounts(
                                {
                                    "case_no": normalised_case_no,
                                    "service_days": projected_order.get("service_days"),
                                    "service_hours_per_day": projected_order.get(
                                        "service_hours_per_day"
                                    ),
                                    "identity_status": projected_identity_status,
                                    "client_floor_fee": projected_order.get("floor_fee"),
                                    "service_start_date": projected_order.get(
                                        "actual_start_date"
                                    ),
                                    "actual_completion_date": projected_order.get(
                                        "actual_end_date"
                                    ),
                                },
                                calculator_assignments,
                                accounting_source["collection_schedule"],
                            )
                            calculated_return_amount = _decimal(
                                calculated_plan["client_ledger_plan"][
                                    "subsidy_return_amount"
                                ],
                                "subsidy_return_amount",
                            )
                            if calculated_return_amount > 0:
                                due_date = calculate_subsidy_return_due_date(
                                    projected_order.get("actual_end_date")
                                )
                                subsidy_return_obligation_result = (
                                    activate_subsidy_return_obligation(
                                        cursor,
                                        payment["id"],
                                        calculated_return_amount,
                                        due_date,
                                    )
                                )
                                if subsidy_return_obligation_result.get("result") not in {
                                    "activated", "existing", "review_required"
                                }:
                                    raise ValueError(
                                        "unexpected subsidy-return obligation result"
                                    )
            order_after = {
                "case_no": normalised_case_no,
                "service_days": change["service_days"],
                "service_hours_per_day": change["service_hours_per_day"],
                "client_name": change["client_name"],
                "identity_status": identity_status,
                "floor_fee": change["floor_fee"],
                "deposit_date": change["deposit_date"],
                "start_date": change["start_date"],
                "end_date": change["end_date"],
                "actual_start_date": change["actual_start_date"],
                "actual_end_date": change["actual_end_date"],
            }
            cursor.execute(
                """INSERT INTO order_assignment_change_audits
                       (case_no, order_before_snapshot, order_after_snapshot,
                        assignment_plan_snapshot, applied_by)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    normalised_case_no, _snapshot(order_before), _snapshot(order_after),
                    _snapshot({"assignments": resolved_plan}), operator,
                ),
            )
            audit_id = cursor.lastrowid
        connection.commit()
        return {
            "case_no": normalised_case_no,
            "removed_schedule_ids": removal_ids,
            "assignments": resolved_plan,
            "generated_schedules": generated_schedules,
            "confirmation": confirmation,
            "subsidy_return_obligation": subsidy_return_obligation_result,
            "audit_id": audit_id,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
