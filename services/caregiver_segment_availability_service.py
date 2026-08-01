"""
================================================================================
檔案名稱: services/caregiver_segment_availability_service.py
功能說明: CaregiverSegmentAvailabilityInternalHelpers pure helper 實作
================================================================================
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List


_STRICT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _as_strict_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    if _STRICT_DATE_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except Exception as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    return parsed


def _as_strict_date_string(value: Any, field_name: str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return _as_strict_date(value, field_name).isoformat()
    raise ValueError(f"{field_name} must be YYYY-MM-DD")


def _as_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _daterange(start: date, end: date) -> List[date]:
    current = start
    dates: list[date] = []
    step = timedelta(days=1)
    while current <= end:
        dates.append(current)
        current += step
    return dates


def _normalise_candidate_staff_ids(candidate_staff_ids: Any) -> list[int]:
    if not isinstance(candidate_staff_ids, list):
        raise ValueError("candidate_staff_ids must be a list")
    cleaned: list[int] = []
    seen = set()
    for staff_id in candidate_staff_ids:
        staff_id = _as_positive_int(staff_id, "candidate_staff_id")
        if staff_id not in seen:
            seen.add(staff_id)
            cleaned.append(staff_id)
    return cleaned


def _ensure_no_unknown_fields(row: dict[str, Any], allowed_fields: set[str], name: str) -> None:
    extra_fields = set(row.keys()) - allowed_fields
    if extra_fields:
        raise ValueError(f"{name} contains unknown fields")


def _normalise_interval_start_end(
    draft: dict[str, Any],
    planned_start: date,
    planned_end: date,
) -> tuple[date, date]:
    segment_start = draft.get("start_date") or planned_start.isoformat()
    segment_end = draft.get("end_date") or planned_end.isoformat()
    return (
        _as_strict_date(segment_start, "segment_draft.start_date"),
        _as_strict_date(segment_end, "segment_draft.end_date"),
    )


def _extract_blocked_days(
    assignment_schedule_days: Any,
    active_lock_days: Any,
) -> tuple[
    dict[int, dict[date, set[str]]],
    list[tuple[int, date, str]],
]:
    def _active_lock_work_date(row: dict[str, Any], *, field_name: str) -> str:
        if "work_date" in row and "lock_date" in row:
            raise ValueError(f"{field_name} cannot provide both work_date and lock_date")
        if "work_date" in row:
            return row["work_date"]
        if "lock_date" in row:
            return row["lock_date"]
        raise ValueError(f"{field_name} must contain work_date")

    blocked: dict[int, dict[date, set[str]]] = {}
    requires_review: list[tuple[int, date, str]] = []

    for row in assignment_schedule_days:
        if not isinstance(row, dict):
            raise ValueError("assignment_schedule_days item must be an object")
        if "lock_date" in row:
            raise ValueError("assignment_schedule_days.item must use work_date, not lock_date")
        _ensure_no_unknown_fields(
            row,
            {"assignment_id", "staff_id", "work_date", "reason_code"},
            "assignment_schedule_days item",
        )
        staff_id_int = _as_positive_int(row.get("staff_id"), "assignment_schedule_days.staff_id")
        work_date = _as_strict_date(row.get("work_date"), "assignment_schedule_days.work_date")
        assignment_id = row.get("assignment_id")
        reason_code = row.get("reason_code", "schedule")
        if reason_code not in {"assignment", "schedule"}:
            raise ValueError("assignment_schedule_days.reason_code must be assignment or schedule")

        if assignment_id is None:
            reason_code = "requires_review"
        else:
            _as_positive_int(assignment_id, "assignment_schedule_days.assignment_id")
            reason_code = str(reason_code)

        staff_blocked = blocked.setdefault(staff_id_int, {})
        staff_blocked.setdefault(work_date, set()).add(reason_code)
        if reason_code == "requires_review":
            requires_review.append((staff_id_int, work_date, reason_code))

    for row in active_lock_days:
        if not isinstance(row, dict):
            raise ValueError("active_lock_days item must be an object")
        _ensure_no_unknown_fields(
            row,
            {"active_marker", "staff_id", "work_date", "lock_date"},
            "active_lock_days item",
        )
        staff_id = _as_positive_int(row.get("staff_id"), "active_lock_days.staff_id")
        work_date = _as_strict_date(
            _active_lock_work_date(row, field_name="active_lock_days.work_date"),
            "active_lock_days.work_date",
        )
        active_marker = row.get("active_marker")

        if active_marker is None:
            continue
        if type(active_marker) is not int:
            raise ValueError("active_lock_days.active_marker must be 0, null, or integer 1")
        if active_marker == 0:
            continue
        if active_marker != 1:
            raise ValueError("active_lock_days.active_marker must be 0, null, or integer 1")

        staff_blocked = blocked.setdefault(staff_id, {})
        staff_blocked.setdefault(work_date, set()).add("active_lock")

    return blocked, requires_review


def validate_segment_search_input(
    planned_start_date: Any,
    planned_end_date: Any,
    segment_count: Any,
    segment_drafts: Any,
    candidate_staff_ids: Any,
    assignment_schedule_days: Any = None,
    active_lock_days: Any = None,
) -> Dict[str, Any]:
    """
    僅做輸入驗證與純化輸出，不做 any allocation search。
    """
    if not isinstance(segment_count, int) or isinstance(segment_count, bool):
        raise ValueError("segment_count must be an integer of 1, 2, 3, or 4")
    if segment_count not in (1, 2, 3, 4):
        raise ValueError("segment_count must be 1, 2, 3, or 4")

    start_date = _as_strict_date(planned_start_date, "planned_start_date")
    end_date = _as_strict_date(planned_end_date, "planned_end_date")
    if start_date > end_date:
        raise ValueError("planned_start_date cannot be after planned_end_date")

    if segment_drafts is None:
        segment_drafts = []
    if not isinstance(segment_drafts, list):
        raise ValueError("segment_drafts must be a list")
    if len(segment_drafts) > segment_count:
        raise ValueError("segment_drafts cannot exceed segment_count")

    candidate_staffs = _normalise_candidate_staff_ids(candidate_staff_ids)

    canonical_drafts: list[dict[str, Any]] = []
    for idx, draft in enumerate(segment_drafts):
        if not isinstance(draft, dict):
            raise ValueError("segment_draft must be a dict")
        extra_keys = set(draft.keys()) - {"staff_id", "start_date", "end_date"}
        if extra_keys:
            raise ValueError("segment_draft has unknown fields")

        canonical: dict[str, Any] = {}
        if "staff_id" in draft:
            staff_id = _as_positive_int(draft["staff_id"], "segment_draft.staff_id")
            if staff_id not in candidate_staffs:
                raise ValueError("segment_draft.staff_id is not in candidate_staff_ids")
            canonical["staff_id"] = staff_id

        if "start_date" in draft:
            canonical["start_date"] = _as_strict_date(
                draft["start_date"],
                f"segment_drafts[{idx}].start_date",
            )
        if "end_date" in draft:
            canonical["end_date"] = _as_strict_date(
                draft["end_date"],
                f"segment_drafts[{idx}].end_date",
            )

        if "start_date" in canonical and "end_date" in canonical:
            if canonical["start_date"] > canonical["end_date"]:
                raise ValueError("segment_draft start_date cannot be after end_date")
        canonical_drafts.append(canonical)

    if assignment_schedule_days is None:
        assignment_schedule_days = []
    if active_lock_days is None:
        active_lock_days = []
    if not isinstance(assignment_schedule_days, list):
        raise ValueError("assignment_schedule_days must be a list")
    if not isinstance(active_lock_days, list):
        raise ValueError("active_lock_days must be a list")

    # schema validation required by spec: malformed occupancy facts must raise
    _extract_blocked_days(assignment_schedule_days, active_lock_days)

    return {
        "planned_start_date": start_date.isoformat(),
        "planned_end_date": end_date.isoformat(),
        "segment_count": segment_count,
        "segment_drafts": [
            {
                **({"staff_id": draft["staff_id"]} if "staff_id" in draft else {}),
                **({"start_date": draft["start_date"].isoformat()} if "start_date" in draft else {}),
                **({"end_date": draft["end_date"].isoformat()} if "end_date" in draft else {}),
            }
            for draft in canonical_drafts
        ],
        "candidate_staff_ids": list(candidate_staffs),
        "assignment_schedule_days": assignment_schedule_days,
        "active_lock_days": active_lock_days,
    }


def _normalise_conflict_set(
    conflicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen = set()
    unique: list[dict[str, Any]] = []
    for conflict in conflicts:
        key = (
            conflict["segment_index"],
            conflict["staff_id"],
            conflict["work_date"],
            conflict["reason_code"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(conflict)
    return sorted(
        unique,
        key=lambda item: (
            item["segment_index"],
            -1 if item["staff_id"] is None else item["staff_id"],
            item["work_date"],
            item["reason_code"],
        ),
    )


def _collect_free_intervals(
    staff_id: int,
    window_start: date,
    window_end: date,
    blocked_map: dict[int, dict[date, set[str]]],
) -> list[tuple[date, date]]:
    intervals: list[tuple[date, date]] = []
    blocked_dates = blocked_map.get(staff_id, {})
    cursor = window_start
    while cursor <= window_end:
        if cursor in blocked_dates:
            cursor += timedelta(days=1)
            continue
        segment_start = cursor
        cursor += timedelta(days=1)
        while cursor <= window_end and cursor not in blocked_dates:
            cursor += timedelta(days=1)
        segment_end = cursor - timedelta(days=1)
        intervals.append((segment_start, segment_end))
    return intervals


def _normalise_segment_drafts(
    canonical_input: Dict[str, Any],
    segment_count: int,
) -> list[dict[str, Any]]:
    raw_drafts = canonical_input["segment_drafts"]
    output = []
    for idx in range(segment_count):
        if idx < len(raw_drafts):
            output.append(raw_drafts[idx])
        else:
            output.append({})
    return output


def _collect_staff_pool(
    draft: dict[str, Any],
    candidate_staff_ids: list[int],
) -> list[int]:
    if "staff_id" in draft and draft["staff_id"] is not None:
        return [draft["staff_id"]]
    return candidate_staff_ids


def derive_segment_availability(
    planned_start_date: Any,
    planned_end_date: Any,
    segment_count: Any,
    segment_drafts: Any,
    candidate_staff_ids: Any,
    assignment_schedule_days: Any = None,
    active_lock_days: Any = None,
) -> Dict[str, Any]:
    """
    純函式，列舉 segments 可用候選、完整組合與衝突紀錄。
    """
    validated = validate_segment_search_input(
        planned_start_date,
        planned_end_date,
        segment_count,
        segment_drafts,
        candidate_staff_ids,
        assignment_schedule_days,
        active_lock_days,
    )

    planned_start = _as_strict_date(validated["planned_start_date"], "planned_start_date")
    planned_end = _as_strict_date(validated["planned_end_date"], "planned_end_date")
    segment_count = validated["segment_count"]
    staff_ids = list(validated["candidate_staff_ids"])
    drafts = _normalise_segment_drafts(validated, segment_count)

    blocked_map, requires_review = _extract_blocked_days(
        validated.get("assignment_schedule_days"),
        validated.get("active_lock_days"),
    )

    segment_candidates: list[dict[str, Any]] = []
    segment_interval_map: list[dict[int, list[tuple[date, date]]]] = []

    # candidate interval generation per segment
    for segment_index in range(segment_count):
        draft = drafts[segment_index]
        staff_pool = _collect_staff_pool(draft, staff_ids)
        draft_start, draft_end = _normalise_interval_start_end(draft, planned_start, planned_end)
        fixed_end = _as_strict_date(draft["end_date"], "segment_draft.end_date") if draft.get("end_date") is not None else None

        window_start = max(draft_start, planned_start)
        window_end = min(
            fixed_end if fixed_end is not None else draft_end,
            planned_end,
        )
        interval_map: dict[int, list[tuple[date, date]]] = {}
        if window_start > window_end:
            segment_interval_map.append(interval_map)
            continue
        for staff_id in staff_pool:
            intervals = _collect_free_intervals(
                staff_id=staff_id,
                window_start=window_start,
                window_end=window_end,
                blocked_map=blocked_map,
            )
            if not intervals:
                continue
            interval_map[staff_id] = intervals
            for start_at, end_at in intervals:
                segment_candidates.append(
                    {
                        "segment_index": segment_index,
                        "staff_id": staff_id,
                        "start_date": start_at.isoformat(),
                        "end_date": end_at.isoformat(),
                    }
                )
        segment_interval_map.append(interval_map)

    # enumerate complete combinations (contiguous no overlap and no gap)
    complete_combinations: list[list[dict[str, Any]]] = []

    def _search(
        segment_idx: int,
        cursor: date,
        used_staff: set[int],
        selected: list[dict[str, Any]],
    ) -> None:
        if segment_idx == segment_count:
            if cursor > planned_end:
                complete_combinations.append([dict(item) for item in selected])
            return

        if cursor > planned_end:
            return

        draft = drafts[segment_idx]
        staff_pool = _collect_staff_pool(draft, staff_ids)
        fixed_staff = draft.get("staff_id")
        fixed_start = _as_strict_date(draft["start_date"], "segment_draft.start_date") if draft.get("start_date") is not None else None
        fixed_end = _as_strict_date(draft["end_date"], "segment_draft.end_date") if draft.get("end_date") is not None else None

        if fixed_start is not None and fixed_start < cursor:
            return

        for staff_id in staff_pool:
            if fixed_staff is not None and staff_id != fixed_staff:
                continue
            if staff_id in used_staff:
                continue

            intervals = segment_interval_map[segment_idx].get(staff_id, [])
            for seg_start, seg_end in intervals:
                if seg_start > cursor:
                    break
                if seg_end < cursor:
                    continue

                actual_start = cursor
                if fixed_start is not None and actual_start != fixed_start:
                    continue
                if fixed_end is not None:
                    if fixed_end < actual_start or fixed_end > seg_end:
                        continue
                    segment_choice = {
                        "segment_index": segment_idx,
                        "staff_id": staff_id,
                        "start_date": actual_start.isoformat(),
                        "end_date": fixed_end.isoformat(),
                    }
                    selected.append(segment_choice)
                    used_staff.add(staff_id)
                    _search(segment_idx + 1, fixed_end + timedelta(days=1), used_staff, selected)
                    used_staff.remove(staff_id)
                    selected.pop()
                    continue

                for end_date in _daterange(actual_start, seg_end):
                    segment_choice = {
                        "segment_index": segment_idx,
                        "staff_id": staff_id,
                        "start_date": actual_start.isoformat(),
                        "end_date": end_date.isoformat(),
                    }
                    selected.append(segment_choice)
                    used_staff.add(staff_id)
                    _search(segment_idx + 1, end_date + timedelta(days=1), used_staff, selected)
                    used_staff.remove(staff_id)
                    selected.pop()

    _search(0, planned_start, set(), [])

    # canonical sorting
    def _combo_sort_key(combo: list[dict[str, Any]]) -> tuple:
        keys: list[Any] = []
        for item in combo:
            keys.extend(
                [
                    item["start_date"],
                    item["staff_id"],
                    item["end_date"],
                ]
            )
        return tuple(keys)

    complete_combinations.sort(key=_combo_sort_key)

    # conflicts from blocked reasons and draft constraints
    conflicts: list[dict[str, Any]] = []

    for segment_index, draft in enumerate(drafts):
        seg_start, seg_end = _normalise_interval_start_end(draft, planned_start, planned_end)
        staff_for_segment = _collect_staff_pool(draft, staff_ids)

        for work_date in _daterange(seg_start, seg_end):
            if work_date < planned_start or work_date > planned_end:
                conflicts.append(
                    {
                        "segment_index": segment_index,
                        "staff_id": draft.get("staff_id"),
                        "work_date": work_date.isoformat(),
                        "reason_code": "outside_case_period",
                    }
                )
                continue
            for staff_id in staff_for_segment:
                reasons = blocked_map.get(staff_id, {}).get(work_date, set())
                for reason_code in sorted(reasons):
                    conflicts.append(
                        {
                            "segment_index": segment_index,
                            "staff_id": staff_id,
                            "work_date": work_date.isoformat(),
                            "reason_code": reason_code,
                        }
                    )

            for reviewed_staff_id, reviewed_date, reason_code in requires_review:
                if reviewed_date == work_date and reviewed_staff_id in staff_for_segment:
                    conflicts.append(
                        {
                            "segment_index": segment_index,
                            "staff_id": reviewed_staff_id,
                            "work_date": reviewed_date.isoformat(),
                            "reason_code": reason_code,
                        }
                    )

        draft_interval: set[date] = set(_daterange(seg_start, seg_end))
        candidate_cover: set[date] = set()
        for staff_id in staff_for_segment:
            for interval_start, interval_end in segment_interval_map[segment_index].get(staff_id, []):
                for day in _daterange(interval_start, interval_end):
                    candidate_cover.add(day)
        if candidate_cover:
            gap_dates = draft_interval - candidate_cover
            for gap_date in sorted(gap_dates):
                conflicts.append(
                    {
                        "segment_index": segment_index,
                        "staff_id": draft.get("staff_id"),
                        "work_date": gap_date.isoformat(),
                        "reason_code": "coverage_gap",
                    }
                )

    # detect overlaps between fixed draft intervals
    fixed_draft_intervals = []
    for idx, draft in enumerate(drafts):
        if "start_date" in draft and "end_date" in draft:
            fixed_draft_intervals.append(
                (
                    idx,
                    _as_strict_date(draft["start_date"], "segment_draft.start_date"),
                    _as_strict_date(draft["end_date"], "segment_draft.end_date"),
                )
            )
    for i in range(len(fixed_draft_intervals)):
        idx_a, s1, e1 = fixed_draft_intervals[i]
        staff_a = drafts[idx_a].get("staff_id")
        for j in range(i + 1, len(fixed_draft_intervals)):
            idx_b, s2, e2 = fixed_draft_intervals[j]
            staff_b = drafts[idx_b].get("staff_id")
            overlap_start = max(s1, s2)
            overlap_end = min(e1, e2)
            if overlap_start <= overlap_end:
                for conflict_day in _daterange(overlap_start, overlap_end):
                    conflicts.append(
                        {
                            "segment_index": idx_a,
                            "staff_id": staff_a,
                            "work_date": conflict_day.isoformat(),
                            "reason_code": "draft_overlap",
                        }
                    )
                    conflicts.append(
                        {
                            "segment_index": idx_b,
                            "staff_id": staff_b,
                            "work_date": conflict_day.isoformat(),
                            "reason_code": "draft_overlap",
                        }
                    )

    # if no full solution, preserve partial traces with deterministic coverage gaps
    if not complete_combinations:
        for segment_index, draft in enumerate(drafts):
            segment_start, segment_end = _normalise_interval_start_end(draft, planned_start, planned_end)
            for gap_day in _daterange(segment_start, segment_end):
                exists = any(
                    item["segment_index"] == segment_index and item["work_date"] == gap_day.isoformat()
                    for item in conflicts
                )
                if not exists:
                    conflicts.append(
                        {
                            "segment_index": segment_index,
                            "staff_id": None,
                            "work_date": gap_day.isoformat(),
                            "reason_code": "coverage_gap",
                        }
                    )

    return {
        "validated_input": {
            "planned_start_date": validated["planned_start_date"],
            "planned_end_date": validated["planned_end_date"],
            "segment_count": validated["segment_count"],
            "segment_drafts": validated["segment_drafts"],
            "candidate_staff_ids": validated["candidate_staff_ids"],
        },
        "complete_combinations": complete_combinations,
        "segment_candidates": sorted(
            segment_candidates,
            key=lambda item: (item["segment_index"], item["staff_id"], item["start_date"], item["end_date"]),
        ),
        "conflicts": _normalise_conflict_set(conflicts),
    }
