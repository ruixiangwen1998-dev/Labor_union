from datetime import date

import pytest

from services import caregiver_segment_availability_query_service as service


class QueryAwareCursor:
    def __init__(self, fixture):
        self.fixture = fixture
        self.current = None
        self.executed = []
        self.closed = False
        self.closed_count = 0

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        self.executed.append((statement, tuple(params) if params is not None else None))

        if "FROM orders o" in statement and "WHERE o.case_no = %s" in statement:
            self.current = self.fixture.get("order")
            return

        if "FROM staff WHERE" in statement or "FROM staff " in statement and "JOIN" not in statement:
            self.current = self.fixture.get("staff_rows", [])
            return

        if "FROM case_staff_assignments" in statement and "INNER JOIN case_staff_assignments" not in statement:
            self.current = self.fixture.get("assignments", [])
            return

        if "FROM staff_schedule s" in statement and "INNER JOIN case_staff_assignments" in statement:
            self.current = self.fixture.get("schedule_rows", [])
            return

        if "FROM staff_schedule" in statement and "INNER JOIN case_staff_assignments" not in statement:
            self.current = self.fixture.get("legacy_schedule_rows", [])
            return

        if "FROM caregiver_availability_lock_days" in statement:
            self.current = self.fixture.get("active_lock_rows", [])
            return

        self.current = self.fixture.get("default")

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self):
        self.closed = True
        self.closed_count += 1


class QueryAwareConnection:
    def __init__(self, fixture):
        self.cursor_obj = QueryAwareCursor(fixture)
        self.closed = False
        self.closed_count = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed_count += 1
        self.closed = True


def _make_connection(**fixture):
    return QueryAwareConnection(fixture)


def _assert_readonly_no_tx(connection: QueryAwareConnection):
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert connection.closed_count == 1
    assert connection.cursor_obj.closed_count == 1
    statements = [item[0] for item in connection.cursor_obj.executed]
    assert all(stmt.startswith("SELECT") for stmt in statements)
    assert all("FOR UPDATE" not in stmt.upper() for stmt in statements)


def test_search_segmented_invalid_inputs_reject_before_db(monkeypatch):
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not hit DB"))

    with pytest.raises(ValueError, match="case_no is required"):
        service.search_segmented_caregiver_availability(None, 2, [], date(2026, 7, 10))

    with pytest.raises(ValueError, match="segment_drafts must be a list"):
        service.search_segmented_caregiver_availability("115000001", 2, (), date(2026, 7, 10))

    with pytest.raises(ValueError, match="segment_count must"):
        service.search_segmented_caregiver_availability("115000001", 0, [], date(2026, 7, 10))

    with pytest.raises(ValueError, match="as_of must"):
        service.search_segmented_caregiver_availability("115000001", 2, [], {"today": "2026-07-10"})


def test_search_segmented_case_not_in_negotiation_is_rejected(monkeypatch):
    connection = _make_connection(
        order={"case_no": "115000001", "status": "已成交", "start_date": "2026-07-01", "end_date": "2026-07-03"},
        )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="case is not in negotiation stage"):
        service.search_segmented_caregiver_availability("115000001", 2, [], "2026-07-10")

    assert connection.closed
    assert connection.closed_count == 1
    assert connection.cursor_obj.closed_count == 1
    assert len(connection.cursor_obj.executed) == 1


def test_search_segmented_uses_one_connection_and_no_tx_and_not_update_planning_window(monkeypatch):
    connections = []

    def get_connection():
        conn = _make_connection(
            order={"case_no": "115000001", "status": "洽談中", "start_date": "2026-07-01", "end_date": "2026-07-03"},
            staff_rows=[{"id": 1}, {"id": 2}],
        )
        connections.append(conn)
        return conn

    monkeypatch.setattr(service, "get_connection", get_connection)
    monkeypatch.setattr(
        service,
        "derive_segment_availability",
        lambda *args, **kwargs: {
            "validated_input": {
                "planned_start_date": kwargs["planned_start_date"],
                "planned_end_date": kwargs["planned_end_date"],
            },
            "complete_combinations": [],
            "segment_candidates": [],
            "conflicts": [],
        },
    )

    result = service.search_segmented_caregiver_availability("115000001", 2, [], "2026-06-01")

    assert len(connections) == 1
    assert result["planned_start_date"] == "2026-07-01"
    assert result["planned_end_date"] == "2026-07-03"
    assert result["feasibility"] == "partial"
    assert connections[0].closed
    _assert_readonly_no_tx(connections[0])


def test_search_segmented_filters_candidates_to_active_staff_only(monkeypatch):
    captured = {}

    def fake_derive_segment_availability(*args, **kwargs):
        captured["candidate_staff_ids"] = kwargs["candidate_staff_ids"]
        return {
            "validated_input": {
                "planned_start_date": kwargs["planned_start_date"],
                "planned_end_date": kwargs["planned_end_date"],
            },
            "complete_combinations": [],
            "segment_candidates": [],
            "conflicts": [],
        }

    connection = _make_connection(
        order={"case_no": "115000001", "status": "洽談中", "start_date": "2026-07-01", "end_date": "2026-07-03"},
        staff_rows=[{"id": 10}, {"id": 20}],
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(service, "derive_segment_availability", fake_derive_segment_availability)

    service.search_segmented_caregiver_availability("115000001", 2, [], "2026-07-10")

    assert captured["candidate_staff_ids"] == [10, 20]
    assert connection.closed


def test_search_segmented_ignores_as_of_for_order_boundary(monkeypatch):
    captured = {}

    def fake_derive_segment_availability(*args, **kwargs):
        captured["input"] = {
            "planned_start_date": kwargs["planned_start_date"],
            "planned_end_date": kwargs["planned_end_date"],
        }
        return {
            "validated_input": {
                "planned_start_date": kwargs["planned_start_date"],
                "planned_end_date": kwargs["planned_end_date"],
            },
            "complete_combinations": [],
            "segment_candidates": [],
            "conflicts": [],
        }

    connection = _make_connection(
        order={"case_no": "115000001", "status": "洽談中", "start_date": "2026-07-01", "end_date": "2026-07-02"},
        staff_rows=[{"id": 1}],
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(service, "derive_segment_availability", fake_derive_segment_availability)

    service.search_segmented_caregiver_availability("115000001", 2, [], "2026-01-01")

    assert captured["input"]["planned_start_date"] == "2026-07-01"
    assert captured["input"]["planned_end_date"] == "2026-07-02"


def test_search_segmented_blocks_assignment_reason_and_schedule_reason(monkeypatch):
    captured = {}

    def fake_derive_segment_availability(*args, **kwargs):
        captured["assignment_schedule_days"] = kwargs["assignment_schedule_days"]
        return {
            "validated_input": {
                "planned_start_date": kwargs["planned_start_date"],
                "planned_end_date": kwargs["planned_end_date"],
            },
            "complete_combinations": [],
            "segment_candidates": [],
            "conflicts": [],
        }

    connection = _make_connection(
        order={"case_no": "115000001", "status": "洽談中", "start_date": "2026-07-01", "end_date": "2026-07-02"},
        staff_rows=[{"id": 1}, {"id": 2}],
        assignments=[
            {"id": 101, "staff_id": 1, "assigned_start_date": "2026-07-01", "assigned_end_date": "2026-07-01"},
            {"id": 102, "staff_id": 2, "assigned_start_date": "2026-07-02", "assigned_end_date": "2026-07-03"},
        ],
        schedule_rows=[
            {"assignment_id": 101, "staff_id": 1, "work_date": "2026-07-01"},
            {"assignment_id": 102, "staff_id": 2, "work_date": "2026-07-02"},
        ],
        legacy_schedule_rows=[],
        active_lock_rows=[],
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(service, "derive_segment_availability", fake_derive_segment_availability)

    service.search_segmented_caregiver_availability("115000001", 2, [], "2026-07-10")

    items = {(item["staff_id"], item["work_date"], item["reason_code"]) for item in captured["assignment_schedule_days"]}
    assert (1, "2026-07-01", "assignment") in items
    assert (2, "2026-07-02", "assignment") in items
    assert (1, "2026-07-01", "schedule") in items
    assert (2, "2026-07-02", "schedule") in items


def test_search_segmented_passes_active_lock_rows_to_helper(monkeypatch):
    captured = {}

    def fake_derive_segment_availability(*args, **kwargs):
        captured["active_lock_days"] = kwargs["active_lock_days"]
        return {
            "validated_input": {
                "planned_start_date": kwargs["planned_start_date"],
                "planned_end_date": kwargs["planned_end_date"],
            },
            "complete_combinations": [],
            "segment_candidates": [],
            "conflicts": [],
        }

    connection = _make_connection(
        order={"case_no": "115000001", "status": "洽談中", "start_date": "2026-07-01", "end_date": "2026-07-02"},
        staff_rows=[{"id": 1}],
        assignments=[],
        schedule_rows=[],
        active_lock_rows=[
            {"staff_id": 1, "lock_date": "2026-07-01", "active_marker": 1},
            {"staff_id": 1, "lock_date": "2026-07-02", "active_marker": 0},
            {"staff_id": 1, "lock_date": "2026-07-03", "active_marker": None},
        ],
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(service, "derive_segment_availability", fake_derive_segment_availability)

    service.search_segmented_caregiver_availability("115000001", 2, [], "2026-07-10")

    rows = captured["active_lock_days"]
    assert len(rows) == 3
    assert rows[0]["lock_date"] == "2026-07-01"
    assert rows[0]["active_marker"] == 1


def test_search_segmented_normalizes_schedule_lock_dates_for_helper(monkeypatch):
    captured = {}

    def fake_derive_segment_availability(*args, **kwargs):
        captured["assignment_schedule_days"] = kwargs["assignment_schedule_days"]
        captured["active_lock_days"] = kwargs["active_lock_days"]
        return {
            "validated_input": {
                "planned_start_date": kwargs["planned_start_date"],
                "planned_end_date": kwargs["planned_end_date"],
            },
            "complete_combinations": [],
            "segment_candidates": [],
            "conflicts": [],
        }

    connection = _make_connection(
        order={"case_no": "115000001", "status": "洽談中", "start_date": date(2026, 7, 1), "end_date": "2026-07-03"},
        staff_rows=[{"id": 1}],
        schedule_rows=[{"assignment_id": 101, "staff_id": 1, "work_date": date(2026, 7, 1)}],
        legacy_schedule_rows=[{"staff_id": 1, "work_date": date(2026, 7, 2)}],
        active_lock_rows=[
            {"staff_id": 1, "lock_date": date(2026, 7, 1), "active_marker": 1},
            {"staff_id": 1, "lock_date": date(2026, 7, 2), "active_marker": 0},
        ],
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(service, "derive_segment_availability", fake_derive_segment_availability)

    service.search_segmented_caregiver_availability("115000001", 2, [], "2026-07-10")

    assignment_schedule_rows = captured["assignment_schedule_days"]
    lock_rows = captured["active_lock_days"]
    assert all(isinstance(row["work_date"], str) for row in assignment_schedule_rows)
    assert {row["work_date"] for row in assignment_schedule_rows} == {"2026-07-01", "2026-07-02"}
    assert all(isinstance(row["lock_date"], str) for row in lock_rows)
    assert lock_rows[0]["lock_date"] == "2026-07-01"
    assert lock_rows[1]["lock_date"] == "2026-07-02"


def test_search_segmented_readonly_invariants_and_sql_safety(monkeypatch):
    connection = _make_connection(
        order={"case_no": "115000001", "status": "洽談中", "start_date": "2026-07-01", "end_date": "2026-07-02"},
        staff_rows=[{"id": 1}],
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    service.search_segmented_caregiver_availability("115000001", 2, [], "2026-07-10")

    _assert_readonly_no_tx(connection)
    first_stmt = connection.cursor_obj.executed[0][0]
    assert "o.case_no" in first_stmt
    assert "O.STAFF_ID" not in first_stmt


def test_search_segmented_rejects_invalid_active_marker_via_helper(monkeypatch):
    connection = _make_connection(
        order={"case_no": "115000001", "status": "洽談中", "start_date": "2026-07-01", "end_date": "2026-07-02"},
        staff_rows=[{"id": 1}],
        active_lock_rows=[{"staff_id": 1, "lock_date": "2026-07-01", "active_marker": True}],
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="active_marker"):
        service.search_segmented_caregiver_availability("115000001", 2, [], "2026-07-10")
    _assert_readonly_no_tx(connection)


def test_search_segmented_closes_cursor_and_connection_on_helper_exception(monkeypatch):
    def raising_derive_segment_availability(*args, **kwargs):
        raise ValueError("helper failed")

    connection = _make_connection(
        order={"case_no": "115000001", "status": "洽談中", "start_date": "2026-07-01", "end_date": "2026-07-02"},
        staff_rows=[{"id": 1}],
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(service, "derive_segment_availability", raising_derive_segment_availability)

    with pytest.raises(ValueError, match="helper failed"):
        service.search_segmented_caregiver_availability("115000001", 2, [], "2026-07-10")

    assert connection.closed_count == 1
    assert connection.cursor_obj.closed_count == 1


def test_search_segmented_closes_cursor_and_connection_on_sql_exception(monkeypatch):
    connection = _make_connection(
        order={"case_no": "115000001", "status": "洽談中", "start_date": "2026-07-01", "end_date": "2026-07-02"},
        staff_rows=[{"id": 1}],
    )

    def fail_execute(*_args, **_kwargs):
        raise RuntimeError("database failed")

    connection.cursor_obj.execute = fail_execute
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="database failed"):
        service.search_segmented_caregiver_availability("115000001", 2, [], "2026-07-10")

    assert connection.closed_count == 1
    assert connection.closed
    assert connection.cursor_obj.closed_count == 1
