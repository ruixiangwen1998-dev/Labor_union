from datetime import date

from services import staff_monthly_calendar_schedule_service as staff_monthly_calendar_schedule_service
import pytest


class FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.current = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self.current = self.responses.pop(0)

    def fetchone(self):
        if isinstance(self.current, list):
            return self.current[0] if self.current else None
        return self.current

    def fetchall(self):
        return self.current


class FakeConnection:
    def __init__(self, responses):
        self.cursor_obj = FakeCursor(responses)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def row(**overrides):
    return {
        "work_date": date(2026, 7, 3),
        "is_work_day": True,
        "is_double_pay": False,
        "notes": None,
        "schedule_id": 100,
        "case_no": "115000001",
        "assignment_id": 1,
        "client_name": "客戶甲",
        **overrides,
    }


def test_get_staff_monthly_calendar_schedule_keeps_per_day_rows_and_base_shape(monkeypatch):
    rows = [
        row(id=10, work_date=date(2026, 7, 3), assignment_id=11, is_work_day=True, client_name="客戶 A"),
        row(id=11, work_date=date(2026, 7, 3), assignment_id=12, is_work_day=False, client_name="客戶 A"),
        row(id=12, work_date=date(2026, 7, 5), assignment_id=13, is_work_day=True, client_name="客戶 B"),
    ]
    connection = FakeConnection([{"id": 7}, rows])
    monkeypatch.setattr(staff_monthly_calendar_schedule_service, "get_connection", lambda: connection)

    result = staff_monthly_calendar_schedule_service.get_staff_monthly_calendar_schedule(
        staff_id=7, year=2026, month=7
    )

    assert result["staff_id"] == 7
    assert result["year"] == 2026
    assert result["month"] == 7
    assert len(result["days"]) == 32
    assert result["days"][0]["status"] == "available"
    assert result["days"][0]["assignment_id"] is None
    assert result["days"][2]["work_date"] == "2026-07-03"
    assert result["days"][2]["status"] == "working"
    assert result["days"][2]["assignment_id"] == 11
    assert result["days"][2]["case_no"] == "115000001"
    assert result["days"][2]["client_name"] == "客戶 A"
    assert result["days"][3]["assignment_id"] == 12
    assert result["days"][3]["status"] == "resting"
    assert result["days"][5]["assignment_id"] == 13
    assert result["days"][5]["status"] == "working"

    assert result["schedule_map"][3]["status"] == "red"
    assert result["schedule_map"][3]["assignment_id"] == 11
    assert result["schedule_map"][5]["status"] == "red"

    for item in result["days"]:
        assert item["staff_id"] == 7
        assert "work_date" in item
        assert "status" in item
        assert "assignment_id" in item
        assert "case_no" in item
        assert "client_name" in item
        if item["assignment_id"] is not None:
            assert item["assignment_id"] != "115000001"

    query, params = connection.cursor_obj.executed[0]
    assert query == "SELECT 1 AS staff_exists FROM staff WHERE id = %s"
    assert params == (7,)

    query, params = connection.cursor_obj.executed[1]
    assert "JOIN case_staff_assignments" in query
    assert params == (7, date(2026, 7, 1), date(2026, 7, 31))
    assert connection.closed is True


def test_get_staff_monthly_calendar_schedule_supports_30_day_month(monkeypatch):
    connection = FakeConnection([{"id": 7}, []])
    monkeypatch.setattr(staff_monthly_calendar_schedule_service, "get_connection", lambda: connection)

    result = staff_monthly_calendar_schedule_service.get_staff_monthly_calendar_schedule(
        staff_id=7, year=2026, month=6
    )

    assert len(result["days"]) == 30
    assert result["days"][0]["status"] == "available"


def test_get_staff_monthly_calendar_schedule_staff_not_found(monkeypatch):
    connection = FakeConnection([None])
    monkeypatch.setattr(staff_monthly_calendar_schedule_service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="服務人員不存在"):
        staff_monthly_calendar_schedule_service.get_staff_monthly_calendar_schedule(
            staff_id=999,
            year=2026,
            month=7,
        )
