import ast
import inspect
import json
from decimal import Decimal
from datetime import date, datetime, timedelta

import pytest

from services.multi_caregiver_assignment_rules import (
    AssignmentPlanTransitionConflict,
    validate_assignment_plan_transition,
    validate_non_overlapping_assignment_interval,
)
import services.multi_caregiver_assignment_rules as rules


def _assignment(assignment_id, start, end, status="active"):
    return {
        "id": assignment_id,
        "status": status,
        "assigned_start_date": start,
        "assigned_end_date": end,
    }


def test_adjacent_service_intervals_are_valid():
    interval = validate_non_overlapping_assignment_interval(
        "2026-06-11",
        "2026-06-20",
        [_assignment(1, "2026-06-01", "2026-06-10")],
    )

    assert interval == (date(2026, 6, 11), date(2026, 6, 20))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-06-10", "2026-06-15"),
        ("2026-06-05", "2026-06-10"),
        ("2026-06-01", "2026-06-20"),
        ("2026-06-05", "2026-06-08"),
    ],
)
def test_any_shared_service_date_is_rejected(start, end):
    with pytest.raises(ValueError, match="overlaps assignment 1"):
        validate_non_overlapping_assignment_interval(
            start,
            end,
            [_assignment(1, "2026-06-01", "2026-06-10")],
        )


def test_cancelled_and_current_assignment_do_not_reserve_dates():
    interval = validate_non_overlapping_assignment_interval(
        "2026-06-01",
        "2026-06-10",
        [
            _assignment(1, "2026-06-01", "2026-06-10"),
            _assignment(2, "2026-06-01", "2026-06-10", status="cancelled"),
        ],
        candidate_assignment_id=1,
    )

    assert interval == (date(2026, 6, 1), date(2026, 6, 10))


def test_active_assignment_without_complete_dates_requires_review():
    with pytest.raises(ValueError, match="requires review"):
        validate_non_overlapping_assignment_interval(
            "2026-06-11",
            "2026-06-20",
            [_assignment(1, None, "2026-06-10")],
        )

    with pytest.raises(ValueError, match="requires review"):
        validate_non_overlapping_assignment_interval(
            "2026-06-11",
            "2026-06-20",
            [_assignment(1, "2026-06-01", None)],
        )


def test_invalid_candidate_range_is_rejected():
    with pytest.raises(ValueError, match="must not be after"):
        validate_non_overlapping_assignment_interval(
            "2026-06-20",
            "2026-06-11",
            [],
        )


def test_datetime_objects_and_date_objects_supported():
    start_dt = datetime(2026, 6, 11, 9, 0, 0)
    end_dt = datetime(2026, 6, 20, 18, 0, 0)

    interval = validate_non_overlapping_assignment_interval(
        start_dt,
        end_dt,
        [_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
    )

    assert interval == (date(2026, 6, 11), date(2026, 6, 20))


def test_invalid_date_formats_and_missing_candidate_dates():
    with pytest.raises(ValueError, match="candidate_start_date is required and must be an ISO date"):
        validate_non_overlapping_assignment_interval(None, "2026-06-10", [])

    with pytest.raises(ValueError, match="candidate_end_date must be an ISO date"):
        validate_non_overlapping_assignment_interval("2026-06-01", "invalid-date", [])


def test_existing_assignment_invalid_range_raises_error():
    with pytest.raises(ValueError, match="invalid service date range"):
        validate_non_overlapping_assignment_interval(
            "2026-06-11",
            "2026-06-20",
            [_assignment(1, "2026-06-10", "2026-06-01")],
        )


def _plan_assignment(
    assignment_id,
    start,
    end,
    *,
    staff_id=11,
    status="active",
    kind="formal",
    original_assignment_id=None,
    substitution_work_date=None,
):
    return {
        "id": assignment_id,
        "case_no": "CASE-1",
        "staff_id": staff_id,
        "status": status,
        "assigned_start_date": start,
        "assigned_end_date": end,
        "kind": kind,
        "original_assignment_id": original_assignment_id,
        "substitution_work_date": substitution_work_date,
    }


def _validate(
    proposed,
    *,
    current=None,
    operation="segment_reconfigure",
    effective=date(2026, 6, 5),
    database_current=None,
    current_interval=(date(2026, 6, 1), date(2026, 6, 10)),
    proposed_interval=(date(2026, 6, 1), date(2026, 6, 10)),
    historical_fact_state="locked",
):
    if database_current is None:
        database_current = min(date(2026, 6, 5), effective)
    return validate_assignment_plan_transition(
        case_no="CASE-1",
        database_current_date=database_current,
        effective_date=effective,
        current_case_start_date=current_interval[0],
        current_case_end_date=current_interval[1],
        proposed_case_start_date=proposed_interval[0],
        proposed_case_end_date=proposed_interval[1],
        operation_kind=operation,
        current_assignments=current
        or [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
        proposed_assignments=proposed,
        historical_fact_state=historical_fact_state,
    )


def _assert_json_safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_safe(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AssertionError(f"json key type not supported: {type(key)!r}")
            _assert_json_safe(item)
        return
    raise AssertionError(f"payload contains unsupported type: {type(value)!r}")


@pytest.mark.parametrize(
    "details",
    [
        {"date": date(2026, 6, 1)},
        {"datetime": datetime(2026, 6, 1, 9, 30)},
        {"decimal": Decimal("1.25")},
        {"set": {"item"}},
        {"tuple": ("item",)},
        {1: "non-string key"},
        {"nonfinite": float("nan")},
        {"nonfinite": float("inf")},
    ],
)
def test_transition_conflict_details_reject_non_json_values(details):
    with pytest.raises(ValueError) as exc:
        AssignmentPlanTransitionConflict("invalid_details", details)

    assert type(exc.value) is ValueError


def test_transition_conflict_details_are_defensively_copied_json_values():
    original = {"nested": {"items": [1, {"value": "kept"}]}}
    conflict = AssignmentPlanTransitionConflict("conflict", original)
    original["nested"]["items"][1]["value"] = "mutated"

    assert conflict.details == {"nested": {"items": [1, {"value": "kept"}]}}
    assert (
        json.dumps(conflict.details, sort_keys=True, separators=(",", ":"))
        == '{"nested":{"items":[1,{"value":"kept"}]}}'
    )


@pytest.mark.parametrize(
    ("current", "proposed"),
    [
        ([{"id": True}], [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]),
        (
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            [{"id": True}],
        ),
        (object(), [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]),
        (
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            object(),
        ),
    ],
)
def test_malformed_transition_rows_raise_plain_value_error_not_typed_conflict(
    current, proposed
):
    with pytest.raises(ValueError) as exc:
        _validate(
            proposed,
            current=current,
            operation="defer_following_assignments",
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )

    assert type(exc.value) is ValueError


def test_future_case_shortening_reports_removed_dates_deterministically():
    result = _validate(
        [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 7))],
        effective=date(2026, 6, 8),
        database_current=date(2026, 6, 5),
        proposed_interval=(date(2026, 6, 1), date(2026, 6, 7)),
    )

    assert result["proposed_case_end_date"] == date(2026, 6, 7)
    assert result["removed_future_dates"] == [
        "2026-06-08",
        "2026-06-09",
        "2026-06-10",
    ]


def test_historical_row_before_proposed_start_is_retained_without_effective_ownership():
    current = [_plan_assignment(1, date(2026, 8, 3), date(2026, 8, 5))]

    result = _validate(
        current,
        current=current,
        effective=date(2026, 8, 4),
        database_current=date(2026, 8, 4),
        current_interval=(date(2026, 8, 3), date(2026, 8, 5)),
        proposed_interval=(date(2026, 8, 4), date(2026, 8, 5)),
    )

    assert result["after_assignments"][0]["assigned_start_date"] == date(2026, 8, 3)
    assert "2026-08-03" not in result["ownership_by_date"]
    assert result["ownership_by_date"] == {
        "2026-08-04": "1",
        "2026-08-05": "1",
    }
    assert result["removed_future_dates"] == []


def test_historical_row_before_proposed_start_still_rejects_rewrite():
    current = [_plan_assignment(1, date(2026, 8, 3), date(2026, 8, 5))]
    rewritten = _plan_assignment(
        1,
        date(2026, 8, 3),
        date(2026, 8, 5),
        staff_id=99,
    )

    with pytest.raises(ValueError, match="changed staff_id"):
        _validate(
            [rewritten],
            current=current,
            effective=date(2026, 8, 4),
            database_current=date(2026, 8, 4),
            current_interval=(date(2026, 8, 3), date(2026, 8, 5)),
            proposed_interval=(date(2026, 8, 4), date(2026, 8, 5)),
        )


def test_case_shortening_cannot_remove_historical_dates():
    with pytest.raises(ValueError, match="before database_current_date"):
        _validate(
            [_plan_assignment(1, date(2026, 6, 4), date(2026, 6, 10))],
            effective=date(2026, 6, 4),
            database_current=date(2026, 6, 5),
            proposed_interval=(date(2026, 6, 4), date(2026, 6, 10)),
        )


def test_future_case_extension_preserves_history():
    result = _validate(
        [
            _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
            _plan_assignment("extension", date(2026, 6, 11), date(2026, 6, 13)),
        ],
        effective=date(2026, 6, 11),
        database_current=date(2026, 6, 5),
        proposed_interval=(date(2026, 6, 1), date(2026, 6, 13)),
    )

    assert result["after_assignments"][0]["assigned_start_date"] == date(2026, 6, 1)
    assert result["ownership_by_date"]["2026-06-11"] == "extension"
    assert result["removed_future_dates"] == []


@pytest.mark.parametrize(
    ("current", "message"),
    [
        (
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
                _plan_assignment(2, date(2026, 6, 6), date(2026, 6, 10)),
            ],
            "current assignments missing ownership",
        ),
        (
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 6)),
                _plan_assignment(2, date(2026, 6, 6), date(2026, 6, 10)),
            ],
            "overlapping",
        ),
        (
            [_plan_assignment(1, date(2026, 5, 31), date(2026, 6, 10))],
            "current case boundaries",
        ),
    ],
)
def test_current_interval_rejects_gap_overlap_and_overflow(current, message):
    with pytest.raises(ValueError, match=message):
        _validate(
            [_plan_assignment("new", date(2026, 6, 1), date(2026, 6, 10))],
            current=current,
            effective=date(2026, 6, 1),
        )


def test_future_legacy_case_without_assignments_can_bootstrap_formal_ownership():
    result = validate_assignment_plan_transition(
        case_no="CASE-1",
        database_current_date=date(2026, 7, 30),
        effective_date=date(2026, 10, 10),
        current_case_start_date=date(2026, 10, 10),
        current_case_end_date=date(2026, 11, 9),
        proposed_case_start_date=date(2026, 10, 10),
        proposed_case_end_date=date(2026, 11, 9),
        operation_kind="segment_reconfigure",
        current_assignments=[],
        proposed_assignments=[
            _plan_assignment(
                "new-1",
                date(2026, 10, 10),
                date(2026, 11, 9),
                status="planned",
            )
        ],
    )

    assert result["before_assignments"] == []
    assert result["ownership_by_date"]["2026-10-10"] == "new-1"
    assert result["ownership_by_date"]["2026-11-09"] == "new-1"


def test_transition_output_stays_stable_across_interval_change():
    proposed = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 7)),
    ]

    first = _validate(
        proposed,
        effective=date(2026, 6, 8),
        proposed_interval=(date(2026, 6, 1), date(2026, 6, 7)),
    )
    second = _validate(
        list(reversed(proposed)),
        effective=date(2026, 6, 8),
        proposed_interval=(date(2026, 6, 1), date(2026, 6, 7)),
    )

    assert first == second
    assert first["removed_future_dates"] == sorted(first["removed_future_dates"])


def test_database_current_date_before_case_allows_future_effective_date():
    result = _validate(
        [_plan_assignment("future", date(2026, 6, 1), date(2026, 6, 10))],
        effective=date(2026, 6, 1),
        database_current=date(2026, 5, 20),
    )

    assert result["effective_date"] == date(2026, 6, 1)
    assert len(result["ownership_by_date"]) == 10


def test_database_current_date_within_case_preserves_history_and_allows_future_change():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
        _plan_assignment(2, date(2026, 6, 5), date(2026, 6, 10), staff_id=12),
    ]
    result = _validate(
        [
            current[0],
            _plan_assignment(2, date(2026, 6, 5), date(2026, 6, 7), staff_id=12),
            _plan_assignment("future", date(2026, 6, 8), date(2026, 6, 10), staff_id=13),
        ],
        current=current,
        effective=date(2026, 6, 8),
        database_current=date(2026, 6, 5),
    )

    assert result["after_assignments"][0]["assigned_start_date"] == date(2026, 6, 1)
    assert result["ownership_by_date"]["2026-06-08"] == "future"


def test_database_current_date_after_case_rejects_without_future_effective_date():
    with pytest.raises(ValueError, match="before database_current_date"):
        _validate(
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            effective=date(2026, 6, 10),
            database_current=date(2026, 6, 11),
        )


@pytest.mark.parametrize("segment_count", [1, 2, 3, 4])
def test_transition_accepts_one_to_four_assignment_rows(segment_count):
    starts = [date(2026, 6, 1) + timedelta(days=index) for index in range(segment_count)]
    ends = starts[1:] + [date(2026, 6, 10) + timedelta(days=1)]
    proposed = [
        _plan_assignment(
            f"new-{index}",
            start,
            end - timedelta(days=1),
            staff_id=10 + index,
        )
        for index, (start, end) in enumerate(zip(starts, ends))
    ]

    result = _validate(proposed, effective=date(2026, 6, 1))

    assert (
        len([row for row in result["after_assignments"] if row["status"] != "cancelled"])
        == segment_count
    )
    assert len(result["ownership_by_date"]) == 10


def test_transition_rejects_fifth_assignment_row():
    proposed = [
        _plan_assignment(f"new-{index}", day, day, staff_id=20 + index)
        for index, day in enumerate(
            [
                date(2026, 6, 1),
                date(2026, 6, 2),
                date(2026, 6, 3),
                date(2026, 6, 4),
                date(2026, 6, 5),
            ]
        )
    ]
    proposed[-1]["assigned_end_date"] = date(2026, 6, 10)

    with pytest.raises(ValueError, match="at most 4"):
        _validate(proposed, effective=date(2026, 6, 1))


@pytest.mark.parametrize(
    ("proposed", "message"),
    [
        (
            [
                    _plan_assignment("first", date(2026, 6, 1), date(2026, 6, 4)),
                _plan_assignment("new", date(2026, 6, 6), date(2026, 6, 10)),
            ],
            "missing ownership",
        ),
        (
            [
                    _plan_assignment("first", date(2026, 6, 1), date(2026, 6, 6)),
                _plan_assignment("new", date(2026, 6, 6), date(2026, 6, 10)),
            ],
            "overlapping",
        ),
        (
            [_plan_assignment("new", date(2026, 5, 31), date(2026, 6, 10))],
            "case boundaries",
        ),
        (
            [_plan_assignment("new", date(2026, 6, 1), date(2026, 6, 11))],
            "case boundaries",
        ),
    ],
)
def test_transition_rejects_gap_overlap_and_case_boundary_overflow(proposed, message):
    with pytest.raises(ValueError, match=message):
        _validate(proposed, effective=date(2026, 6, 1))


def test_omitted_future_assignment_is_cancelled_and_does_not_keep_ownership():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
        _plan_assignment(2, date(2026, 6, 5), date(2026, 6, 10), staff_id=12),
    ]
    result = _validate(
        [
            _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
            _plan_assignment("replacement", date(2026, 6, 5), date(2026, 6, 10), staff_id=13),
        ],
        current=current,
    )

    assert result["ownership_by_date"]["2026-06-05"] == "replacement"
    assert result["cancelled"][0]["before"]["id"] == 2
    assert result["cancelled"][0]["after"]["status"] == "cancelled"
    assert next(row for row in result["after_assignments"] if row["id"] == 2)["status"] == "cancelled"


def test_effective_date_preserves_today_and_all_earlier_ownership():
    proposed = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
        _plan_assignment("early-replacement", date(2026, 6, 5), date(2026, 6, 5), staff_id=12),
        _plan_assignment("future", date(2026, 6, 6), date(2026, 6, 10), staff_id=13),
    ]

    with pytest.raises(ValueError, match="before effective_date"):
        _validate(proposed, effective=date(2026, 6, 6))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "single_day_substitute"),
        ("original_assignment_id", 99),
        ("substitution_work_date", date(2026, 6, 3)),
    ],
)
def test_historical_assignment_substitution_metadata_is_immutable(field, value):
    proposed = _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))
    proposed[field] = value
    if field == "kind":
        proposed["original_assignment_id"] = 99
        proposed["substitution_work_date"] = date(2026, 6, 3)

    with pytest.raises(ValueError):
        _validate([proposed])


def test_single_day_substitute_requires_independent_prefix_substitute_suffix():
    proposed = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
        _plan_assignment(
            "substitute",
            date(2026, 6, 5),
            date(2026, 6, 5),
            staff_id=99,
            kind="single_day_substitute",
            original_assignment_id=1,
            substitution_work_date=date(2026, 6, 5),
        ),
        _plan_assignment("suffix", date(2026, 6, 6), date(2026, 6, 10)),
    ]

    result = _validate(proposed, operation="single_day_substitute")

    assert result["after_assignments"][0]["assigned_end_date"] == date(2026, 6, 4)
    assert result["ownership_by_date"]["2026-06-05"] == "substitute"
    assert result["ownership_by_date"]["2026-06-06"] == "suffix"
    assert result["after_assignments"][0]["staff_id"] == 11
    assert result["after_assignments"][2]["staff_id"] == 11


def test_segment_reconfigure_retains_existing_historical_substitute_row():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
        _plan_assignment(
            2,
            date(2026, 6, 5),
            date(2026, 6, 5),
            staff_id=99,
            kind="single_day_substitute",
            original_assignment_id=1,
            substitution_work_date=date(2026, 6, 5),
        ),
        _plan_assignment(3, date(2026, 6, 6), date(2026, 6, 10)),
    ]
    proposed = [
        current[0],
        current[1],
        _plan_assignment(3, date(2026, 6, 6), date(2026, 6, 7)),
        _plan_assignment("future", date(2026, 6, 8), date(2026, 6, 10), staff_id=12),
    ]

    result = _validate(proposed, current=current, effective=date(2026, 6, 8))

    assert result["ownership_by_date"]["2026-06-05"] == "2"
    assert result["ownership_by_date"]["2026-06-08"] == "future"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda rows: rows.__setitem__(1, {**rows[1], "id": 1}),
        lambda rows: rows.__setitem__(1, {**rows[1], "original_assignment_id": 999}),
        lambda rows: rows.__setitem__(1, {**rows[1], "substitution_work_date": date(2026, 6, 6)}),
        lambda rows: rows.__setitem__(2, {**rows[2], "staff_id": 88}),
        lambda rows: rows.__setitem__(2, {**rows[2], "id": 1}),
    ],
)
def test_single_day_substitute_rejects_reuse_and_ownership_errors(mutator):
    rows = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
        _plan_assignment(
            "substitute",
            date(2026, 6, 5),
            date(2026, 6, 5),
            staff_id=99,
            kind="single_day_substitute",
            original_assignment_id=1,
            substitution_work_date=date(2026, 6, 5),
        ),
        _plan_assignment("suffix", date(2026, 6, 6), date(2026, 6, 10)),
    ]
    mutator(rows)

    with pytest.raises(ValueError):
        _validate(rows, operation="single_day_substitute")


@pytest.mark.parametrize(
    "bad_row",
    [
        [],
        {"id": True},
        {**_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)), "unknown": 1},
    ],
)
def test_transition_requires_strict_assignment_mappings(bad_row):
    with pytest.raises(ValueError):
        _validate([bad_row])


def test_transition_output_is_stable_and_contains_no_internal_id_kind():
    proposed = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
        _plan_assignment("new", date(2026, 6, 5), date(2026, 6, 10), staff_id=12),
    ]

    first = _validate(proposed)
    second = _validate(list(reversed(proposed)))

    assert first == second
    assert "id_kind" not in repr(first)
    assert list(first["ownership_by_date"]) == sorted(first["ownership_by_date"])


def test_transition_has_no_db_clock_environment_or_filesystem_capability():
    tree = ast.parse(inspect.getsource(rules))
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }

    assert imported_roots.isdisjoint({"os", "pathlib", "sqlite3", "pymysql", "requests"})
    assert calls.isdisjoint(
        {
            "date.today",
            "datetime.now",
            "datetime.utcnow",
            "open",
            "get_connection",
        }
    )


@pytest.mark.parametrize(
    ("defer_days", "expected_end"),
    [
        (1, date(2026, 6, 11)),
        (2, date(2026, 6, 12)),
        (3, date(2026, 6, 13)),
    ],
)
def test_defer_following_assignments_extends_single_assignment_multiday(defer_days, expected_end):
    current = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]
    result = _validate(
        [_plan_assignment(1, date(2026, 6, 1), expected_end)],
        current=current,
        operation="defer_following_assignments",
        effective=date(2026, 6, 5),
        proposed_interval=(date(2026, 6, 1), date(2026, 6, 10 + defer_days)),
    )

    assert result["operation_kind"] == "defer_following_assignments"
    assert result["after_assignments"][0]["assigned_end_date"] == expected_end
    assert result["ownership_by_date"][f"2026-06-{10 + defer_days:02d}"] == "1"


def test_defer_following_assignments_preserves_affected_start_and_later_row_shift_with_defer_days():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
        _plan_assignment(
            2,
            date(2026, 6, 6),
            date(2026, 6, 7),
            staff_id=12,
        ),
        _plan_assignment(
            3,
            date(2026, 6, 8),
            date(2026, 6, 10),
            staff_id=13,
        ),
    ]
    proposed = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 7)),
        _plan_assignment(
            2,
            date(2026, 6, 8),
            date(2026, 6, 9),
            staff_id=12,
        ),
        _plan_assignment(
            3,
            date(2026, 6, 10),
            date(2026, 6, 12),
            staff_id=13,
        ),
    ]

    result = _validate(
        proposed,
        current=current,
        operation="defer_following_assignments",
        effective=date(2026, 6, 3),
        proposed_interval=(date(2026, 6, 1), date(2026, 6, 12)),
    )

    after_assignments = [row for row in result["after_assignments"] if row["status"] != "cancelled"]
    assert [row["id"] for row in after_assignments] == [1, 2, 3]
    assert [row["staff_id"] for row in after_assignments] == [11, 12, 13]
    assert after_assignments[0]["assigned_start_date"] == date(2026, 6, 1)
    assert after_assignments[0]["assigned_end_date"] == date(2026, 6, 7)
    assert after_assignments[1]["assigned_start_date"] == date(2026, 6, 8)
    assert after_assignments[1]["assigned_end_date"] == date(2026, 6, 9)
    assert after_assignments[2]["assigned_start_date"] == date(2026, 6, 10)
    assert after_assignments[2]["assigned_end_date"] == date(2026, 6, 12)
    assert [row["status"] for row in after_assignments] == ["active", "active", "active"]
    assert result["ownership_by_date"]["2026-06-10"] == "3"


def test_defer_following_assignments_rejects_zero_or_negative_defer_days():
    current = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 5),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )
    assert exc.value.code == "defer_affected_assignment_missing"
    assert exc.value.details == {"assignment_ids": [1]}
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 9))],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 5),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 9)),
        )
    assert exc.value.code == "defer_days_not_positive"
    assert exc.value.details == {
        "current_case_end_date": "2026-06-10",
        "proposed_case_end_date": "2026-06-09",
        "derived_defer_days": -1,
    }


@pytest.mark.parametrize(
    ("proposed", "proposed_interval", "expected_code"),
    [
        (
            [_plan_assignment("new", date(2026, 6, 2), date(2026, 6, 10))],
            (date(2026, 6, 2), date(2026, 6, 10)),
            "defer_case_start_changed",
        ),
        (
            [_plan_assignment("new", date(2026, 6, 1), date(2026, 6, 10))],
            (date(2026, 6, 1), date(2026, 6, 10)),
            "defer_days_not_positive",
        ),
        (
            [_plan_assignment("new", date(2026, 6, 1), date(2026, 6, 11))],
            (date(2026, 6, 1), date(2026, 6, 11)),
            "defer_assignment_created",
        ),
        (
            [],
            (date(2026, 6, 1), date(2026, 6, 11)),
            "defer_assignment_cancelled",
        ),
    ],
)
def test_defer_conflict_priority_is_deterministic_for_combined_invalid_plans(
    proposed, proposed_interval, expected_code
):
    current = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]

    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            proposed,
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 5),
            proposed_interval=proposed_interval,
        )

    assert exc.value.code == expected_code


def test_defer_following_assignments_rejects_cancelled_row_omission_before_count():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
        _plan_assignment(2, date(2026, 6, 1), date(2026, 6, 1), status="cancelled"),
    ]
    proposed = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 11))]

    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            proposed,
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 5),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )

    assert exc.value.code == "defer_assignment_row_count_changed"
    assert exc.value.details == {"current_row_count": 2, "proposed_row_count": 1}


def test_defer_following_assignments_rejects_case_start_change_with_typed_conflict():
    current = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 11))],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 5),
            proposed_interval=(date(2026, 6, 2), date(2026, 6, 11)),
        )
    assert exc.value.code == "defer_case_start_changed"
    assert exc.value.details == {
        "current_case_start_date": "2026-06-01",
        "proposed_case_start_date": "2026-06-02",
    }


def test_defer_following_assignments_rejects_assignment_created_with_typed_conflict():
    current = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 11)),
                _plan_assignment("new", date(2026, 6, 12), date(2026, 6, 12)),
            ],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 5),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )
    assert exc.value.code == "defer_assignment_created"
    assert exc.value.details == {"proposed_new_keys": ["new"]}


def test_defer_following_assignments_rejects_assignment_cancelled_with_typed_conflict():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
        _plan_assignment(2, date(2026, 6, 6), date(2026, 6, 10), staff_id=12),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
                _plan_assignment(2, date(2026, 6, 6), date(2026, 6, 10), staff_id=12, status="cancelled"),
            ],
                current=current,
                operation="defer_following_assignments",
                effective=date(2026, 6, 3),
                proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
            )
    assert exc.value.code == "defer_assignment_cancelled"
    assert exc.value.details == {"cancelled_assignment_ids": [2]}


def test_defer_following_assignments_rejects_row_count_changed_with_typed_conflict():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
        _plan_assignment(
            2,
            date(2026, 6, 6),
            date(2026, 6, 10),
            staff_id=12,
        ),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 7)),
            ],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 3),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 12)),
        )
    assert exc.value.code == "defer_assignment_cancelled"
    assert exc.value.details == {"cancelled_assignment_ids": [2]}


def test_defer_following_assignments_rejects_id_order_changed_with_typed_conflict():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
        _plan_assignment(
            2,
            date(2026, 6, 6),
            date(2026, 6, 10),
            staff_id=12,
        ),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [
                _plan_assignment(2, date(2026, 6, 1), date(2026, 6, 5), staff_id=11),
                _plan_assignment(1, date(2026, 6, 6), date(2026, 6, 10), staff_id=12),
            ],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 3),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )
    assert exc.value.code == "defer_assignment_id_order_changed"
    assert exc.value.details == {
        "current_assignment_ids": [1, 2],
        "proposed_assignment_ids": [2, 1],
    }


def test_defer_following_assignments_rejects_metadata_changed_with_typed_conflict():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
        _plan_assignment(
            2,
            date(2026, 6, 6),
            date(2026, 6, 10),
            staff_id=12,
        ),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 6)),
                _plan_assignment(
                    2,
                    date(2026, 6, 7),
                    date(2026, 6, 11),
                    staff_id=99,
                ),
            ],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 3),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )
    assert exc.value.code == "defer_assignment_metadata_changed"
    assert exc.value.details == {
        "assignment_id": 2,
        "field": "staff_id",
        "expected": 12,
        "actual": 99,
    }


def test_defer_following_assignments_rejects_affected_effective_date_out_of_assignment():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
        _plan_assignment(
            2,
            date(2026, 6, 6),
            date(2026, 6, 10),
            staff_id=12,
        ),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
                _plan_assignment(2, date(2026, 6, 7), date(2026, 6, 11), staff_id=12),
            ],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 5),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )
    assert exc.value.code == "defer_effective_date_outside_affected_assignment"
    assert exc.value.details == {
        "effective_date": "2026-06-05",
        "affected_assignment_id": 2,
        "affected_start_date": "2026-06-06",
        "affected_end_date": "2026-06-10",
    }


def test_defer_following_assignments_rejects_affected_missing_with_typed_conflict():
    current = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 5),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )
    assert exc.value.code == "defer_affected_assignment_missing"
    assert exc.value.details == {"assignment_ids": [1]}


def test_defer_following_assignments_shifts_later_rows_equally():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
        _plan_assignment(
            2,
            date(2026, 6, 5),
            date(2026, 6, 7),
            staff_id=12,
        ),
        _plan_assignment(
            3,
            date(2026, 6, 8),
            date(2026, 6, 10),
            staff_id=13,
        ),
    ]
    proposed = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
        _plan_assignment(
            2,
            date(2026, 6, 6),
            date(2026, 6, 8),
            staff_id=12,
        ),
        _plan_assignment(
            3,
            date(2026, 6, 9),
            date(2026, 6, 11),
            staff_id=13,
        ),
    ]

    result = _validate(
        proposed,
        current=current,
        operation="defer_following_assignments",
        effective=date(2026, 6, 3),
        proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
    )

    assert [row["id"] for row in result["after_assignments"]] == [1, 2, 3]
    assert [row["staff_id"] for row in result["after_assignments"]] == [11, 12, 13]


def test_defer_following_assignments_rejects_shift_mismatch_with_typed_conflict():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
        _plan_assignment(2, date(2026, 6, 5), date(2026, 6, 10), staff_id=12),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
                _plan_assignment(2, date(2026, 6, 9), date(2026, 6, 11), staff_id=12),
            ],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 3),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )
    assert exc.value.code == "defer_assignment_shift_mismatch"
    assert exc.value.details == {
        "assignment_id": 2,
        "row_role": "following",
        "defer_days": 1,
        "expected_start_date": "2026-06-06",
        "expected_end_date": "2026-06-11",
        "actual_start_date": "2026-06-09",
        "actual_end_date": "2026-06-11",
    }


def test_defer_following_assignments_contract_conflict_payload_is_json_safe():
    current = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 5),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 10)),
        )
    payload = {"code": exc.value.code, "details": exc.value.details}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    loaded = json.loads(raw)
    _assert_json_safe(loaded)
    assert loaded["code"] == "defer_days_not_positive"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda rows: rows[1].update(staff_id=99),
        lambda rows: rows[1].update(assigned_start_date=date(2026, 6, 5)),
        lambda rows: rows[1].update(assigned_end_date=date(2026, 6, 8)),
        lambda rows: (rows[0].update(id=2), rows[1].update(id=1)),
    ],
)
def test_defer_following_assignments_rejects_ownership_or_unequal_shift(mutator):
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 5)),
        _plan_assignment(2, date(2026, 6, 6), date(2026, 6, 10), staff_id=12),
    ]
    proposed = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 6)),
        _plan_assignment(2, date(2026, 6, 7), date(2026, 6, 11), staff_id=12),
    ]
    mutator(proposed)

    with pytest.raises(ValueError):
        _validate(
            proposed,
            current=current,
            operation="defer_following_assignments",
            effective=date(2026, 6, 3),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 11)),
        )


def test_historical_fact_state_defaults_locked_and_unlocked_requires_audit():
    current = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))]
    proposed = [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 7))]

    with pytest.raises(ValueError, match="before database_current_date"):
        _validate(
            proposed,
            current=current,
            effective=date(2026, 6, 8),
            database_current=date(2026, 6, 9),
            proposed_interval=(date(2026, 6, 1), date(2026, 6, 7)),
        )

    result = _validate(
        proposed,
        current=current,
        effective=date(2026, 6, 8),
        database_current=date(2026, 6, 9),
        proposed_interval=(date(2026, 6, 1), date(2026, 6, 7)),
        historical_fact_state="unlocked",
    )
    assert result["historical_fact_state"] == "unlocked"
    assert result["requires_audit"] is True


@pytest.mark.parametrize("state", ["", "allow_historical_edit", True, None])
def test_historical_fact_state_rejects_client_style_bypass_values(state):
    with pytest.raises(ValueError, match="historical_fact_state"):
        _validate(
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            historical_fact_state=state,
        )


def _batch_substitute(key, work_date, *, original_assignment_id=1, staff_id=99):
    return _plan_assignment(
        key,
        work_date,
        work_date,
        staff_id=staff_id,
        kind="single_day_substitute",
        original_assignment_id=original_assignment_id,
        substitution_work_date=work_date,
    )


def _batch_validate(proposed, *, current=None, defer_days=0, effective=date(2026, 6, 1), **kwargs):
    current_case_end = max(
        (row["assigned_end_date"] for row in (current or [])),
        default=date(2026, 6, 10),
    )
    return _validate(
        proposed,
        current=current,
        operation="batch_leave_resolution",
        effective=effective,
        database_current=date(2026, 6, 1),
        current_interval=(date(2026, 6, 1), current_case_end),
        proposed_interval=(date(2026, 6, 1), current_case_end + timedelta(days=defer_days)),
        **kwargs,
    )


def test_batch_leave_resolution_allows_two_adjacent_pure_substitutes():
    result = _batch_validate(
        [
            _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 2)),
            _batch_substitute("sub-a", date(2026, 6, 3)),
            _batch_substitute("sub-b", date(2026, 6, 4)),
            _plan_assignment("formal-tail", date(2026, 6, 5), date(2026, 6, 10)),
        ],
        effective=date(2026, 6, 3),
    )

    assert result["operation_kind"] == "batch_leave_resolution"
    assert result["ownership_by_date"]["2026-06-03"] == "sub-a"
    assert result["ownership_by_date"]["2026-06-04"] == "sub-b"
    assert result["ownership_by_date"]["2026-06-05"] == "formal-tail"


def test_batch_leave_resolution_allows_one_pure_substitute():
    result = _batch_validate(
        [
            _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 2)),
            _batch_substitute("sub-a", date(2026, 6, 3)),
            _plan_assignment("formal-tail", date(2026, 6, 4), date(2026, 6, 10)),
        ],
        effective=date(2026, 6, 3),
    )

    assert result["ownership_by_date"]["2026-06-03"] == "sub-a"


@pytest.mark.parametrize("defer_days", [1, 2, 3])
def test_batch_leave_resolution_allows_pure_defer(defer_days):
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
        _plan_assignment(2, date(2026, 6, 11), date(2026, 6, 13), staff_id=12),
    ]
    result = _batch_validate(
        [
            _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10 + defer_days)),
            _plan_assignment(
                2,
                date(2026, 6, 11 + defer_days),
                date(2026, 6, 13 + defer_days),
                staff_id=12,
            ),
        ],
        current=current,
        defer_days=defer_days,
        effective=date(2026, 6, 5),
    )

    after_by_id = {row["id"]: row for row in result["after_assignments"]}
    assert after_by_id[1]["assigned_end_date"] == date(2026, 6, 10 + defer_days)
    assert after_by_id[2]["assigned_start_date"] == date(2026, 6, 11 + defer_days)


def test_batch_leave_resolution_allows_defer_before_substitute_effective_date():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
        _plan_assignment(2, date(2026, 6, 11), date(2026, 6, 13), staff_id=12),
    ]
    result = _batch_validate(
        [
            _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
            _batch_substitute("sub-a", date(2026, 6, 5)),
            _plan_assignment("formal-tail", date(2026, 6, 6), date(2026, 6, 11)),
            _plan_assignment(2, date(2026, 6, 12), date(2026, 6, 14), staff_id=12),
        ],
        current=current,
        defer_days=1,
        effective=date(2026, 6, 3),
    )

    assert result["effective_date"] == date(2026, 6, 3)
    assert result["ownership_by_date"]["2026-06-05"] == "sub-a"


def test_batch_leave_resolution_rejects_effective_date_outside_inferred_target():
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 4)),
                _batch_substitute("sub-a", date(2026, 6, 5)),
                _plan_assignment(
                    "formal-tail", date(2026, 6, 6), date(2026, 6, 11)
                ),
            ],
            defer_days=1,
            effective=date(2026, 6, 11),
        )

    assert exc.value.code == "batch_leave_target_mismatch"
    assert exc.value.details["field"] == "effective_date"


@pytest.mark.parametrize(
    ("current", "proposed", "defer_days", "expected_code"),
    [
        (
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            0,
            "batch_leave_target_mismatch",
        ),
        (
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10))],
            [_plan_assignment(1, date(2026, 6, 1), date(2026, 6, 9))],
            -1,
            "batch_defer_shift_invalid",
        ),
        (
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
                _plan_assignment(2, date(2026, 6, 11), date(2026, 6, 13), staff_id=12),
            ],
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 11)),
                _plan_assignment(2, date(2026, 6, 11), date(2026, 6, 14), staff_id=12),
            ],
            1,
            "batch_leave_target_mismatch",
        ),
        (
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
                _plan_assignment(2, date(2026, 6, 11), date(2026, 6, 13), staff_id=12),
            ],
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 11)),
                _plan_assignment(2, date(2026, 6, 12), date(2026, 6, 15), staff_id=12),
            ],
            1,
            "batch_defer_shift_invalid",
        ),
    ],
    ids=["no-action", "negative", "ambiguous-target", "nonuniform-shift"],
)
def test_batch_leave_resolution_rejects_invalid_pure_defer_shapes(
    current, proposed, defer_days, expected_code
):
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(
            proposed,
            current=current,
            defer_days=defer_days,
            effective=date(2026, 6, 5),
        )

    assert exc.value.code == expected_code
    assert json.loads(json.dumps(exc.value.details, sort_keys=True)) == exc.value.details


def test_batch_leave_resolution_allows_three_substitutes_at_row_limit():
    result = _batch_validate(
        [
            _batch_substitute("sub-a", date(2026, 6, 1)),
            _batch_substitute("sub-b", date(2026, 6, 2)),
            _batch_substitute("sub-c", date(2026, 6, 3)),
            _plan_assignment(1, date(2026, 6, 4), date(2026, 6, 10)),
        ]
    )

    assert len(result["after_assignments"]) == 4


def test_batch_leave_resolution_rejects_fifth_active_row_with_typed_conflict():
    rows = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 2)),
        _batch_substitute("sub-a", date(2026, 6, 3)),
        _batch_substitute("sub-b", date(2026, 6, 4)),
        _plan_assignment("formal-tail", date(2026, 6, 5), date(2026, 6, 9)),
        _plan_assignment("extra", date(2026, 6, 10), date(2026, 6, 10)),
    ]

    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(rows, effective=date(2026, 6, 3))

    assert exc.value.code == "assignment_row_limit_exceeded"
    assert exc.value.details == {"maximum_active_rows": 4, "actual_active_rows": 5}


@pytest.mark.parametrize("defer_days", [1, 2, 3])
def test_batch_leave_resolution_mixed_substitution_and_defer_shifts_later_rows(defer_days):
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
        _plan_assignment(2, date(2026, 6, 11), date(2026, 6, 13), staff_id=12),
    ]
    result = _batch_validate(
        [
            _batch_substitute("sub-a", date(2026, 6, 1)),
            _batch_substitute("sub-b", date(2026, 6, 2)),
            _plan_assignment(1, date(2026, 6, 3), date(2026, 6, 10 + defer_days)),
            _plan_assignment(
                2,
                date(2026, 6, 11 + defer_days),
                date(2026, 6, 13 + defer_days),
                staff_id=12,
            ),
        ],
        current=current,
        defer_days=defer_days,
    )

    after_by_id = {row["id"]: row for row in result["after_assignments"]}
    assert after_by_id[2]["assigned_start_date"] == date(2026, 6, 11 + defer_days)
    assert after_by_id[2]["assigned_end_date"] == date(2026, 6, 13 + defer_days)


def test_batch_leave_resolution_does_not_shift_later_row_when_delta_is_zero():
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
        _plan_assignment(2, date(2026, 6, 11), date(2026, 6, 13), staff_id=12),
    ]
    result = _batch_validate(
        [
            _batch_substitute("sub-a", date(2026, 6, 1)),
            _batch_substitute("sub-b", date(2026, 6, 2)),
            _plan_assignment(1, date(2026, 6, 3), date(2026, 6, 10)),
            _plan_assignment(2, date(2026, 6, 11), date(2026, 6, 13), staff_id=12),
        ],
        current=current,
    )

    assert {row["id"]: row for row in result["after_assignments"]}[2]["assigned_start_date"] == date(2026, 6, 11)


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda rows: rows.__setitem__(2, _batch_substitute("sub-c", date(2026, 6, 3))),
            "batch_substitute_date_duplicate",
        ),
        (
            lambda rows: rows[1].update(original_assignment_id=2),
            "batch_leave_target_mismatch",
        ),
        (
            lambda rows: rows[1].update(assigned_end_date=date(2026, 6, 4)),
            "batch_substitute_lineage_invalid",
        ),
    ],
)
def test_batch_leave_resolution_rejects_invalid_substitute_lineage_deterministically(mutator, expected_code):
    rows = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 2)),
        _batch_substitute("sub-a", date(2026, 6, 3)),
        _batch_substitute("sub-b", date(2026, 6, 4)),
        _plan_assignment("formal-tail", date(2026, 6, 5), date(2026, 6, 10)),
    ]
    mutator(rows)

    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(rows, effective=date(2026, 6, 3))

    assert exc.value.code == expected_code
    assert json.loads(json.dumps(exc.value.details, sort_keys=True)) == exc.value.details


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    [
        (
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 2)),
                _batch_substitute("sub-a", date(2026, 6, 3)),
                _batch_substitute("sub-b", date(2026, 6, 4)),
                _plan_assignment("formal-tail", date(2026, 6, 6), date(2026, 6, 10)),
            ],
            "assignment_daily_ownership_invalid",
        ),
        (
            [
                _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 3)),
                _batch_substitute("sub-a", date(2026, 6, 3)),
                _batch_substitute("sub-b", date(2026, 6, 4)),
                _plan_assignment("formal-tail", date(2026, 6, 5), date(2026, 6, 10)),
            ],
            "assignment_daily_ownership_invalid",
        ),
    ],
)
def test_batch_leave_resolution_reports_fragment_gap_or_overlap(rows, expected_code):
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(rows, effective=date(2026, 6, 3))

    assert exc.value.code == expected_code


def test_batch_leave_resolution_rejects_original_staff_ownership_change_and_negative_delta():
    rows = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 2)),
        _batch_substitute("sub-a", date(2026, 6, 3)),
        _batch_substitute("sub-b", date(2026, 6, 4)),
        _plan_assignment("formal-tail", date(2026, 6, 5), date(2026, 6, 10), staff_id=77),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(rows, effective=date(2026, 6, 3))
    assert exc.value.code == "batch_original_staff_ownership_changed"

    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(rows, defer_days=-1, effective=date(2026, 6, 3))
    assert exc.value.code == "batch_defer_shift_invalid"


def test_batch_leave_resolution_locks_historical_ownership_with_typed_json_safe_details():
    rows = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 2)),
        _batch_substitute("sub-a", date(2026, 6, 3)),
        _batch_substitute("sub-b", date(2026, 6, 4)),
        _plan_assignment("formal-tail", date(2026, 6, 5), date(2026, 6, 10)),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _validate(
            rows,
            operation="batch_leave_resolution",
            effective=date(2026, 6, 3),
            database_current=date(2026, 6, 5),
        )

    assert exc.value.code == "historical_ownership_locked"
    assert json.loads(json.dumps(exc.value.details, sort_keys=True)) == exc.value.details


def _cancelled_substitute(
    assignment_id=9,
    work_date=date(2026, 6, 3),
    *,
    original_assignment_id=77,
):
    return _plan_assignment(
        assignment_id,
        work_date,
        work_date,
        status="cancelled",
        staff_id=98,
        kind="single_day_substitute",
        original_assignment_id=original_assignment_id,
        substitution_work_date=work_date,
    )


def test_batch_leave_resolution_excludes_retained_cancelled_substitute_from_candidates():
    cancelled = _cancelled_substitute()
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
        cancelled,
    ]
    result = _batch_validate(
        [
            _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 2)),
            _batch_substitute("sub-a", date(2026, 6, 3)),
            _batch_substitute("sub-b", date(2026, 6, 4)),
            _plan_assignment("formal-tail", date(2026, 6, 5), date(2026, 6, 10)),
            cancelled,
        ],
        current=current,
        effective=date(2026, 6, 3),
    )

    retained_cancelled = [
        row
        for row in result["after_assignments"]
        if row["id"] == 9 and row["status"] == "cancelled"
    ]
    assert retained_cancelled == [cancelled]
    assert result["ownership_by_date"]["2026-06-03"] == "sub-a"


def test_batch_leave_resolution_allows_one_new_substitute_with_a_retained_cancelled_row():
    cancelled = _cancelled_substitute()
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 10)),
        cancelled,
    ]
    result = _batch_validate(
        [
            _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 2)),
            _batch_substitute("sub-a", date(2026, 6, 3)),
            _plan_assignment("formal-tail", date(2026, 6, 4), date(2026, 6, 10)),
            cancelled,
        ],
        current=current,
        effective=date(2026, 6, 3),
    )

    assert result["ownership_by_date"]["2026-06-03"] == "sub-a"


def test_batch_leave_resolution_retains_valid_active_substitute_before_target_for_zero_delta():
    existing_substitute = _plan_assignment(
        9,
        date(2026, 6, 1),
        date(2026, 6, 1),
        staff_id=98,
        kind="single_day_substitute",
        original_assignment_id=1,
        substitution_work_date=date(2026, 6, 1),
    )
    current = [
        existing_substitute,
        _plan_assignment(1, date(2026, 6, 2), date(2026, 6, 10)),
    ]
    result = _batch_validate(
        [
            existing_substitute,
            _batch_substitute("sub-a", date(2026, 6, 2)),
            _batch_substitute("sub-b", date(2026, 6, 3)),
            _plan_assignment(1, date(2026, 6, 4), date(2026, 6, 10)),
        ],
        current=current,
        effective=date(2026, 6, 2),
    )

    assert result["ownership_by_date"]["2026-06-01"] == "9"
    assert len([row for row in result["after_assignments"] if row["status"] != "cancelled"]) == 4


def test_batch_leave_resolution_rejects_active_substitute_after_target_when_defer_requires_shift():
    existing_substitute = _plan_assignment(
        9,
        date(2026, 6, 9),
        date(2026, 6, 9),
        staff_id=98,
        kind="single_day_substitute",
        original_assignment_id=1,
        substitution_work_date=date(2026, 6, 9),
    )
    current = [
        _plan_assignment(1, date(2026, 6, 1), date(2026, 6, 8)),
        existing_substitute,
    ]
    shifted_substitute = {**existing_substitute}
    shifted_substitute["assigned_start_date"] = date(2026, 6, 10)
    shifted_substitute["assigned_end_date"] = date(2026, 6, 10)

    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(
            [
                _batch_substitute("sub-a", date(2026, 6, 1)),
                _batch_substitute("sub-b", date(2026, 6, 2)),
                _plan_assignment(1, date(2026, 6, 3), date(2026, 6, 9)),
                shifted_substitute,
            ],
            current=current,
            defer_days=1,
        )

    assert exc.value.code == "batch_defer_shift_invalid"
    assert exc.value.details["reason"] == "existing_active_substitute_requires_shift"


def test_batch_leave_resolution_rejects_invalid_existing_active_substitute_lineage():
    invalid_substitute = _plan_assignment(
        9,
        date(2026, 6, 1),
        date(2026, 6, 2),
        staff_id=98,
        kind="single_day_substitute",
        original_assignment_id=1,
        substitution_work_date=date(2026, 6, 1),
    )
    current = [
        invalid_substitute,
        _plan_assignment(1, date(2026, 6, 3), date(2026, 6, 10)),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(
            [
                invalid_substitute,
                _batch_substitute("sub-a", date(2026, 6, 3)),
                _batch_substitute("sub-b", date(2026, 6, 4)),
                _plan_assignment(1, date(2026, 6, 5), date(2026, 6, 10)),
            ],
            current=current,
            effective=date(2026, 6, 3),
        )

    assert exc.value.code == "batch_substitute_lineage_invalid"
    assert exc.value.details["reason"] == "existing_active_substitute_lineage_invalid"


def test_batch_leave_resolution_row_limit_counts_retained_active_substitute():
    existing_substitute = _plan_assignment(
        9,
        date(2026, 6, 1),
        date(2026, 6, 1),
        staff_id=98,
        kind="single_day_substitute",
        original_assignment_id=1,
        substitution_work_date=date(2026, 6, 1),
    )
    current = [
        existing_substitute,
        _plan_assignment(1, date(2026, 6, 2), date(2026, 6, 8)),
        _plan_assignment(2, date(2026, 6, 9), date(2026, 6, 10), staff_id=12),
    ]
    with pytest.raises(AssignmentPlanTransitionConflict) as exc:
        _batch_validate(
            [
                existing_substitute,
                _batch_substitute("sub-a", date(2026, 6, 2)),
                _batch_substitute("sub-b", date(2026, 6, 3)),
                _plan_assignment(1, date(2026, 6, 4), date(2026, 6, 8)),
                _plan_assignment(2, date(2026, 6, 9), date(2026, 6, 10), staff_id=12),
            ],
            current=current,
            effective=date(2026, 6, 2),
        )

    assert exc.value.code == "assignment_row_limit_exceeded"
    assert exc.value.details["actual_active_rows"] == 5
