"""Pure leave/substitution preview calculation from canonical server facts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from services.caregiver_segment_availability_service import _extract_blocked_days
from services.multi_caregiver_assignment_rules import (
    AssignmentPlanTransitionConflict,
    validate_assignment_plan_transition,
)


_BATCH_PREVIEW_CONTRACT_VERSION = "assignment-leave-substitution-batch-preview/v1"


def canonicalize_assignment_leave_resolution_batch_request(
    request: Mapping[str, Any],
    original_assignment_schedule: Mapping[str, Any],
    conflict_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate batch request intent and build deterministic canonical intent + lineage."""
    if not isinstance(request, Mapping):
        raise ValueError("request must be a mapping")
    if not isinstance(original_assignment_schedule, Mapping):
        raise ValueError("original_assignment_schedule must be a mapping")
    if not isinstance(conflict_snapshot, Mapping):
        raise ValueError("conflict_snapshot must be a mapping")

    allowed_request_fields = {
        "contract_version",
        "case_no",
        "original_assignment_id",
        "items",
    }
    if set(request.keys()) != allowed_request_fields:
        raise ValueError("request contains unsupported or missing fields")

    if request.get("contract_version") != _BATCH_PREVIEW_CONTRACT_VERSION:
        raise ValueError("contract_version must be assignment-leave-substitution-batch-preview/v1")
    case_no = request.get("case_no")
    if not isinstance(case_no, str) or not case_no.strip():
        raise ValueError("case_no must be a non-empty string")
    canonical_case_no = case_no.strip()
    original_assignment_id = _positive_int(
        request.get("original_assignment_id"), "original_assignment_id"
    )

    items = request.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a non-empty list")
    if len(items) == 0:
        raise ValueError("items must be a non-empty list")

    required_item_fields = {
        "original_schedule_id",
        "work_date",
        "resolution_type",
        "substitute_staff_id",
    }
    parsed_items: list[dict[str, Any]] = []
    seen_schedule_ids: set[int] = set()
    seen_work_dates: set[date] = set()
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, Mapping):
            raise ValueError(f"items[{index}] must be a mapping")
        if not required_item_fields.issubset(raw_item) or set(raw_item) - (
            required_item_fields | {"is_double_pay"}
        ):
            raise ValueError("item contains unsupported or missing fields")

        schedule_id = _positive_int(
            raw_item.get("original_schedule_id"), "original_schedule_id"
        )
        if schedule_id in seen_schedule_ids:
            raise ValueError("duplicate original_schedule_id in items")
        seen_schedule_ids.add(schedule_id)

        work_day = _iso_date_string(raw_item.get("work_date"), "work_date")
        if work_day in seen_work_dates:
            raise ValueError("duplicate work_date in items")
        seen_work_dates.add(work_day)

        resolution_type = raw_item.get("resolution_type")
        if not isinstance(resolution_type, str) or resolution_type not in (
            "defer_following_assignments",
            "substitute",
        ):
            raise ValueError(
                "resolution_type must be defer_following_assignments or substitute"
            )
        substitute_staff_value = raw_item.get("substitute_staff_id")
        if resolution_type == "substitute":
            substitute_staff_id = _positive_int(
                substitute_staff_value, "substitute_staff_id"
            )
        elif substitute_staff_value is not None:
            raise ValueError("substitute_staff_id must be null when deferring assignments")
        else:
            substitute_staff_id = None
        is_double_pay = raw_item.get("is_double_pay", False)
        if type(is_double_pay) is not bool:
            raise ValueError("is_double_pay must be bool")
        if resolution_type == "defer_following_assignments" and is_double_pay:
            raise ValueError("is_double_pay must be false when deferring assignments")

        parsed_items.append(
            {
                "original_schedule_id": schedule_id,
                "work_date": work_day,
                "resolution_type": resolution_type,
                "substitute_staff_id": substitute_staff_id,
                "is_double_pay": is_double_pay,
            }
        )

    original_assignment = original_assignment_schedule.get("assignment")
    if not isinstance(original_assignment, Mapping):
        raise ValueError("original_assignment_schedule.assignment must be a mapping")
    original = dict(original_assignment)
    if not original:
        raise ValueError("original assignment ownership mismatch")
    if _positive_int(original.get("id"), "original_assignment_id") != original_assignment_id:
        raise ValueError("original assignment ownership mismatch")
    if original.get("case_no") != canonical_case_no:
        raise ValueError("original assignment ownership mismatch")
    original_staff_id = _positive_int(original.get("staff_id"), "original staff_id")

    original_schedule_rows = original_assignment_schedule.get("schedule_days")
    if not isinstance(original_schedule_rows, list):
        raise ValueError("original_assignment_schedule.schedule_days must be a list")
    original_schedule_identities: dict[int, tuple[int, int, str, int, date]] = {}
    for index, row in enumerate(original_schedule_rows):
        if not isinstance(row, Mapping):
            raise ValueError("original_assignment_schedule.schedule_days must contain mappings")
        row_assignment_id = _positive_int(
            row.get("assignment_id"), f"assignment_schedule_days[{index}].assignment_id"
        )
        row_schedule_id = _positive_int(
            row.get("id"), f"assignment_schedule_days[{index}].id"
        )
        if row_schedule_id in original_schedule_identities:
            raise ValueError(
                "original_schedule_id does not belong to original_assignment_id"
            )
        row_case_no = row.get("case_no")
        row_staff_id = _positive_int(
            row.get("staff_id"), f"assignment_schedule_days[{index}].staff_id"
        )
        row_work_date = _date(
            row.get("work_date"), f"assignment_schedule_days[{index}].work_date"
        )
        if row_assignment_id != original_assignment_id:
            raise ValueError(
                "original_schedule_id does not belong to original_assignment_id"
            )
        if row_case_no != canonical_case_no or row_staff_id != original_staff_id:
            raise ValueError("original schedule ownership mismatch")
        original_schedule_identities[row_schedule_id] = (
            row_schedule_id,
            row_assignment_id,
            row_case_no,
            row_staff_id,
            row_work_date,
        )

    if not original_schedule_identities:
        raise ValueError("original assignment schedule is missing")

    snapshot_assignments = conflict_snapshot.get("assignments")
    if not isinstance(snapshot_assignments, list):
        raise ValueError("conflict_snapshot.assignments must be a list")
    snapshot_rows: list[dict[str, Any]] = []
    for index, row in enumerate(snapshot_assignments):
        if not isinstance(row, Mapping):
            raise ValueError("conflict_snapshot.assignments must contain mappings")
        if _positive_int(row.get("id"), f"conflict_snapshot.assignments[{index}].id") != original_assignment_id:
            continue
        snapshot_rows.append(dict(row))
    if len(snapshot_rows) != 1:
        raise ValueError("original assignment ownership mismatch")
    snapshot_assignment = snapshot_rows[0]
    if (
        snapshot_assignment.get("case_no") != canonical_case_no
        or _positive_int(snapshot_assignment.get("staff_id"), "assignment staff_id")
        != original_staff_id
    ):
        raise ValueError("original assignment ownership mismatch")

    snapshot_schedule_rows = conflict_snapshot.get("assignment_schedule_days")
    if not isinstance(snapshot_schedule_rows, list):
        raise ValueError("conflict_snapshot.assignment_schedule_days must be a list")
    snapshot_schedule_identities: dict[int, tuple[int, int, str, int, date]] = {}
    for index, row in enumerate(snapshot_schedule_rows):
        if not isinstance(row, Mapping):
            raise ValueError(
                "conflict_snapshot.assignment_schedule_days must contain mappings"
            )
        row_assignment_id = _positive_int(
            row.get("assignment_id"),
            f"conflict_snapshot.assignment_schedule_days[{index}].assignment_id",
        )
        row_schedule_id = _positive_int(
            row.get("id"),
            f"conflict_snapshot.assignment_schedule_days[{index}].id",
        )
        if row_assignment_id != original_assignment_id:
            continue
        if row_schedule_id in snapshot_schedule_identities:
            raise ValueError(
                "original_schedule_id does not belong to original_assignment_id"
            )
        row_case_no = row.get("case_no")
        row_staff_id = _positive_int(
            row.get("staff_id"),
            f"conflict_snapshot.assignment_schedule_days[{index}].staff_id",
        )
        row_work_date = _date(
            row.get("work_date"),
            f"conflict_snapshot.assignment_schedule_days[{index}].work_date",
        )
        if row_case_no != canonical_case_no or row_staff_id != original_staff_id:
            raise ValueError("original schedule ownership mismatch")
        snapshot_schedule_identities[row_schedule_id] = (
            row_schedule_id,
            row_assignment_id,
            row_case_no,
            row_staff_id,
            row_work_date,
        )

    if original_schedule_identities.keys() != snapshot_schedule_identities.keys():
        raise ValueError(
            "original_schedule_id does not belong to original_assignment_id; "
            "schedule snapshots do not match"
        )
    if original_schedule_identities != snapshot_schedule_identities:
        raise ValueError("original schedule ownership mismatch: schedule snapshots do not match")

    canonical_items: list[dict[str, Any]] = []
    for item in parsed_items:
        schedule_id = item["original_schedule_id"]
        schedule_identity = original_schedule_identities.get(schedule_id)
        if schedule_identity is None:
            raise ValueError("original_schedule_id does not belong to original_assignment_id")
        canonical_schedule_day = schedule_identity[4]
        if item["work_date"] != canonical_schedule_day:
            raise ValueError("work_date must match original schedule day")

        canonical_items.append(
            {
                "work_date": item["work_date"],
                "original_schedule_id": item["original_schedule_id"],
                "resolution_type": item["resolution_type"],
                "substitute_staff_id": item["substitute_staff_id"],
                "is_double_pay": item["is_double_pay"],
            }
        )

    sorted_items = sorted(
        canonical_items,
        key=lambda item: (item["work_date"], item["original_schedule_id"]),
    )
    canonical_batch_items = []
    canonical_lineage = []
    for batch_item_index, item in enumerate(sorted_items):
        canonical_item = {
            **item,
            "batch_item_index": batch_item_index,
        }
        canonical_item["work_date"] = canonical_item["work_date"].isoformat()
        canonical_batch_items.append(canonical_item)

        lineage_item = {
            "batch_item_index": batch_item_index,
            "original_assignment_id": original_assignment_id,
            "original_schedule_id": item["original_schedule_id"],
            "original_staff_id": original_staff_id,
            "work_date": item["work_date"].isoformat(),
        }
        canonical_lineage.append(lineage_item)

    return {
        "canonical_batch_intent": {
            "contract_version": _BATCH_PREVIEW_CONTRACT_VERSION,
            "case_no": canonical_case_no,
            "original_assignment_id": original_assignment_id,
            "items": canonical_batch_items,
        },
        "item_lineage": {
            "items": canonical_lineage,
        },
    }


def _iso_date_string(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc
    if value != parsed.isoformat():
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    return parsed


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc
    if value != parsed.isoformat():
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    return parsed


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive decimal")
    try:
        result = Decimal(str(value))
    except (TypeError, InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive decimal") from exc
    if result <= 0:
        raise ValueError(f"{field_name} must be a positive decimal")
    return result


def _fingerprinted(result: dict[str, Any]) -> dict[str, Any]:
    def json_value(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {
                str(key): json_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [json_value(item) for item in value]
        return value

    canonical = json.dumps(
        json_value(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {**result, "preview_fingerprint": hashlib.sha256(canonical).hexdigest()}


_DOMAIN_RULES_CASE_NO = "__pure_domain_case__"
_SEGMENT_REF_PATTERN = re.compile(r"^(current|derived|substitute):(\d+)$")
_ALLOWED_SEGMENT_KINDS = {"formal", "single_day_substitute", "substitute"}
_ALLOWED_SEGMENT_STATUSES = {"planned", "active", "completed", "replaced", "cancelled"}
_ALLOWED_RESOLUTIONS = {"defer", "substitute"}
_ALLOWED_DIAGNOSTIC_CODES = {
    "batch_leave_target_mismatch",
    "batch_substitute_lineage_invalid",
    "batch_substitute_date_duplicate",
    "batch_original_staff_ownership_changed",
    "batch_defer_shift_invalid",
    "assignment_row_limit_exceeded",
    "assignment_daily_ownership_invalid",
    "historical_ownership_locked",
}
_ALLOWED_DIAGNOSTIC_FACT_KEYS = {
    "field",
    "reason",
    "expected",
    "actual",
    "service_day",
    "segment_ref",
    "segment_refs",
    "original_segment_ref",
    "expected_defer_days",
    "actual_defer_days",
    "maximum_active_segments",
    "actual_active_segments",
    "database_current_date",
    "effective_date",
}
_ASSIGNMENT_TRANSITION_TOP_LEVEL_KEYS = {
    "case_no",
    "operation_kind",
    "historical_fact_state",
    "requires_audit",
    "effective_date",
    "current_case_start_date",
    "current_case_end_date",
    "proposed_case_start_date",
    "proposed_case_end_date",
    "removed_future_dates",
    "before_assignments",
    "after_assignments",
    "created",
    "retained",
    "truncated",
    "cancelled",
    "facts",
    "ownership_by_date",
}
_ASSIGNMENT_TRANSITION_ASSIGNMENT_KEYS = {
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
# MultiCaregiver transition facts are explicit lifecycle buckets.
_ASSIGNMENT_TRANSITION_FACT_KEYS = {
    "created",
    "retained",
    "truncated",
    "cancelled",
}


def _domain_rules_reference_identity(
    value: Any, field_name: str
) -> tuple[int, int | str | bytes, Any]:
    if isinstance(value, bool):
        return (0, 1 if value else 0, value)
    if isinstance(value, int):
        return (1, f"{value:d}", value)
    if isinstance(value, str):
        if not value:
            raise ValueError(f"{field_name} must be bool, int, or non-empty str")
        return (2, value.encode("utf-8"), value)
    raise ValueError(f"{field_name} must be bool, int, or non-empty str")


def _domain_rules_ref_to_key(value: Any, field_name: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical segment ref")
    match = _SEGMENT_REF_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"{field_name} must be a canonical segment ref")
    namespace, raw_index = match.groups()
    if raw_index != "0" and raw_index.startswith("0"):
        raise ValueError(f"{field_name} must be a canonical decimal index")
    return namespace, int(raw_index)


def _domain_rules_sorted_ids(
    values: set[tuple[int, int | str | bytes, Any]]
) -> list[tuple[int, int | str | bytes, Any]]:
    return sorted(values, key=_domain_rules_reference_sort_key)


def _domain_rules_reference_sort_key(
    identity: tuple[int, int | str | bytes, Any]
) -> tuple[int, int | str | bytes]:
    namespace, canonical_key, _raw_value = identity
    if namespace in {0, 1, 2}:
        return (namespace, canonical_key)
    raise ValueError("invalid identity namespace")


def _domain_rules_segment_ref_sort_key(ref: str) -> tuple[int, int]:
    namespace, index = _domain_rules_ref_to_key(ref, "segment_ref")
    namespace_rank = {"current": 0, "derived": 1, "substitute": 2}[namespace]
    return (namespace_rank, index)


def _domain_rules_parse_service_plan(
    *, plan: Mapping[str, Any], plan_name: str, require_current_refs: bool = False
) -> dict[str, Any]:
    if set(plan) != {"segments", "daily_ownership", "service_period"}:
        raise ValueError(
            f"{plan_name} must contain exact keys: segments, daily_ownership, service_period"
        )

    service_period = plan.get("service_period")
    if not isinstance(service_period, Mapping):
        raise ValueError(f"{plan_name}.service_period must be a mapping")
    if set(service_period) != {"start", "end"}:
        raise ValueError(f"{plan_name}.service_period must contain exact keys: start/end")
    period_start = _date(service_period["start"], f"{plan_name}.service_period.start")
    period_end = _date(service_period["end"], f"{plan_name}.service_period.end")
    if period_start > period_end:
        raise ValueError(f"{plan_name}.service_period start must be <= end")

    raw_segments = plan.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError(f"{plan_name}.segments must be a non-empty list")
    segments: dict[str, dict[str, Any]] = {}
    for index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, Mapping):
            raise ValueError(f"{plan_name}.segments[{index}] must be a mapping")
        expected_segment_keys = {
            "segment_ref",
            "caregiver_ref",
            "status",
            "service_period",
            "segment_kind",
            "lineage",
        }
        if set(raw_segment) != expected_segment_keys:
            raise ValueError(f"{plan_name}.segments[{index}] has unsupported or missing keys")

        segment_ref = raw_segment.get("segment_ref")
        if not isinstance(segment_ref, str):
            raise ValueError(f"{plan_name}.segments[{index}].segment_ref must be a canonical ref")
        segment_namespace, segment_index = _domain_rules_ref_to_key(
            segment_ref,
            f"{plan_name}.segments[{index}].segment_ref",
        )
        if require_current_refs and segment_namespace != "current":
            raise ValueError(
                f"{plan_name}.segments[{index}].segment_ref must be current:N"
            )
        if not isinstance(segment_ref, str):
            raise ValueError(f"{plan_name}.segments[{index}].segment_ref must be a canonical ref")
        if segment_ref in segments:
            raise ValueError(f"{plan_name}.segments[{index}] has duplicate segment_ref")

        caregiver_ref = _domain_rules_reference_identity(
            raw_segment.get("caregiver_ref"),
            f"{plan_name}.segments[{index}].caregiver_ref",
        )
        status = raw_segment.get("status")
        if status not in _ALLOWED_SEGMENT_STATUSES:
            raise ValueError(
                f"{plan_name}.segments[{index}].status must be planned/active/completed/replaced/cancelled"
            )
        segment_kind = raw_segment.get("segment_kind")
        if segment_kind not in _ALLOWED_SEGMENT_KINDS:
            raise ValueError(
                f"{plan_name}.segments[{index}].segment_kind must be formal/single_day_substitute/substitute"
            )

        segment_period = raw_segment.get("service_period")
        if not isinstance(segment_period, Mapping):
            raise ValueError(f"{plan_name}.segments[{index}].service_period must be a mapping")
        if set(segment_period) != {"start", "end"}:
            raise ValueError(
                f"{plan_name}.segments[{index}].service_period must contain exact keys: start/end"
            )
        segment_start = _date(
            segment_period.get("start"),
            f"{plan_name}.segments[{index}].service_period.start",
        )
        segment_end = _date(
            segment_period.get("end"),
            f"{plan_name}.segments[{index}].service_period.end",
        )
        if segment_start > segment_end:
            raise ValueError(
                f"{plan_name}.segments[{index}].service_period start must be <= end"
            )

        lineage = raw_segment.get("lineage")
        if not isinstance(lineage, Mapping):
            raise ValueError(f"{plan_name}.segments[{index}].lineage must be a mapping")
        if set(lineage) != {"original_segment_ref", "substitution_service_day"}:
            raise ValueError(
                f"{plan_name}.segments[{index}].lineage must contain exact keys"
            )
        lineage_original = lineage.get("original_segment_ref")
        lineage_day_raw = lineage.get("substitution_service_day")
        if segment_kind in {"substitute", "single_day_substitute"}:
            if lineage_original is None:
                raise ValueError(
                    f"{plan_name}.segments[{index}] substitute must reference original_segment_ref"
                )
            lineage_original_ref = _domain_rules_ref_to_key(
                lineage_original,
                f"{plan_name}.segments[{index}].lineage.original_segment_ref",
            )
            if lineage_day_raw is None:
                raise ValueError(
                    f"{plan_name}.segments[{index}].lineage.substitution_service_day is required for substitutes"
                )
            lineage_day = _date(
                lineage_day_raw,
                f"{plan_name}.segments[{index}].lineage.substitution_service_day",
            )
        elif lineage_original is not None or lineage_day_raw is not None:
            raise ValueError(
                f"{plan_name}.segments[{index}] formal segments require lineage null"
            )
        else:
            lineage_original_ref = None
            lineage_day = None

        segments[segment_ref] = {
            "segment_ref": segment_ref,
            "segment_namespace": segment_namespace,
            "segment_index": segment_index,
            "caregiver_ref": caregiver_ref,
            "status": status,
            "segment_kind": segment_kind,
            "service_start": segment_start,
            "service_end": segment_end,
            "lineage_original_segment_ref": lineage_original_ref,
            "lineage_substitution_service_day": lineage_day,
            "source": raw_segment,
        }

    if require_current_refs:
        current_indices = sorted(
            segment["segment_index"] for segment in segments.values()
        )
        if current_indices != list(range(len(current_indices))):
            raise ValueError(
                f"{plan_name}.segments current refs must be consecutive from 0"
            )

    raw_ownership = plan.get("daily_ownership")
    if not isinstance(raw_ownership, list) or not raw_ownership:
        raise ValueError(f"{plan_name}.daily_ownership must be a non-empty list")
    ownership = []
    ownership_days: set[date] = set()
    for row_index, raw_row in enumerate(raw_ownership):
        if not isinstance(raw_row, Mapping):
            raise ValueError(
                f"{plan_name}.daily_ownership[{row_index}] must be a mapping"
            )
        if set(raw_row) != {"service_day", "segment_ref", "caregiver_ref"}:
            raise ValueError(
                f"{plan_name}.daily_ownership[{row_index}] has unsupported or missing keys"
            )
        service_day = _date(
            raw_row.get("service_day"),
            f"{plan_name}.daily_ownership[{row_index}].service_day",
        )
        if service_day < period_start or service_day > period_end:
            raise ValueError(f"{plan_name}.daily_ownership[{row_index}] outside service period")
        ownership_segment_ref = raw_row.get("segment_ref")
        if ownership_segment_ref not in segments:
            raise ValueError(
                f"{plan_name}.daily_ownership[{row_index}] references unknown segment_ref"
            )
        ownership_caregiver = _domain_rules_reference_identity(
            raw_row.get("caregiver_ref"),
            f"{plan_name}.daily_ownership[{row_index}].caregiver_ref",
        )
        if ownership_caregiver != segments[ownership_segment_ref]["caregiver_ref"]:
            raise ValueError(
                f"{plan_name}.daily_ownership[{row_index}] caregiver_ref must match segment caregiver_ref"
            )
        if service_day in ownership_days:
            raise ValueError(
                f"{plan_name}.daily_ownership[{row_index}] duplicates service_day"
            )
        ownership_days.add(service_day)
        ownership.append(
            {
                "service_day": service_day,
                "segment_ref": ownership_segment_ref,
                "caregiver_ref": ownership_caregiver,
            }
        )

    expected_days = {
        day
        for day in (
            period_start + timedelta(days=offset)
            for offset in range((period_end - period_start).days + 1)
        )
    }
    if ownership_days != expected_days:
        raise ValueError(f"{plan_name}.daily_ownership must cover exact service period")

    return {
        "segments": segments,
        "segment_refs": list(segments),
        "service_period": {
            "start": period_start,
            "end": period_end,
        },
        "ownership": ownership,
    }


def _domain_rules_segment_ref_order(plan_segments: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(plan_segments, key=_domain_rules_segment_ref_sort_key)


def _domain_rules_parse_intent(
    intent: Mapping[str, Any], plan_segments: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if set(intent) != {"original_segment_ref", "items"}:
        raise ValueError("canonical_leave_intent must contain exact keys")
    raw_original_ref = intent.get("original_segment_ref")
    original_segment = _domain_rules_ref_to_key(
        raw_original_ref,
        "canonical_leave_intent.original_segment_ref",
    )
    if original_segment[0] != "current":
        raise ValueError(
            "canonical_leave_intent.original_segment_ref must be a current segment ref"
        )
    if f"current:{original_segment[1]}" not in plan_segments:
        raise ValueError(
            "canonical_leave_intent.original_segment_ref is not in before plan"
        )

    raw_items = intent.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("canonical_leave_intent.items must be a non-empty list")
    items: list[dict[str, Any]] = []
    item_refs: set[int] = set()
    service_days: set[date] = set()
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise ValueError(f"canonical_leave_intent.items[{index}] must be a mapping")
        if set(raw_item) != {
            "item_ref",
            "service_day",
            "resolution",
            "substitute_caregiver_ref",
        }:
            raise ValueError(f"canonical_leave_intent.items[{index}] has unsupported or missing keys")
        item_ref = raw_item.get("item_ref")
        if isinstance(item_ref, bool) or not isinstance(item_ref, int) or item_ref < 0:
            raise ValueError("canonical_leave_intent.item_ref must be a non-negative int")
        if item_ref in item_refs:
            raise ValueError("canonical_leave_intent.item_ref must be unique")
        item_refs.add(item_ref)
        service_day = _date(
            raw_item.get("service_day"),
            f"canonical_leave_intent.items[{index}].service_day",
        )
        if service_day in service_days:
            raise ValueError("canonical_leave_intent items must not duplicate service_day")
        service_days.add(service_day)
        resolution = raw_item.get("resolution")
        if resolution not in _ALLOWED_RESOLUTIONS:
            raise ValueError(
                "canonical_leave_intent.items[{index}].resolution must be defer or substitute"
            )
        substitute_identity = raw_item.get("substitute_caregiver_ref")
        if resolution == "substitute":
            if substitute_identity is None:
                raise ValueError(
                    "substitute intent item requires substitute_caregiver_ref"
                )
            substitute_caregiver_ref = _domain_rules_reference_identity(
                substitute_identity,
                f"canonical_leave_intent.items[{index}].substitute_caregiver_ref",
            )
        else:
            if substitute_identity is not None:
                raise ValueError(
                    "defer intent item must not include substitute_caregiver_ref"
                )
            substitute_caregiver_ref = None
        items.append(
            {
                "item_ref": item_ref,
                "service_day": service_day,
                "resolution": resolution,
                "substitute_caregiver_ref": substitute_caregiver_ref,
            }
        )

    expected_refs = list(range(0, len(items)))
    if sorted(item_refs) != expected_refs:
        raise ValueError("canonical_leave_intent.item_ref must be consecutive from 0")
    items.sort(key=lambda item: item["item_ref"])
    return {
        "original_segment_ref": f"current:{original_segment[1]}",
        "items": items,
        "substitute_item_refs": [
            item["item_ref"] for item in items if item["resolution"] == "substitute"
        ],
    }


def _domain_rules_build_rule_rows(
    *,
    plan_segments: dict[str, dict[str, Any]],
    staff_by_identity: dict[tuple[int, int | str | bytes, Any], int],
) -> tuple[list[dict[str, Any]], dict[str, str | int], dict[str | int, str]]:
    rows = []
    segment_id_to_ref: dict[str, str | int] = {}
    rule_id_to_ref: dict[str | int, str] = {}
    for ref in sorted(
        plan_segments,
        key=lambda item: (
            plan_segments[item]["service_start"],
            plan_segments[item]["service_end"],
            _domain_rules_segment_ref_sort_key(item),
        ),
    ):
        segment = plan_segments[ref]
        namespace, index = _domain_rules_ref_to_key(ref, "segment_ref")
        if namespace == "current":
            rule_id: str | int = index + 1
        elif namespace == "derived":
            rule_id = f"__derived__:{index}"
        else:
            rule_id = f"__substitute__:{index}"
        original_assignment_id = None
        substitution_work_date = None
        if segment["segment_kind"] in {"substitute", "single_day_substitute"}:
            original_original_ref = f"{segment['lineage_original_segment_ref'][0]}:{segment['lineage_original_segment_ref'][1]}"
            original_assignment_id = (
                _domain_rules_segment_ref_to_rule_id(
                    original_original_ref,
                    plan_segments,
                )
            )
            substitution_work_date = segment["lineage_substitution_service_day"]
        segment_id_to_ref[str(ref)] = rule_id
        rule_id_to_ref[rule_id] = str(ref)
        rows.append(
            {
                "id": rule_id,
                "case_no": _DOMAIN_RULES_CASE_NO,
                "staff_id": staff_by_identity[segment["caregiver_ref"]],
                "status": segment["status"],
                "assigned_start_date": segment["service_start"],
                "assigned_end_date": segment["service_end"],
                "kind": (
                    "single_day_substitute"
                    if segment["segment_kind"] == "single_day_substitute"
                    else segment["segment_kind"]
                ),
                "original_assignment_id": original_assignment_id,
                "substitution_work_date": substitution_work_date,
            }
        )
    return rows, segment_id_to_ref, rule_id_to_ref


def _domain_rules_segment_ref_to_rule_id(
    segment_ref: str,
    plan_segments: dict[str, dict[str, Any]],
) -> str | int:
    namespace, index = _domain_rules_ref_to_key(segment_ref, "segment_ref")
    if namespace == "current":
        return index + 1
    if namespace == "derived":
        return f"__derived__:{index}"
    return f"__substitute__:{index}"


def _domain_rules_reverse_segment_id(
    *, value: Any, reverse_map: Mapping[str | int, str]
) -> str:
    if isinstance(value, bool):
        raise ValueError("assignment_id must not be bool")
    if isinstance(value, int):
        if value not in reverse_map:
            raise ValueError(f"unknown assignment identity {value}")
        return reverse_map[value]
    if isinstance(value, str):
        if value in reverse_map:
            return reverse_map[value]
        raise ValueError(f"unknown assignment identity {value}")
    raise ValueError("assignment_id must be JSON-safe identity")


def _domain_rules_reverse_staff_id(
    *,
    value: Any,
    staff_id_to_identity: Mapping[int, tuple[int, int | str | bytes, Any]],
) -> Any:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("staff_id must be a positive integer mapping key")
    identity = staff_id_to_identity.get(value)
    if identity is None:
        raise ValueError(f"unknown staff identity {value}")
    return identity[2]


def _domain_rules_canonical_plan(
    *,
    plan_segments: dict[str, dict[str, Any]],
    service_period: dict[str, date],
) -> dict[str, Any]:
    segment_refs = [
        ref
        for ref in plan_segments
        if _SEGMENT_REF_PATTERN.fullmatch(ref)
    ]
    segment_refs = sorted(segment_refs, key=_domain_rules_segment_ref_sort_key)

    segments = []
    for ref in segment_refs:
        source = plan_segments[ref]
        segments.append(
            {
                "segment_ref": ref,
                "caregiver_ref": source["caregiver_ref"][2],
                "status": source["status"],
                "service_period": {
                    "start": source["service_start"],
                    "end": source["service_end"],
                },
                "segment_kind": source["segment_kind"],
                "lineage": {
                    "original_segment_ref": (
                        f"{source['lineage_original_segment_ref'][0]}:{source['lineage_original_segment_ref'][1]}"
                    if source["lineage_original_segment_ref"]
                    else None
                ),
                "substitution_service_day": source["lineage_substitution_service_day"],
            },
        }
    )
    ownership_source: list[dict[str, Any]] = []
    if "_ownership_for_canonical" in plan_segments:
        ownership_source = plan_segments["_ownership_for_canonical"]
    elif "_ownership_by_ref" in plan_segments:
        ownership_source = plan_segments["_ownership_by_ref"]
    elif "ownership" in plan_segments:
        ownership_source = plan_segments["ownership"]

    ownership = [
        {
            "service_day": row["service_day"],
            "segment_ref": row["segment_ref"],
            "caregiver_ref": row["caregiver_ref"][2],
        }
        for row in sorted(
            ownership_source,
            key=lambda row: row["service_day"].isoformat(),
        )
    ]
    return {
        "segments": segments,
        "daily_ownership": sorted(
            [
                {
                    "service_day": row["service_day"],
                    "segment_ref": row["segment_ref"],
                    "caregiver_ref": row["caregiver_ref"][2],
                }
                for row in plan_segments["_ownership_by_ref"]
            ],
            key=lambda row: row["service_day"].isoformat(),
        )
        if "_ownership_by_ref" in plan_segments
        else ownership,
        "service_period": service_period,
    }


def _domain_rules_reverse_diagnostics(
    *,
    code: str,
    details: Mapping[str, Any],
    transition_code_facts: Mapping[str | int, str],
    transition_staff_identities: Mapping[int, tuple[int, int | str | bytes, Any]],
) -> dict[str, Any]:
    if code not in _ALLOWED_DIAGNOSTIC_CODES:
        raise ValueError("dependency conflict code is not allowlisted")
    if not isinstance(details, Mapping):
        raise ValueError("dependency conflict details must be a mapping")

    facts: dict[str, Any] = {}

    def _as_date(value: Any) -> str:
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, datetime):
            raise ValueError("details date must not include datetime")
        if isinstance(value, str):
            date_value = value
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                _ = datetime.strptime(value, "%Y-%m-%d").date()
            else:
                raise ValueError("details date must be YYYY-MM-DD")
            return date_value
        return value

    def _segment_refs(values: Any) -> list[str] | str:
        if not isinstance(values, (list, tuple, set)):
            raise ValueError("segment_refs details must be a sequence")
        reversed_values = [
            _domain_rules_reverse_segment_id(
                value=value,
                reverse_map=transition_code_facts,
            )
            for value in values
        ]
        return sorted(reversed_values)

    def _as_int(value: Any, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"details.{field} must be an integer")
        return value

    for key, value in details.items():
        if key == "field":
            if not isinstance(value, str):
                raise ValueError("details field must be a string")
            facts["field"] = value
        elif key == "reason":
            if not isinstance(value, str):
                raise ValueError("details reason must be a string")
            facts["reason"] = value
        elif key == "expected":
            facts["expected"] = _as_date(value)
        elif key == "actual":
            facts["actual"] = _as_date(value)
        elif key == "service_day":
            facts["service_day"] = _as_date(value)
        elif key in {"date", "substitution_work_day", "substitution_work_date"}:
            facts["service_day"] = _as_date(value)
        elif key in {"expected_staff_id", "actual_staff_id"}:
            key_name = "expected" if key == "expected_staff_id" else "actual"
            facts[key_name] = _domain_rules_reverse_staff_id(
                value=value,
                staff_id_to_identity=transition_staff_identities,
            )
        elif key in {"original_assignment_id", "original_segment_ref"}:
            facts["original_segment_ref"] = _domain_rules_reverse_segment_id(
                value=value,
                reverse_map=transition_code_facts,
            )
        elif key in {"segment_ref", "target_assignment_id", "assignment_id"}:
            facts["segment_ref"] = _domain_rules_reverse_segment_id(
                value=value,
                reverse_map=transition_code_facts,
            )
        elif key in {"segment_refs", "assignment_ids", "candidate_assignment_ids", "current_case_assignment_ids"}:
            facts["segment_refs"] = _segment_refs(value)
        elif key in {"actual_defer_days", "expected_defer_days", "derived_defer_days", "defer_days"}:
            normalized_key = (
                key
                if key in {"actual_defer_days", "expected_defer_days"}
                else "expected_defer_days"
            )
            facts[normalized_key] = _as_int(value, key)
        elif key in {"maximum_active_rows", "actual_active_rows", "maximum_active_segments", "actual_active_segments"}:
            facts["maximum_active_segments" if key == "maximum_active_rows" else "actual_active_segments"] = (
                _as_int(value, key)
            )
        elif key in {"database_current_date", "effective_date"}:
            facts[key] = _as_date(value)
        else:
            raise ValueError(f"unknown dependency detail key: {key}")

    unknown = set(facts) - _ALLOWED_DIAGNOSTIC_FACT_KEYS
    if unknown:
        raise ValueError(f"dependency detail contains unknown fact keys: {sorted(unknown)}")

    if code == "batch_substitute_date_duplicate" and "segment_refs" not in facts:
        if "assignment_ids" not in details:
            raise ValueError("dependency details missing assignment_ids")

    def _reject_temporary_identity(value: Any) -> None:
        if isinstance(value, str):
            if value == _DOMAIN_RULES_CASE_NO or re.fullmatch(
                r"__(?:derived|substitute)__:\d+", value
            ):
                raise ValueError("dependency details leak temporary adapter identity")
            return
        if value is None or isinstance(value, (bool, int)):
            return
        if isinstance(value, float):
            if value != value or value in {float("inf"), float("-inf")}:
                raise ValueError("dependency details must be JSON-safe")
            return
        if isinstance(value, list):
            for item in value:
                _reject_temporary_identity(item)
            return
        if isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                if not isinstance(nested_key, str):
                    raise ValueError("dependency details must use string keys")
                _reject_temporary_identity(nested_value)
            return
        raise ValueError("dependency details must be JSON-safe")

    _reject_temporary_identity(facts)
    return facts


def validate_assignment_leave_resolution_domain_transition(
    *,
    case_ref: Any,
    database_current_date: Any,
    historical_fact_state: str,
    before_service_plan: Mapping[str, Any],
    canonical_leave_intent: Mapping[str, Any],
    candidate_after_service_plan: Mapping[str, Any],
) -> dict[str, Any]:
    _domain_rules_reference_identity(case_ref, "case_ref")
    db_current = _date(database_current_date, "database_current_date")
    if historical_fact_state not in {"bootstrap", "unlocked", "locked"}:
        raise ValueError("historical_fact_state must be bootstrap, unlocked, or locked")

    before = _domain_rules_parse_service_plan(
        plan=before_service_plan,
        plan_name="before_service_plan",
        require_current_refs=True,
    )
    candidate = _domain_rules_parse_service_plan(
        plan=candidate_after_service_plan,
        plan_name="candidate_after_service_plan",
        require_current_refs=False,
    )
    intent = _domain_rules_parse_intent(canonical_leave_intent, before["segments"])

    before_refs = {segment["caregiver_ref"] for segment in before["segments"].values()}
    candidate_refs = {segment["caregiver_ref"] for segment in candidate["segments"].values()}
    intent_refs = {
        item["substitute_caregiver_ref"]
        for item in intent["items"]
        if item["resolution"] == "substitute"
    }
    expected_caregiver_refs = set(before_refs) | set(intent_refs)
    if candidate_refs != expected_caregiver_refs:
        raise ValueError(
            "candidate_after_service_plan caregiver refs must match before refs and substitute intent refs"
        )

    identity_values: set[tuple[int, int | str | bytes, Any]] = set()
    for row in before["segments"].values():
        identity_values.add(row["caregiver_ref"])
    for row in candidate["segments"].values():
        identity_values.add(row["caregiver_ref"])
    for item in intent["items"]:
        if item["resolution"] == "substitute" and item["substitute_caregiver_ref"] is not None:
            identity_values.add(item["substitute_caregiver_ref"])
    identity_order = _domain_rules_sorted_ids(identity_values)
    staff_by_identity = {identity: index + 1 for index, identity in enumerate(identity_order)}
    staff_by_id = {staff_id: identity for identity, staff_id in staff_by_identity.items()}

    before_current = [
        ref for ref in _domain_rules_segment_ref_order(before["segments"])
        if ref.startswith("current:")
    ]
    candidate_current = [
        ref for ref in _domain_rules_segment_ref_order(candidate["segments"])
        if ref.startswith("current:")
    ]
    if before_current != candidate_current:
        raise ValueError("candidate_after_service_plan current segment refs must match before")
    derived_refs = [
        ref
        for ref in _domain_rules_segment_ref_order(candidate["segments"])
        if ref.startswith("derived:")
    ]
    derived_numbers = [
        _domain_rules_ref_to_key(ref, "candidate_after_service_plan segment_ref")[1]
        for ref in derived_refs
    ]
    if derived_numbers != list(range(len(derived_numbers))):
        raise ValueError(
            "candidate_after_service_plan derived segment refs must be consecutive from 0"
        )
    if derived_refs != sorted(
        derived_refs,
        key=lambda ref: (
            candidate["segments"][ref]["service_start"],
            candidate["segments"][ref]["service_end"],
            _domain_rules_segment_ref_sort_key(ref),
        ),
    ):
        raise ValueError(
            "candidate_after_service_plan derived refs must follow canonical service-period order"
        )

    substitute_refs = {
        _domain_rules_ref_to_key(ref, "candidate_after_service_plan segment_ref")[1]
        for ref in candidate["segments"]
        if ref.startswith("substitute:")
    }
    if substitute_refs != set(intent["substitute_item_refs"]):
        raise ValueError(
            "candidate_after_service_plan substitute refs must match substitute intent ordinals"
        )

    for ref, segment in candidate["segments"].items():
        if ref.startswith("derived:"):
            original = before["segments"][
                intent["original_segment_ref"]
            ]
            if (
                segment["segment_kind"] != "formal"
                or segment["caregiver_ref"] != original["caregiver_ref"]
            ):
                raise ValueError(
                    "derived segments must be formal fragments of the original caregiver"
                )
        if segment["segment_kind"] == "formal":
            if segment["lineage_original_segment_ref"] is not None:
                raise ValueError(f"{ref} formal segment must not contain lineage")
        else:
            if segment["segment_kind"] in {"substitute", "single_day_substitute"}:
                if segment["lineage_original_segment_ref"][0] != "current":
                    raise ValueError(f"{ref} substitute lineage must target current segment")
                if ref.startswith("substitute:"):
                    item_index = int(ref.split(":")[1])
                    if item_index not in intent["substitute_item_refs"]:
                        raise ValueError(
                            f"{ref} substitution index must match intent item_ref"
                        )
                    item = intent["items"][item_index]
                    if item["service_day"] != segment["service_start"]:
                        raise ValueError(
                            "substitute intent service day must match substitute segment service day"
                    )
                    if (
                        item["substitute_caregiver_ref"]
                        != segment["caregiver_ref"]
                    ):
                        raise ValueError(
                            "substitute intent caregiver must match substitute segment caregiver"
                        )
                    if segment["service_start"] != segment["service_end"]:
                        raise ValueError(
                            "substitute segment must be single-day"
                        )
                    if segment["lineage_substitution_service_day"] != item["service_day"]:
                        raise ValueError(
                            "substitute segment lineage substitution day must match intent day"
                        )
                    if segment["lineage_original_segment_ref"][1] != _domain_rules_ref_to_key(intent["original_segment_ref"], "original_segment_ref")[1]:
                        raise ValueError(
                            "substitute segment must target canonical original segment"
                        )

    original_ref_key = _domain_rules_ref_to_key(intent["original_segment_ref"], "original_segment_ref")
    for ref in candidate["segments"].values():
        if ref["segment_kind"] in {"substitute", "single_day_substitute"}:
            original_target = ref["lineage_original_segment_ref"]
            if original_target is None or original_target[0] != "current" or original_target[1] != original_ref_key[1]:
                raise ValueError("substitute lineage must reference canonical original segment")

    for row in before["ownership"]:
        if row["segment_ref"] not in before["segments"]:
            raise ValueError("before_service_plan ownership must reference before segment")
    for row in candidate["ownership"]:
        if row["segment_ref"] not in candidate["segments"]:
            raise ValueError("candidate_after_service_plan ownership must reference candidate segment")

    before_rows, before_segment_to_rule, before_rule_to_segment = _domain_rules_build_rule_rows(
        plan_segments=before["segments"],
        staff_by_identity=staff_by_identity,
    )
    candidate_rows, candidate_segment_to_rule, candidate_rule_to_segment = _domain_rules_build_rule_rows(
        plan_segments=candidate["segments"],
        staff_by_identity=staff_by_identity,
    )

    transition_input = {
        "case_no": _DOMAIN_RULES_CASE_NO,
        "database_current_date": db_current,
        "historical_fact_state": historical_fact_state,
        "effective_date": min(item["service_day"] for item in intent["items"]),
        "current_case_start_date": before["service_period"]["start"],
        "current_case_end_date": before["service_period"]["end"],
        "proposed_case_start_date": candidate["service_period"]["start"],
        "proposed_case_end_date": candidate["service_period"]["end"],
        "operation_kind": "batch_leave_resolution",
        "current_assignments": before_rows,
        "proposed_assignments": candidate_rows,
    }

    def _domain_rules_validate_assignment_row(
        *,
        row: Any,
        field_name: str,
    ) -> dict[str, Any]:
        if not isinstance(row, Mapping):
            raise ValueError(f"{field_name} must be a mapping")
        if set(row) != _ASSIGNMENT_TRANSITION_ASSIGNMENT_KEYS:
            raise ValueError(f"{field_name} has unexpected assignment keys")
        case_no = row["case_no"]
        if not isinstance(case_no, str) or not case_no.strip():
            raise ValueError(f"{field_name}.case_no must be a non-empty string")
        status = row["status"]
        if status not in _ALLOWED_SEGMENT_STATUSES:
            raise ValueError(f"{field_name}.status must be a valid assignment status")
        kind = row["kind"]
        if kind not in _ALLOWED_SEGMENT_KINDS:
            raise ValueError(f"{field_name}.kind must be a valid assignment kind")
        staff_id = _positive_int(row["staff_id"], f"{field_name}.staff_id")
        assignment_id = row["id"]
        if isinstance(assignment_id, bool) or not isinstance(
            assignment_id, (int, str)
        ):
            raise ValueError(f"{field_name}.id must be a positive integer or string")
        if isinstance(assignment_id, int) and assignment_id <= 0:
            raise ValueError(f"{field_name}.id must be a positive integer when numeric")
        if isinstance(assignment_id, str) and not assignment_id:
            raise ValueError(f"{field_name}.id must be a non-empty string when string")
        original_assignment_id = row["original_assignment_id"]
        if original_assignment_id is not None:
            original_assignment_id = _positive_int(
                original_assignment_id,
                f"{field_name}.original_assignment_id",
            )
        substitution_work_date = None if row["substitution_work_date"] is None else (
            _date(
                row["substitution_work_date"],
                f"{field_name}.substitution_work_date",
            )
            if row["substitution_work_date"] is not None
            else None
        )
        if kind == "formal":
            if original_assignment_id is not None or substitution_work_date is not None:
                raise ValueError(f"{field_name} formal assignment must not define substitute lineage")
        elif original_assignment_id is None or substitution_work_date is None:
                raise ValueError(
                f"{field_name} substitute assignment must define original_assignment_id and substitution_work_date"
            )
        if status == "cancelled":
            start = row["assigned_start_date"]
            end = row["assigned_end_date"]
            if start is not None:
                start = _date(start, f"{field_name}.assigned_start_date")
            if end is not None:
                end = _date(end, f"{field_name}.assigned_end_date")
        else:
            start = _date(row["assigned_start_date"], f"{field_name}.assigned_start_date")
            end = _date(row["assigned_end_date"], f"{field_name}.assigned_end_date")
            if start > end:
                raise ValueError(f"{field_name}.assigned period must be in range")
        return {
            "id": assignment_id,
            "case_no": case_no.strip(),
            "staff_id": staff_id,
            "status": status,
            "assigned_start_date": start,
            "assigned_end_date": end,
            "kind": kind,
            "original_assignment_id": original_assignment_id,
            "substitution_work_date": substitution_work_date,
        }

    def _iter_transition_days(*, start: date, end: date) -> list[date]:
        current = start
        days: list[date] = []
        while current <= end:
            days.append(current)
            current += timedelta(days=1)
        return days

    def _validate_transition_facts(value: Any, *, field_name: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{field_name} must be a mapping")
        if set(value) != _ASSIGNMENT_TRANSITION_FACT_KEYS:
            raise ValueError(f"{field_name} has unexpected keys")
        for name, rows in value.items():
            if not isinstance(rows, list):
                raise ValueError(f"{field_name}[{name}] must be a list")
            if name in {"truncated", "cancelled"}:
                for index, item in enumerate(rows):
                    if not isinstance(item, Mapping):
                        raise ValueError(
                            f"{field_name}[{name}][{index}] must be a mapping"
                        )
                    if set(item) != {"before", "after"}:
                        raise ValueError(
                            f"{field_name}[{name}][{index}] must have before/after"
                        )
                    _domain_rules_validate_assignment_row(
                        row=item["before"],
                        field_name=f"{field_name}[{name}][{index}].before",
                    )
                    _domain_rules_validate_assignment_row(
                        row=item["after"],
                        field_name=f"{field_name}[{name}][{index}].after",
                    )
            else:
                for index, row in enumerate(rows):
                    _domain_rules_validate_assignment_row(
                        row=row,
                        field_name=f"{field_name}[{name}][{index}]",
                    )
        return value

    def _validate_exact_assignment_rows(
        value: Any,
        expected: list[dict[str, Any]],
        *,
        field_name: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} must be a list")
        validated = [
            _domain_rules_validate_assignment_row(
                row=row,
                field_name=f"{field_name}[{index}]",
            )
            for index, row in enumerate(value)
        ]
        if validated != expected:
            raise ValueError(f"dependency {field_name} must equal the projected plan rows")
        return validated

    def _expected_transition_facts() -> dict[str, list[dict[str, Any]]]:
        before_by_id = {row["id"]: row for row in before_rows}
        expected = {name: [] for name in _ASSIGNMENT_TRANSITION_FACT_KEYS}
        for after in candidate_rows:
            before_row = before_by_id.get(after["id"])
            if before_row is None:
                expected["created"].append(after)
            elif after["status"] == "cancelled":
                expected["cancelled"].append({"before": before_row, "after": after})
            elif after == before_row:
                expected["retained"].append(after)
            else:
                expected["truncated"].append({"before": before_row, "after": after})
        expected["created"].sort(
            key=lambda row: (
                str(row["id"]),
                row["assigned_start_date"] or date.min,
            )
        )
        expected["retained"].sort(
            key=lambda row: (
                str(row["id"]),
                row["assigned_start_date"] or date.min,
            )
        )
        expected["truncated"].sort(
            key=lambda item: (
                str(item["before"]["id"]),
                item["before"]["assigned_start_date"] or date.min,
            )
        )
        expected["cancelled"].sort(
            key=lambda item: (
                str(item["before"]["id"]),
                item["before"]["assigned_start_date"] or date.min,
            )
        )
        return expected

    transition: dict[str, Any] | None = None
    try:
        transition = validate_assignment_plan_transition(**transition_input)
    except AssignmentPlanTransitionConflict as exc:
        facts = _domain_rules_reverse_diagnostics(
            code=exc.code,
            details=exc.details,
            transition_code_facts=candidate_rule_to_segment,
            transition_staff_identities=staff_by_id,
        )
        scope_ref = "transition"
        if "segment_ref" in facts and isinstance(facts["segment_ref"], str):
            scope_ref = facts["segment_ref"]
        elif "original_segment_ref" in facts and isinstance(facts["original_segment_ref"], str):
            scope_ref = facts["original_segment_ref"]
        elif "segment_refs" in facts and isinstance(facts["segment_refs"], list) and len(facts["segment_refs"]) == 1:
            scope_ref = facts["segment_refs"][0]
        elif "service_day" in facts and isinstance(facts["service_day"], str):
            scope_ref = f"service-day:{facts['service_day']}"
        return {
            "valid": False,
            "after_service_plan": None,
            "transition_diagnostics": [
                {
                    "code": exc.code,
                    "scope_ref": scope_ref,
                    "facts": facts,
                }
            ],
        }
    except ValueError:
        raise

    if transition is None or not isinstance(transition, Mapping):
        raise ValueError("dependency return shape is missing expected fields")

    transition_top_level_keys = set(transition)
    if transition_top_level_keys != _ASSIGNMENT_TRANSITION_TOP_LEVEL_KEYS:
        missing_keys = sorted(_ASSIGNMENT_TRANSITION_TOP_LEVEL_KEYS - transition_top_level_keys)
        extra_keys = sorted(transition_top_level_keys - _ASSIGNMENT_TRANSITION_TOP_LEVEL_KEYS)
        message_parts = ["dependency return shape is not exact"]
        if missing_keys:
            message_parts.append(f"missing={missing_keys}")
        if extra_keys:
            message_parts.append(f"extra={extra_keys}")
        raise ValueError("; ".join(message_parts))

    if transition["case_no"] != _DOMAIN_RULES_CASE_NO:
        raise ValueError("dependency case_no changed")
    if transition["operation_kind"] != "batch_leave_resolution":
        raise ValueError("dependency operation_kind changed")
    if transition["historical_fact_state"] != historical_fact_state:
        raise ValueError("dependency historical_fact_state changed")
    transition_requires_audit = transition["requires_audit"]
    if not isinstance(transition_requires_audit, bool):
        raise ValueError("dependency requires_audit must be bool")
    if transition_requires_audit != (historical_fact_state == "unlocked"):
        raise ValueError("dependency requires_audit mismatch")

    transition_effective_date = _date(transition["effective_date"], "dependency.effective_date")
    expected_effective_date = min(item["service_day"] for item in intent["items"])
    if transition_effective_date != expected_effective_date:
        raise ValueError("dependency effective_date changed")
    current_case_start = _date(transition["current_case_start_date"], "dependency.current_case_start_date")
    current_case_end = _date(transition["current_case_end_date"], "dependency.current_case_end_date")
    proposed_case_start = _date(transition["proposed_case_start_date"], "dependency.proposed_case_start_date")
    proposed_case_end = _date(transition["proposed_case_end_date"], "dependency.proposed_case_end_date")
    if current_case_start != before["service_period"]["start"]:
        raise ValueError("dependency current_case_start_date changed")
    if current_case_end != before["service_period"]["end"]:
        raise ValueError("dependency current_case_end_date changed")
    if proposed_case_start != candidate["service_period"]["start"]:
        raise ValueError("dependency proposed_case_start_date changed")
    if proposed_case_end != candidate["service_period"]["end"]:
        raise ValueError("dependency proposed_case_end_date changed")

    transition_removed = transition["removed_future_dates"]
    if not isinstance(transition_removed, list):
        raise ValueError("dependency.removed_future_dates must be a list")
    transition_removed_dates = sorted(
        (
            _date(item, "dependency.removed_future_dates item").isoformat()
            for item in transition_removed
        )
    )
    expected_removed = sorted(
        (
            day.isoformat()
            for day in _iter_transition_days(start=current_case_start, end=current_case_end)
            if day >= db_current and not (proposed_case_start <= day <= proposed_case_end)
        )
    )
    if transition_removed_dates != expected_removed:
        raise ValueError("dependency.removed_future_dates changed")

    before_assignments = transition["before_assignments"]
    after_assignments_rows = transition["after_assignments"]
    created = transition["created"]
    retained = transition["retained"]
    truncated = transition["truncated"]
    cancelled = transition["cancelled"]
    before_facts = _validate_transition_facts(transition["facts"], field_name="facts")

    _validate_exact_assignment_rows(
        before_assignments,
        before_rows,
        field_name="before_assignments",
    )
    _validate_exact_assignment_rows(
        after_assignments_rows,
        candidate_rows,
        field_name="after_assignments",
    )
    expected_transition_facts = _expected_transition_facts()
    for name, value in (
        ("created", created),
        ("retained", retained),
        ("truncated", truncated),
        ("cancelled", cancelled),
    ):
        if value != expected_transition_facts[name]:
            raise ValueError(
                f"dependency {name} must equal the projected transition facts"
            )
    if before_facts != expected_transition_facts:
        raise ValueError("dependency facts must equal the projected transition facts")

    before_assignment_ids: set[str | int] = set()
    for index, row in enumerate(before_assignments):
        validated_row = _domain_rules_validate_assignment_row(
            row=row,
            field_name=f"before_assignments[{index}]",
        )
        if validated_row["case_no"] != _DOMAIN_RULES_CASE_NO:
            raise ValueError("before_assignments case_no changed")
        assignment_id = validated_row["id"]
        if assignment_id not in before_rule_to_segment:
            raise ValueError("dependency before_assignments contains unknown assignment id")
        if assignment_id in before_assignment_ids:
            raise ValueError("dependency before_assignments contains duplicate assignment id")
        before_assignment_ids.add(assignment_id)
    if before_assignment_ids != set(before_rule_to_segment):
        raise ValueError("dependency before_assignments changed plan assignments")

    for index, row in enumerate(created):
        validated_row = _domain_rules_validate_assignment_row(
            row=row,
            field_name=f"created[{index}]",
        )
        if validated_row["id"] not in candidate_rule_to_segment:
            raise ValueError("dependency created contains unknown assignment id")

    for index, row in enumerate(retained):
        validated_row = _domain_rules_validate_assignment_row(
            row=row,
            field_name=f"retained[{index}]",
        )
        if validated_row["id"] not in candidate_rule_to_segment:
            raise ValueError("dependency retained contains unknown assignment id")
    for index, row in enumerate(truncated):
        if "before" not in row or "after" not in row:
            raise ValueError(f"truncated[{index}] must have before and after")
        before = _domain_rules_validate_assignment_row(
            row=row["before"],
            field_name=f"truncated[{index}].before",
        )
        if before["id"] not in before_assignment_ids:
            raise ValueError("dependency truncated fact contains unknown before assignment id")
        after = _domain_rules_validate_assignment_row(
            row=row["after"],
            field_name=f"truncated[{index}].after",
        )
        if after["id"] != before["id"]:
            raise ValueError("dependency truncated fact must preserve assignment id")
        if after["id"] not in candidate_rule_to_segment and after["id"] not in before_rule_to_segment:
            raise ValueError("dependency truncated fact contains unknown after assignment id")
    for index, row in enumerate(cancelled):
        if "before" not in row or "after" not in row:
            raise ValueError(f"cancelled[{index}] must have before and after")
        before = _domain_rules_validate_assignment_row(
            row=row["before"],
            field_name=f"cancelled[{index}].before",
        )
        if before["id"] not in before_assignment_ids:
            raise ValueError("dependency cancelled fact contains unknown before assignment id")
        after = _domain_rules_validate_assignment_row(
            row=row["after"],
            field_name=f"cancelled[{index}].after",
        )
        if after["id"] not in candidate_rule_to_segment and after["id"] not in before_rule_to_segment:
            raise ValueError("dependency cancelled fact contains unknown after assignment id")

    if not isinstance(before_facts, Mapping):
        raise ValueError("facts must be a mapping")

    after_segments: dict[str, dict[str, Any]] = {}
    after_ownership_rows: list[dict[str, Any]] = []

    seen_segment_refs: set[str] = set()
    for index, row in enumerate(after_assignments_rows):
        assignment = _domain_rules_validate_assignment_row(
            row=row,
            field_name=f"after_assignments[{index}]",
        )
        assignment_id = assignment["id"]
        segment_ref = candidate_rule_to_segment.get(assignment["id"])
        if segment_ref is None:
            if assignment_id in before_rule_to_segment and assignment["status"] == "cancelled":
                continue
            raise ValueError("dependency output identity could not be reversed")
        if segment_ref in seen_segment_refs:
            raise ValueError("dependency output contains duplicate assignment identity")
        seen_segment_refs.add(segment_ref)
        _domain_rules_ref_to_key(segment_ref, "segment_ref")
        segment = candidate["segments"][segment_ref]
        if assignment["status"] != segment["status"] and segment["status"] != "cancelled":
            raise ValueError("dependency output status invalid")
        if assignment["case_no"] != _DOMAIN_RULES_CASE_NO:
            raise ValueError("dependency output case_no changed")
        if assignment["staff_id"] <= 0:
            raise ValueError("dependency staff_id must be positive")
        segment_kind = segment["segment_kind"]
        if assignment["kind"] not in {"formal", "single_day_substitute", "substitute"}:
            raise ValueError("dependency output kind invalid")
        if assignment["status"] != segment["status"]:
            raise ValueError("dependency output status mismatch")
        if assignment["kind"] != segment_kind:
            raise ValueError("dependency output segment mapping changed")
        if assignment["assigned_start_date"] != segment["service_start"]:
            raise ValueError("dependency output period start changed")
        if assignment["assigned_end_date"] != segment["service_end"]:
            raise ValueError("dependency output period end changed")
        staff_identity = staff_by_id.get(assignment["staff_id"])
        if staff_identity is None:
            raise ValueError("dependency output staff identity unknown")
        if staff_identity != segment["caregiver_ref"]:
            raise ValueError("dependency output segment mapping changed")
        if assignment["kind"] in {"substitute", "single_day_substitute"}:
            if (
                assignment["original_assignment_id"] is None
                or assignment["substitution_work_date"] is None
            ):
                raise ValueError(
                    "dependency output substitute assignment requires substitute lineage"
                )
        elif assignment["original_assignment_id"] is not None:
            raise ValueError(
                "dependency output formal assignment must not define substitute metadata"
            )
        after_segments[segment_ref] = {
            "segment_ref": segment_ref,
            "caregiver_ref": staff_identity[2],
            "status": assignment["status"],
            "service_period": {
                "start": assignment["assigned_start_date"],
                "end": assignment["assigned_end_date"],
            },
            "segment_kind": segment_kind,
            "lineage": {
                "original_segment_ref": None
                if segment["lineage_original_segment_ref"] is None
                else (
                    f"{segment['lineage_original_segment_ref'][0]}:{segment['lineage_original_segment_ref'][1]}"
                    if isinstance(segment["lineage_original_segment_ref"], tuple)
                    else segment["lineage_original_segment_ref"]
                ),
                "substitution_service_day": segment["lineage_substitution_service_day"],
            },
        }

    if seen_segment_refs != set(candidate["segments"]):
        raise ValueError("dependency output must preserve all candidate segments")
    if not isinstance(transition["ownership_by_date"], Mapping):
        raise ValueError("dependency ownership_by_date must be a mapping")

    expected_ownership_by_date = {
        row["service_day"].isoformat(): candidate_segment_to_rule[row["segment_ref"]]
        for row in candidate["ownership"]
    }
    normalized_ownership_by_date = {}
    for day, ownership_ref in transition["ownership_by_date"].items():
        normalized_ref = ownership_ref
        if isinstance(ownership_ref, str) and ownership_ref.isdigit():
            numeric_ref = int(ownership_ref)
            if numeric_ref in candidate_rule_to_segment:
                normalized_ref = numeric_ref
        normalized_ownership_by_date[day] = normalized_ref
    if normalized_ownership_by_date != expected_ownership_by_date:
        raise ValueError(
            "dependency ownership_by_date must equal candidate daily ownership"
        )

    for day, ownership_ref in normalized_ownership_by_date.items():
        try:
            service_day = _date(day, "dependency.ownership_by_date key")
        except ValueError as exc:
            raise ValueError("dependency ownership_by_date contains invalid date") from exc
        if not (proposed_case_start <= service_day <= proposed_case_end):
            raise ValueError("dependency ownership_by_date contains date outside proposed case period")
        ownership_segment_ref = candidate_rule_to_segment.get(ownership_ref)
        if ownership_segment_ref is None:
            raise ValueError("dependency ownership_by_date changed unknown assignment")
        if ownership_segment_ref not in after_segments:
            raise ValueError("dependency ownership_by_date changed plan segment ownership")
        after_ownership_rows.append(
            {
                "service_day": service_day,
                "segment_ref": ownership_segment_ref,
                "caregiver_ref": after_segments[ownership_segment_ref]["caregiver_ref"],
            }
        )

    after_service_plan = {
        "segments": [after_segments[ref] for ref in _domain_rules_segment_ref_order(after_segments)],
        "daily_ownership": sorted(after_ownership_rows, key=lambda row: row["service_day"].isoformat()),
        "service_period": {
            "start": candidate["service_period"]["start"],
            "end": candidate["service_period"]["end"],
        },
    }

    expected_after_plan = _domain_rules_canonical_plan(
        plan_segments=candidate["segments"] | {"_ownership_for_canonical": candidate["ownership"], "_ownership_by_ref": candidate["ownership"]},
        service_period=candidate["service_period"],
    )
    if after_service_plan != expected_after_plan:
        raise ValueError("dependency output must align exactly with candidate service plan")

    diagnostics = []
    return {
        "valid": True,
        "after_service_plan": after_service_plan,
        "transition_diagnostics": diagnostics,
    }


def compute_assignment_leave_resolution_preview_from_snapshot(
    request: Mapping[str, Any],
    original_assignment_schedule: Mapping[str, Any],
    conflict_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Calculate a deterministic preview without reading or mutating external state."""
    if not isinstance(request, Mapping):
        raise ValueError("request must be a mapping")
    if not isinstance(original_assignment_schedule, Mapping):
        raise ValueError("original_assignment_schedule must be a mapping")
    if not isinstance(conflict_snapshot, Mapping):
        raise ValueError("conflict_snapshot must be a mapping")

    case_no = request.get("case_no")
    if not isinstance(case_no, str) or not case_no.strip():
        raise ValueError("case_no must be a non-empty string")
    canonical_case_no = case_no.strip()
    assignment_id = _positive_int(
        request.get("original_assignment_id"), "original_assignment_id"
    )
    schedule_id = _positive_int(
        request.get("original_schedule_id"), "original_schedule_id"
    )
    if isinstance(request.get("work_date"), datetime):
        raise ValueError("work_date must be YYYY-MM-DD")
    leave_date = _date(request.get("work_date"), "work_date")
    resolution_type = request.get("resolution_type")
    if resolution_type not in {"defer_following_assignments", "substitute"}:
        raise ValueError(
            "resolution_type must be defer_following_assignments or substitute"
        )
    substitute_value = request.get("substitute_staff_id")
    if resolution_type == "substitute":
        substitute_id = _positive_int(substitute_value, "substitute_staff_id")
    elif substitute_value is not None:
        raise ValueError("substitute_staff_id must be null when deferring assignments")
    else:
        substitute_id = None

    snapshot_top_level = {
        "database_current_date",
        "assignments",
        "assignment_schedule_days",
        "active_lock_days",
        "historical_facts",
    }
    if set(conflict_snapshot) != snapshot_top_level:
        raise ValueError("conflict_snapshot keys are invalid")

    if not isinstance(conflict_snapshot["database_current_date"], (str, date)):
        raise ValueError("database_current_date must be YYYY-MM-DD")
    database_current_date = _date(conflict_snapshot["database_current_date"], "database_current_date")

    original = dict(original_assignment_schedule.get("assignment") or {})
    if original.get("id") != assignment_id or original.get("case_no") != canonical_case_no:
        raise ValueError("original assignment ownership mismatch")
    original_staff_id = _positive_int(original.get("staff_id"), "original staff_id")
    original_schedule_rows = original_assignment_schedule.get("schedule_days")
    if not isinstance(original_schedule_rows, list):
        raise ValueError("original_assignment_schedule.schedule_days must be a list")
    for row in original_schedule_rows:
        if not isinstance(row, Mapping):
            raise ValueError("original_assignment_schedule.schedule_days must contain mappings")
    matching_schedule_rows = [
        row
        for row in original_schedule_rows
        if _positive_int(row.get("id"), "assignment schedule id") == schedule_id
    ]
    if len(matching_schedule_rows) != 1:
        raise ValueError("original_schedule_id does not belong to original_assignment_id")
    original_day = dict(matching_schedule_rows[0])
    if (
        _positive_int(original_day.get("assignment_id"), "assignment id") != assignment_id
        or original_day.get("case_no") != canonical_case_no
        or _positive_int(original_day.get("staff_id"), "original staff_id")
        != original_staff_id
        or _date(original_day.get("work_date"), "schedule work_date") != leave_date
    ):
        raise ValueError("original schedule ownership mismatch")
    if original_day.get("is_work_day") is not True:
        raise ValueError("original schedule day is not a work day")

    def _strict_history_payload(
        value: Any,
        field_name: str,
        _seen: set[int] | None = None,
    ) -> Any:
        if _seen is None:
            _seen = set()

        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            if value != value or value in {float("inf"), -float("inf")}:
                raise ValueError("invalid JSON-safe value")
            return value
        if isinstance(value, Mapping):
            if id(value) in _seen:
                raise ValueError(f"{field_name} must be JSON-safe")
            _seen.add(id(value))
            try:
                copied: dict[str, Any] = {}
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
                    if not isinstance(key, str):
                        raise ValueError("history details must use string keys")
                    copied[key] = _strict_history_payload(item, field_name, _seen=_seen)
                return copied
            finally:
                _seen.remove(id(value))
        if isinstance(value, list):
            if id(value) in _seen:
                raise ValueError(f"{field_name} must be JSON-safe")
            _seen.add(id(value))
            try:
                return [
                    _strict_history_payload(item, field_name, _seen=_seen)
                    for item in value
                ]
            finally:
                _seen.remove(id(value))
        raise ValueError(f"{field_name} must be JSON-safe")

    def _required_snapshot_row_keys(row: Mapping[str, Any], expected: set[str], name: str) -> None:
        if set(row) != expected:
            raise ValueError(f"{name} must contain exact fields")

    assignment_keys = {
        "id",
        "case_no",
        "staff_id",
        "status",
        "assigned_start_date",
        "assigned_end_date",
        "planned_hours",
        "actual_hours",
    }
    schedule_keys = {
        "id",
        "case_no",
        "staff_id",
        "assignment_id",
        "work_date",
        "is_work_day",
        "is_double_pay",
        "notes",
        "requires_review",
    }
    lock_keys = {
        "id",
        "lock_id",
        "plan_id",
        "case_no",
        "segment_id",
        "staff_id",
        "lock_date",
    }
    historical_keys = {
        "id",
        "case_no",
        "original_assignment_id",
        "original_schedule_id",
        "work_date",
        "resolution_type",
        "substitute_assignment_id",
        "event_key",
        "occurred_at",
    }
    hours_keys = {
        "id",
        "case_no",
        "assignment_id",
        "original_hours",
        "adjusted_hours",
        "reason",
        "adjusted_at",
    }
    payment_keys = {
        "id",
        "case_no",
        "assignment_id",
        "payment_status",
    }
    settlement_keys = {
        "id",
        "case_no",
        "assignment_id",
        "settlement_id",
        "status",
    }
    historical_fact_keys = {
        "leave_substitution_events",
        "actual_hours_adjustments",
        "non_cancelled_payments",
        "active_settlements",
    }

    raw_current = conflict_snapshot["assignments"]
    if not isinstance(raw_current, list):
        raise ValueError("conflict_snapshot.assignments must be a list")
    current_assignments: list[dict[str, Any]] = []
    current_by_id: dict[int, dict[str, Any]] = {}
    for row_index, raw_row in enumerate(raw_current):
        if not isinstance(raw_row, Mapping):
            raise ValueError("conflict_snapshot.assignments must contain mappings")
        _required_snapshot_row_keys(
            raw_row, assignment_keys, "conflict_snapshot.assignments row"
        )
        row = dict(raw_row)
        row_id = _positive_int(row["id"], f"conflict_snapshot.assignments[{row_index}].id")
        if row_id in current_by_id:
            raise ValueError("conflict_snapshot.assignments contains duplicate id")
        row["id"] = row_id
        if row["case_no"] != canonical_case_no:
            raise ValueError("conflict_snapshot.assignments case_no mismatch")
        row["staff_id"] = _positive_int(row["staff_id"], f"conflict_snapshot.assignments[{row_index}].staff_id")
        if not isinstance(row["status"], str):
            raise ValueError("conflict_snapshot.assignments.status must be a string")
        row["assigned_start_date"] = _date(
            row["assigned_start_date"], f"conflict_snapshot.assignments[{row_index}].assigned_start_date"
        )
        row["assigned_end_date"] = _date(
            row["assigned_end_date"], f"conflict_snapshot.assignments[{row_index}].assigned_end_date"
        )
        row["planned_hours"] = _positive_decimal(
            row["planned_hours"], f"conflict_snapshot.assignments[{row_index}].planned_hours"
        )
        row["actual_hours"] = _positive_decimal(
            row["actual_hours"], f"conflict_snapshot.assignments[{row_index}].actual_hours"
        )
        current_assignments.append(row)
        current_by_id[row_id] = row

    raw_schedule_rows = conflict_snapshot["assignment_schedule_days"]
    if not isinstance(raw_schedule_rows, list):
        raise ValueError("conflict_snapshot.assignment_schedule_days must be a list")
    schedule_by_id: dict[int, dict[str, Any]] = {}
    schedule_rows: list[dict[str, Any]] = []
    for row_index, raw_row in enumerate(raw_schedule_rows):
        if not isinstance(raw_row, Mapping):
            raise ValueError(
                "conflict_snapshot.assignment_schedule_days must contain mappings"
            )
        _required_snapshot_row_keys(
            raw_row,
            schedule_keys,
            "conflict_snapshot.assignment_schedule_days row",
        )
        row = dict(raw_row)
        row["id"] = _positive_int(row["id"], f"conflict_snapshot.assignment_schedule_days[{row_index}].id")
        if row["id"] in schedule_by_id:
            raise ValueError("conflict_snapshot.assignment_schedule_days contains duplicate id")
        if row["case_no"] != canonical_case_no:
            raise ValueError("conflict_snapshot.assignment_schedule_days case_no mismatch")
        row["staff_id"] = _positive_int(
            row["staff_id"],
            f"conflict_snapshot.assignment_schedule_days[{row_index}].staff_id",
        )
        if row["is_work_day"] is not True and row["is_work_day"] is not False:
            raise ValueError("conflict_snapshot.assignment_schedule_days row is_work_day must be bool")
        if row["is_double_pay"] is not True and row["is_double_pay"] is not False:
            raise ValueError("conflict_snapshot.assignment_schedule_days row is_double_pay must be bool")
        if row["requires_review"] is not True and row["requires_review"] is not False:
            raise ValueError("conflict_snapshot.assignment_schedule_days row requires_review must be bool")
        if isinstance(row["notes"], (dict, list)):
            raise ValueError("conflict_snapshot.assignment_schedule_days row notes must be a string")
        if row["notes"] is not None and not isinstance(row["notes"], str):
            raise ValueError("conflict_snapshot.assignment_schedule_days row notes must be a string")
        row["work_date"] = _date(
            row["work_date"], f"conflict_snapshot.assignment_schedule_days[{row_index}].work_date"
        )
        if row["assignment_id"] is None:
            if row["requires_review"] is not True:
                raise ValueError(
                    "conflict_snapshot.assignment_schedule_days legacy row requires_review must be true"
                )
        else:
            if row["requires_review"] is not False:
                raise ValueError(
                    "conflict_snapshot.assignment_schedule_days owned row requires_review must be false"
                )
            row["assignment_id"] = _positive_int(
                row["assignment_id"],
                f"conflict_snapshot.assignment_schedule_days[{row_index}].assignment_id",
            )
            if row["assignment_id"] not in current_by_id:
                raise ValueError(
                    "conflict_snapshot.assignment_schedule_days references unknown assignment_id"
                )
            if (
                row["staff_id"]
                != current_by_id[row["assignment_id"]]["staff_id"]
            ):
                raise ValueError(
                    "conflict_snapshot.assignment_schedule_days staff ownership mismatch"
                )
        schedule_by_id[row["id"]] = row
        schedule_rows.append(row)

    if schedule_id not in schedule_by_id:
        raise ValueError("original_schedule_id does not belong to original_assignment_id")
    if schedule_by_id[schedule_id]["assignment_id"] != assignment_id:
        raise ValueError("original schedule ownership mismatch")

    if not isinstance(conflict_snapshot["historical_facts"], Mapping):
        raise ValueError("conflict_snapshot.historical_facts must be a mapping")
    if set(conflict_snapshot["historical_facts"]) != historical_fact_keys:
        raise ValueError("conflict_snapshot.historical_facts keys are invalid")
    raw_facts = conflict_snapshot["historical_facts"]

    leave_events = raw_facts["leave_substitution_events"]
    if not isinstance(leave_events, list):
        raise ValueError("historical_facts.leave_substitution_events must be a list")
    if any(not isinstance(row, Mapping) for row in leave_events):
        raise ValueError("historical_facts.leave_substitution_events must contain mappings")

    validated_leave_events: list[dict[str, Any]] = []
    leave_event_ids: set[int] = set()
    for row_index, raw_row in enumerate(leave_events):
        _required_snapshot_row_keys(
            raw_row,
            historical_keys,
            f"historical_facts.leave_substitution_events[{row_index}]",
        )
        row = dict(raw_row)
        row_id = _positive_int(
            row["id"],
            f"historical_facts.leave_substitution_events[{row_index}].id",
        )
        if row_id in leave_event_ids:
            raise ValueError(
                "historical_facts.leave_substitution_events contains duplicate id"
            )
        leave_event_ids.add(row_id)
        row["id"] = row_id
        if row["case_no"] != canonical_case_no:
            raise ValueError("historical_facts.leave_substitution_events case_no mismatch")
        row["original_assignment_id"] = _positive_int(
            row["original_assignment_id"],
            f"historical_facts.leave_substitution_events[{row_index}].original_assignment_id",
        )
        row["original_schedule_id"] = _positive_int(
            row["original_schedule_id"],
            f"historical_facts.leave_substitution_events[{row_index}].original_schedule_id",
        )
        row["work_date"] = _date(
            row["work_date"], f"historical_facts.leave_substitution_events[{row_index}].work_date"
        )
        if row["original_assignment_id"] != assignment_id:
            raise ValueError("historical_facts.leave_substitution_events assignment mismatch")
        if row["original_schedule_id"] not in schedule_by_id:
            raise ValueError(
                "historical_facts.leave_substitution_events schedule lineage mismatch"
            )
        if row["work_date"] != schedule_by_id[row["original_schedule_id"]]["work_date"]:
            raise ValueError(
                "historical_facts.leave_substitution_events schedule lineage mismatch"
            )
        if row["substitute_assignment_id"] is not None:
            row["substitute_assignment_id"] = _positive_int(
                row["substitute_assignment_id"],
                f"historical_facts.leave_substitution_events[{row_index}].substitute_assignment_id",
            )
        if not isinstance(row["resolution_type"], str):
            raise ValueError("historical_facts.leave_substitution_events.resolution_type must be a string")
        if not isinstance(row["event_key"], str):
            raise ValueError("historical_facts.leave_substitution_events.event_key must be a string")
        if not isinstance(row["occurred_at"], (str, date)):
            raise ValueError("historical_facts.leave_substitution_events.occurred_at must be a string")
        validated_leave_events.append(row)
    leave_events = validated_leave_events

    actual_adjustments = raw_facts["actual_hours_adjustments"]
    if not isinstance(actual_adjustments, list):
        raise ValueError("historical_facts.actual_hours_adjustments must be a list")
    if any(not isinstance(row, Mapping) for row in actual_adjustments):
        raise ValueError("historical_facts.actual_hours_adjustments must contain mappings")

    validated_adjustments: list[dict[str, Any]] = []
    adjustment_ids: set[int] = set()
    for row_index, raw_row in enumerate(actual_adjustments):
        _required_snapshot_row_keys(
            raw_row,
            hours_keys,
            f"historical_facts.actual_hours_adjustments[{row_index}]",
        )
        row = dict(raw_row)
        row_id = _positive_int(
            row["id"], f"historical_facts.actual_hours_adjustments[{row_index}].id"
        )
        if row_id in adjustment_ids:
            raise ValueError(
                "historical_facts.actual_hours_adjustments contains duplicate id"
            )
        adjustment_ids.add(row_id)
        row["id"] = row_id
        if row["case_no"] != canonical_case_no:
            raise ValueError("historical_facts.actual_hours_adjustments case_no mismatch")
        row["assignment_id"] = _positive_int(
            row["assignment_id"],
            f"historical_facts.actual_hours_adjustments[{row_index}].assignment_id",
        )
        if row["assignment_id"] not in current_by_id:
            raise ValueError("historical_facts.actual_hours_adjustments references unknown assignment_id")
        row["original_hours"] = _positive_decimal(
            row["original_hours"],
            f"historical_facts.actual_hours_adjustments[{row_index}].original_hours",
        )
        row["adjusted_hours"] = _positive_decimal(
            row["adjusted_hours"],
            f"historical_facts.actual_hours_adjustments[{row_index}].adjusted_hours",
        )
        if not isinstance(row["reason"], str):
            raise ValueError("historical_facts.actual_hours_adjustments.reason must be a string")
        if not isinstance(row["adjusted_at"], str):
            raise ValueError("historical_facts.actual_hours_adjustments.adjusted_at must be a string")
        validated_adjustments.append(row)
    actual_adjustments = validated_adjustments

    payments = raw_facts["non_cancelled_payments"]
    if not isinstance(payments, list):
        raise ValueError("historical_facts.non_cancelled_payments must be a list")
    if any(not isinstance(row, Mapping) for row in payments):
        raise ValueError("historical_facts.non_cancelled_payments must contain mappings")

    validated_payments: list[dict[str, Any]] = []
    payment_ids: set[int] = set()
    for row_index, raw_row in enumerate(payments):
        _required_snapshot_row_keys(
            raw_row,
            payment_keys,
            f"historical_facts.non_cancelled_payments[{row_index}]",
        )
        row = dict(raw_row)
        row_id = _positive_int(
            row["id"], f"historical_facts.non_cancelled_payments[{row_index}].id"
        )
        if row_id in payment_ids:
            raise ValueError(
                "historical_facts.non_cancelled_payments contains duplicate id"
            )
        payment_ids.add(row_id)
        row["id"] = row_id
        if row["case_no"] != canonical_case_no:
            raise ValueError("historical_facts.non_cancelled_payments case_no mismatch")
        row["assignment_id"] = _positive_int(
            row["assignment_id"],
            f"historical_facts.non_cancelled_payments[{row_index}].assignment_id",
        )
        if row["assignment_id"] not in current_by_id:
            raise ValueError("historical_facts.non_cancelled_payments references unknown assignment_id")
        if not isinstance(row["payment_status"], str):
            raise ValueError("historical_facts.non_cancelled_payments.payment_status must be a string")
        validated_payments.append(row)
    payments = validated_payments

    settlements = raw_facts["active_settlements"]
    if not isinstance(settlements, list):
        raise ValueError("historical_facts.active_settlements must be a list")
    if any(not isinstance(row, Mapping) for row in settlements):
        raise ValueError("historical_facts.active_settlements must contain mappings")

    validated_settlements: list[dict[str, Any]] = []
    settlement_ids: set[int] = set()
    for row_index, raw_row in enumerate(settlements):
        _required_snapshot_row_keys(
            raw_row,
            settlement_keys,
            f"historical_facts.active_settlements[{row_index}]",
        )
        row = dict(raw_row)
        row_id = _positive_int(
            row["id"], f"historical_facts.active_settlements[{row_index}].id"
        )
        if row_id in settlement_ids:
            raise ValueError("historical_facts.active_settlements contains duplicate id")
        settlement_ids.add(row_id)
        row["id"] = row_id
        if row["case_no"] != canonical_case_no:
            raise ValueError("historical_facts.active_settlements case_no mismatch")
        row["assignment_id"] = _positive_int(
            row["assignment_id"], f"historical_facts.active_settlements[{row_index}].assignment_id"
        )
        row["settlement_id"] = _positive_int(
            row["settlement_id"], f"historical_facts.active_settlements[{row_index}].settlement_id"
        )
        if row["assignment_id"] not in current_by_id:
            raise ValueError("historical_facts.active_settlements references unknown assignment_id")
        if not isinstance(row["status"], str):
            raise ValueError("historical_facts.active_settlements.status must be a string")
        validated_settlements.append(row)
    settlements = validated_settlements

    raw_locks = conflict_snapshot["active_lock_days"]
    if not isinstance(raw_locks, list):
        raise ValueError("conflict_snapshot.active_lock_days must be a list")
    lock_ids: set[int] = set()
    lock_rows: list[dict[str, Any]] = []
    for row_index, raw_row in enumerate(raw_locks):
        if not isinstance(raw_row, Mapping):
            raise ValueError("conflict_snapshot.active_lock_days must contain mappings")
        _required_snapshot_row_keys(
            raw_row, lock_keys, f"conflict_snapshot.active_lock_days[{row_index}]"
        )
        row = dict(raw_row)
        lock_id = _positive_int(
            row["id"], f"conflict_snapshot.active_lock_days[{row_index}].id"
        )
        if lock_id in lock_ids:
            raise ValueError("conflict_snapshot.active_lock_days contains duplicate id")
        lock_ids.add(lock_id)
        row["id"] = lock_id
        row["lock_id"] = _positive_int(
            row["lock_id"], f"conflict_snapshot.active_lock_days[{row_index}].lock_id"
        )
        row["plan_id"] = _positive_int(
            row["plan_id"], f"conflict_snapshot.active_lock_days[{row_index}].plan_id"
        )
        row["segment_id"] = _positive_int(
            row["segment_id"], f"conflict_snapshot.active_lock_days[{row_index}].segment_id"
        )
        row["staff_id"] = _positive_int(
            row["staff_id"], f"conflict_snapshot.active_lock_days[{row_index}].staff_id"
        )
        row["lock_date"] = _date(
            row["lock_date"], f"conflict_snapshot.active_lock_days[{row_index}].lock_date"
        )
        if row["case_no"] != canonical_case_no:
            raise ValueError("conflict_snapshot.active_lock_days case_no mismatch")
        lock_rows.append(
            {
                "active_marker": 1,
                "staff_id": row["staff_id"],
                "lock_date": row["lock_date"].isoformat(),
            }
        )

    current = [
        {
            "id": row["id"],
            "case_no": row["case_no"],
            "staff_id": row["staff_id"],
            "status": row["status"],
            "assigned_start_date": row["assigned_start_date"],
            "assigned_end_date": row["assigned_end_date"],
            "kind": "formal",
            "original_assignment_id": None,
            "substitution_work_date": None,
        }
        for row in current_assignments
    ]
    original_row = None
    for row in current:
        if row["id"] == assignment_id:
            original_row = row
            break
    if original_row is None:
        raise ValueError("original assignment is absent from case snapshot")
    if original_row["case_no"] != canonical_case_no or original_row["staff_id"] != original_staff_id:
        raise ValueError("original assignment ownership mismatch")
    current_by_id = {row["id"]: row for row in current}
    if assignment_id not in current_by_id:
        raise ValueError("original assignment is absent from case snapshot")
    active_current = [row for row in current if row["status"] != "cancelled"]
    if not active_current:
        return _fingerprinted(
            {
                "status": "blocked",
                "resolution_type": resolution_type,
                "historical_fact_state": "bootstrap",
                "requires_confirmation": False,
                "requires_audit": False,
                "assignment_transition_plan": None,
                "availability_conflicts": [],
                "assignment_transition_conflicts": [],
                "assignment_service_impacts": [],
                "required_hours": None,
                "provisional_actual_hours": None,
                "blocking_reasons": ["order_assignment_synchronization_required"],
                "review_reasons": [],
            }
        )

    current_start = min(
        _date(row["assigned_start_date"], "assigned_start_date") for row in active_current
    )
    current_end = max(
        _date(row["assigned_end_date"], "assigned_end_date") for row in active_current
    )
    ordered_active = sorted(
        active_current,
        key=lambda row: (
            _date(row["assigned_start_date"], "assigned_start_date"),
            row["id"],
        ),
    )
    original_index = next(
        (index for index, row in enumerate(ordered_active) if row["id"] == assignment_id),
        None,
    )
    if original_index is None:
        raise ValueError("original assignment is not active")
    affected_assignment_ids = (
        {row["id"] for row in ordered_active[original_index:]}
        if resolution_type == "defer_following_assignments"
        else {assignment_id}
    )
    if schedule_by_id[schedule_id]["assignment_id"] != assignment_id:
        raise ValueError("original schedule ownership mismatch")

    immutable_rows = list(leaves := [])  # type: ignore[var-annotated]
    immutable_rows.extend(settlements)
    immutable_rows.extend(payments)
    immutable_rows.extend(actual_adjustments)
    locked = any(
        row["assignment_id"] in affected_assignment_ids for row in immutable_rows
    )

    historical = leave_date < database_current_date
    historical_case_rows = bool(
        leave_events
        or [row for row in schedule_rows if row["assignment_id"] is not None]
        or actual_adjustments
        or payments
        or settlements
    )
    historical_fact_state = (
        "locked"
        if locked
        else "unlocked"
        if historical and historical_case_rows
        else "bootstrap"
    )
    if historical_fact_state == "locked":
        return _fingerprinted(
            {
                "status": "blocked",
                "resolution_type": resolution_type,
                "historical_fact_state": "locked",
                "requires_confirmation": False,
                "requires_audit": False,
                "assignment_transition_plan": None,
                "availability_conflicts": [],
                "assignment_transition_conflicts": [],
                "assignment_service_impacts": [],
                "required_hours": None,
                "provisional_actual_hours": None,
                "blocking_reasons": ["historical_facts_locked"],
                "review_reasons": [],
            }
        )

    proposed = [dict(row) for row in current]
    proposed_end = current_end
    operation_kind = resolution_type
    if resolution_type == "defer_following_assignments":
        ordered = sorted(
            (row for row in proposed if row["status"] != "cancelled"),
            key=lambda row: (
                _date(row["assigned_start_date"], "assigned_start_date"),
                row["id"],
            ),
        )
        affected_index = next(
            index for index, row in enumerate(ordered) if row["id"] == assignment_id
        )
        ordered[affected_index]["assigned_end_date"] = (
            _date(ordered[affected_index]["assigned_end_date"], "assigned_end_date")
            + timedelta(days=1)
        )
        for row in ordered[affected_index + 1 :]:
            row["assigned_start_date"] = (
                _date(row["assigned_start_date"], "assigned_start_date")
                + timedelta(days=1)
            )
            row["assigned_end_date"] = (
                _date(row["assigned_end_date"], "assigned_end_date")
                + timedelta(days=1)
            )
        proposed_end += timedelta(days=1)
    else:
        original_after_row = next(row for row in proposed if row["id"] == assignment_id)
        start = _date(original_after_row["assigned_start_date"], "assigned_start_date")
        end = _date(original_after_row["assigned_end_date"], "assigned_end_date")
        original_after_row["status"] = "cancelled"
        replacements: list[dict[str, Any]] = []
        if start < leave_date:
            replacements.append(
                {
                    **original_after_row,
                    "id": f"original-{assignment_id}-prefix",
                    "status": "active",
                    "assigned_start_date": start,
                    "assigned_end_date": leave_date - timedelta(days=1),
                }
            )
        replacements.append(
            {
                "id": f"substitute-{assignment_id}-{leave_date.isoformat()}",
                "case_no": canonical_case_no,
                "staff_id": substitute_id,
                "status": "active",
                "assigned_start_date": leave_date,
                "assigned_end_date": leave_date,
                "kind": "single_day_substitute",
                "original_assignment_id": assignment_id,
                "substitution_work_date": leave_date,
            }
        )
        if leave_date < end:
            replacements.append(
                {
                    **original_after_row,
                    "id": f"original-{assignment_id}-suffix",
                    "status": "active",
                    "assigned_start_date": leave_date + timedelta(days=1),
                    "assigned_end_date": end,
                }
            )
        proposed.extend(replacements)
        operation_kind = "single_day_substitute"

    try:
        transition = validate_assignment_plan_transition(
            case_no=canonical_case_no,
            database_current_date=database_current_date,
            effective_date=leave_date,
            current_case_start_date=current_start,
            current_case_end_date=current_end,
            proposed_case_start_date=current_start,
            proposed_case_end_date=proposed_end,
            operation_kind=operation_kind,
            current_assignments=current,
            proposed_assignments=proposed,
            historical_fact_state=historical_fact_state,
        )
    except AssignmentPlanTransitionConflict as exc:
        if not isinstance(exc.details, Mapping):
            raise ValueError(
                "AssignmentPlanTransitionConflict details must be a mapping"
            )
        transition_conflict = {
            "category": "assignment_transition",
            "code": exc.code,
            "details": _strict_history_payload(exc.details, "AssignmentPlanTransitionConflict details"),
        }
        return _fingerprinted(
            {
                "status": "blocked",
                "resolution_type": resolution_type,
                "historical_fact_state": historical_fact_state,
                "requires_confirmation": False,
                "requires_audit": historical_fact_state == "unlocked",
                "assignment_transition_plan": None,
                "assignment_transition_conflicts": [
                    {
                        "category": transition_conflict["category"],
                        "code": transition_conflict["code"],
                        "details": transition_conflict["details"],
                    }
                ],
                "availability_conflicts": [],
                "assignment_service_impacts": [],
                "required_hours": None,
                "provisional_actual_hours": None,
                "blocking_reasons": ["assignment_transition_conflict"],
                "review_reasons": [],
            }
        )

    current_intervals = {
        (row["id"], row["staff_id"]): (
            _date(row["assigned_start_date"], "assigned_start_date"),
            _date(row["assigned_end_date"], "assigned_end_date"),
        )
        for row in active_current
    }
    dates_to_check: list[tuple[int, date, int | str]] = []
    for row in transition["after_assignments"]:
        if row["status"] == "cancelled":
            continue
        start = _date(row["assigned_start_date"], "assigned_start_date")
        end = _date(row["assigned_end_date"], "assigned_end_date")
        before = current_intervals.get((row["id"], row["staff_id"]))
        candidate_date = start
        while candidate_date <= end:
            if before is None or not (before[0] <= candidate_date <= before[1]):
                dates_to_check.append((row["staff_id"], candidate_date, row["id"]))
            candidate_date += timedelta(days=1)

    occupancy_rows: list[dict[str, Any]] = []
    for row in schedule_rows:
        if row["assignment_id"] in affected_assignment_ids:
            continue
        occupancy_rows.append(
            {
                "assignment_id": row["assignment_id"],
                "staff_id": row["staff_id"],
                "work_date": row["work_date"].isoformat(),
            }
        )
    locked_rows_for_conflict = [
        dict(row) for row in lock_rows
    ]
    blocked_days, helper_review = _extract_blocked_days(occupancy_rows, locked_rows_for_conflict)
    conflicts = [
        {
            "assignment_id": row_id,
            "staff_id": staff_id,
            "work_date": candidate_date.isoformat(),
            "reason_code": reason_code,
        }
        for staff_id, candidate_date, row_id in dates_to_check
        for reason_code in sorted(blocked_days.get(staff_id, {}).get(candidate_date, set()))
    ]

    service_hours = _positive_decimal(
        original.get("service_hours_per_day"), "service_hours_per_day"
    )
    required_hours = sum(
        (
            _positive_decimal(row.get("planned_hours"), "planned_hours")
            for row in current_assignments
            if row["status"] != "cancelled"
        ),
        Decimal("0"),
    )
    ownership_impacts: list[dict[str, Any]] = []
    total_hours = Decimal("0")
    for row in transition["after_assignments"]:
        if row["status"] == "cancelled":
            continue
        start = _date(row["assigned_start_date"], "assigned_start_date")
        end = _date(row["assigned_end_date"], "assigned_end_date")
        service_days = (end - start).days + 1
        if (
            resolution_type == "defer_following_assignments"
            and row["id"] == assignment_id
            and start <= leave_date <= end
        ):
            service_days -= 1
        hours = service_hours * service_days
        total_hours += hours
        ownership_impacts.append(
            {
                "assignment_id": row["id"],
                "staff_id": row["staff_id"],
                "service_days": service_days,
                "actual_hours": hours,
            }
        )

    blocking_reasons: list[str] = []
    review_reasons: list[str] = []
    if conflicts:
        blocking_reasons.append("availability_conflict")
    if total_hours != required_hours:
        blocking_reasons.append("order_service_hours_mismatch")
    if historical_fact_state == "unlocked":
        review_reasons.append("historical_audit_required")
    if helper_review:
        review_reasons.append("legacy_schedule_ownership_review")
    status = (
        "blocked"
        if blocking_reasons
        else "requires_review"
        if review_reasons
        else "ready"
    )
    return _fingerprinted(
        {
            "status": status,
            "resolution_type": resolution_type,
            "historical_fact_state": historical_fact_state,
            "requires_confirmation": status != "blocked",
            "requires_audit": historical_fact_state == "unlocked",
            "assignment_transition_plan": transition,
            "availability_conflicts": conflicts,
            "assignment_transition_conflicts": [],
            "assignment_service_impacts": ownership_impacts,
            "required_hours": required_hours,
            "provisional_actual_hours": total_hours,
            "blocking_reasons": blocking_reasons,
            "review_reasons": review_reasons,
        }
    )


_PURE_TRANSITION_DIAGNOSTIC_FACT_KEYS = {
    "field", "reason", "expected", "actual", "service_day", "segment_ref",
    "segment_refs", "original_segment_ref", "caregiver_ref", "source_kind",
    "expected_defer_days", "actual_defer_days", "maximum_active_segments",
    "actual_active_segments", "expected_service_days", "actual_service_days",
    "expected_service_hours", "actual_service_hours", "database_current_date",
    "effective_date",
}


def _pure_transition_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite Decimal-compatible value")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite Decimal-compatible value") from exc
    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal-compatible value")
    return decimal_value


def _pure_transition_plan_without_commitment(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "segments": deepcopy(plan["segments"]),
        "daily_ownership": deepcopy(plan["daily_ownership"]),
        "service_period": deepcopy(plan["service_period"]),
    }


def _pure_transition_validate_before_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or set(plan) != {
        "segments", "daily_ownership", "service_period", "service_commitment"
    }:
        raise ValueError("before_service_plan must contain exact keys")
    adapter_plan = _pure_transition_plan_without_commitment(plan)
    parsed = _domain_rules_parse_service_plan(
        plan=adapter_plan,
        plan_name="before_service_plan",
        require_current_refs=True,
    )
    expected_refs = [f"current:{index}" for index in range(len(plan["segments"]))]
    if [segment["segment_ref"] for segment in plan["segments"]] != expected_refs:
        raise ValueError("before_service_plan.segments must be ordered current refs")
    ownership_dates = [
        _date(row["service_day"], "before_service_plan.daily_ownership.service_day")
        for row in plan["daily_ownership"]
    ]
    if ownership_dates != sorted(ownership_dates):
        raise ValueError("before_service_plan.daily_ownership must be ordered by service_day")
    commitment = plan["service_commitment"]
    if not isinstance(commitment, Mapping) or set(commitment) != {
        "required_service_days", "hours_per_service_day", "required_total_hours"
    }:
        raise ValueError("before_service_plan.service_commitment must contain exact keys")
    required_days = commitment["required_service_days"]
    if isinstance(required_days, bool) or not isinstance(required_days, int) or required_days < 0:
        raise ValueError("service_commitment.required_service_days must be a non-negative int")
    hours_per_day = _pure_transition_decimal(
        commitment["hours_per_service_day"], "service_commitment.hours_per_service_day"
    )
    if hours_per_day <= 0:
        raise ValueError("service_commitment.hours_per_service_day must be positive")
    required_total = _pure_transition_decimal(
        commitment["required_total_hours"], "service_commitment.required_total_hours"
    )
    if required_total != Decimal(required_days) * hours_per_day:
        raise ValueError("service_commitment.required_total_hours must equal days times hours")
    return {
        "raw": deepcopy(dict(plan)),
        "parsed": parsed,
        "required_days": required_days,
        "hours_per_day": hours_per_day,
        "required_total": required_total,
    }


def _pure_transition_diagnostic(code: str, scope_ref: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(facts, Mapping) or set(facts) - _PURE_TRANSITION_DIAGNOSTIC_FACT_KEYS:
        raise ValueError("pure transition diagnostic facts are invalid")
    return {"code": code, "scope_ref": scope_ref, "facts": dict(facts)}


def _pure_transition_diagnostic_sort_key(diagnostic: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        diagnostic["code"],
        diagnostic["scope_ref"],
        json.dumps(diagnostic["facts"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _pure_transition_validate_inputs(
    *,
    canonical_intent: Any,
    item_lineage: Any,
    before_service_plan: Any,
    eligibility_facts: Any,
) -> dict[str, Any]:
    if not isinstance(canonical_intent, Mapping) or set(canonical_intent) != {
        "case_ref", "original_segment_ref", "items"
    }:
        raise ValueError("canonical_intent must contain exact keys")
    case_identity = _domain_rules_reference_identity(canonical_intent["case_ref"], "canonical_intent.case_ref")
    original_ref = canonical_intent["original_segment_ref"]
    namespace, original_index = _domain_rules_ref_to_key(
        original_ref, "canonical_intent.original_segment_ref"
    )
    if namespace != "current":
        raise ValueError("canonical_intent.original_segment_ref must be current:N")
    plan = _pure_transition_validate_before_plan(before_service_plan)
    if original_ref not in plan["parsed"]["segments"]:
        raise ValueError("canonical_intent.original_segment_ref is absent from before_service_plan")
    raw_items = canonical_intent["items"]
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("canonical_intent.items must be a non-empty list")
    items: list[dict[str, Any]] = []
    seen_days: set[date] = set()
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping) or set(raw_item) != {
            "item_ref", "service_day", "resolution", "substitute_caregiver_ref"
        }:
            raise ValueError("canonical_intent item must contain exact keys")
        if raw_item["item_ref"] != index or isinstance(raw_item["item_ref"], bool):
            raise ValueError("canonical_intent item_ref must be consecutive")
        service_day = _date(raw_item["service_day"], "canonical_intent.items.service_day")
        if service_day in seen_days:
            raise ValueError("canonical_intent items must not duplicate service_day")
        seen_days.add(service_day)
        resolution = raw_item["resolution"]
        if resolution not in _ALLOWED_RESOLUTIONS:
            raise ValueError("canonical_intent item resolution is invalid")
        substitute = raw_item["substitute_caregiver_ref"]
        if resolution == "defer":
            if substitute is not None:
                raise ValueError("defer item substitute_caregiver_ref must be null")
            typed_substitute = None
        else:
            typed_substitute = _domain_rules_reference_identity(
                substitute, "canonical_intent.items.substitute_caregiver_ref"
            )
        owner = next(
            (row for row in plan["parsed"]["ownership"] if row["service_day"] == service_day),
            None,
        )
        if owner is None or owner["segment_ref"] != original_ref:
            raise ValueError("canonical_intent item must target original segment daily ownership")
        items.append({
            "item_ref": index,
            "service_day": service_day,
            "resolution": resolution,
            "substitute": typed_substitute,
            "substitute_raw": None if typed_substitute is None else typed_substitute[2],
        })
    if [item["service_day"] for item in items] != sorted(item["service_day"] for item in items):
        raise ValueError("canonical_intent.items must be ordered by service_day")
    if not isinstance(item_lineage, list) or len(item_lineage) != len(items):
        raise ValueError("item_lineage must exactly match canonical_intent.items")
    for item, lineage in zip(items, item_lineage):
        if not isinstance(lineage, Mapping) or set(lineage) != {"item_ref", "original_service_day_ref"}:
            raise ValueError("item_lineage item must contain exact keys")
        if lineage["item_ref"] != item["item_ref"] or isinstance(lineage["item_ref"], bool):
            raise ValueError("item_lineage item_ref must match canonical intent")
        if lineage["original_service_day_ref"] != f"service-day:{item['service_day'].isoformat()}":
            raise ValueError("item_lineage original_service_day_ref must exactly match service day")
    if not isinstance(eligibility_facts, Mapping) or set(eligibility_facts) != {
        "database_current_date", "historical_protection", "occupancy"
    }:
        raise ValueError("eligibility_facts must contain exact keys")
    database_current_date = _date(
        eligibility_facts["database_current_date"], "eligibility_facts.database_current_date"
    )
    protection = eligibility_facts["historical_protection"]
    if not isinstance(protection, Mapping) or set(protection) != {"state", "protected_segment_refs"}:
        raise ValueError("historical_protection must contain exact keys")
    state = protection["state"]
    if state not in {"bootstrap", "unlocked", "locked"}:
        raise ValueError("historical_protection.state is invalid")
    protected = protection["protected_segment_refs"]
    if not isinstance(protected, list) or any(not isinstance(ref, str) for ref in protected):
        raise ValueError("historical_protection.protected_segment_refs must be a list")
    if protected != sorted(set(protected), key=_domain_rules_segment_ref_sort_key):
        raise ValueError("historical_protection.protected_segment_refs must be canonical unique refs")
    if any(_domain_rules_ref_to_key(ref, "protected_segment_ref")[0] != "current" or ref not in plan["parsed"]["segments"] for ref in protected):
        raise ValueError("historical_protection refs must be before current refs")
    if (state == "bootstrap" and protected) or (state == "locked" and not protected):
        raise ValueError("historical_protection state and protected refs are inconsistent")
    raw_occupancy = eligibility_facts["occupancy"]
    if not isinstance(raw_occupancy, list):
        raise ValueError("eligibility_facts.occupancy must be a list")
    occupancy = []
    source_rank = {"formal_service": 0, "waiting_deposit_lock": 1, "legacy_unresolved": 2}
    for raw in raw_occupancy:
        if not isinstance(raw, Mapping) or set(raw) != {
            "caregiver_ref", "service_day", "source_kind", "source_segment_ref"
        }:
            raise ValueError("eligibility occupancy item must contain exact keys")
        caregiver = _domain_rules_reference_identity(raw["caregiver_ref"], "occupancy.caregiver_ref")
        service_day = _date(raw["service_day"], "occupancy.service_day")
        source_kind = raw["source_kind"]
        if source_kind not in source_rank:
            raise ValueError("occupancy.source_kind is invalid")
        source_ref = raw["source_segment_ref"]
        if source_ref is not None:
            if not isinstance(source_ref, str) or source_ref not in plan["parsed"]["segments"]:
                raise ValueError("occupancy.source_segment_ref must be a before current ref")
            matching_owner = next((row for row in plan["parsed"]["ownership"] if row["service_day"] == service_day), None)
            if matching_owner is None or matching_owner["segment_ref"] != source_ref or matching_owner["caregiver_ref"] != caregiver:
                raise ValueError("occupancy source_segment_ref must match before daily ownership")
        occupancy.append({"caregiver": caregiver, "service_day": service_day, "source_kind": source_kind, "source_ref": source_ref})
    if occupancy != sorted(occupancy, key=lambda row: (row["service_day"], _domain_rules_reference_sort_key(row["caregiver"]), source_rank[row["source_kind"]], row["source_ref"] or "")):
        raise ValueError("eligibility_facts.occupancy must be canonically ordered")
    return {
        "intent": deepcopy(dict(canonical_intent)), "items": items, "plan": plan,
        "case_identity": case_identity, "original_ref": f"current:{original_index}",
        "database_current_date": database_current_date, "historical_state": state,
        "protected": protected, "occupancy": occupancy,
    }


def _pure_transition_candidate(context: Mapping[str, Any]) -> dict[str, Any]:
    parsed = context["plan"]["parsed"]
    raw_plan = context["plan"]["raw"]
    original_ref = context["original_ref"]
    items = context["items"]
    defer_days = sum(item["resolution"] == "defer" for item in items)
    substitute_by_day = {item["service_day"]: item for item in items if item["resolution"] == "substitute"}
    original_segment = parsed["segments"][original_ref]
    original_index = _domain_rules_ref_to_key(original_ref, "original_segment_ref")[1]
    old_owner_by_day = {row["service_day"]: row for row in parsed["ownership"]}
    plan_end = parsed["service_period"]["end"]
    extension_days = [plan_end + timedelta(days=index) for index in range(1, defer_days + 1)]
    formal_days = [
        day for day, owner in old_owner_by_day.items()
        if owner["segment_ref"] == original_ref and day not in substitute_by_day
    ] + extension_days
    formal_days = sorted(formal_days)
    groups: list[list[date]] = []
    for day in formal_days:
        if not groups or day != groups[-1][-1] + timedelta(days=1):
            groups.append([day])
        else:
            groups[-1].append(day)
    target_ref_by_day: dict[date, str] = {}
    derived_segments = []
    current_target = deepcopy(original_segment["source"])
    if groups:
        current_target["service_period"] = {"start": groups[0][0], "end": groups[0][-1]}
        for day in groups[0]:
            target_ref_by_day[day] = original_ref
        for derived_index, group in enumerate(groups[1:]):
            ref = f"derived:{derived_index}"
            target_ref_by_day.update({day: ref for day in group})
            derived_segments.append({
                "segment_ref": ref,
                "caregiver_ref": original_segment["caregiver_ref"][2],
                "status": original_segment["status"],
                "service_period": {"start": group[0], "end": group[-1]},
                "segment_kind": "formal",
                "lineage": {"original_segment_ref": None, "substitution_service_day": None},
            })
    else:
        current_target["status"] = "cancelled"
    current_segments = []
    for ref in _domain_rules_segment_ref_order(parsed["segments"]):
        current_segments.append(current_target if ref == original_ref else deepcopy(parsed["segments"][ref]["source"]))
    substitute_segments = [
        {
            "segment_ref": f"substitute:{item['item_ref']}",
            "caregiver_ref": item["substitute_raw"],
            "status": original_segment["status"],
            "service_period": {"start": item["service_day"], "end": item["service_day"]},
            "segment_kind": "single_day_substitute",
            "lineage": {"original_segment_ref": original_ref, "substitution_service_day": item["service_day"]},
        }
        for item in items if item["resolution"] == "substitute"
    ]
    daily_ownership = []
    for day in sorted(set(old_owner_by_day) | set(extension_days)):
        if day in substitute_by_day:
            item = substitute_by_day[day]
            daily_ownership.append({"service_day": day, "segment_ref": f"substitute:{item['item_ref']}", "caregiver_ref": item["substitute_raw"]})
        elif day in target_ref_by_day:
            daily_ownership.append({"service_day": day, "segment_ref": target_ref_by_day[day], "caregiver_ref": original_segment["caregiver_ref"][2]})
        else:
            owner = old_owner_by_day[day]
            daily_ownership.append({"service_day": day, "segment_ref": owner["segment_ref"], "caregiver_ref": owner["caregiver_ref"][2]})
    return {
        "segments": current_segments + derived_segments + substitute_segments,
        "daily_ownership": daily_ownership,
        "service_period": {"start": raw_plan["service_period"]["start"], "end": plan_end + timedelta(days=defer_days)},
        "service_commitment": deepcopy(raw_plan["service_commitment"]),
    }


def _pure_transition_impacts(*, before: Mapping[str, Any], after: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    defer_days = {item["service_day"] for item in context["items"] if item["resolution"] == "defer"}
    before_by_day = {row["service_day"]: _domain_rules_reference_identity(row["caregiver_ref"], "before caregiver_ref") for row in before["daily_ownership"]}
    after_by_day = {row["service_day"]: _domain_rules_reference_identity(row["caregiver_ref"], "after caregiver_ref") for row in after["daily_ownership"] if _date(row["service_day"], "after service_day") not in defer_days}
    before_counts: dict[tuple[int, int | str | bytes, Any], int] = {}
    after_counts: dict[tuple[int, int | str | bytes, Any], int] = {}
    for identity in before_by_day.values():
        before_counts[identity] = before_counts.get(identity, 0) + 1
    for identity in after_by_day.values():
        after_counts[identity] = after_counts.get(identity, 0) + 1
    hours = context["plan"]["hours_per_day"]
    identities = sorted(set(before_counts) | set(after_counts), key=_domain_rules_reference_sort_key)
    per_caregiver = [
        {
            "caregiver_ref": identity[2],
            "before_service_days": before_counts.get(identity, 0),
            "after_service_days": after_counts.get(identity, 0),
            "delta_service_days": after_counts.get(identity, 0) - before_counts.get(identity, 0),
            "before_service_hours": Decimal(before_counts.get(identity, 0)) * hours,
            "after_service_hours": Decimal(after_counts.get(identity, 0)) * hours,
            "delta_service_hours": Decimal(after_counts.get(identity, 0) - before_counts.get(identity, 0)) * hours,
        }
        for identity in identities
    ]
    before_days = sum(before_counts.values())
    after_days = sum(after_counts.values())
    total = {
        "before_service_days": before_days,
        "after_service_days": after_days,
        "delta_service_days": after_days - before_days,
        "before_service_hours": Decimal(before_days) * hours,
        "after_service_hours": Decimal(after_days) * hours,
        "delta_service_hours": Decimal(after_days - before_days) * hours,
        "required_service_days": context["plan"]["required_days"],
        "required_total_hours": context["plan"]["required_total"],
    }
    return {"per_caregiver": per_caregiver, "total": total}


def calculate_assignment_leave_resolution_batch_transition(
    *,
    canonical_intent: Mapping[str, Any],
    item_lineage: list[Mapping[str, Any]],
    before_service_plan: Mapping[str, Any],
    eligibility_facts: Mapping[str, Any],
) -> dict[str, Any]:
    """Calculate one pure aggregate leave transition from frozen pure-domain facts."""
    context = _pure_transition_validate_inputs(
        canonical_intent=canonical_intent,
        item_lineage=item_lineage,
        before_service_plan=before_service_plan,
        eligibility_facts=eligibility_facts,
    )
    candidate = _pure_transition_candidate(context)
    rules_result = validate_assignment_leave_resolution_domain_transition(
        case_ref=context["case_identity"][2],
        database_current_date=context["database_current_date"],
        historical_fact_state=context["historical_state"],
        before_service_plan=_pure_transition_plan_without_commitment(context["plan"]["raw"]),
        canonical_leave_intent={
            "original_segment_ref": context["original_ref"],
            "items": [
                {"item_ref": item["item_ref"], "service_day": item["service_day"], "resolution": item["resolution"], "substitute_caregiver_ref": item["substitute_raw"]}
                for item in context["items"]
            ],
        },
        candidate_after_service_plan=_pure_transition_plan_without_commitment(candidate),
    )
    if not isinstance(rules_result, Mapping) or set(rules_result) != {"valid", "after_service_plan", "transition_diagnostics"} or type(rules_result["valid"]) is not bool or not isinstance(rules_result["transition_diagnostics"], list):
        raise ValueError("domain transition Rules returned an invalid contract")
    before_copy = deepcopy(context["plan"]["raw"])
    intent_copy = deepcopy(context["intent"])
    if not rules_result["valid"]:
        if rules_result["after_service_plan"] is not None:
            raise ValueError("invalid Rules transition must not return an after plan")
        return {
            "canonical_intent": intent_copy,
            "service_plan_transition": {"before": before_copy, "intent": intent_copy, "after": None, "impacts": None},
            "canonical_eligibility": {"transition_valid": False, "applicable": False, "blocking_diagnostics": deepcopy(rules_result["transition_diagnostics"]), "review_diagnostics": []},
        }
    if rules_result["after_service_plan"] is None:
        raise ValueError("valid Rules transition must return an after plan")
    after = {**deepcopy(rules_result["after_service_plan"]), "service_commitment": deepcopy(context["plan"]["raw"]["service_commitment"])}
    impacts = _pure_transition_impacts(before=before_copy, after=after, context=context)
    blocking = []
    review = []
    before_owner_by_day = {
        _date(row["service_day"], "before service_day"): _domain_rules_reference_identity(row["caregiver_ref"], "before caregiver_ref")
        for row in before_copy["daily_ownership"]
    }
    after_owner_by_day = {
        _date(row["service_day"], "after service_day"): (row, _domain_rules_reference_identity(row["caregiver_ref"], "after caregiver_ref"))
        for row in after["daily_ownership"]
    }
    for occupancy in context["occupancy"]:
        after_owner = after_owner_by_day.get(occupancy["service_day"])
        if after_owner is None or after_owner[1] != occupancy["caregiver"] or before_owner_by_day.get(occupancy["service_day"]) == after_owner[1]:
            continue
        code = {"formal_service": "formal_service_conflict", "waiting_deposit_lock": "waiting_deposit_lock_conflict", "legacy_unresolved": "legacy_ownership_requires_review"}[occupancy["source_kind"]]
        diagnostic = _pure_transition_diagnostic(code, f"service-day:{occupancy['service_day'].isoformat()}", {"caregiver_ref": occupancy["caregiver"][2], "service_day": occupancy["service_day"].isoformat(), "source_kind": occupancy["source_kind"], "segment_ref": after_owner[0]["segment_ref"]})
        (review if occupancy["source_kind"] == "legacy_unresolved" else blocking).append(diagnostic)
    if context["original_ref"] in context["protected"]:
        diagnostic = _pure_transition_diagnostic(
            "historical_ownership_locked" if context["historical_state"] == "locked" else "historical_change_requires_review",
            context["original_ref"],
            {"segment_ref": context["original_ref"], "effective_date": context["items"][0]["service_day"].isoformat(), "database_current_date": context["database_current_date"].isoformat()},
        )
        (blocking if context["historical_state"] == "locked" else review).append(diagnostic)
    total = impacts["total"]
    if total["after_service_days"] != total["required_service_days"] or total["after_service_hours"] != total["required_total_hours"]:
        blocking.append(_pure_transition_diagnostic("service_commitment_mismatch", "transition", {"expected_service_days": total["required_service_days"], "actual_service_days": total["after_service_days"], "expected_service_hours": str(total["required_total_hours"]), "actual_service_hours": str(total["after_service_hours"])}))
    return {
        "canonical_intent": intent_copy,
        "service_plan_transition": {"before": before_copy, "intent": intent_copy, "after": after, "impacts": impacts},
        "canonical_eligibility": {"transition_valid": True, "applicable": not blocking, "blocking_diagnostics": sorted(blocking, key=_pure_transition_diagnostic_sort_key), "review_diagnostics": sorted(review, key=_pure_transition_diagnostic_sort_key)},
    }


def compute_assignment_leave_resolution_batch_preview_from_snapshot(
    request: Mapping[str, Any],
    original_assignment_schedule: Mapping[str, Any],
    conflict_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Project validated legacy snapshot facts into the one pure batch transition."""
    canonicalized = canonicalize_assignment_leave_resolution_batch_request(
        request,
        original_assignment_schedule,
        conflict_snapshot,
    )
    if not isinstance(conflict_snapshot, Mapping) or set(conflict_snapshot) != {
        "database_current_date", "assignments", "assignment_schedule_days", "active_lock_days", "historical_facts"
    }:
        raise ValueError("conflict_snapshot keys are invalid")
    database_current_date = _date(conflict_snapshot["database_current_date"], "database_current_date")
    legacy_intent = canonicalized["canonical_batch_intent"]
    legacy_lineage = canonicalized["item_lineage"]
    if not isinstance(legacy_intent, Mapping) or not isinstance(legacy_lineage, Mapping):
        raise ValueError("canonicalizer returned an invalid contract")
    original = original_assignment_schedule.get("assignment")
    if not isinstance(original, Mapping) or _positive_int(original.get("id"), "original assignment id") != legacy_intent.get("original_assignment_id"):
        raise ValueError("original assignment ownership mismatch")
    if original.get("case_no") != legacy_intent.get("case_no"):
        raise ValueError("original assignment ownership mismatch")
    hours_per_day = _positive_decimal(original.get("service_hours_per_day"), "service_hours_per_day")

    assignment_keys = {"id", "case_no", "staff_id", "status", "assigned_start_date", "assigned_end_date", "planned_hours", "actual_hours"}
    schedule_keys = {"id", "case_no", "staff_id", "assignment_id", "work_date", "is_work_day", "is_double_pay", "notes", "requires_review"}
    assignments = conflict_snapshot["assignments"]
    schedules = conflict_snapshot["assignment_schedule_days"]
    locks = conflict_snapshot["active_lock_days"]
    facts = conflict_snapshot["historical_facts"]
    if not isinstance(assignments, list) or not isinstance(schedules, list) or not isinstance(locks, list) or not isinstance(facts, Mapping) or set(facts) != {"leave_substitution_events", "actual_hours_adjustments", "non_cancelled_payments", "active_settlements"}:
        raise ValueError("conflict_snapshot has an invalid dependency contract")
    parsed_assignments = []
    for index, raw in enumerate(assignments):
        if not isinstance(raw, Mapping) or set(raw) != assignment_keys:
            raise ValueError("conflict_snapshot.assignments row must contain exact fields")
        row = dict(raw)
        row["id"] = _positive_int(row["id"], f"assignments[{index}].id")
        row["staff_id"] = _positive_int(row["staff_id"], f"assignments[{index}].staff_id")
        if row["case_no"] != legacy_intent["case_no"] or not isinstance(row["status"], str):
            raise ValueError("conflict_snapshot assignment ownership mismatch")
        row["assigned_start_date"] = _date(row["assigned_start_date"], "assigned_start_date")
        row["assigned_end_date"] = _date(row["assigned_end_date"], "assigned_end_date")
        if row["assigned_start_date"] > row["assigned_end_date"]:
            raise ValueError("assignment service period is invalid")
        row["planned_hours"] = _positive_decimal(row["planned_hours"], "planned_hours")
        row["actual_hours"] = _positive_decimal(row["actual_hours"], "actual_hours")
        parsed_assignments.append(row)
    if len({row["id"] for row in parsed_assignments}) != len(parsed_assignments):
        raise ValueError("conflict_snapshot.assignments contains duplicate id")
    active = sorted((row for row in parsed_assignments if row["status"] != "cancelled"), key=lambda row: (row["assigned_start_date"], row["id"]))
    if not active:
        raise ValueError("case snapshot has no active assignments")
    id_to_ref = {row["id"]: f"current:{index}" for index, row in enumerate(active)}
    original_id = _positive_int(legacy_intent["original_assignment_id"], "original_assignment_id")
    if original_id not in id_to_ref:
        raise ValueError("original assignment is absent from active case snapshot")
    staff_ids = {row["staff_id"] for row in parsed_assignments}
    for raw in schedules:
        if not isinstance(raw, Mapping) or set(raw) != schedule_keys:
            raise ValueError("conflict_snapshot.assignment_schedule_days row must contain exact fields")
        staff_ids.add(_positive_int(raw.get("staff_id"), "schedule staff_id"))
    for item in legacy_intent.get("items", []):
        if isinstance(item, Mapping) and item.get("substitute_staff_id") is not None:
            staff_ids.add(_positive_int(item["substitute_staff_id"], "substitute_staff_id"))
    lock_keys = {"id", "lock_id", "plan_id", "case_no", "segment_id", "staff_id", "lock_date"}
    validated_locks = []
    for raw in locks:
        if not isinstance(raw, Mapping) or set(raw) != lock_keys or raw.get("case_no") != legacy_intent["case_no"]:
            raise ValueError("conflict_snapshot.active_lock_days row is invalid")
        row = dict(raw)
        row["id"] = _positive_int(row["id"], "lock id")
        row["lock_id"] = _positive_int(row["lock_id"], "lock_id")
        row["plan_id"] = _positive_int(row["plan_id"], "plan_id")
        row["segment_id"] = _positive_int(row["segment_id"], "segment_id")
        row["staff_id"] = _positive_int(row["staff_id"], "lock staff_id")
        row["lock_date"] = _date(row["lock_date"], "lock_date")
        staff_ids.add(row["staff_id"])
        validated_locks.append(row)
    caregiver_by_staff = {staff_id: f"caregiver:{index}" for index, staff_id in enumerate(sorted(staff_ids))}
    segments = [{"segment_ref": id_to_ref[row["id"]], "caregiver_ref": caregiver_by_staff[row["staff_id"]], "status": row["status"], "service_period": {"start": row["assigned_start_date"], "end": row["assigned_end_date"]}, "segment_kind": "formal", "lineage": {"original_segment_ref": None, "substitution_service_day": None}} for row in active]
    ownership = []
    occupancy = []
    seen_schedule_ids, seen_days = set(), set()
    for raw in schedules:
        row = dict(raw)
        row_id = _positive_int(row.get("id"), "schedule id")
        if row_id in seen_schedule_ids:
            raise ValueError("conflict_snapshot.assignment_schedule_days contains duplicate id")
        seen_schedule_ids.add(row_id)
        if row.get("case_no") != legacy_intent["case_no"] or row.get("is_work_day") not in {True, False} or row.get("is_double_pay") not in {True, False} or row.get("requires_review") not in {True, False} or row.get("notes") is not None and not isinstance(row.get("notes"), str):
            raise ValueError("conflict_snapshot schedule row is invalid")
        staff_id = _positive_int(row.get("staff_id"), "schedule staff_id")
        day = _date(row.get("work_date"), "schedule work_date")
        assignment_id = row.get("assignment_id")
        if assignment_id is None:
            if row["requires_review"] is not True:
                raise ValueError("legacy schedule row requires review")
            occupancy.append({"caregiver_ref": caregiver_by_staff[staff_id], "service_day": day, "source_kind": "legacy_unresolved", "source_segment_ref": None})
            continue
        assignment_id = _positive_int(assignment_id, "schedule assignment_id")
        if assignment_id not in id_to_ref or row["requires_review"] is not False:
            raise ValueError("schedule ownership is invalid")
        owner = next(item for item in active if item["id"] == assignment_id)
        if owner["staff_id"] != staff_id:
            raise ValueError("schedule staff ownership mismatch")
        if row["is_work_day"]:
            if day in seen_days:
                raise ValueError("daily ownership must be unique")
            seen_days.add(day)
            ownership.append({"service_day": day, "segment_ref": id_to_ref[assignment_id], "caregiver_ref": caregiver_by_staff[staff_id]})
        occupancy.append({"caregiver_ref": caregiver_by_staff[staff_id], "service_day": day, "source_kind": "formal_service", "source_segment_ref": id_to_ref[assignment_id]})
    ownership.sort(key=lambda row: row["service_day"])
    if not ownership:
        raise ValueError("snapshot has no assignment-owned work days")
    period = {"start": ownership[0]["service_day"], "end": ownership[-1]["service_day"]}
    if any(period["start"] + timedelta(days=offset) not in seen_days for offset in range((period["end"] - period["start"]).days + 1)):
        raise ValueError("daily ownership must cover the service period")
    for row in validated_locks:
        occupancy.append({"caregiver_ref": caregiver_by_staff[row["staff_id"]], "service_day": row["lock_date"], "source_kind": "waiting_deposit_lock", "source_segment_ref": None})
    locked_ids = set()
    historical_present = False
    for name in ("leave_substitution_events", "actual_hours_adjustments", "non_cancelled_payments", "active_settlements"):
        rows = facts[name]
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise ValueError(f"historical_facts.{name} must be a list of mappings")
        historical_present = historical_present or bool(rows)
        if name != "leave_substitution_events":
            for row in rows:
                assignment_id = _positive_int(row.get("assignment_id"), f"historical_facts.{name}.assignment_id")
                if assignment_id not in id_to_ref:
                    raise ValueError("historical facts reference unknown assignment")
                locked_ids.add(assignment_id)
    protected = [id_to_ref[assignment_id] for assignment_id in sorted(locked_ids, key=lambda value: id_to_ref[value])]
    history_state = "locked" if protected else "unlocked" if historical_present else "bootstrap"
    pure_intent = {"case_ref": f"case:{legacy_intent['case_no']}", "original_segment_ref": id_to_ref[original_id], "items": [{"item_ref": item["batch_item_index"], "service_day": _date(item["work_date"], "work_date"), "resolution": "defer" if item["resolution_type"] == "defer_following_assignments" else "substitute", "substitute_caregiver_ref": None if item["substitute_staff_id"] is None else caregiver_by_staff[item["substitute_staff_id"]]} for item in legacy_intent["items"]]}
    pure_lineage = [{"item_ref": item["batch_item_index"], "original_service_day_ref": f"service-day:{item['work_date']}"} for item in legacy_lineage.get("items", [])]
    before_plan = {"segments": segments, "daily_ownership": ownership, "service_period": period, "service_commitment": {"required_service_days": len(ownership), "hours_per_service_day": hours_per_day, "required_total_hours": Decimal(len(ownership)) * hours_per_day}}
    eligibility = {"database_current_date": database_current_date, "historical_protection": {"state": history_state, "protected_segment_refs": protected}, "occupancy": sorted(occupancy, key=lambda row: (row["service_day"], row["source_kind"], row["caregiver_ref"], row["source_segment_ref"] or ""))}
    transition_result = calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=pure_intent,
        item_lineage=pure_lineage,
        before_service_plan=before_plan,
        eligibility_facts=eligibility,
    )
    if not isinstance(transition_result, Mapping) or set(transition_result) != {"canonical_intent", "service_plan_transition", "canonical_eligibility"}:
        raise ValueError("transition returned an invalid contract")
    service_transition = transition_result["service_plan_transition"]
    if not isinstance(service_transition, Mapping) or set(service_transition) != {"before", "intent", "after", "impacts"}:
        raise ValueError("transition service plan returned an invalid contract")
    eligibility_result = transition_result["canonical_eligibility"]
    if not isinstance(eligibility_result, Mapping) or set(eligibility_result) != {"transition_valid", "applicable", "blocking_diagnostics", "review_diagnostics"}:
        raise ValueError("transition eligibility returned an invalid contract")
    if type(eligibility_result["transition_valid"]) is not bool or type(eligibility_result["applicable"]) is not bool or not isinstance(eligibility_result["blocking_diagnostics"], list) or not isinstance(eligibility_result["review_diagnostics"], list):
        raise ValueError("transition eligibility returned an invalid contract")
    if not eligibility_result["transition_valid"] and (service_transition["after"] is not None or service_transition["impacts"] is not None):
        raise ValueError("invalid transition must not return an after plan or impacts")
    if eligibility_result["transition_valid"] and (service_transition["after"] is None or service_transition["impacts"] is None):
        raise ValueError("valid transition must return an after plan and impacts")
    blocking = eligibility_result["blocking_diagnostics"]
    review = eligibility_result["review_diagnostics"]
    status = "blocked" if not eligibility_result["transition_valid"] or blocking else "requires_review" if review else "ready"
    result = {
        "contract_version": _BATCH_PREVIEW_CONTRACT_VERSION,
        "canonical_intent": deepcopy(transition_result["canonical_intent"]),
        "double_pay_preferences": [
            {
                "item_ref": item["batch_item_index"],
                "is_double_pay": item["is_double_pay"],
            }
            for item in legacy_intent["items"]
        ],
        "service_plan_transition": deepcopy(service_transition),
        "canonical_eligibility": deepcopy(eligibility_result),
        "status": status,
        "requires_confirmation": status != "blocked",
    }
    fingerprint_payload = {
        key: result[key]
        for key in (
            "canonical_intent",
            "double_pay_preferences",
            "service_plan_transition",
            "canonical_eligibility",
        )
    }
    return {**result, "preview_fingerprint": _fingerprinted(fingerprint_payload)["preview_fingerprint"]}
