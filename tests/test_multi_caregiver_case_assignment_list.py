from datetime import date

import pytest

from services import multi_caregiver_schedule_read as service


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class QueryAwareCursor:
    def __init__(self, fixture):
        self.fixture = fixture
        self.current = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized_sql = " ".join(sql.split()).lower()
        self.executed.append((normalized_sql, tuple(params) if params is not None else None))
        if "from case_staff_assignments a" in normalized_sql:
            case_no = params[0] if params else ""
            rows = list(self.fixture["assignments_by_case"].get(case_no, []))
            if "a.status <> 'cancelled'" in normalized_sql:
                rows = [row for row in rows if row.get("status") != "cancelled"]
            self.current = rows
        else:
            self.current = []

    def fetchall(self):
        return self.current


class QueryAwareConnection:
    def __init__(self, fixture):
        self.cursor_obj = QueryAwareCursor(fixture)
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def assignment(**overrides):
    return {
        "id": 21,
        "case_no": "115000001",
        "staff_id": 8,
        "staff_name": "王月嫂",
        "status": "active",
        "assigned_start_date": date(2026, 6, 1),
        "assigned_end_date": date(2026, 6, 3),
        "original_assigned_start_date": date(2026, 6, 1),
        "original_assigned_end_date": date(2026, 6, 3),
        "planned_hours": 18,
        "actual_hours": 18,
        "service_days": 12,
        "service_hours_per_day": 9,
        **overrides,
    }


def _assert_only_select_no_transaction(connection: FakeConnection | QueryAwareConnection):
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert all(statement.upper().startswith("SELECT") for statement, _ in connection.cursor_obj.executed)


def test_lists_only_formal_assignments_for_explicit_case(monkeypatch):
    connection = FakeConnection([assignment(), assignment(id=22, staff_id=9)])
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.list_case_schedule_assignments(" 115000001 ")

    assert [item["id"] for item in result["assignments"]] == [21, 22]
    sql, params = connection.cursor_obj.executed[0]
    assert params == ("115000001",)
    assert "a.status <> 'cancelled'" in sql
    assert "ORDER BY a.assigned_start_date ASC, a.id ASC" in sql
    assert "staff_schedule" in sql
    assert "assignment_schedule_leave_substitution_events" in sql
    assert not any("cancelled" in row.get("status") for row in result["assignments"])
    assert result["assignments"][0]["original_assigned_start_date"] == date(
        2026, 6, 1
    )
    assert result["assignments"][0]["adjusted_assigned_end_date"] == date(
        2026, 6, 3
    )
    assert result["assignments"][0]["original_scheduled_service_days"] == 2
    assert result["summary"]["target_service_days"] == 12
    assert result["summary"]["target_service_hours"] == "108"
    assert result["summary"]["has_service_gap"] is True
    assert result["summary"]["has_service_overlap"] is False
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


@pytest.mark.parametrize("case_no", [None, "", "   ", 115000001])
def test_rejects_invalid_case_no_before_opening_connection(monkeypatch, case_no):
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not connect"))

    with pytest.raises(ValueError, match="non-empty string"):
        service.list_case_schedule_assignments(case_no)


def test_returns_empty_list_when_selected_case_has_no_active_assignments(monkeypatch):
    connection = FakeConnection([])
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    assert service.list_case_schedule_assignments("115000001") == {"assignments": []}
    assert connection.closed is True


@pytest.mark.parametrize(
    "bad_row,match",
    [
        ({**assignment(), "id": 0}, "assignment_id"),
        ({**assignment(), "id": True}, "assignment_id"),
        ({**assignment(), "staff_id": -3}, "staff_id"),
        ({**assignment(), "staff_id": True}, "staff_id"),
        ({**assignment(), "case_no": "WRONG"}, "case_no"),
        ({**assignment(), "status": "cancelled"}, "cancelled"),
        ({**assignment(), "assigned_start_date": None}, "date range is incomplete"),
        ({**assignment(), "assigned_end_date": None}, "date range is incomplete"),
        ({**assignment(), "assigned_start_date": date(2026, 6, 10), "assigned_end_date": date(2026, 6, 1)}, "assigned_start_date"),
        ({k: v for k, v in assignment().items() if k != "planned_hours"}, "planned_hours"),
        ({k: v for k, v in assignment().items() if k != "actual_hours"}, "actual_hours"),
    ],
)
def test_rejects_invalid_assignment_record_before_returning(monkeypatch, bad_row, match):
    connection = FakeConnection([bad_row])
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match=match):
        service.list_case_schedule_assignments("115000001")

    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


def test_query_aware_returns_only_target_case_active_assignments_without_merging(monkeypatch):
    fixture = {
        "assignments_by_case": {
            "115000001": [
                assignment(
                    id=21,
                    planned_hours=24,
                    actual_hours=18,
                    assigned_start_date=date(2026, 6, 1),
                    assigned_end_date=date(2026, 6, 20),
                ),
                assignment(
                    id=22,
                    staff_id=8,
                    case_no="115000001",
                    planned_hours=80,
                    actual_hours=60,
                    assigned_start_date=date(2026, 7, 1),
                    assigned_end_date=date(2026, 7, 20),
                    service_hours_per_day=10,
                ),
                assignment(
                    id=23,
                    status="cancelled",
                    staff_id=8,
                    case_no="115000001",
                    planned_hours=24,
                    actual_hours=12,
                    assigned_start_date=date(2026, 8, 1),
                    assigned_end_date=date(2026, 8, 10),
                ),
            ],
            "115000002": [
                assignment(
                    id=31,
                    case_no="115000002",
                    staff_id=8,
                    planned_hours=30,
                    actual_hours=20,
                    assigned_start_date=date(2026, 6, 1),
                    assigned_end_date=date(2026, 6, 3),
                    service_hours_per_day=6,
                ),
            ],
        },
        "legacy_rows": [
            {
                "id": 77,
                "case_no": "115000001",
                "staff_id": 8,
                "assignment_id": None,
            }
        ],
    }
    connection = QueryAwareConnection(fixture)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.list_case_schedule_assignments(" 115000001 ")

    assert [item["id"] for item in result["assignments"]] == [21, 22]
    assert result["assignments"][0]["planned_hours"] == 24
    assert result["assignments"][0]["actual_hours"] == 18
    assert result["assignments"][1]["planned_hours"] == 80
    assert result["assignments"][1]["actual_hours"] == 60
    assert result["assignments"][0]["assigned_start_date"] == date(2026, 6, 1)
    assert result["assignments"][1]["assigned_start_date"] == date(2026, 7, 1)
    assert any(row["status"] == "cancelled" for row in fixture["assignments_by_case"]["115000001"])
    assert "a.status <> 'cancelled'" in connection.cursor_obj.executed[0][0]
    assert connection.cursor_obj.executed[0][1] == ("115000001",)
    assert "staff_schedule" in connection.cursor_obj.executed[0][0]
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)
