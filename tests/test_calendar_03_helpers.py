import importlib.util
from datetime import date
from pathlib import Path


def _load_calendar_module():
    spec = importlib.util.spec_from_file_location(
        "ui_pages_03_calendar_test",
        Path("ui/pages/03_calendar.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _calendar_module():
    return _load_calendar_module()


def test_extract_case_assignments_for_staff_filters_cancelled_and_staff():
    mod = _calendar_module()
    assignments = [
        {"id": 1, "staff_id": 11, "status": "active"},
        {"id": 2, "staff_id": 22, "status": "active"},
        {"id": 3, "staff_id": 11, "status": "cancelled"},
        {"id": 4, "status": "active"},
        {"id": 5, "staff_id": "11", "status": "active"},
    ]

    result = mod._extract_case_assignments_for_staff(assignments, "11")
    assert [item["id"] for item in result] == [1, 5]


def test_parse_stored_rest_dates_requires_array():
    mod = _calendar_module()
    parsed, error = mod._parse_stored_rest_dates('{"2026-08-01":"x"}')
    assert parsed == set()
    assert error is not None
    assert "不是清單格式" in error


def test_parse_stored_rest_dates_rejects_invalid_elements():
    mod = _calendar_module()
    parsed, error = mod._parse_stored_rest_dates('["2026-8-1", "2026-08-01"]')
    assert parsed == set()
    assert error is not None
    assert "不合法日期" in error
    assert "2026-8-1" in error


def test_parse_stored_rest_dates_accepts_valid_list():
    mod = _calendar_module()
    parsed, error = mod._parse_stored_rest_dates('["2026-08-01"]')
    assert parsed == {date(2026, 8, 1)}
    assert error is None


def test_normalise_calendar_schedule_map_restores_json_day_keys():
    mod = _calendar_module()

    result = mod._normalise_calendar_schedule_map(
        {
            "1": {"status": "red", "case_no": "CASE-1"},
            2: {"status": "green", "case_no": "CASE-2"},
            "bad": {"status": "yellow"},
            "32": {"status": "yellow"},
        }
    )

    assert result == {
        1: {"status": "red", "case_no": "CASE-1"},
        2: {"status": "green", "case_no": "CASE-2"},
    }


def test_safe_date_keeps_missing_actual_date_available_for_planned_fallback():
    mod = _calendar_module()

    assert mod.safe_date("") is None
    assert mod.safe_date(None) is None
    assert mod.safe_date("2026-07-03") == date(2026, 7, 3)
