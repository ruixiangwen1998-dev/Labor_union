"""Pure validation rules for multi-caregiver service date intervals."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any


ALLOWED_STATUSES = {"planned", "active", "completed", "replaced", "cancelled"}
ALLOWED_KINDS = {"formal", "single_day_substitute", "substitute"}
ASSIGNMENT_FIELDS = {
    "id",
    "case_no",
    "staff_id",
    "status",
    "assigned_start_date",
    "assigned_end_date",
    "kind",
    "original_assignment_id",
    "substitution_work_date",
}


def _as_json_safe(value: Any) -> Any:
    """Return a defensive JSON value copy, rejecting implicit conversions."""

    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("details must not contain non-finite floats")
        return value
    if isinstance(value, list):
        return [_as_json_safe(item) for item in value]
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("details mappings must use string keys")
            copied[key] = _as_json_safe(item)
        return copied
    raise ValueError(
        "details must contain only JSON primitives, lists, and string-key mappings"
    )


class AssignmentPlanTransitionConflict(ValueError):
    """Typed conflict with machine-readable details for deterministic handling."""

    code: str
    details: dict[str, Any]

    def __init__(self, code: str, details: Mapping[str, Any] | None = None):
        self.code = code
        if details is None:
            raw_details = {}
        elif not isinstance(details, Mapping):
            raise ValueError("details must be a mapping")
        else:
            raw_details = details
        self.details = _as_json_safe(raw_details)
        # Ensure json.dumps can consume details in current process environments.
        json.dumps(self.details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        super().__init__(code)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "details": self.details}


def _normalize_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO date") from exc
    raise ValueError(f"{field_name} is required and must be an ISO date")


def _normalize_optional_date(value: Any, field_name: str) -> date | None:
    if value is None:
        return None
    return _normalize_date(value, field_name)


def _normalize_case_no(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("case_no must be a trimmed non-empty string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("case_no must be a trimmed non-empty string")
    return normalized


def _normalize_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _normalize_status(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("status must be a non-empty string")
    status = value.strip().lower()
    if not status:
        raise ValueError("status must be a non-empty string")
    if status not in ALLOWED_STATUSES:
        raise ValueError("status must be one of planned, active, completed, replaced, cancelled")
    return status


def _normalize_kind(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("kind must be a non-empty string")
    kind = value.strip().lower()
    if not kind:
        raise ValueError("kind must be a non-empty string")
    if kind not in ALLOWED_KINDS:
        raise ValueError("kind must be formal, single_day_substitute, or substitute")
    return kind


def _normalize_operation_kind(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("operation_kind must be a non-empty string")
    operation = value.strip().lower()
    if not operation:
        raise ValueError("operation_kind must be a non-empty string")
    if operation not in {
        "segment_reconfigure",
        "single_day_substitute",
        "defer_following_assignments",
        "batch_leave_resolution",
    }:
        raise ValueError(
            "operation_kind must be segment_reconfigure, single_day_substitute, "
            "defer_following_assignments, or batch_leave_resolution"
        )
    return operation


def _normalize_historical_fact_state(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(
            "historical_fact_state must be bootstrap, unlocked, or locked"
        )
    state = value.strip().lower()
    if state not in {"bootstrap", "unlocked", "locked"}:
        raise ValueError(
            "historical_fact_state must be bootstrap, unlocked, or locked"
        )
    return state


def _normalize_key(value: Any, current_ids: set[int] | None = None) -> tuple[str, int | str]:
    if isinstance(value, bool) or value is None:
        raise ValueError("id must be a positive integer or a non-empty key string")
    if isinstance(value, int):
        if value < 1:
            raise ValueError("id must be a positive integer")
        return "existing", value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("id must be a positive integer or a non-empty key string")
        if text.isdigit():
            parsed = int(text)
            if parsed > 0 and current_ids and parsed in current_ids:
                return "existing", parsed
        return "new", text
    raise ValueError("id must be a positive integer or a non-empty key string")


def _iter_days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _normalize_assignment_row(
    row: Mapping[str, Any],
    *,
    allow_new_key: bool,
    current_ids: set[int] | None,
) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("assignment row must be a mapping")

    extra = set(row.keys()) - ASSIGNMENT_FIELDS
    if extra:
        raise ValueError(f"assignment row has unknown fields: {', '.join(sorted(extra))}")

    if "id" not in row:
        raise ValueError("assignment row requires id")
    key_kind, assignment_id = _normalize_key(row.get("id"), current_ids)
    if key_kind == "new" and not allow_new_key:
        raise ValueError("new assignment key is only allowed for proposed assignments")

    status = _normalize_status(row.get("status"))
    case_no = _normalize_case_no(row.get("case_no"))
    staff_id = _normalize_positive_int(row.get("staff_id"), "staff_id")
    kind = _normalize_kind(row.get("kind", "formal"))
    start = _normalize_optional_date(row.get("assigned_start_date"), "assigned_start_date")
    end = _normalize_optional_date(row.get("assigned_end_date"), "assigned_end_date")
    if status != "cancelled":
        if start is None or end is None:
            raise ValueError("assigned_start_date and assigned_end_date are required for non-cancelled assignments")
        if start > end:
            raise ValueError("assigned_start_date must not be after assigned_end_date")

    original_assignment_id = row.get("original_assignment_id")
    if original_assignment_id is not None:
        original_assignment_id = _normalize_positive_int(
            original_assignment_id,
            "original_assignment_id",
        )

    substitution_work_date = _normalize_optional_date(
        row.get("substitution_work_date"),
        "substitution_work_date",
    )
    if kind == "formal":
        if original_assignment_id is not None or substitution_work_date is not None:
            raise ValueError(
                "formal assignments cannot define original_assignment_id or substitution_work_date"
            )
    elif original_assignment_id is None or substitution_work_date is None:
        raise ValueError(
            "substitute assignments require original_assignment_id and substitution_work_date"
        )

    return {
        "id": assignment_id,
        "id_kind": key_kind,
        "case_no": case_no,
        "staff_id": staff_id,
        "status": status,
        "assigned_start_date": start,
        "assigned_end_date": end,
        "kind": kind,
        "original_assignment_id": original_assignment_id,
        "substitution_work_date": substitution_work_date,
    }


def _build_ownership(assignments: Iterable[Mapping[str, Any]]) -> dict[date, int | str]:
    ownership: dict[date, int | str] = {}
    for row in assignments:
        if row["status"] == "cancelled":
            continue
        start = row["assigned_start_date"]
        end = row["assigned_end_date"]
        if not isinstance(start, date) or not isinstance(end, date):
            raise ValueError("active assignments must have assigned_start_date and assigned_end_date")
        if start > end:
            raise ValueError(f"assignment {row['id']} has invalid date range")
        for current in _iter_days(start, end):
            if current in ownership:
                raise ValueError(f"date {current.isoformat()} has overlapping assignment ownership")
            ownership[current] = row["id"]
    return ownership


def _ensure_full_coverage(ownership: dict[date, Any], case_start: date, case_end: date, label: str) -> None:
    for current in _iter_days(case_start, case_end):
        if current not in ownership:
            raise ValueError(f"{label} missing ownership on {current.isoformat()}")


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "id_kind"}


def _batch_conflict(code: str, **details: Any) -> None:
    """Raise a deterministic, JSON-safe batch leave conflict."""

    raise AssignmentPlanTransitionConflict(code=code, details=details)


def _validate_batch_leave_resolution(
    *,
    current_active: list[dict[str, Any]],
    proposed_normalized: list[dict[str, Any]],
    after_active: list[dict[str, Any]],
    before_ownership: Mapping[date, int | str],
    current_case_start: date,
    current_case_end: date,
    proposed_case_start: date,
    proposed_case_end: date,
    database_current_date: date,
    effective_date: date,
    historical_fact_state: str,
) -> dict[date, dict[str, Any]]:
    """Validate the aggregate substitute/defer shape without caller batch facts.

    The date delta is deliberately derived only from the two case end dates.
    This function knows nothing about batch items or leave-event counts: those
    belong to the caller that canonicalises the leave request.
    """

    current_active = sorted(
        current_active,
        key=lambda row: (row["assigned_start_date"], str(row["id"])),
    )
    if proposed_case_start != current_case_start:
        _batch_conflict(
            "batch_leave_target_mismatch",
            field="proposed_case_start_date",
            expected=current_case_start.isoformat(),
            actual=proposed_case_start.isoformat(),
        )
    defer_days = (proposed_case_end - current_case_end).days
    if defer_days < 0:
        _batch_conflict(
            "batch_defer_shift_invalid",
            current_case_end_date=current_case_end.isoformat(),
            proposed_case_end_date=proposed_case_end.isoformat(),
            derived_defer_days=defer_days,
        )

    substitute_rows = [
        row
        for row in proposed_normalized
        if row["id_kind"] == "new"
        and row["status"] != "cancelled"
        and row["kind"] in {"single_day_substitute", "substitute"}
    ]
    after_by_id = {row["id"]: row for row in after_active}
    current_by_id = {row["id"]: row for row in current_active}
    if not substitute_rows and defer_days == 0:
        _batch_conflict(
            "batch_leave_target_mismatch",
            reason="batch_requires_substitute_or_positive_defer",
            actual_substitute_rows=0,
            derived_defer_days=0,
        )
    substitute_days: dict[date, dict[str, Any]] = {}
    if substitute_rows:
        original_ids = {row["original_assignment_id"] for row in substitute_rows}
        if None in original_ids or len(original_ids) != 1:
            _batch_conflict(
                "batch_leave_target_mismatch",
                original_assignment_ids=sorted(
                    str(assignment_id) for assignment_id in original_ids
                ),
            )
        original_id = next(iter(original_ids))
        original = current_by_id.get(original_id)
        if original is None or original["kind"] != "formal":
            _batch_conflict(
                "batch_leave_target_mismatch",
                original_assignment_id=original_id,
                expected_current_formal_assignment=True,
            )
        target_index = next(
            index for index, row in enumerate(current_active) if row["id"] == original_id
        )
        for substitute in sorted(
            substitute_rows,
            key=lambda row: (row["substitution_work_date"], str(row["id"])),
        ):
            work_date = substitute["substitution_work_date"]
            assert work_date is not None
            if work_date in substitute_days:
                _batch_conflict(
                    "batch_substitute_date_duplicate",
                    substitution_work_date=work_date.isoformat(),
                    assignment_ids=sorted(
                        [str(substitute_days[work_date]["id"]), str(substitute["id"])]
                    ),
                )
            if (
                substitute["assigned_start_date"] != work_date
                or substitute["assigned_end_date"] != work_date
                or not (
                    original["assigned_start_date"]
                    <= work_date
                    <= original["assigned_end_date"]
                )
                or before_ownership.get(work_date) != original_id
            ):
                _batch_conflict(
                    "batch_substitute_lineage_invalid",
                    assignment_id=str(substitute["id"]),
                    original_assignment_id=original_id,
                    substitution_work_date=work_date.isoformat(),
                )
            substitute_days[work_date] = substitute
    else:
        changed_indexes = [
            index
            for index, before in enumerate(current_active)
            if (after := after_by_id.get(before["id"])) is not None
            and (
                after["assigned_start_date"] != before["assigned_start_date"]
                or after["assigned_end_date"] != before["assigned_end_date"]
            )
        ]
        target_indexes = [
            index
            for index, before in enumerate(current_active)
            if before["kind"] == "formal"
            and (after := after_by_id.get(before["id"])) is not None
            and after["assigned_start_date"] == before["assigned_start_date"]
            and after["assigned_end_date"]
            == before["assigned_end_date"] + timedelta(days=defer_days)
        ]
        if len(target_indexes) != 1:
            _batch_conflict(
                "batch_leave_target_mismatch",
                reason="unable_to_infer_unique_defer_target",
                derived_defer_days=defer_days,
                candidate_assignment_ids=[
                    current_active[index]["id"] for index in target_indexes
                ],
            )
        target_index = target_indexes[0]
        if not changed_indexes or changed_indexes[0] != target_index:
            _batch_conflict(
                "batch_defer_shift_invalid",
                reason="defer_target_must_be_first_changed_existing_assignment",
                target_assignment_id=current_active[target_index]["id"],
                first_changed_assignment_id=(
                    None
                    if not changed_indexes
                    else current_active[changed_indexes[0]]["id"]
                ),
            )
        original = current_active[target_index]
        original_id = original["id"]

    if not (
        original["assigned_start_date"]
        <= effective_date
        <= original["assigned_end_date"]
    ):
        _batch_conflict(
            "batch_leave_target_mismatch",
            field="effective_date",
            target_assignment_id=original_id,
            target_start_date=original["assigned_start_date"].isoformat(),
            target_end_date=original["assigned_end_date"].isoformat(),
            actual=effective_date.isoformat(),
        )
    if historical_fact_state == "locked" and effective_date < database_current_date:
        _batch_conflict(
            "historical_ownership_locked",
            effective_date=effective_date.isoformat(),
            database_current_date=database_current_date.isoformat(),
            target_assignment_id=original_id,
        )

    if len(after_active) > 4:
        _batch_conflict(
            "assignment_row_limit_exceeded",
            maximum_active_rows=4,
            actual_active_rows=len(after_active),
        )

    for index, existing_substitute in enumerate(current_active):
        if existing_substitute["kind"] not in {"single_day_substitute", "substitute"}:
            continue
        existing_original = current_by_id.get(
            existing_substitute["original_assignment_id"]
        )
        if (
            existing_substitute["assigned_start_date"]
            != existing_substitute["assigned_end_date"]
            or existing_substitute["assigned_start_date"]
            != existing_substitute["substitution_work_date"]
            or existing_original is None
            or existing_original["kind"] != "formal"
        ):
            _batch_conflict(
                "batch_substitute_lineage_invalid",
                assignment_id=existing_substitute["id"],
                original_assignment_id=existing_substitute["original_assignment_id"],
                substitution_work_date=existing_substitute[
                    "substitution_work_date"
                ].isoformat(),
                reason="existing_active_substitute_lineage_invalid",
            )
        if defer_days >= 1 and index > target_index:
            _batch_conflict(
                "batch_defer_shift_invalid",
                assignment_id=existing_substitute["id"],
                defer_days=defer_days,
                reason="existing_active_substitute_requires_shift",
            )
    for index, before in enumerate(current_active):
        after = after_by_id.get(before["id"])
        if after is None:
            _batch_conflict(
                "batch_defer_shift_invalid",
                assignment_id=before["id"],
                reason="existing_assignment_missing",
            )
        for field in ("staff_id", "status", "kind", "original_assignment_id", "substitution_work_date"):
            if after[field] != before[field]:
                _batch_conflict(
                    "batch_defer_shift_invalid",
                    assignment_id=before["id"],
                    field=field,
                    expected=before[field],
                    actual=after[field],
                )
        if index < target_index:
            expected_start, expected_end = before["assigned_start_date"], before["assigned_end_date"]
        elif index == target_index:
            if substitute_rows:
                continue
            expected_start = before["assigned_start_date"]
            expected_end = before["assigned_end_date"] + timedelta(days=defer_days)
        else:
            expected_start = before["assigned_start_date"] + timedelta(days=defer_days)
            expected_end = before["assigned_end_date"] + timedelta(days=defer_days)
        if after["assigned_start_date"] != expected_start or after["assigned_end_date"] != expected_end:
            _batch_conflict(
                "batch_defer_shift_invalid",
                assignment_id=before["id"],
                defer_days=defer_days,
                expected_start_date=expected_start.isoformat(),
                expected_end_date=expected_end.isoformat(),
                actual_start_date=after["assigned_start_date"].isoformat(),
                actual_end_date=after["assigned_end_date"].isoformat(),
            )

    ownership: dict[date, dict[str, Any]] = {}
    for row in sorted(
        after_active,
        key=lambda item: (item["assigned_start_date"], str(item["id"])),
    ):
        for owned_day in _iter_days(row["assigned_start_date"], row["assigned_end_date"]):
            existing = ownership.get(owned_day)
            if existing is not None:
                _batch_conflict(
                    "assignment_daily_ownership_invalid",
                    reason="overlap",
                    date=owned_day.isoformat(),
                    assignment_ids=sorted([str(existing["id"]), str(row["id"])]),
                )
            ownership[owned_day] = row

    coverage_start = max(proposed_case_start, database_current_date)
    for owned_day in _iter_days(coverage_start, proposed_case_end):
        if owned_day not in ownership:
            _batch_conflict(
                "assignment_daily_ownership_invalid",
                reason="missing_ownership",
                date=owned_day.isoformat(),
            )
    for owned_day in ownership:
        if not (proposed_case_start <= owned_day <= proposed_case_end):
            _batch_conflict(
                "assignment_daily_ownership_invalid",
                reason="outside_proposed_case",
                date=owned_day.isoformat(),
            )

    target_end = original["assigned_end_date"] + timedelta(days=defer_days)
    for owned_day in _iter_days(original["assigned_start_date"], target_end):
        owner = ownership.get(owned_day)
        if owned_day in substitute_days:
            if owner is None or owner["id"] != substitute_days[owned_day]["id"]:
                _batch_conflict(
                    "batch_substitute_lineage_invalid",
                    substitution_work_date=owned_day.isoformat(),
                    expected_assignment_id=str(substitute_days[owned_day]["id"]),
                    actual_assignment_id=None if owner is None else str(owner["id"]),
                )
            continue
        if owner is None:
            _batch_conflict(
                "assignment_daily_ownership_invalid",
                reason="missing_original_formal_fragment",
                date=owned_day.isoformat(),
            )
        if owner["staff_id"] != original["staff_id"] or owner["kind"] != "formal":
            _batch_conflict(
                "batch_original_staff_ownership_changed",
                date=owned_day.isoformat(),
                original_assignment_id=original_id,
                expected_staff_id=original["staff_id"],
                expected_kind="formal",
                actual_assignment_id=str(owner["id"]),
                actual_staff_id=owner["staff_id"],
                actual_kind=owner["kind"],
            )
    return ownership


def validate_non_overlapping_assignment_interval(
    candidate_start_date: date | str,
    candidate_end_date: date | str,
    existing_assignments: Iterable[Mapping[str, Any]],
    candidate_assignment_id: int | None = None,
) -> tuple[date, date]:
    """Return a valid inclusive interval or raise ``ValueError``.

    Existing assignments must belong to one case and expose ``id``, ``status``,
    ``assigned_start_date``, and ``assigned_end_date``.  Cancelled assignments
    and the assignment being edited do not reserve dates.
    """

    candidate_start = _normalize_date(candidate_start_date, "candidate_start_date")
    candidate_end = _normalize_date(candidate_end_date, "candidate_end_date")
    if candidate_start > candidate_end:
        raise ValueError("candidate_start_date must not be after candidate_end_date")

    try:
        assignment_rows = iter(existing_assignments)
    except TypeError as exc:
        raise ValueError("existing_assignments must be iterable") from exc

    for assignment in assignment_rows:
        if not isinstance(assignment, Mapping):
            raise ValueError("existing assignment must be a mapping")
        assignment_id = assignment.get("id")
        if assignment_id == candidate_assignment_id:
            continue
        if assignment.get("status") == "cancelled":
            continue

        try:
            existing_start = _normalize_date(
                assignment.get("assigned_start_date"),
                f"assignment {assignment_id} assigned_start_date",
            )
            existing_end = _normalize_date(
                assignment.get("assigned_end_date"),
                f"assignment {assignment_id} assigned_end_date",
            )
        except ValueError as exc:
            raise ValueError(
                f"assignment {assignment_id} has incomplete service dates and requires review"
            ) from exc

        if existing_start > existing_end:
            raise ValueError(f"assignment {assignment_id} has an invalid service date range")
        if candidate_start <= existing_end and candidate_end >= existing_start:
            raise ValueError(f"service date range overlaps assignment {assignment_id}")

    return candidate_start, candidate_end


def validate_assignment_plan_transition(
    *,
    case_no: str,
    database_current_date: date | str | datetime,
    effective_date: date | str | datetime,
    current_case_start_date: date | str | datetime,
    current_case_end_date: date | str | datetime,
    proposed_case_start_date: date | str | datetime,
    proposed_case_end_date: date | str | datetime,
    operation_kind: str,
    current_assignments: Iterable[Mapping[str, Any]],
    proposed_assignments: Iterable[Mapping[str, Any]],
    historical_fact_state: str = "locked",
) -> dict[str, Any]:
    """Validate and normalize a full assignment transition plan."""

    target_case_no = _normalize_case_no(case_no)
    db_current = _normalize_date(database_current_date, "database_current_date")
    effective = _normalize_date(effective_date, "effective_date")
    current_case_start = _normalize_date(
        current_case_start_date,
        "current_case_start_date",
    )
    current_case_end = _normalize_date(
        current_case_end_date,
        "current_case_end_date",
    )
    proposed_case_start = _normalize_date(
        proposed_case_start_date,
        "proposed_case_start_date",
    )
    proposed_case_end = _normalize_date(
        proposed_case_end_date,
        "proposed_case_end_date",
    )
    operation = _normalize_operation_kind(operation_kind)
    historical_state = _normalize_historical_fact_state(historical_fact_state)
    if current_case_start > current_case_end:
        raise ValueError(
            "current_case_start_date must not be after current_case_end_date"
        )
    if proposed_case_start > proposed_case_end:
        raise ValueError(
            "proposed_case_start_date must not be after proposed_case_end_date"
        )
    transition_start = min(current_case_start, proposed_case_start)
    transition_end = max(current_case_end, proposed_case_end)
    if effective < transition_start or effective > transition_end:
        raise ValueError("effective_date must be within the transition date boundaries")
    if (
        effective < db_current
        and historical_state == "locked"
        and operation != "batch_leave_resolution"
    ):
        raise ValueError("effective_date cannot be before database_current_date")

    try:
        current_rows = list(current_assignments)
    except TypeError as exc:
        raise ValueError("current_assignments must be iterable") from exc
    try:
        proposed_rows = list(proposed_assignments)
    except TypeError as exc:
        raise ValueError("proposed_assignments must be iterable") from exc

    normalized_current: list[dict[str, Any]] = []
    current_by_id: dict[int, dict[str, Any]] = {}
    current_all_by_id: dict[int, dict[str, Any]] = {}
    seen_current_ids: set[int] = set()
    for row in current_rows:
        normalized = _normalize_assignment_row(row, allow_new_key=False, current_ids=None)
        if normalized["case_no"] != target_case_no:
            raise ValueError("current assignment case_no mismatch")
        assignment_id = normalized["id"]
        if assignment_id in seen_current_ids:
            raise ValueError(f"duplicate current assignment id {assignment_id}")
        seen_current_ids.add(assignment_id)
        current_all_by_id[assignment_id] = normalized
        if normalized["status"] != "cancelled":
            current_by_id[assignment_id] = normalized
        normalized_current.append(normalized)

    normalized_proposed: list[dict[str, Any]] = []
    proposed_existing: dict[int, dict[str, Any]] = {}
    proposed_new: dict[str, dict[str, Any]] = {}
    for row in proposed_rows:
        normalized = _normalize_assignment_row(
            row,
            allow_new_key=True,
            current_ids=seen_current_ids,
        )
        if normalized["case_no"] != target_case_no:
            raise ValueError("proposed assignment case_no mismatch")

        if normalized["id_kind"] == "existing":
            assignment_id = normalized["id"]
            if assignment_id not in current_by_id and not (
                operation in {"defer_following_assignments", "batch_leave_resolution"}
                and assignment_id in current_all_by_id
            ):
                raise ValueError("proposed assignment id must reference a current assignment")
            if assignment_id in proposed_existing:
                raise ValueError(f"duplicate proposed assignment id {assignment_id}")
            before = current_all_by_id[assignment_id]
            if operation == "batch_leave_resolution" and before["status"] == "cancelled":
                for retained_field in ASSIGNMENT_FIELDS:
                    if retained_field == "id":
                        continue
                    if normalized[retained_field] != before[retained_field]:
                        raise ValueError(
                            f"cancelled assignment {assignment_id} changed {retained_field}"
                        )
            immutable_fields = []
            if operation not in {"defer_following_assignments", "batch_leave_resolution"}:
                immutable_fields.extend(
                    [
                        "staff_id",
                        "kind",
                        "original_assignment_id",
                        "substitution_work_date",
                    ]
                )
                immutable_fields.append("assigned_start_date")
            for immutable_field in immutable_fields:
                if normalized[immutable_field] != before[immutable_field]:
                    raise ValueError(
                        f"existing assignment {assignment_id} changed {immutable_field}"
                    )
            if normalized["status"] not in {before["status"], "cancelled"}:
                raise ValueError(
                    f"existing assignment {assignment_id} changed status"
                )
            if (
                normalized["status"] != "cancelled"
                and operation not in {"defer_following_assignments", "batch_leave_resolution"}
            ):
                before_end = before["assigned_end_date"]
                after_end = normalized["assigned_end_date"]
                if after_end > before_end:
                    raise ValueError(
                        f"existing assignment {assignment_id} cannot extend its end date"
                    )
                if after_end != before_end and after_end != effective - timedelta(days=1):
                    raise ValueError(
                        f"existing assignment {assignment_id} may only end on the day before effective_date"
                    )
            proposed_existing[assignment_id] = normalized
        else:
            assignment_key = normalized["id"]
            if assignment_key in proposed_new:
                if operation == "batch_leave_resolution":
                    _batch_conflict(
                        "batch_substitute_lineage_invalid",
                        reason="duplicate_proposed_new_key",
                        assignment_id=assignment_key,
                    )
                raise ValueError(f"duplicate proposed assignment key {assignment_key}")
            proposed_new[assignment_key] = normalized

        normalized_proposed.append(normalized)

    normalized_current_non_cancelled = [
        row for row in normalized_current if row["status"] != "cancelled"
    ]
    before_assignments = [_public_row(row) for row in normalized_current]
    before_active = [row for row in normalized_current_non_cancelled]
    if len(before_active) > 4:
        raise ValueError("current plan may contain at most 4 non-cancelled assignments")
    for row in before_active:
        if (
            row["assigned_start_date"] < current_case_start
            or row["assigned_end_date"] > current_case_end
        ):
            raise ValueError(
                "current assignment interval must be within current case boundaries"
            )
    before_ownership = _build_ownership(before_active)
    if before_active or operation != "segment_reconfigure":
        _ensure_full_coverage(
            before_ownership,
            current_case_start,
            current_case_end,
            "current assignments",
        )

    if operation == "defer_following_assignments":
        current_ordered = sorted(
            normalized_current,
            key=lambda row: (row["assigned_start_date"] or date.min, str(row["id"])),
        )
        proposed_existing_ordered = sorted(
            (row for row in normalized_proposed if row["id_kind"] == "existing"),
            key=lambda row: (row["assigned_start_date"] or date.min, str(row["id"])),
        )
        proposed_active_ordered = [
            row for row in proposed_existing_ordered if row["status"] != "cancelled"
        ]
        current_active_ordered = [
            row for row in current_ordered if row["status"] != "cancelled"
        ]
        if current_case_start != proposed_case_start:
            raise AssignmentPlanTransitionConflict(
                code="defer_case_start_changed",
                details={
                    "current_case_start_date": current_case_start.isoformat(),
                    "proposed_case_start_date": proposed_case_start.isoformat(),
                },
            )
        defer_days = (proposed_case_end - current_case_end).days
        if defer_days < 1:
            raise AssignmentPlanTransitionConflict(
                code="defer_days_not_positive",
                details={
                    "current_case_end_date": current_case_end.isoformat(),
                    "proposed_case_end_date": proposed_case_end.isoformat(),
                    "derived_defer_days": defer_days,
                },
            )
        if proposed_new:
            raise AssignmentPlanTransitionConflict(
                code="defer_assignment_created",
                details={"proposed_new_keys": sorted(proposed_new)},
            )
        cancelled_assignment_ids = [
            row["id"]
            for row in proposed_existing_ordered
            if row["status"] == "cancelled"
            and current_all_by_id[row["id"]]["status"] != "cancelled"
        ]
        proposed_active_ids = {
            row["id"] for row in proposed_active_ordered
        }
        cancelled_assignment_ids.extend(
            row["id"]
            for row in current_active_ordered
            if row["id"] not in proposed_active_ids
            and row["id"] not in cancelled_assignment_ids
        )
        if cancelled_assignment_ids:
            raise AssignmentPlanTransitionConflict(
                code="defer_assignment_cancelled",
                details={"cancelled_assignment_ids": cancelled_assignment_ids},
            )
        if len(current_ordered) != len(proposed_existing_ordered):
            raise AssignmentPlanTransitionConflict(
                code="defer_assignment_row_count_changed",
                details={
                    "current_row_count": len(current_ordered),
                    "proposed_row_count": len(proposed_existing_ordered),
                },
            )
        if [row["id"] for row in current_ordered] != [
            row["id"] for row in proposed_existing_ordered
        ]:
            raise AssignmentPlanTransitionConflict(
                code="defer_assignment_id_order_changed",
                details={
                    "current_assignment_ids": [row["id"] for row in current_ordered],
                    "proposed_assignment_ids": [row["id"] for row in proposed_active_ordered],
                },
            )

        changed_index: int | None = None
        for before, after in zip(current_ordered, proposed_existing_ordered):
            for field in (
                "id",
                "staff_id",
                "status",
                "kind",
                "original_assignment_id",
                "substitution_work_date",
            ):
                if before[field] != after[field]:
                    raise AssignmentPlanTransitionConflict(
                        code="defer_assignment_metadata_changed",
                        details={
                            "assignment_id": before["id"],
                            "field": field,
                            "expected": before[field],
                            "actual": after[field],
                        },
                    )

        for index, (before, after) in enumerate(
            zip(current_active_ordered, proposed_active_ordered)
        ):
            if (
                before["assigned_start_date"] != after["assigned_start_date"]
                or before["assigned_end_date"] != after["assigned_end_date"]
            ):
                if changed_index is None:
                    changed_index = index

        if changed_index is None:
            raise AssignmentPlanTransitionConflict(
                code="defer_affected_assignment_missing",
                details={"assignment_ids": [row["id"] for row in current_active_ordered]},
            )
        affected_before = current_active_ordered[changed_index]
        if not (
            affected_before["assigned_start_date"]
            <= effective
            <= affected_before["assigned_end_date"]
        ):
            raise AssignmentPlanTransitionConflict(
                code="defer_effective_date_outside_affected_assignment",
                details={
                    "effective_date": effective.isoformat(),
                    "affected_assignment_id": affected_before["id"],
                    "affected_start_date": affected_before[
                        "assigned_start_date"
                    ].isoformat(),
                    "affected_end_date": affected_before[
                        "assigned_end_date"
                    ].isoformat(),
                },
            )

        defer_delta = timedelta(days=defer_days)
        for index, (before, after) in enumerate(
            zip(current_active_ordered, proposed_active_ordered)
        ):
            if index < changed_index:
                expected_start = before["assigned_start_date"]
                expected_end = before["assigned_end_date"]
            elif index == changed_index:
                expected_start = before["assigned_start_date"]
                expected_end = before["assigned_end_date"] + defer_delta
            else:
                expected_start = before["assigned_start_date"] + defer_delta
                expected_end = before["assigned_end_date"] + defer_delta
            if (
                after["assigned_start_date"] != expected_start
                or after["assigned_end_date"] != expected_end
            ):
                raise AssignmentPlanTransitionConflict(
                    code="defer_assignment_shift_mismatch",
                    details={
                        "assignment_id": before["id"],
                        "row_role": (
                            "before"
                            if index < changed_index
                            else "affected"
                            if index == changed_index
                            else "following"
                        ),
                        "defer_days": defer_days,
                        "expected_start_date": expected_start.isoformat(),
                        "expected_end_date": expected_end.isoformat(),
                        "actual_start_date": after["assigned_start_date"].isoformat(),
                        "actual_end_date": after["assigned_end_date"].isoformat(),
                    },
                )

    after_assignments: list[dict[str, Any]] = []
    facts_created: list[dict[str, Any]] = []
    facts_retained: list[dict[str, Any]] = []
    facts_truncated: list[dict[str, Any]] = []
    facts_cancelled: list[dict[str, Any]] = []

    used_existing = set[int]()
    for row in normalized_current:
        if row["status"] == "cancelled":
            if row["id"] in proposed_existing:
                used_existing.add(row["id"])
            after_assignments.append(_public_row(row))
            continue

        assignment_id = row["id"]
        proposed = proposed_existing.get(assignment_id)
        used_existing.add(assignment_id)

        if proposed is None:
            start = row["assigned_start_date"]
            end = row["assigned_end_date"]
            assert start is not None and end is not None
            if end < effective:
                after = _public_row(row)
                after_assignments.append(after)
                facts_retained.append(after)
            elif start >= effective:
                cancelled = _public_row(row)
                cancelled["status"] = "cancelled"
                after_assignments.append(cancelled)
                facts_cancelled.append(
                    {"before": _public_row(row), "after": cancelled}
                )
            else:
                truncated_end = effective - timedelta(days=1)
                if truncated_end >= start:
                    truncated = _public_row(row)
                    truncated["assigned_end_date"] = truncated_end
                    after_assignments.append(truncated)
                    facts_truncated.append(
                        {"before": _public_row(row), "after": truncated}
                    )
                else:
                    cancelled = _public_row(row)
                    cancelled["status"] = "cancelled"
                    after_assignments.append(cancelled)
                    facts_cancelled.append(
                        {"before": _public_row(row), "after": cancelled}
                    )
            continue

        after = _public_row(proposed)
        after_assignments.append(after)

        if after["status"] == "cancelled":
            facts_cancelled.append({"before": _public_row(row), "after": after})
        elif (
            after["status"] == row["status"]
            and after["staff_id"] == row["staff_id"]
            and after["assigned_start_date"] == row["assigned_start_date"]
            and after["assigned_end_date"] == row["assigned_end_date"]
            and after["kind"] == row["kind"]
        ):
            facts_retained.append(after)
        else:
            facts_truncated.append({"before": _public_row(row), "after": after})

    for key, row in proposed_existing.items():
        if key in used_existing:
            continue
        if row["status"] != "cancelled":
            public = _public_row(row)
            facts_created.append(public)
            after_assignments.append(public)
        else:
            after_assignments.append(_public_row(row))

    for key, row in proposed_new.items():
        if row["status"] != "cancelled":
            public = _public_row(row)
            facts_created.append(public)
            after_assignments.append(public)
        else:
            after_assignments.append(_public_row(row))

    after_non_cancelled = [row for row in after_assignments if row["status"] != "cancelled"]
    if operation == "batch_leave_resolution":
        batch_ownership = _validate_batch_leave_resolution(
            current_active=before_active,
            proposed_normalized=normalized_proposed,
            after_active=after_non_cancelled,
            before_ownership=before_ownership,
            current_case_start=current_case_start,
            current_case_end=current_case_end,
            proposed_case_start=proposed_case_start,
            proposed_case_end=proposed_case_end,
            database_current_date=db_current,
            effective_date=effective,
            historical_fact_state=historical_state,
        )
    else:
        batch_ownership = None
    if len(after_non_cancelled) > 4:
        raise ValueError("assignment transition may contain at most 4 non-cancelled assignments")
    for row in after_non_cancelled:
        if (
            row["assigned_start_date"] < proposed_case_start
            and row["id"] not in current_by_id
        ):
            raise ValueError(
                "new proposed assignment interval must be within proposed case boundaries"
            )

    after_ownership = (
        {owned_day: row["id"] for owned_day, row in batch_ownership.items()}
        if batch_ownership is not None
        else _build_ownership(after_non_cancelled)
    )
    proposed_effective_ownership = {
        owned_day: assignment_id
        for owned_day, assignment_id in after_ownership.items()
        if owned_day >= db_current
    }
    for owned_day in proposed_effective_ownership:
        if not (proposed_case_start <= owned_day <= proposed_case_end):
            raise ValueError(
                "proposed assignment effective ownership must be within proposed case boundaries"
            )
    proposed_coverage_start = max(proposed_case_start, db_current)
    if proposed_coverage_start <= proposed_case_end:
        _ensure_full_coverage(
            proposed_effective_ownership,
            proposed_coverage_start,
            proposed_case_end,
            "proposed assignments",
        )

    history_cutoff = db_current - timedelta(days=1)
    unchanged_cutoff = effective - timedelta(days=1)
    intersection_start = max(current_case_start, proposed_case_start)
    intersection_end = min(current_case_end, proposed_case_end)
    unchanged_end = min(intersection_end, unchanged_cutoff)
    if intersection_start <= unchanged_end:
        unchanged_days = _iter_days(intersection_start, unchanged_end)
    else:
        unchanged_days = ()
    if historical_state == "locked":
        for current_day in unchanged_days:
            before_owner = before_ownership.get(current_day)
            after_owner = after_ownership.get(current_day)
            if before_owner != after_owner:
                if operation == "batch_leave_resolution":
                    _batch_conflict(
                        "historical_ownership_locked",
                        date=current_day.isoformat(),
                        expected_assignment_id=None if before_owner is None else str(before_owner),
                        actual_assignment_id=None if after_owner is None else str(after_owner),
                    )
                raise ValueError(
                    f"assignment ownership before effective_date modified on {current_day.isoformat()}"
                )

    for assignment_id, before_row in current_by_id.items():
        if historical_state != "locked":
            continue
        before_start = before_row["assigned_start_date"]
        before_end = before_row["assigned_end_date"]
        assert before_start is not None and before_end is not None
        historical_intersection_start = max(
            before_start,
            current_case_start,
            proposed_case_start,
        )
        historical_intersection_end = min(
            before_end,
            current_case_end,
            proposed_case_end,
            history_cutoff,
        )
        if historical_intersection_start > historical_intersection_end:
            continue

        after_row = None
        for row in after_non_cancelled:
            if row["id"] == assignment_id:
                after_row = row
                break
        if after_row is None:
            if operation == "batch_leave_resolution":
                _batch_conflict(
                    "historical_ownership_locked",
                    assignment_id=assignment_id,
                    reason="historical_assignment_removed",
                )
            raise ValueError(f"historical assignment {assignment_id} was removed before db current date")

        if after_row["staff_id"] != before_row["staff_id"]:
            if operation == "batch_leave_resolution":
                _batch_conflict(
                    "historical_ownership_locked",
                    assignment_id=assignment_id,
                    field="staff_id",
                    expected=before_row["staff_id"],
                    actual=after_row["staff_id"],
                )
            raise ValueError(f"historical assignment {assignment_id} changed staff_id")
        if after_row["status"] != before_row["status"]:
            raise ValueError(f"historical assignment {assignment_id} changed status")
        if after_row["assigned_start_date"] != before_row["assigned_start_date"]:
            raise ValueError(f"historical assignment {assignment_id} changed start date")
        if after_row["kind"] != before_row["kind"]:
            raise ValueError(f"historical assignment {assignment_id} changed kind")
        if after_row["original_assignment_id"] != before_row["original_assignment_id"]:
            raise ValueError(
                f"historical assignment {assignment_id} changed original_assignment_id"
            )
        if after_row["substitution_work_date"] != before_row["substitution_work_date"]:
            raise ValueError(
                f"historical assignment {assignment_id} changed substitution_work_date"
            )
        required_min_end = historical_intersection_end
        if after_row["assigned_end_date"] is None or after_row["assigned_end_date"] < required_min_end:
            raise ValueError(f"historical assignment {assignment_id} changed end date into history")

    substitute_rows: list[dict[str, Any]] = []
    for row in after_non_cancelled:
        if row["kind"] in {"single_day_substitute", "substitute"}:
            if row["original_assignment_id"] is None or row["substitution_work_date"] is None:
                raise ValueError(
                    "single-day substitute rows require original_assignment_id and substitution_work_date"
                )
            substitute_rows.append(row)
    new_substitute_rows = [
        row for row in substitute_rows if row["id"] not in current_by_id
    ]

    if operation == "single_day_substitute":
        if len(new_substitute_rows) != 1:
            raise ValueError(
                "single_day_substitute requires exactly one new substitute assignment"
            )
        substitute = new_substitute_rows[0]
        if substitute["assigned_start_date"] != substitute["assigned_end_date"]:
            raise ValueError("single-day substitute must be one-day long")
        if substitute["assigned_start_date"] != substitute["substitution_work_date"]:
            raise ValueError("substitute interval must match substitution_work_date")
        if substitute["id"] in current_by_id:
            raise ValueError("substitute assignment must be a new key")
        original_assignment_id = substitute["original_assignment_id"]
        if original_assignment_id is None:
            raise ValueError("single-day substitute requires original_assignment_id")
        original = current_by_id.get(original_assignment_id)
        if original is None:
            raise ValueError("single-day substitute requires a valid original assignment id")
        substitute_day = substitute["substitution_work_date"]
        if substitute_day is None:
            raise ValueError("substitute assignment requires substitution_work_date")
        assert substitute_day is not None
        if not (original["assigned_start_date"] <= substitute_day <= original["assigned_end_date"]):
            raise ValueError("substitution_work_date must be within original assignment period")
        if substitute["id"] == original["id"]:
            raise ValueError("substitute assignment cannot reuse original assignment id")
        if before_ownership.get(substitute_day) != original["id"]:
            raise ValueError("substitution requires an original ownership day")
        if after_ownership.get(substitute_day) != substitute["id"]:
            raise ValueError("substitution day must be owned by substitute assignment")
        for current_day in _iter_days(
            original["assigned_start_date"],
            original["assigned_end_date"],
        ):
            if current_day == substitute_day:
                continue
            owner_id = after_ownership.get(current_day)
            owner = next(
                (row for row in after_non_cancelled if row["id"] == owner_id),
                None,
            )
            if owner is None or owner["staff_id"] != original["staff_id"]:
                raise ValueError(
                    "original assignment dates around substitution must remain with original staff"
                )
            if owner["kind"] != "formal":
                raise ValueError(
                    "prefix and suffix around substitution must be formal assignments"
                )
        prefix_owner = (
            after_ownership.get(substitute_day - timedelta(days=1))
            if substitute_day > original["assigned_start_date"]
            else None
        )
        suffix_owner = (
            after_ownership.get(substitute_day + timedelta(days=1))
            if substitute_day < original["assigned_end_date"]
            else None
        )
        if (
            prefix_owner is not None
            and suffix_owner is not None
            and prefix_owner == suffix_owner
        ):
            raise ValueError(
                "prefix and suffix around substitution must use independent assignments"
            )
    elif operation == "segment_reconfigure":
        if new_substitute_rows:
            raise ValueError(
                "segment_reconfigure does not allow new substitute rows"
            )

    assignment_transition_plan: dict[str, Any] = {
        "case_no": target_case_no,
        "operation_kind": operation,
        "historical_fact_state": historical_state,
        "requires_audit": historical_state == "unlocked",
        "effective_date": effective,
        "current_case_start_date": current_case_start,
        "current_case_end_date": current_case_end,
        "proposed_case_start_date": proposed_case_start,
        "proposed_case_end_date": proposed_case_end,
        "removed_future_dates": [
            removed_day.isoformat()
            for removed_day in _iter_days(current_case_start, current_case_end)
            if removed_day >= db_current
            and not (proposed_case_start <= removed_day <= proposed_case_end)
        ],
        "before_assignments": sorted(
            before_assignments,
            key=lambda row: (row["assigned_start_date"] or date.min, str(row["id"])),
        ),
        "after_assignments": sorted(
            after_assignments,
            key=lambda row: (
                row["assigned_start_date"] or date.min,
                str(row["id"]),
                row["status"],
            ),
        ),
        "created": sorted(
            facts_created,
            key=lambda row: (str(row["id"]), row["assigned_start_date"] or date.min),
        ),
        "retained": sorted(
            facts_retained,
            key=lambda row: (str(row["id"]), row["assigned_start_date"] or date.min),
        ),
        "truncated": sorted(
            facts_truncated,
            key=lambda item: (str(item["before"]["id"]), item["before"]["assigned_start_date"] or date.min),
        ),
        "cancelled": sorted(
            facts_cancelled,
            key=lambda item: (
                str(item["before"]["id"]) if item["before"] is not None else "",
                item["before"]["assigned_start_date"] or date.min,
            )
            if item["before"] is not None
            else ("", date.min),
        ),
        "facts": {
            "created": sorted(
                facts_created,
                key=lambda row: (str(row["id"]), row["assigned_start_date"] or date.min),
            ),
            "retained": sorted(
                facts_retained,
                key=lambda row: (str(row["id"]), row["assigned_start_date"] or date.min),
            ),
            "truncated": sorted(
                facts_truncated,
                key=lambda item: (
                    str(item["before"]["id"]),
                    item["before"]["assigned_start_date"] or date.min,
                ),
            ),
            "cancelled": sorted(
                facts_cancelled,
                key=lambda item: (
                    str(item["before"]["id"]) if item["before"] is not None else "",
                    item["before"]["assigned_start_date"] or date.min,
                )
                if item["before"] is not None
                else ("", date.min),
            ),
        },
        "ownership_by_date": {
            owned_day.isoformat(): str(assignment_id)
            for owned_day, assignment_id in sorted(
                proposed_effective_ownership.items()
            )
        },
    }

    return assignment_transition_plan
