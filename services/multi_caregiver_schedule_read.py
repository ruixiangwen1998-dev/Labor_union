"""Read one formal caregiver assignment and only its owned schedule days."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING
from typing import Any

from services.db_service import get_connection


class AssignmentScheduleConflictSnapshotDomainError(Exception):
    """Immutable, JSON-safe domain error for public conflict snapshots."""

    __slots__ = ("_category", "_code", "_details")

    _ALLOWED_CODES = {
        ("not_found", "case_not_found"),
        ("conflict", "assignment_identity_changed_during_snapshot"),
    }

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("AssignmentScheduleConflictSnapshotDomainError is immutable")

    def __init__(self, category: str, code: str, details: dict[str, Any]) -> None:
        if (category, code) not in self._ALLOWED_CODES:
            raise ValueError("invalid conflict snapshot domain error")
        object.__setattr__(self, "_category", category)
        object.__setattr__(self, "_code", code)
        object.__setattr__(self, "_details", self._validate_and_freeze_details(code, details))
        Exception.__init__(self, code)

    @staticmethod
    def _validate_and_freeze_details(code: str, details: dict[str, Any]) -> tuple[Any, ...]:
        if not isinstance(details, dict):
            raise ValueError("domain error details must be a dict")
        if code == "case_not_found":
            case_no = details.get("case_no")
            if set(details) != {"case_no"} or not isinstance(case_no, str):
                raise ValueError("invalid case_not_found details")
            return (case_no,)

        if set(details) != {"case_no", "before", "after"} or not isinstance(
            details.get("case_no"), str
        ):
            raise ValueError("invalid assignment identity drift details")
        before = AssignmentScheduleConflictSnapshotDomainError._freeze_identity_list(details["before"])
        after = AssignmentScheduleConflictSnapshotDomainError._freeze_identity_list(details["after"])
        return (details["case_no"], before, after)

    @staticmethod
    def _freeze_identity_list(value: Any) -> tuple[tuple[int, int], ...]:
        if not isinstance(value, list):
            raise ValueError("assignment identity details must be a list")
        frozen: list[tuple[int, int]] = []
        for row in value:
            if not isinstance(row, dict) or set(row) != {"assignment_id", "staff_id"}:
                raise ValueError("invalid assignment identity detail")
            assignment_id = row["assignment_id"]
            staff_id = row["staff_id"]
            if (
                isinstance(assignment_id, bool)
                or not isinstance(assignment_id, int)
                or assignment_id < 1
                or isinstance(staff_id, bool)
                or not isinstance(staff_id, int)
                or staff_id < 1
            ):
                raise ValueError("invalid assignment identity detail")
            frozen.append((assignment_id, staff_id))
        if frozen != sorted(set(frozen)):
            raise ValueError("assignment identity details must be canonical")
        return tuple(frozen)

    @property
    def category(self) -> str:
        return self._category

    @property
    def code(self) -> str:
        return self._code

    @property
    def details(self) -> dict[str, Any]:
        return self.as_dict()["details"]

    def as_dict(self) -> dict[str, Any]:
        if self._code == "case_not_found":
            details: dict[str, Any] = {"case_no": self._details[0]}
        else:
            details = {
                "case_no": self._details[0],
                "before": [
                    {"assignment_id": assignment_id, "staff_id": staff_id}
                    for assignment_id, staff_id in self._details[1]
                ],
                "after": [
                    {"assignment_id": assignment_id, "staff_id": staff_id}
                    for assignment_id, staff_id in self._details[2]
                ],
            }
        return {"category": self._category, "code": self._code, "details": details}


def _validate_assignment_id(assignment_id: Any) -> int:
    if isinstance(assignment_id, bool) or not isinstance(assignment_id, int) or assignment_id < 1:
        raise ValueError("assignment_id must be a positive integer")
    return assignment_id


def _validate_case_no(case_no: Any) -> str:
    if not isinstance(case_no, str) or not case_no.strip():
        raise ValueError("case_no must be a non-empty string")
    return case_no.strip()


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        if value != value.strip():
            raise ValueError("invalid date value")
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("invalid date value")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid date value") from exc
    raise ValueError("invalid date value")


def _validate_case_assignment(assignment: dict[str, Any], expected_case_no: str) -> dict[str, Any]:
    if not isinstance(assignment, dict):
        raise ValueError("assignment must be a dict")
    if not isinstance(expected_case_no, str) or not expected_case_no.strip():
        raise ValueError("expected_case_no must be a non-empty string")

    assignment_id = assignment.get("id")
    staff_id = assignment.get("staff_id")
    if isinstance(assignment_id, bool) or not isinstance(assignment_id, int) or assignment_id < 1:
        raise ValueError("assignment_id must be a positive integer")
    if isinstance(staff_id, bool) or not isinstance(staff_id, int) or staff_id < 1:
        raise ValueError("staff_id must be a positive integer")

    if assignment.get("case_no") != expected_case_no:
        raise ValueError("assignment case_no mismatch")

    if assignment.get("status") == "cancelled":
        raise ValueError("cancelled assignment should not be returned")

    if "planned_hours" not in assignment:
        raise ValueError("planned_hours is required")
    if "actual_hours" not in assignment:
        raise ValueError("actual_hours is required")

    start_raw = assignment.get("assigned_start_date")
    end_raw = assignment.get("assigned_end_date")
    if start_raw is None or end_raw is None:
        raise ValueError("assignment date range is incomplete")
    assigned_start_date = _as_date(start_raw)
    assigned_end_date = _as_date(end_raw)
    if assigned_start_date > assigned_end_date:
        raise ValueError("assignment assigned_start_date cannot be after assigned_end_date")
    assignment["assigned_start_date"] = assigned_start_date
    assignment["assigned_end_date"] = assigned_end_date
    return assignment


def list_case_schedule_assignments(case_no: str) -> dict[str, Any]:
    """List selectable, non-cancelled formal assignments for one chosen case."""

    case_no = _validate_case_no(case_no)
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT a.id, a.case_no, a.staff_id, a.status,
                          a.assigned_start_date, a.assigned_end_date,
                          a.original_assigned_start_date,
                          a.original_assigned_end_date,
                          a.planned_hours, a.actual_hours, o.service_days,
                          o.service_hours_per_day,
                          s.name AS staff_name,
                          (SELECT COUNT(*)
                             FROM staff_schedule ss
                            WHERE ss.assignment_id = a.id
                              AND ss.is_work_day = TRUE) AS actual_service_days,
                          (SELECT COUNT(*)
                             FROM staff_schedule ss
                            WHERE ss.assignment_id = a.id
                              AND ss.is_work_day = FALSE) AS rest_days,
                          (SELECT COUNT(*)
                             FROM assignment_schedule_leave_substitution_events e
                            WHERE e.substitute_assignment_id = a.id
                              AND e.resolution_type = 'substitute') AS substitute_service_days,
                          (SELECT COUNT(*)
                             FROM assignment_schedule_leave_substitution_events e
                            WHERE e.original_assignment_id = a.id
                              AND e.resolution_type = 'defer_following_assignments') AS deferred_leave_days,
                          (SELECT COUNT(*)
                              FROM assignment_schedule_leave_substitution_events e
                             WHERE e.original_assignment_id = a.id
                               AND e.resolution_type IN ('defer_following_assignments', 'substitute')) AS leave_resolution_days
                     FROM case_staff_assignments a
                     JOIN orders o ON o.case_no = a.case_no
                     JOIN staff s ON s.id = a.staff_id
                    WHERE a.case_no = %s AND a.status <> 'cancelled'
                    ORDER BY a.assigned_start_date ASC, a.id ASC""",
                (case_no,),
            )
            assignments = cursor.fetchall()
        assignments = [_validate_case_assignment(assignment, case_no) for assignment in assignments or []]
        if not assignments:
            return {"assignments": []}
        for assignment in assignments:
            hours_per_day = Decimal(str(assignment["service_hours_per_day"]))
            if not hours_per_day.is_finite() or hours_per_day <= 0:
                raise ValueError("service_hours_per_day must be positive")
            planned_hours = Decimal(str(assignment["planned_hours"] or 0))
            assignment["required_service_days"] = int(
                (planned_hours / hours_per_day).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            assignment["original_assigned_start_date"] = _as_date(
                assignment.get("original_assigned_start_date")
                or assignment["assigned_start_date"]
            )
            assignment["original_assigned_end_date"] = _as_date(
                assignment.get("original_assigned_end_date")
                or assignment["assigned_end_date"]
            )
            assignment["adjusted_assigned_start_date"] = assignment[
                "assigned_start_date"
            ]
            assignment["adjusted_assigned_end_date"] = assignment[
                "assigned_end_date"
            ]
            assignment["original_scheduled_service_days"] = assignment[
                "required_service_days"
            ]
            for field in (
                "actual_service_days",
                "rest_days",
                "substitute_service_days",
                "deferred_leave_days",
                "leave_resolution_days",
            ):
                assignment[field] = int(assignment.get(field) or 0)
            assignment["makeup_service_days"] = assignment[
                "deferred_leave_days"
            ]
        target_service_days = int(assignments[0]["service_days"] or 0)
        hours_per_day = Decimal(str(assignments[0]["service_hours_per_day"]))
        target_service_hours = Decimal(target_service_days) * hours_per_day
        actual_service_days = sum(
            row["actual_service_days"] for row in assignments
        )
        actual_hours = sum(
            (Decimal(str(row["actual_hours"] or 0)) for row in assignments),
            Decimal("0"),
        )
        return {
            "assignments": assignments,
            "summary": {
                "required_service_days": sum(
                    row["required_service_days"] for row in assignments
                ),
                "actual_service_days": actual_service_days,
                "actual_hours": str(actual_hours),
                "adjusted_start_date": min(
                    row["assigned_start_date"] for row in assignments
                ),
                "adjusted_end_date": max(
                    row["assigned_end_date"] for row in assignments
                ),
                "target_service_days": target_service_days,
                "target_service_hours": str(target_service_hours),
                "has_service_gap": (
                    actual_service_days < target_service_days
                    or actual_hours < target_service_hours
                ),
                "has_service_overlap": (
                    actual_service_days > target_service_days
                    or actual_hours > target_service_hours
                ),
                "rest_days": sum(row["rest_days"] for row in assignments),
                "substitute_service_days": sum(
                    row["substitute_service_days"] for row in assignments
                ),
                "deferred_leave_days": sum(
                    row["deferred_leave_days"] for row in assignments
                ),
            },
        }
    finally:
        connection.close()


def get_assignment_schedule(assignment_id: int) -> dict[str, Any]:
    """Return only the daily schedule rows owned by one explicit assignment.

    The query deliberately does not use ``orders.staff_id`` or a date-based
    fallback. Legacy rows without an assignment relation remain outside this
    read model until an administrator explicitly reviews them.
    """

    assignment_id = _validate_assignment_id(assignment_id)
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_DATE as current_date")
            current_date_row = cursor.fetchone()
            if current_date_row is None:
                raise ValueError("unable to load database current date")
            if isinstance(current_date_row, dict):
                database_current_date = current_date_row.get(
                    "current_date",
                    current_date_row.get("CURRENT_DATE"),
                )
            else:
                database_current_date = current_date_row[0] if current_date_row else None
            if database_current_date is None:
                raise ValueError("unable to load database current date")
            if isinstance(database_current_date, datetime):
                database_current_date = database_current_date.date()
            elif isinstance(database_current_date, str):
                database_current_date = date.fromisoformat(database_current_date)
            else:
                database_current_date = _as_date(database_current_date)

            cursor.execute(
                """SELECT a.id, a.case_no, a.staff_id, a.status,
                          a.assigned_start_date, a.assigned_end_date,
                          a.planned_hours, a.actual_hours,
                          o.service_hours_per_day,
                          s.name AS staff_name, c.name AS client_name
                     FROM case_staff_assignments a
                     JOIN orders o ON o.case_no = a.case_no
                     JOIN staff s ON s.id = a.staff_id
                     JOIN clients c ON c.id = o.client_id
                    WHERE a.id = %s""",
                (assignment_id,),
            )
            assignment = cursor.fetchone()
            if assignment is None:
                raise ValueError("assignment does not exist")

            cursor.execute(
                """SELECT id
                   FROM actual_hours_adjustments
                   WHERE assignment_id = %s
                   LIMIT 1""",
                (assignment_id,),
            )
            has_actual_hours_adjustments = bool(cursor.fetchone())

            cursor.execute(
                """SELECT id
                   FROM staff_payments
                   WHERE assignment_id = %s AND payment_status <> 'cancelled'
                   LIMIT 1""",
                (assignment_id,),
            )
            has_active_staff_payment = bool(cursor.fetchone())

            cursor.execute(
                """SELECT smsd.id
                   FROM staff_monthly_settlement_details smsd
                   JOIN staff_monthly_settlements sms ON sms.id = smsd.settlement_id
                   WHERE smsd.assignment_id = %s AND sms.status <> 'cancelled'
                   LIMIT 1""",
                (assignment_id,),
            )
            has_active_monthly_settlement = bool(cursor.fetchone())

            reasons: list[str] = []
            if assignment["status"] == "cancelled":
                reasons.append("cancelled_assignment")
            if has_actual_hours_adjustments:
                reasons.append("actual_hours_adjustment_exists")
            if has_active_staff_payment:
                reasons.append("active_staff_payment")
            if has_active_monthly_settlement:
                reasons.append("active_monthly_settlement")

            cursor.execute(
                """SELECT id, case_no, staff_id, assignment_id, work_date,
                          is_work_day, is_double_pay, notes
                     FROM staff_schedule
                    WHERE assignment_id = %s
                    ORDER BY work_date ASC, id ASC""",
                (assignment_id,),
            )
            schedule_days = cursor.fetchall()
            for schedule_day in schedule_days:
                if schedule_day.get("assignment_id") != assignment_id:
                    raise ValueError("schedule day does not belong to assignment")
                if schedule_day.get("case_no") != assignment["case_no"]:
                    raise ValueError("schedule day case does not match assignment")
                if schedule_day.get("staff_id") != assignment["staff_id"]:
                    raise ValueError("schedule day staff does not match assignment")
                work_date = _as_date(schedule_day["work_date"])
                schedule_day["is_historical"] = work_date < database_current_date

            return {
                "assignment": assignment,
                "schedule_days": schedule_days,
                "database_current_date": database_current_date,
                "adjustment_guard": {
                    "is_cancelled": assignment["status"] == "cancelled",
                    "has_actual_hours_adjustments": has_actual_hours_adjustments,
                    "has_active_staff_payment": has_active_staff_payment,
                    "has_active_monthly_settlement": has_active_monthly_settlement,
                    "reasons": reasons,
                },
            }
    finally:
        connection.close()


def _canonical_assignment_identity(
    assignment_rows: list[dict[str, Any]],
) -> list[dict[str, int]]:
    """Return strict, stable assignment identities without accepting duplicate facts."""

    identities: set[tuple[int, int]] = set()
    for row in assignment_rows:
        if not isinstance(row, dict):
            raise ValueError("invalid assignment identity row")
        assignment_id = row.get("id")
        staff_id = row.get("staff_id")
        if (
            isinstance(assignment_id, bool)
            or not isinstance(assignment_id, int)
            or assignment_id < 1
            or isinstance(staff_id, bool)
            or not isinstance(staff_id, int)
            or staff_id < 1
        ):
            raise ValueError("invalid assignment identity")
        identity = (assignment_id, staff_id)
        if identity in identities:
            raise ValueError("duplicate assignment identity")
        identities.add(identity)
    return [
        {"assignment_id": assignment_id, "staff_id": staff_id}
        for assignment_id, staff_id in sorted(identities)
    ]


def _get_case_schedule_conflict_snapshot_with_cursor(
    cursor: Any,
    case_no: str,
    extra_staff_ids: list[int],
    range_start: str,
    range_end: str,
    lock_rows: bool,
    *,
    typed_case_not_found: bool,
    verify_assignment_identity: bool,
) -> dict[str, Any]:
    """Return canonical conflict facts using a caller-owned cursor."""

    if cursor is None or not callable(getattr(cursor, "execute", None)):
        raise ValueError("cursor must support execute")
    if type(lock_rows) is not bool:
        raise ValueError("lock_rows must be a bool")
    case_no = _validate_case_no(case_no)
    if not isinstance(extra_staff_ids, list):
        raise ValueError("extra_staff_ids must be a list")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in extra_staff_ids
    ):
        raise ValueError("extra_staff_ids must contain positive integers")
    canonical_extra_staff_ids = tuple(sorted(set(extra_staff_ids)))
    start_date = _as_date(range_start)
    end_date = _as_date(range_end)
    if start_date > end_date:
        raise ValueError("range_start cannot be after range_end")

    def require_id(row: dict[str, Any], field: str) -> int:
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"invalid {field}")
        return value

    def require_case(row: dict[str, Any]) -> None:
        if row.get("case_no") != case_no:
            raise ValueError("row case_no mismatch")

    def normalize_date(row: dict[str, Any], field: str) -> None:
        row[field] = _as_date(row.get(field))

    def rows(value: Any, label: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)) or any(not isinstance(row, dict) for row in value):
            raise ValueError(f"invalid {label} rows")
        return [dict(row) for row in value]

    lock_clause = " FOR UPDATE" if lock_rows else ""
    if True:
        if True:
            cursor.execute("SELECT CURRENT_DATE AS current_date")
            current_date_row = cursor.fetchone()
            if not isinstance(current_date_row, dict):
                raise ValueError("unable to load database current date")
            database_current_date = _as_date(
                current_date_row.get("current_date", current_date_row.get("CURRENT_DATE"))
            )

            cursor.execute(
                f"SELECT case_no FROM orders WHERE case_no = %s{lock_clause}",
                (case_no,),
            )
            order_row = cursor.fetchone()
            if order_row is None:
                if typed_case_not_found:
                    raise AssignmentScheduleConflictSnapshotDomainError(
                        "not_found", "case_not_found", {"case_no": case_no}
                    )
                raise ValueError("case does not exist")
            if not isinstance(order_row, dict):
                raise ValueError("order row must be a mapping")
            order_case_no = order_row.get("case_no")
            if not isinstance(order_case_no, str) or order_case_no != case_no:
                raise ValueError("order case_no mismatch")

            cursor.execute(
                f"""SELECT id, case_no, staff_id, status, assigned_start_date,
                          assigned_end_date, planned_hours, actual_hours
                     FROM case_staff_assignments
                    WHERE case_no = %s
                    ORDER BY id ASC{lock_clause}""",
                (case_no,),
            )
            assignments = rows(cursor.fetchall(), "assignment")
            assignment_ids: set[int] = set()
            assignment_staff_ids: set[int] = set()
            for row in assignments:
                require_case(row)
                assignment_ids.add(require_id(row, "id"))
                assignment_staff_ids.add(require_id(row, "staff_id"))
                normalize_date(row, "assigned_start_date")
                normalize_date(row, "assigned_end_date")
                if row["assigned_start_date"] > row["assigned_end_date"]:
                    raise ValueError("invalid assignment date range")
            assignments.sort(key=lambda row: row["id"])
            initial_assignment_identity = _canonical_assignment_identity(assignments)
            query_staff_ids = sorted(assignment_staff_ids.union(canonical_extra_staff_ids))

            if query_staff_ids:
                placeholders = ", ".join(["%s"] * len(query_staff_ids))
                cursor.execute(
                    f"""SELECT id, case_no, staff_id, assignment_id, work_date,
                               is_work_day, is_double_pay, notes
                          FROM staff_schedule
                         WHERE work_date BETWEEN %s AND %s
                           AND (case_no = %s OR staff_id IN ({placeholders}))
                         ORDER BY staff_id ASC, work_date ASC, id ASC{lock_clause}""",
                    (start_date, end_date, case_no, *query_staff_ids),
                )
            else:
                cursor.execute(
                    f"""SELECT id, case_no, staff_id, assignment_id, work_date,
                               is_work_day, is_double_pay, notes
                          FROM staff_schedule
                         WHERE work_date BETWEEN %s AND %s
                           AND case_no = %s
                         ORDER BY staff_id ASC, work_date ASC, id ASC{lock_clause}""",
                    (start_date, end_date, case_no),
                )
            schedule_days = rows(cursor.fetchall(), "schedule")
            for row in schedule_days:
                require_id(row, "id")
                require_id(row, "staff_id")
                normalize_date(row, "work_date")
                assignment_id = row.get("assignment_id")
                if assignment_id is None:
                    row["requires_review"] = True
                else:
                    require_id(row, "assignment_id")
                    row["requires_review"] = False
                    if row.get("case_no") == case_no and assignment_id not in assignment_ids:
                        raise ValueError("schedule assignment ownership mismatch")
            schedule_days.sort(key=lambda row: (row["staff_id"], row["work_date"], row["id"]))

            active_lock_days = []
            if query_staff_ids:
                placeholders = ", ".join(["%s"] * len(query_staff_ids))
                cursor.execute(
                    f"""SELECT d.id, l.id AS lock_id, l.plan_id, p.case_no,
                               d.segment_id, d.staff_id, d.lock_date
                          FROM caregiver_availability_lock_days d
                          JOIN caregiver_availability_locks l ON l.id = d.lock_id
                          JOIN caregiver_matching_plans p ON p.id = l.plan_id
                         WHERE d.staff_id IN ({placeholders})
                           AND d.lock_date BETWEEN %s AND %s
                           AND l.status = 'active' AND l.is_active = 1
                           AND d.active_marker = 1
                         ORDER BY d.staff_id ASC, d.lock_date ASC, d.id ASC{lock_clause}""",
                    (*query_staff_ids, start_date, end_date),
                )
                active_lock_days = rows(cursor.fetchall(), "active lock")
                for row in active_lock_days:
                    require_id(row, "id")
                    require_id(row, "lock_id")
                    require_id(row, "plan_id")
                    require_id(row, "segment_id")
                    require_id(row, "staff_id")
                    normalize_date(row, "lock_date")
                active_lock_days.sort(key=lambda row: (row["staff_id"], row["lock_date"], row["id"]))

            cursor.execute(
                f"""SELECT id, case_no, original_assignment_id, original_schedule_id,
                          work_date, resolution_type, substitute_assignment_id,
                          event_key, occurred_at
                     FROM assignment_schedule_leave_substitution_events
                    WHERE case_no = %s AND work_date BETWEEN %s AND %s
                    ORDER BY work_date ASC, id ASC{lock_clause}""",
                (case_no, start_date, end_date),
            )
            events = rows(cursor.fetchall(), "event")
            for row in events:
                require_case(row)
                require_id(row, "id")
                require_id(row, "original_assignment_id")
                require_id(row, "original_schedule_id")
                if row.get("substitute_assignment_id") is not None:
                    require_id(row, "substitute_assignment_id")
                normalize_date(row, "work_date")
            events.sort(key=lambda row: (row["work_date"], row["id"]))

            cursor.execute(
                f"""SELECT h.id, a.case_no, h.assignment_id, h.original_hours,
                          h.adjusted_hours, h.reason, h.adjusted_at
                     FROM actual_hours_adjustments h
                     JOIN case_staff_assignments a ON a.id = h.assignment_id
                    WHERE a.case_no = %s
                    ORDER BY h.assignment_id ASC, h.id ASC{lock_clause}""",
                (case_no,),
            )
            adjustments = rows(cursor.fetchall(), "actual hours adjustment")
            for row in adjustments:
                require_case(row)
                require_id(row, "id")
                require_id(row, "assignment_id")
            adjustments.sort(key=lambda row: (row["assignment_id"], row["id"]))

            cursor.execute(
                f"""SELECT sp.id, a.case_no, sp.assignment_id, sp.payment_status
                     FROM staff_payments sp
                     JOIN case_staff_assignments a ON a.id = sp.assignment_id
                    WHERE a.case_no = %s AND sp.payment_status <> 'cancelled'
                    ORDER BY sp.assignment_id ASC, sp.id ASC{lock_clause}""",
                (case_no,),
            )
            payments = rows(cursor.fetchall(), "payment")
            for row in payments:
                require_case(row)
                require_id(row, "id")
                require_id(row, "assignment_id")
            payments.sort(key=lambda row: (row["assignment_id"], row["id"]))

            cursor.execute(
                f"""SELECT smsd.id, a.case_no, smsd.assignment_id,
                          smsd.settlement_id, sms.status
                     FROM staff_monthly_settlement_details smsd
                     JOIN staff_monthly_settlements sms ON sms.id = smsd.settlement_id
                     JOIN case_staff_assignments a ON a.id = smsd.assignment_id
                    WHERE a.case_no = %s AND sms.status <> 'cancelled'
                    ORDER BY smsd.assignment_id ASC, smsd.id ASC{lock_clause}""",
                (case_no,),
            )
            settlements = rows(cursor.fetchall(), "settlement")
            for row in settlements:
                require_case(row)
                require_id(row, "id")
                require_id(row, "assignment_id")
                require_id(row, "settlement_id")
            settlements.sort(key=lambda row: (row["assignment_id"], row["id"]))

            if verify_assignment_identity:
                cursor.execute(
                    """SELECT id, staff_id
                         FROM case_staff_assignments
                        WHERE case_no = %s
                        ORDER BY id ASC, staff_id ASC""",
                    (case_no,),
                )
                final_assignment_identity = _canonical_assignment_identity(
                    rows(cursor.fetchall(), "final assignment identity")
                )
                if final_assignment_identity != initial_assignment_identity:
                    raise AssignmentScheduleConflictSnapshotDomainError(
                        "conflict",
                        "assignment_identity_changed_during_snapshot",
                        {
                            "case_no": case_no,
                            "before": initial_assignment_identity,
                            "after": final_assignment_identity,
                        },
                    )

            return {
                "database_current_date": database_current_date,
                "assignments": assignments,
                "assignment_schedule_days": schedule_days,
                "active_lock_days": active_lock_days,
                "historical_facts": {
                    "leave_substitution_events": events,
                    "actual_hours_adjustments": adjustments,
                    "non_cancelled_payments": payments,
                    "active_settlements": settlements,
                },
            }


def get_case_schedule_conflict_snapshot_with_cursor(
    cursor: Any,
    case_no: str,
    extra_staff_ids: list[int],
    range_start: str,
    range_end: str,
    lock_rows: bool,
) -> dict[str, Any]:
    """Return canonical conflict facts using a caller-owned cursor."""

    return _get_case_schedule_conflict_snapshot_with_cursor(
        cursor,
        case_no,
        extra_staff_ids,
        range_start,
        range_end,
        lock_rows,
        typed_case_not_found=False,
        verify_assignment_identity=False,
    )


def get_case_schedule_conflict_snapshot(
    case_no: str,
    extra_staff_ids: list[int],
    range_start: str,
    range_end: str,
) -> dict[str, Any]:
    """Return canonical scheduling conflict facts from one read-only DB session."""

    case_no = _validate_case_no(case_no)
    if not isinstance(extra_staff_ids, list):
        raise ValueError("extra_staff_ids must be a list")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in extra_staff_ids
    ):
        raise ValueError("extra_staff_ids must contain positive integers")
    extra_staff_ids = tuple(sorted(set(extra_staff_ids)))
    start_date = _as_date(range_start)
    end_date = _as_date(range_end)
    if start_date > end_date:
        raise ValueError("range_start cannot be after range_end")

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            return _get_case_schedule_conflict_snapshot_with_cursor(
                cursor,
                case_no,
                list(extra_staff_ids),
                range_start,
                range_end,
                False,
                typed_case_not_found=True,
                verify_assignment_identity=True,
            )
    finally:
        connection.close()
