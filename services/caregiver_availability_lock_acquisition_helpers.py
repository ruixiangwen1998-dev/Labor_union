from __future__ import annotations

from datetime import date, timedelta
from typing import Any


_ALLOWED_CONFLICT_TYPES = {"assignment", "schedule", "active_lock"}


def _assert_strict_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if value.strip() != value:
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    return value


def _assert_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _assert_smallint_one_or_null(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be 1 or null")
    if value != 1:
        raise ValueError(f"{field_name} must be 1 or null")
    return value


def _assert_exact_date(value: Any, field_name: str) -> date:
    if value.__class__ is not date:
        raise ValueError(f"{field_name} must be date")
    return value


def _assert_exact_keys(row: Any, expected_keys: frozenset[str], field_name: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{field_name} must be a dict")
    if set(row.keys()) != expected_keys:
        raise ValueError(f"{field_name} has unexpected keys")
    return row


def _assert_row_list(values: Any, field_name: str) -> list[Any]:
    if not isinstance(values, list):
        raise ValueError(f"{field_name} must be a list")
    return values


def _iter_dates(start: date, end: date) -> list[str]:
    current = start
    rows: list[str] = []
    while current <= end:
        rows.append(current.isoformat())
        current += timedelta(days=1)
    return rows


def normalize_lock_acquisition_request(
    case_no: Any,
    plan_id: Any,
    event_key: Any,
    actor: Any,
    lock_id: Any,
) -> dict[str, Any]:
    """Validate request fields only."""

    return {
        "case_no": _assert_strict_string(case_no, "case_no"),
        "plan_id": _assert_positive_int(plan_id, "plan_id"),
        "event_key": _assert_strict_string(event_key, "event_key"),
        "actor": _assert_strict_string(actor, "actor"),
        "lock_id": _assert_positive_int(lock_id, "lock_id"),
    }


def normalize_plan_snapshot(
    case_no: str,
    plan_id: int,
    plan_row: dict[str, Any],
    segment_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Normalize plan header/segment rows into stable canonical snapshot."""
    case_no = _assert_strict_string(case_no, "case_no")
    plan_id = _assert_positive_int(plan_id, "plan_id")

    plan_row = _assert_exact_keys(
        plan_row,
        frozenset({"id", "case_no", "status", "is_active", "start_date", "end_date"}),
        "plan_row",
    )

    plan_id_value = _assert_positive_int(plan_row["id"], "plan_row.id")
    if plan_id_value != plan_id:
        raise ValueError("plan_row.id must match plan_id")
    if plan_row["case_no"] != case_no:
        raise ValueError("plan_row.case_no must match case_no")

    if plan_row["status"] not in {
        "draft",
        "proposed",
        "accepted",
        "rejected",
        "superseded",
        "cancelled",
    }:
        raise ValueError("plan_row.status is invalid")

    _assert_smallint_one_or_null(plan_row["is_active"], "plan_row.is_active")

    case_start = _assert_exact_date(plan_row["start_date"], "plan_row.start_date")
    case_end = _assert_exact_date(plan_row["end_date"], "plan_row.end_date")
    if case_start > case_end:
        raise ValueError("plan_row.start_date cannot be after plan_row.end_date")

    segment_rows = _assert_row_list(segment_rows, "segment_rows")
    if not (1 <= len(segment_rows) <= 4):
        raise ValueError("segment_rows must contain one to four items")

    normalized_segments: list[dict[str, Any]] = []
    for index, row in enumerate(segment_rows):
        row = _assert_exact_keys(
            row,
            frozenset(
                {
                    "id",
                    "plan_id",
                    "segment_order",
                    "staff_id",
                    "assigned_start_date",
                    "assigned_end_date",
                }
            ),
            f"segment_rows[{index}]",
        )

        segment_plan_id = _assert_positive_int(row["plan_id"], f"segment_rows[{index}].plan_id")
        if segment_plan_id != plan_id:
            raise ValueError("segment_rows item plan_id must match plan_id")

        segment_order = _assert_positive_int(
            row["segment_order"],
            f"segment_rows[{index}].segment_order",
        )
        if segment_order > 4:
            raise ValueError("segment_rows item.segment_order must be between 1 and 4")

        segment_id = _assert_positive_int(row["id"], f"segment_rows[{index}].id")
        staff_id = _assert_positive_int(row["staff_id"], f"segment_rows[{index}].staff_id")

        assigned_start = _assert_exact_date(
            row["assigned_start_date"],
            f"segment_rows[{index}].assigned_start_date",
        )
        assigned_end = _assert_exact_date(
            row["assigned_end_date"],
            f"segment_rows[{index}].assigned_end_date",
        )
        if assigned_start > assigned_end:
            raise ValueError("segment_rows item.assigned_start_date cannot be after assigned_end_date")

        normalized_segments.append(
            {
                "segment_id": segment_id,
                "segment_order": segment_order,
                "staff_id": staff_id,
                "assigned_start_date": assigned_start,
                "assigned_end_date": assigned_end,
            }
        )

    normalized_segments = sorted(normalized_segments, key=lambda row: row["segment_order"])

    segment_orders = [row["segment_order"] for row in normalized_segments]
    if segment_orders != list(range(1, len(segment_orders) + 1)):
        raise ValueError("segment_rows must have contiguous segment_order from 1")

    expected_start = case_start
    staff_ids: list[int] = []
    staff_id_seen: set[int] = set()

    for segment in normalized_segments:
        if segment["staff_id"] in staff_id_seen:
            raise ValueError("segment staff_id must be unique")
        staff_id_seen.add(segment["staff_id"])

        if segment["assigned_start_date"] != expected_start:
            raise ValueError("segments must exactly cover case period without gaps")
        if segment["assigned_end_date"] < case_start or segment["assigned_end_date"] > case_end:
            raise ValueError("segments must stay inside case period")

        expected_start = segment["assigned_end_date"] + timedelta(days=1)
        staff_ids.append(segment["staff_id"])

    if expected_start != case_end + timedelta(days=1):
        raise ValueError("segments must exactly cover case period without gaps")

    lock_rows: list[dict[str, Any]] = []
    for segment in normalized_segments:
        for lock_date in _iter_dates(
            segment["assigned_start_date"],
            segment["assigned_end_date"],
        ):
            lock_rows.append(
                {
                    "segment_id": segment["segment_id"],
                    "staff_id": segment["staff_id"],
                    "lock_date": lock_date,
                }
            )

    return {
        "case_no": case_no,
        "plan_id": plan_id,
        "case_start_date": case_start.isoformat(),
        "case_end_date": case_end.isoformat(),
        "segments": [
            {
                "segment_id": segment["segment_id"],
                "segment_order": segment["segment_order"],
                "staff_id": segment["staff_id"],
                "assigned_start_date": segment["assigned_start_date"].isoformat(),
                "assigned_end_date": segment["assigned_end_date"].isoformat(),
            }
            for segment in normalized_segments
        ],
        "staff_ids": sorted(staff_id_seen),
        "lock_rows": sorted(
            lock_rows,
            key=lambda row: (row["lock_date"], row["segment_id"], row["staff_id"]),
        ),
    }


def normalize_conflicts(conflict_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize conflict facts with deterministic ordering and dedupe."""

    conflict_rows = _assert_row_list(conflict_rows, "conflict_rows")
    if not all(row is not None for row in conflict_rows):
        raise ValueError("conflict_rows item cannot be None")

    required_keys = frozenset({"source_type", "source_id", "staff_id", "lock_date"})
    normalized: list[dict[str, Any]] = []

    for index, row in enumerate(conflict_rows):
        row = _assert_exact_keys(row, required_keys, f"conflict_rows[{index}]")

        source_type = row["source_type"]
        if not isinstance(source_type, str):
            raise ValueError("conflict_rows.item.source_type must be a string")
        if source_type not in _ALLOWED_CONFLICT_TYPES:
            raise ValueError("conflict_rows.item.source_type is invalid")

        source_id = _assert_positive_int(row["source_id"], f"conflict_rows[{index}].source_id")
        staff_id = _assert_positive_int(row["staff_id"], f"conflict_rows[{index}].staff_id")
        lock_date = _assert_exact_date(row["lock_date"], f"conflict_rows[{index}].lock_date")

        normalized.append(
            {
                "staff_id": staff_id,
                "lock_date": lock_date.isoformat(),
                "source_type": source_type,
                "source_id": source_id,
            }
        )

    ordered = sorted(
        normalized,
        key=lambda item: (
            item["staff_id"],
            item["lock_date"],
            item["source_type"],
            item["source_id"],
        ),
    )

    unique: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str, int]] = set()
    for item in ordered:
        signature = (
            item["staff_id"],
            item["lock_date"],
            item["source_type"],
            item["source_id"],
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(item)

    return unique


def build_acquired_event_payload(
    request: dict[str, Any],
    plan_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Build deterministic payload used by lock_acquired event."""
    request = _assert_exact_keys(
        request,
        frozenset({"case_no", "plan_id", "event_key", "actor", "lock_id"}),
        "request",
    )
    plan_snapshot = _assert_exact_keys(
        plan_snapshot,
        frozenset(
            {
                "case_no",
                "plan_id",
                "case_start_date",
                "case_end_date",
                "segments",
                "staff_ids",
                "lock_rows",
            }
        ),
        "plan_snapshot",
    )
    _assert_strict_string(request["case_no"], "request.case_no")
    _assert_strict_string(request["actor"], "request.actor")
    _assert_strict_string(request["event_key"], "request.event_key")
    _assert_positive_int(request["plan_id"], "request.plan_id")
    _assert_positive_int(request["lock_id"], "request.lock_id")

    snapshot_case_no = _assert_strict_string(
        plan_snapshot["case_no"],
        "plan_snapshot.case_no",
    )
    snapshot_plan_id = _assert_positive_int(
        plan_snapshot["plan_id"],
        "plan_snapshot.plan_id",
    )
    if snapshot_case_no != request["case_no"]:
        raise ValueError("plan_snapshot.case_no must match request.case_no")
    if snapshot_plan_id != request["plan_id"]:
        raise ValueError("plan_snapshot.plan_id must match request.plan_id")

    try:
        case_start = date.fromisoformat(
            _assert_strict_string(
                plan_snapshot["case_start_date"],
                "plan_snapshot.case_start_date",
            )
        )
        case_end = date.fromisoformat(
            _assert_strict_string(
                plan_snapshot["case_end_date"],
                "plan_snapshot.case_end_date",
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("plan_snapshot case dates must be ISO dates") from exc

    segments = _assert_row_list(plan_snapshot["segments"], "plan_snapshot.segments")
    staff_ids = _assert_row_list(plan_snapshot["staff_ids"], "plan_snapshot.staff_ids")
    lock_rows = _assert_row_list(plan_snapshot["lock_rows"], "plan_snapshot.lock_rows")

    canonical_segments: list[dict[str, Any]] = []
    raw_segments: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        segment = _assert_exact_keys(
            segment,
            frozenset(
                {
                    "segment_id",
                    "segment_order",
                    "staff_id",
                    "assigned_start_date",
                    "assigned_end_date",
                }
            ),
            f"plan_snapshot.segments[{index}]",
        )
        try:
            assigned_start = date.fromisoformat(
                _assert_strict_string(
                    segment["assigned_start_date"],
                    f"plan_snapshot.segments[{index}].assigned_start_date",
                )
            )
            assigned_end = date.fromisoformat(
                _assert_strict_string(
                    segment["assigned_end_date"],
                    f"plan_snapshot.segments[{index}].assigned_end_date",
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("plan_snapshot segment dates must be ISO dates") from exc
        segment_id = _assert_positive_int(
            segment["segment_id"],
            f"plan_snapshot.segments[{index}].segment_id",
        )
        segment_order = _assert_positive_int(
            segment["segment_order"],
            f"plan_snapshot.segments[{index}].segment_order",
        )
        staff_id = _assert_positive_int(
            segment["staff_id"],
            f"plan_snapshot.segments[{index}].staff_id",
        )
        raw_segments.append(
            {
                "id": segment_id,
                "plan_id": snapshot_plan_id,
                "segment_order": segment_order,
                "staff_id": staff_id,
                "assigned_start_date": assigned_start,
                "assigned_end_date": assigned_end,
            }
        )
        canonical_segments.append(
            {
                "segment_id": segment_id,
                "segment_order": segment_order,
                "staff_id": staff_id,
                "assigned_start_date": assigned_start.isoformat(),
                "assigned_end_date": assigned_end.isoformat(),
            }
        )

    rebuilt = normalize_plan_snapshot(
        case_no=snapshot_case_no,
        plan_id=snapshot_plan_id,
        plan_row={
            "id": snapshot_plan_id,
            "case_no": snapshot_case_no,
            "status": "proposed",
            "is_active": 1,
            "start_date": case_start,
            "end_date": case_end,
        },
        segment_rows=raw_segments,
    )
    normalized_staff_ids = [
        _assert_positive_int(value, f"plan_snapshot.staff_ids[{index}]")
        for index, value in enumerate(staff_ids)
    ]
    if normalized_staff_ids != rebuilt["staff_ids"]:
        raise ValueError("plan_snapshot.staff_ids are not canonical")

    canonical_lock_rows: list[dict[str, Any]] = []
    for index, row in enumerate(lock_rows):
        row = _assert_exact_keys(
            row,
            frozenset({"segment_id", "staff_id", "lock_date"}),
            f"plan_snapshot.lock_rows[{index}]",
        )
        try:
            lock_date = date.fromisoformat(
                _assert_strict_string(
                    row["lock_date"],
                    f"plan_snapshot.lock_rows[{index}].lock_date",
                )
            ).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("plan_snapshot lock dates must be ISO dates") from exc
        canonical_lock_rows.append(
            {
                "segment_id": _assert_positive_int(
                    row["segment_id"],
                    f"plan_snapshot.lock_rows[{index}].segment_id",
                ),
                "staff_id": _assert_positive_int(
                    row["staff_id"],
                    f"plan_snapshot.lock_rows[{index}].staff_id",
                ),
                "lock_date": lock_date,
            }
        )
    if canonical_segments != rebuilt["segments"]:
        raise ValueError("plan_snapshot.segments are not canonical")
    if canonical_lock_rows != rebuilt["lock_rows"]:
        raise ValueError("plan_snapshot.lock_rows are not canonical")

    return {
        "case_no": request["case_no"],
        "plan_id": request["plan_id"],
        "actor": request["actor"],
        "lock_id": request["lock_id"],
        "segments": [dict(segment) for segment in rebuilt["segments"]],
        "staff_ids": list(rebuilt["staff_ids"]),
        "lock_rows": [dict(row) for row in rebuilt["lock_rows"]],
        "case_start_date": rebuilt["case_start_date"],
        "case_end_date": rebuilt["case_end_date"],
    }


def normalize_lock_acquisition_inputs(
    case_no: Any,
    plan_id: Any,
    event_key: Any,
    actor: Any,
    lock_id: Any,
    plan_row: dict[str, Any],
    segment_rows: list[dict[str, Any]],
    conflict_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate and canonicalize full lock acquisition request context."""
    canonical_request = normalize_lock_acquisition_request(
        case_no=case_no,
        plan_id=plan_id,
        event_key=event_key,
        actor=actor,
        lock_id=lock_id,
    )
    canonical_plan_snapshot = normalize_plan_snapshot(
        case_no=canonical_request["case_no"],
        plan_id=canonical_request["plan_id"],
        plan_row=plan_row,
        segment_rows=segment_rows,
    )
    canonical_conflicts = normalize_conflicts(conflict_rows)
    acquired_event_payload = build_acquired_event_payload(
        request=canonical_request,
        plan_snapshot=canonical_plan_snapshot,
    )

    return {
        "canonical_request": canonical_request,
        "canonical_plan_snapshot": canonical_plan_snapshot,
        "canonical_conflicts": canonical_conflicts,
        "acquired_event_payload": acquired_event_payload,
    }
