"""Read-only, assignment-owned payroll reconciliation.

This module deliberately accepts a caller-owned DictCursor.  It is a
reconciliation primitive, not a payment writer: callers retain transaction
ownership and decide what to do with a clean result.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


_CENT = Decimal("0.01")
_ACTIVE_ASSIGNMENT_STATUSES = {"planned", "active", "completed", "replaced"}
_PENDING_SUBSTITUTION_FIELDS = {
    "case_no",
    "event_key",
    "original_assignment_id",
    "original_schedule_id",
    "work_date",
    "resolution_type",
    "substitute_assignment_id",
    "prefix_assignment_id",
    "suffix_assignment_id",
}


def _decimal(value: Any, field: str) -> Decimal:
    """Return a finite, non-negative Decimal without ever accepting floats."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be a finite non-negative Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite non-negative Decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be a finite non-negative Decimal")
    return result


def _money(value: Any, field: str) -> Decimal:
    """Return a payroll amount normalized to cents with the mandated rounding."""
    return _decimal(value, field).quantize(_CENT, rounding=ROUND_HALF_UP)


def _date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO date") from exc
    raise ValueError(f"{field} must be an ISO date")


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _row(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a DictCursor mapping")
    return value


def _rows(value: Any, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be DictCursor mappings")
    return [_row(item, label) for item in value]


def _error(code: str, assignment_id: int | None = None, work_date: date | None = None) -> dict[str, Any]:
    return {"assignment_id": assignment_id, "work_date": work_date, "code": code}


def _sort_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        errors,
        key=lambda item: (
            item["assignment_id"] is None,
            item["assignment_id"] or 0,
            item["work_date"] or date.min,
            item["code"],
        ),
    )


def _q(cursor: Any, sql: str, params: tuple[Any, ...], label: str) -> list[Mapping[str, Any]]:
    cursor.execute(sql, params)
    return _rows(cursor.fetchall(), label)


def _allocate_family_floor_fee(
    original_id: int,
    substitute_ids: set[int],
    details: dict[int, dict[str, Any]],
    *,
    replacement_ids: tuple[int | None, int | None] | None = None,
) -> None:
    """Check family allocation, using explicit replacements for a cancelled root."""
    replacements = tuple(item for item in (replacement_ids or ()) if item is not None)
    family_ids = [original_id, *replacements, *sorted(substitute_ids)]
    family_ids = list(dict.fromkeys(family_ids))
    if any(item not in details for item in family_ids):
        return
    total_days = sum((details[item]["actual_service_days"] for item in family_ids), 0)
    pool = sum((details[item]["floor_fee_allocated"] for item in family_ids), Decimal("0"))
    if total_days <= 0:
        return
    allocated: dict[int, Decimal] = {}
    remainder = pool
    remainder_owner = original_id
    if replacement_ids is not None:
        remainder_owner = next(
            (item for item in (*replacement_ids, *sorted(substitute_ids)) if item is not None),
            original_id,
        )
    for item in family_ids:
        if item == remainder_owner:
            continue
        share = (pool * Decimal(details[item]["actual_service_days"]) / Decimal(total_days)).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
        allocated[item] = share
        remainder -= share
    allocated[remainder_owner] = remainder.quantize(_CENT, rounding=ROUND_HALF_UP)
    for item, expected in allocated.items():
        details[item]["expected_family_floor_fee"] = expected


def reconcile_assignment_payroll_with_cursor(
    cursor: Any,
    case_no: str,
    pending_substitution_event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic reconciliation result using only ``cursor`` reads.

    A malformed or cross-case row is represented as an error where possible;
    malformed cursor shapes and invalid input fail immediately because guessing
    payroll ownership is unsafe.
    """
    if not isinstance(case_no, str) or not case_no.strip():
        raise ValueError("case_no must be a non-empty string")
    case_no = case_no.strip()
    if not all(callable(getattr(cursor, method, None)) for method in ("execute", "fetchone", "fetchall")):
        raise ValueError("cursor must be a caller-owned DictCursor")
    pending_event: dict[str, Any] | None = None
    if pending_substitution_event is not None:
        if not isinstance(pending_substitution_event, Mapping):
            raise ValueError("pending_substitution_event must be a single mapping")
        if set(pending_substitution_event) != _PENDING_SUBSTITUTION_FIELDS:
            raise ValueError("pending_substitution_event fields are invalid")
        pending_event = dict(pending_substitution_event)
        if pending_event["case_no"] != case_no:
            raise ValueError("pending_substitution_event case_no does not match request")
        event_key = pending_event["event_key"]
        if not isinstance(event_key, str) or not event_key.strip() or event_key != event_key.strip():
            raise ValueError("pending_substitution_event event_key must be canonical")
        pending_event["original_assignment_id"] = _positive_int(
            pending_event["original_assignment_id"],
            "pending_substitution_event.original_assignment_id",
        )
        pending_event["original_schedule_id"] = _positive_int(
            pending_event["original_schedule_id"],
            "pending_substitution_event.original_schedule_id",
        )
        pending_event["substitute_assignment_id"] = _positive_int(
            pending_event["substitute_assignment_id"],
            "pending_substitution_event.substitute_assignment_id",
        )
        for role in ("prefix_assignment_id", "suffix_assignment_id"):
            value = pending_event[role]
            pending_event[role] = (
                None
                if value is None
                else _positive_int(value, f"pending_substitution_event.{role}")
            )
        pending_event["work_date"] = _date(
            pending_event["work_date"],
            "pending_substitution_event.work_date",
        )
        if pending_event["resolution_type"] != "substitute":
            raise ValueError("pending_substitution_event resolution_type must be substitute")
        if pending_event["original_assignment_id"] == pending_event["substitute_assignment_id"]:
            raise ValueError("pending_substitution_event assignments must be distinct")
        lineage_ids = [
            pending_event["original_assignment_id"],
            pending_event["substitute_assignment_id"],
            pending_event["prefix_assignment_id"],
            pending_event["suffix_assignment_id"],
        ]
        present_lineage_ids = [item for item in lineage_ids if item is not None]
        if len(present_lineage_ids) != len(set(present_lineage_ids)):
            raise ValueError("pending_substitution_event lineage assignments must be distinct")

    cursor.execute(
        """SELECT case_no, service_days, service_hours_per_day, floor_fee
             FROM orders WHERE case_no = %s FOR UPDATE""",
        (case_no,),
    )
    order = cursor.fetchone()
    if order is None:
        raise ValueError("order does not exist")
    order = _row(order, "order")
    if order.get("case_no") != case_no:
        raise ValueError("order case_no does not match request")
    service_days = _decimal(order.get("service_days"), "orders.service_days")
    if service_days != service_days.to_integral_value():
        raise ValueError("orders.service_days must be a whole number")
    service_hours_per_day = _decimal(order.get("service_hours_per_day"), "orders.service_hours_per_day")
    floor_fee = _money(order.get("floor_fee"), "orders.floor_fee")

    assignments = _q(
        cursor,
        """SELECT id, case_no, staff_id, status, actual_hours, hourly_rate, floor_fee_allocated
             FROM case_staff_assignments WHERE case_no = %s FOR UPDATE""",
        (case_no,), "assignments",
    )
    schedules = _q(
        cursor,
        """SELECT id, case_no, staff_id, assignment_id, work_date, is_work_day, is_double_pay
             FROM staff_schedule WHERE case_no = %s FOR UPDATE""",
        (case_no,), "schedules",
    )
    reviews = _q(
        cursor,
        """SELECT schedule_id, review_status, resolved_assignment_id
             FROM staff_schedule_assignment_reviews WHERE review_status = 'review_required' FOR UPDATE""",
        (), "schedule reviews",
    )
    events = _q(
        cursor,
        """SELECT event_key, case_no, original_assignment_id, original_schedule_id,
                  work_date, resolution_type, substitute_assignment_id
             FROM assignment_schedule_leave_substitution_events
            WHERE case_no = %s FOR UPDATE""",
        (case_no,), "substitution events",
    )
    payments = _q(
        cursor,
        """SELECT assignment_id, case_no, staff_id, service_hours, hourly_rate,
                  service_salary, floor_fee_amount, payment_status
             FROM staff_payments WHERE case_no = %s FOR UPDATE""",
        (case_no,), "staff payments",
    )
    settlements = _q(
        cursor,
        """SELECT d.assignment_id, p.case_no, p.staff_id, p.service_hours, p.hourly_rate,
                  p.service_salary, p.floor_fee_amount
             FROM staff_monthly_settlement_details d
             JOIN staff_payments p ON p.id = d.staff_payment_id
            WHERE p.case_no = %s FOR UPDATE""",
        (case_no,), "settlement details",
    )

    errors: list[dict[str, Any]] = []
    active: dict[int, Mapping[str, Any]] = {}
    cancelled: dict[int, Mapping[str, Any]] = {}
    details: dict[int, dict[str, Any]] = {}
    for assignment in assignments:
        try:
            assignment_id = _positive_int(assignment.get("id"), "assignment.id")
        except ValueError:
            raise
        if assignment.get("case_no") != case_no:
            errors.append(_error("assignment_case_mismatch", assignment_id))
            continue
        if assignment.get("status") == "cancelled":
            cancelled[assignment_id] = assignment
            continue
        if assignment.get("status") not in _ACTIVE_ASSIGNMENT_STATUSES:
            errors.append(_error("assignment_status_invalid", assignment_id))
            continue
        if assignment_id in active:
            errors.append(_error("duplicate_assignment", assignment_id))
            continue
        try:
            staff_id = _positive_int(assignment.get("staff_id"), "assignment.staff_id")
            actual_hours = _decimal(assignment.get("actual_hours"), "assignment.actual_hours")
            hourly_rate = _money(assignment.get("hourly_rate"), "assignment.hourly_rate")
            allocated = _money(assignment.get("floor_fee_allocated"), "assignment.floor_fee_allocated")
        except ValueError:
            errors.append(_error("assignment_amount_invalid", assignment_id))
            continue
        active[assignment_id] = assignment
        details[assignment_id] = {
            "assignment_id": assignment_id, "staff_id": staff_id,
            "actual_hours": actual_hours, "hourly_rate": hourly_rate,
            "floor_fee_allocated": allocated, "actual_service_days": 0,
            "double_pay_hours": Decimal("0"), "service_salary": Decimal("0"),
        }

    schedule_ids: set[int] = set()
    schedules_by_id: dict[int, tuple[int | None, date, str | None]] = {}
    schedule_owner_by_day: dict[tuple[int, date], int] = {}
    days_by_assignment: defaultdict[int, set[date]] = defaultdict(set)
    double_by_assignment: defaultdict[int, int] = defaultdict(int)
    for schedule in schedules:
        assignment_id = schedule.get("assignment_id")
        try:
            schedule_id = _positive_int(schedule.get("id"), "schedule.id")
            day = _date(schedule.get("work_date"), "schedule.work_date")
        except ValueError:
            errors.append(_error("schedule_shape_invalid"))
            continue
        schedule_ids.add(schedule_id)
        if schedule_id in schedules_by_id:
            errors.append(_error("duplicate_schedule", work_date=day))
            continue
        schedules_by_id[schedule_id] = (assignment_id, day, schedule.get("case_no"))
        if assignment_id is None:
            errors.append(_error("legacy_schedule_requires_review", work_date=day))
            continue
        pending_cancelled_root = (
            pending_event is not None
            and assignment_id == pending_event["original_assignment_id"]
            and assignment_id in cancelled
            and schedule_id == pending_event["original_schedule_id"]
        )
        if pending_cancelled_root:
            if (
                schedule.get("case_no") != case_no
                or schedule.get("is_work_day") is not False
                or schedule.get("is_double_pay") is not False
            ):
                errors.append(_error("cancelled_original_schedule_invalid", assignment_id, day))
            continue
        if assignment_id not in active:
            errors.append(_error("schedule_assignment_invalid", work_date=day))
            continue
        detail = details[assignment_id]
        if schedule.get("case_no") != case_no or schedule.get("staff_id") != detail["staff_id"]:
            errors.append(_error("schedule_ownership_mismatch", assignment_id, day))
            continue
        if not isinstance(schedule.get("is_work_day"), bool) or not isinstance(schedule.get("is_double_pay"), bool):
            errors.append(_error("schedule_flag_invalid", assignment_id, day))
            continue
        if schedule["is_double_pay"] and not schedule["is_work_day"]:
            errors.append(_error("double_pay_non_work_day", assignment_id, day))
        if schedule["is_work_day"]:
            owner_key = (detail["staff_id"], day)
            existing_owner = schedule_owner_by_day.get(owner_key)
            if existing_owner is not None and existing_owner != assignment_id:
                errors.append(_error("duplicate_assignment_work_day", assignment_id, day))
            schedule_owner_by_day[owner_key] = assignment_id
            if day in days_by_assignment[assignment_id]:
                errors.append(_error("duplicate_assignment_date", assignment_id, day))
            days_by_assignment[assignment_id].add(day)
            if schedule["is_double_pay"]:
                double_by_assignment[assignment_id] += 1

    for review in reviews:
        if review.get("review_status") == "review_required" and review.get("schedule_id") in schedule_ids:
            errors.append(_error("schedule_review_required"))

    substitute_to_original: dict[int, int] = {}
    event_days: set[tuple[int, date]] = set()
    event_keys: set[str] = set()
    event_view = [*events]
    pending_cancelled_family: tuple[int, int | None, int | None, int] | None = None
    if pending_event is not None:
        if any(event.get("event_key") == pending_event["event_key"] for event in events):
            raise ValueError("pending_substitution_event event_key already exists")
        event_view.append(pending_event)
    for event in event_view:
        event_key = event.get("event_key")
        if event_key is not None:
            if not isinstance(event_key, str) or not event_key.strip() or event_key in event_keys:
                errors.append(_error("duplicate_substitution_event_key"))
                continue
            event_keys.add(event_key)
        try:
            original_id = _positive_int(event.get("original_assignment_id"), "event.original_assignment_id")
            original_schedule_id = _positive_int(
                event.get("original_schedule_id"), "event.original_schedule_id"
            )
            event_day = _date(event.get("work_date"), "event.work_date")
        except ValueError:
            errors.append(_error("substitution_event_invalid"))
            continue
        is_pending = event is pending_event
        cancelled_original = is_pending and original_id in cancelled
        if event.get("case_no") != case_no or (original_id not in active and not cancelled_original):
            errors.append(_error("substitution_event_ownership_mismatch", original_id, event_day))
            continue
        original_schedule = schedules_by_id.get(original_schedule_id)
        if (
            original_schedule is None
            or original_schedule[0] != original_id
            or original_schedule[1] != event_day
            or original_schedule[2] != case_no
        ):
            errors.append(_error("original_schedule_ownership_mismatch", original_id, event_day))
            continue
        if event.get("resolution_type") != "substitute":
            continue
        substitute_id = event.get("substitute_assignment_id")
        if substitute_id not in active or substitute_id == original_id:
            errors.append(_error("substitution_assignment_invalid", original_id, event_day))
            continue
        if cancelled_original:
            original_row = cancelled[original_id]
            try:
                original_staff_id = _positive_int(original_row.get("staff_id"), "assignment.staff_id")
                original_rate = _money(original_row.get("hourly_rate"), "assignment.hourly_rate")
                original_allocated = _money(
                    original_row.get("floor_fee_allocated"), "assignment.floor_fee_allocated"
                )
            except ValueError:
                errors.append(_error("assignment_amount_invalid", original_id))
                continue
            if _decimal(original_row.get("actual_hours"), "assignment.actual_hours") != Decimal("0"):
                errors.append(_error("cancelled_original_amount_nonzero", original_id))
            details[original_id] = {
                "assignment_id": original_id,
                "staff_id": original_staff_id,
                "actual_hours": Decimal("0"),
                "hourly_rate": original_rate,
                "floor_fee_allocated": original_allocated,
                "actual_service_days": 0,
                "double_pay_hours": Decimal("0"),
                "service_salary": Decimal("0"),
            }
            prefix_id = pending_event["prefix_assignment_id"]
            suffix_id = pending_event["suffix_assignment_id"]
            for role_id in (prefix_id, suffix_id):
                if role_id is None:
                    continue
                if role_id not in active:
                    errors.append(_error("substitution_replacement_lineage_invalid", role_id, event_day))
                    continue
                replacement = active[role_id]
                if (
                    replacement.get("case_no") != case_no
                    or replacement.get("staff_id") != original_staff_id
                    or details[role_id]["hourly_rate"] != original_rate
                ):
                    errors.append(_error("substitution_replacement_lineage_invalid", role_id, event_day))
            if details[substitute_id]["hourly_rate"] != original_rate:
                errors.append(_error("substitute_hourly_rate_mismatch", substitute_id, event_day))
            pending_cancelled_family = (original_id, prefix_id, suffix_id, substitute_id)
        if (original_id, event_day) in event_days or substitute_id in substitute_to_original:
            errors.append(_error("duplicate_substitution_event", substitute_id, event_day))
            continue
        event_days.add((original_id, event_day))
        substitute_to_original[substitute_id] = original_id
        if event_day in days_by_assignment[original_id]:
            errors.append(_error("original_owns_substitute_day", original_id, event_day))
        if event_day not in days_by_assignment[substitute_id]:
            errors.append(_error("substitute_does_not_own_event_day", substitute_id, event_day))
        if not cancelled_original and details[substitute_id]["hourly_rate"] != details[original_id]["hourly_rate"]:
            errors.append(_error("substitute_hourly_rate_mismatch", substitute_id, event_day))

    for assignment_id, detail in details.items():
        service_days_count = len(days_by_assignment[assignment_id])
        detail["actual_service_days"] = service_days_count
        expected_hours = service_hours_per_day * service_days_count
        if detail["actual_hours"] != expected_hours:
            errors.append(_error("actual_hours_mismatch", assignment_id))
        double_hours = service_hours_per_day * double_by_assignment[assignment_id]
        detail["double_pay_hours"] = double_hours
        if double_hours > detail["actual_hours"]:
            errors.append(_error("double_pay_hours_exceed_actual", assignment_id))
        detail["service_salary"] = (
            (detail["actual_hours"] + double_hours) * detail["hourly_rate"]
        ).quantize(_CENT, rounding=ROUND_HALF_UP)

    families: defaultdict[int, set[int]] = defaultdict(set)
    for substitute_id, original_id in substitute_to_original.items():
        families[original_id].add(substitute_id)
    for original_id, substitute_ids in families.items():
        if pending_cancelled_family is not None and original_id == pending_cancelled_family[0]:
            _, prefix_id, suffix_id, _ = pending_cancelled_family
            _allocate_family_floor_fee(
                original_id,
                substitute_ids,
                details,
                replacement_ids=(prefix_id, suffix_id),
            )
        else:
            _allocate_family_floor_fee(original_id, substitute_ids, details)
    for assignment_id, detail in details.items():
        if "expected_family_floor_fee" in detail and detail["floor_fee_allocated"] != detail["expected_family_floor_fee"]:
            errors.append(_error("substitution_floor_fee_mismatch", assignment_id))

    payment_by_assignment: dict[int, Mapping[str, Any]] = {}
    for payment in payments:
        assignment_id = payment.get("assignment_id")
        if assignment_id not in active or assignment_id in payment_by_assignment:
            errors.append(_error("payment_assignment_invalid", assignment_id if isinstance(assignment_id, int) else None))
            continue
        payment_by_assignment[assignment_id] = payment
    for payment in [*payments, *settlements]:
        assignment_id = payment.get("assignment_id")
        if assignment_id not in details:
            continue
        detail = details[assignment_id]
        if payment.get("case_no") != case_no or payment.get("staff_id") != detail["staff_id"]:
            errors.append(_error("payment_ownership_mismatch", assignment_id))
            continue
        try:
            snapshot = {
                "service_hours": _decimal(payment.get("service_hours"), "payment.service_hours"),
                "hourly_rate": _money(payment.get("hourly_rate"), "payment.hourly_rate"),
                "service_salary": _money(payment.get("service_salary"), "payment.service_salary"),
                "floor_fee_amount": _money(payment.get("floor_fee_amount"), "payment.floor_fee_amount"),
            }
        except ValueError:
            errors.append(_error("payment_snapshot_invalid", assignment_id))
            continue
        expected = {"service_hours": detail["actual_hours"], "hourly_rate": detail["hourly_rate"],
                    "service_salary": detail["service_salary"], "floor_fee_amount": detail["floor_fee_allocated"]}
        if snapshot != expected:
            errors.append(_error("payment_snapshot_mismatch", assignment_id))

    actual_hours_total = sum((item["actual_hours"] for item in details.values()), Decimal("0"))
    actual_days_total = sum((item["actual_service_days"] for item in details.values()), 0)
    target_hours = service_days * service_hours_per_day
    floor_fee_total = sum((item["floor_fee_allocated"] for item in details.values()), Decimal("0"))
    if actual_hours_total != target_hours:
        errors.append(_error("case_actual_hours_mismatch"))
    if actual_days_total != int(service_days):
        errors.append(_error("case_actual_service_days_mismatch"))
    if floor_fee_total != floor_fee:
        errors.append(_error("case_floor_fee_mismatch"))

    assignment_details = [details[key] for key in sorted(details)]
    return {
        "case_no": case_no, "target_hours": target_hours,
        "actual_hours_total": actual_hours_total, "actual_service_days_total": actual_days_total,
        "floor_fee_total": floor_fee_total, "assignments": assignment_details,
        "errors": _sort_errors(errors), "can_create_staff_payments": not errors,
    }
