"""Matching plan version persistence service."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from services.db_service import get_connection
from services.caregiver_segment_availability_query_service import (
    search_segmented_caregiver_availability,
)

_STRICT_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_case_no(case_no: Any) -> str:
    if not isinstance(case_no, str):
        raise ValueError("case_no is required")
    normalized = case_no.strip()
    if not normalized:
        raise ValueError("case_no is required")
    return normalized


def _normalize_created_by(created_by: Any) -> str:
    if not isinstance(created_by, str):
        raise ValueError("created_by is required")
    normalized = created_by.strip()
    if not normalized:
        raise ValueError("created_by is required")
    return normalized


def _normalize_ymd(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    if not _STRICT_YMD.fullmatch(value):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    return value


def _normalize_db_date(value: Any, field_name: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if hasattr(value, "isoformat"):
        value_str = value.isoformat()
    elif isinstance(value, str):
        value_str = value
    else:
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    return _normalize_ymd(value_str, field_name)


def _normalize_segments(segments: Any) -> list[dict[str, Any]]:
    if not isinstance(segments, list):
        raise ValueError("segments must be a list")
    if len(segments) not in (1, 2, 3, 4):
        raise ValueError("segments must contain 1, 2, 3, or 4 items")

    normalized: list[dict[str, Any]] = []
    seen_staff: set[int] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("segment must be a dict")
        start_value = segment.get("start_date", segment.get("assigned_start_date"))
        end_value = segment.get("end_date", segment.get("assigned_end_date"))
        if start_value is None or end_value is None:
            raise ValueError("segment.start_date and segment.end_date are required")
        extra_fields = set(segment.keys()) - {
            "staff_id",
            "start_date",
            "end_date",
            "assigned_start_date",
            "assigned_end_date",
        }
        if extra_fields:
            raise ValueError("segment contains unknown fields")
        if "start_date" in segment and "assigned_start_date" in segment:
            if segment["start_date"] != segment["assigned_start_date"]:
                raise ValueError("segment start_date mismatch")
        if "end_date" in segment and "assigned_end_date" in segment:
            if segment["end_date"] != segment["assigned_end_date"]:
                raise ValueError("segment end_date mismatch")

        staff_id = segment.get("staff_id")
        if isinstance(staff_id, bool) or not isinstance(staff_id, int):
            raise ValueError("segment.staff_id must be a positive integer")
        if staff_id <= 0:
            raise ValueError("segment.staff_id must be a positive integer")
        if staff_id in seen_staff:
            raise ValueError("segment staff_id must be unique")
        seen_staff.add(staff_id)

        start_date = _normalize_ymd(start_value, "segment.start_date")
        end_date = _normalize_ymd(end_value, "segment.end_date")
        if start_date > end_date:
            raise ValueError("segment.start_date cannot be after segment.end_date")
        normalized.append(
            {
                "staff_id": staff_id,
                "assigned_start_date": start_date,
                "assigned_end_date": end_date,
            }
        )

    for previous, current in zip(normalized, normalized[1:]):
        previous_end = datetime.strptime(
            previous["assigned_end_date"], "%Y-%m-%d"
        ).date()
        current_start = datetime.strptime(
            current["assigned_start_date"], "%Y-%m-%d"
        ).date()
        expected_start = previous_end + timedelta(days=1)
        if current_start < expected_start:
            raise ValueError("segments must not overlap or be out of order")
        if current_start > expected_start:
            raise ValueError("segments must be contiguous without gaps")

    return normalized


def _segments_signature(segments: list[dict[str, Any]]) -> tuple[tuple[int, int, str, str], ...]:
    return tuple(
        (
            idx,
            segment["staff_id"],
            segment["assigned_start_date"],
            segment["assigned_end_date"],
        )
        for idx, segment in enumerate(segments)
    )


def _completion_signature(combo: list[dict[str, Any]]) -> tuple[tuple[int, int, str, str], ...]:
    return tuple(
        (
            int(item["segment_index"]),
            int(item["staff_id"]),
            _normalize_ymd(item["start_date"], "combo.start_date"),
            _normalize_ymd(item["end_date"], "combo.end_date"),
        )
        for item in combo
    )


def _safe_close(resource: Any, state: dict[str, bool]) -> BaseException | None:
    if state.get("closed"):
        return None

    state["closed"] = True
    try:
        resource.close()
    except BaseException as exc:  # noqa: BLE001
        return exc
    return None


def _as_sql_payload(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "staff_id": segment["staff_id"],
            "start_date": segment["assigned_start_date"],
            "end_date": segment["assigned_end_date"],
        }
        for segment in segments
    ]


def create_matching_plan_version(
    case_no: Any,
    segments: Any,
    created_by: Any,
    as_of: Any,
) -> dict[str, Any]:
    """Create or reuse a proposed matching plan version for one case.

    Validation uses the latest availability result first. If payload matches an
    exact complete combination, persistence is done in one transaction.
    """

    case_no_value = _normalize_case_no(case_no)
    created_by_value = _normalize_created_by(created_by)
    as_of_value = _normalize_ymd(as_of, "as_of")
    normalized_segments = _normalize_segments(segments)

    availability = search_segmented_caregiver_availability(
        case_no=case_no_value,
        segment_count=len(normalized_segments),
        segment_drafts=_as_sql_payload(normalized_segments),
        as_of=as_of_value,
    )

    complete_combinations = availability.get("complete_combinations")
    if not isinstance(complete_combinations, list):
        raise ValueError("availability result malformed")

    feasibility = availability.get("feasibility")
    if feasibility != "complete":
        raise ValueError("submitted segments must match a complete combination")

    conflicts = availability.get("conflicts")
    if not isinstance(conflicts, list):
        raise ValueError("availability result malformed")
    if conflicts:
        raise ValueError("submitted segments must match a complete combination")

    target_signature = _segments_signature(normalized_segments)
    matched = any(
        _completion_signature(item) == target_signature for item in complete_combinations
    )
    if not matched:
        raise ValueError("submitted segments must match a complete combination")

    connection = None
    cursor = None
    cursor_closed = {"closed": False}
    connection_closed = {"closed": False}
    try:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT o.case_no, o.status, o.start_date, o.end_date\n"
            "FROM orders o\n"
            "WHERE o.case_no = %s FOR UPDATE",
            (case_no_value,),
        )
        order_row = cursor.fetchone()
        if order_row is None:
            raise ValueError("case not found")

        if order_row["status"] != "洽談中":
            raise ValueError("case is not in negotiation stage")

        _normalize_db_date(order_row["start_date"], "start_date")
        _normalize_db_date(order_row["end_date"], "end_date")

        cursor.execute(
            "SELECT id, version, status, is_active\n"
            "FROM caregiver_matching_plans\n"
            "WHERE case_no = %s\n"
            "ORDER BY version DESC\n"
            "FOR UPDATE",
            (case_no_value,),
        )
        plans = cursor.fetchall() or []
        if any(row["status"] == "accepted" for row in plans):
            raise ValueError("case is not editable while an accepted plan exists")

        cursor.execute(
            "SELECT p.id AS plan_id,\n"
            "       s.segment_order,\n"
            "       s.staff_id,\n"
            "       s.assigned_start_date,\n"
            "       s.assigned_end_date\n"
            "FROM caregiver_matching_plan_segments s\n"
            "INNER JOIN caregiver_matching_plans p ON p.id = s.plan_id\n"
            "WHERE p.case_no = %s\n"
            "ORDER BY p.id, s.segment_order\n"
            "FOR UPDATE",
            (case_no_value,),
        )
        plan_segments = cursor.fetchall() or []
        segments_by_plan: dict[int, list[dict[str, Any]]] = {}
        for row in plan_segments:
            normalized_row = {
                "plan_id": row["plan_id"],
                "segment_order": row["segment_order"],
                "staff_id": row["staff_id"],
                "assigned_start_date": _normalize_db_date(
                    row["assigned_start_date"],
                    "assigned_start_date",
                ),
                "assigned_end_date": _normalize_db_date(
                    row["assigned_end_date"],
                    "assigned_end_date",
                ),
            }
            segments_by_plan.setdefault(row["plan_id"], []).append(normalized_row)

        cursor.execute(
            "SELECT l.id,\n"
            "       l.plan_id,\n"
            "       l.status,\n"
            "       l.is_active\n"
            "FROM caregiver_availability_locks l\n"
            "INNER JOIN caregiver_matching_plans p ON p.id = l.plan_id\n"
            "WHERE p.case_no = %s\n"
            "  AND l.status = 'active'\n"
            "  AND l.is_active = 1\n"
            "FOR UPDATE",
            (case_no_value,),
        )
        active_locks = cursor.fetchall() or []

        cursor.execute(
            "SELECT ld.id,\n"
            "       ld.lock_id,\n"
            "       ld.segment_id,\n"
            "       ld.staff_id,\n"
            "       ld.lock_date,\n"
            "       ld.active_marker\n"
            "FROM caregiver_availability_lock_days ld\n"
            "INNER JOIN caregiver_availability_locks l ON l.id = ld.lock_id\n"
            "INNER JOIN caregiver_matching_plans p ON p.id = l.plan_id\n"
            "WHERE p.case_no = %s\n"
            "  AND l.status = 'active'\n"
            "  AND l.is_active = 1\n"
            "  AND ld.active_marker = 1\n"
            "FOR UPDATE",
            (case_no_value,),
        )
        active_lock_days = cursor.fetchall() or []

        if active_locks or active_lock_days:
            raise ValueError("case has an active availability lock")

        for plan in plans:
            if plan.get("status") != "proposed" or plan.get("is_active") != 1:
                continue
            current_segments = sorted(
                segments_by_plan.get(plan["id"], []),
                key=lambda item: item["segment_order"],
            )
            if _segments_signature(current_segments) == target_signature:
                connection.rollback()
                return {
                    "plan_id": plan["id"],
                    "case_no": plan.get("case_no", case_no_value),
                    "version": plan["version"],
                    "status": "proposed",
                    "result": "existing",
                    "segments": [
                        {
                            "segment_order": row["segment_order"],
                            "staff_id": row["staff_id"],
                            "assigned_start_date": row["assigned_start_date"],
                            "assigned_end_date": row["assigned_end_date"],
                        }
                        for row in current_segments
                    ],
                }

        cursor.execute(
            "SELECT MAX(version) AS max_version\n"
            "FROM caregiver_matching_plans\n"
            "WHERE case_no = %s",
            (case_no_value,),
        )
        max_version_row = cursor.fetchone() or {}
        max_version = int(max_version_row.get("max_version") or 0)
        new_version = max_version + 1

        cursor.execute(
            "UPDATE caregiver_matching_plans\n"
            "SET status = 'superseded', is_active = NULL\n"
            "WHERE case_no = %s\n"
            "  AND is_active = 1\n"
            "  AND status IN ('draft', 'proposed')",
            (case_no_value,),
        )
        cursor.execute(
            "INSERT INTO caregiver_matching_plans\n"
            "(case_no, version, status, is_active, start_date, end_date, created_by)\n"
            "VALUES (%s, %s, 'proposed', 1, %s, %s, %s)",
            (
                case_no_value,
                new_version,
                normalized_segments[0]["assigned_start_date"],
                normalized_segments[-1]["assigned_end_date"],
                created_by_value,
            ),
        )
        plan_id = cursor.lastrowid
        for index, segment in enumerate(normalized_segments, start=1):
            cursor.execute(
                "INSERT INTO caregiver_matching_plan_segments\n"
                "(plan_id, segment_order, staff_id, assigned_start_date, assigned_end_date)\n"
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    plan_id,
                    index,
                    segment["staff_id"],
                    segment["assigned_start_date"],
                    segment["assigned_end_date"],
                ),
            )

        connection.commit()
        return {
            "plan_id": plan_id,
            "case_no": case_no_value,
            "version": new_version,
            "status": "proposed",
            "result": "created",
            "segments": [
                {
                    "segment_order": index + 1,
                    "staff_id": segment["staff_id"],
                    "assigned_start_date": segment["assigned_start_date"],
                    "assigned_end_date": segment["assigned_end_date"],
                }
                for index, segment in enumerate(normalized_segments)
            ],
        }
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        cursor_error: BaseException | None = None
        connection_error: BaseException | None = None
        if cursor is not None:
            cursor_error = _safe_close(cursor, cursor_closed)
        if connection is not None:
            connection_error = _safe_close(connection, connection_closed)
        if cursor_error is not None:
            raise cursor_error
        if connection_error is not None:
            raise connection_error
