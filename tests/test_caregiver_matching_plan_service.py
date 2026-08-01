from __future__ import annotations

from datetime import date, timedelta

import pytest

from services import caregiver_matching_plan_service as service
from services.caregiver_segment_availability_service import derive_segment_availability


class MatchingPlanServiceCursor:
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.executed: list[tuple[str, tuple | None]] = []
        self.current = None
        self.closed = False
        self.closed_count = 0
        self.lastrowid = None
        self._insert_plan_id = fixtures.get("plan_id", 9000)
        self._raise_on_close = fixtures.get("raise_on_cursor_close", False)
        self._segment_insert_count = 0

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        self.executed.append((statement, tuple(params) if params is not None else None))

        raise_on_statement = self.fixtures.get("raise_on_statement")
        if raise_on_statement is not None:
            if isinstance(raise_on_statement, str):
                if raise_on_statement == statement or raise_on_statement in statement:
                    raise RuntimeError("injected sql failure")
            else:
                for token in raise_on_statement:
                    if token in statement:
                        raise RuntimeError("injected sql failure")

        if self.fixtures.get("raise_on_contains") and self.fixtures["raise_on_contains"] in statement:
            raise RuntimeError("injected sql failure")

        if "SELECT o.case_no" in statement and "FROM orders o" in statement:
            self.current = self.fixtures.get("order")
            return

        if "SELECT id, version, status, is_active" in statement and "caregiver_matching_plans" in statement:
            self.current = self.fixtures.get("plans", [])
            return

        if "SELECT l.id," in statement and "caregiver_availability_locks" in statement:
            lock_count = int(self.fixtures.get("active_lock_count", 0))
            if lock_count:
                self.current = [{"id": lock_count, "plan_id": self.fixtures.get("plan_id", 1), "status": "active", "is_active": 1}]
            else:
                self.current = []
            return

        if "SELECT ld.id," in statement and "caregiver_availability_lock_days" in statement:
            lock_day_count = int(self.fixtures.get("active_lock_day_count", 0))
            if lock_day_count:
                self.current = [
                    {
                        "id": lock_day_count,
                        "lock_id": self.fixtures.get("plan_id", 1),
                        "segment_id": 1,
                        "staff_id": 1,
                        "lock_date": "2026-07-01",
                        "active_marker": 1,
                    }
                ]
            else:
                self.current = []
            return

        if "SELECT p.id AS plan_id" in statement and "assigned_start_date" in statement:
            self.current = self.fixtures.get("segments", [])
            return

        if "SELECT MAX(version) AS max_version" in statement and "caregiver_matching_plans" in statement:
            self.current = {"max_version": self.fixtures.get("max_version", 0)}
            return

        if "INSERT INTO caregiver_matching_plans" in statement:
            self.current = None
            self.lastrowid = self._insert_plan_id
            self._insert_plan_id += 1
            return

        if "INSERT INTO caregiver_matching_plan_segments" in statement:
            self._segment_insert_count += 1
            if self._segment_insert_count == self.fixtures.get(
                "raise_on_segment_insert_number"
            ):
                raise RuntimeError("injected segment insert failure")
            self.current = None
            return

        if "UPDATE caregiver_matching_plans" in statement:
            self.current = {"affected": 1}
            return

        self.current = None

    def fetchone(self):
        result = self.current
        self.current = None
        if result is None:
            return None
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def fetchall(self):
        result = self.current
        self.current = None
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return [result]

    def close(self):
        if self._raise_on_close:
            self._raise_on_close = False
            raise RuntimeError("injected cursor close failure")
        self.closed = True
        self.closed_count += 1


class MatchingPlanServiceConnection:
    def __init__(self, fixtures):
        self.cursor_obj = MatchingPlanServiceCursor(fixtures)
        self.closed = False
        self.closed_count = 0
        self.commits = 0
        self.rollbacks = 0
        self._raise_on_commit = fixtures.get("raise_on_commit", False)
        self._raise_on_rollback = fixtures.get("raise_on_rollback", False)
        self._raise_on_close = fixtures.get("raise_on_connection_close", False)
        self._raise_on_cursor_create = fixtures.get("raise_on_cursor_create", False)

    def cursor(self):
        if self._raise_on_cursor_create:
            raise RuntimeError("injected cursor creation failure")
        return self.cursor_obj

    def commit(self):
        if self._raise_on_commit:
            self._raise_on_commit = False
            raise RuntimeError("injected commit failure")
        self.commits += 1

    def rollback(self):
        if self._raise_on_rollback:
            self._raise_on_rollback = False
            raise RuntimeError("injected rollback failure")
        self.rollbacks += 1

    def close(self):
        if self._raise_on_close:
            self._raise_on_close = False
            raise RuntimeError("injected connection close failure")
        self.closed = True
        self.closed_count += 1


class MatchingPlanServicePymysqlConnection:
    def __init__(self, fixtures):
        self.cursor_obj = MatchingPlanServiceCursor(fixtures)
        self.open = True
        self.closed_count = 0
        self.commits = 0
        self.rollbacks = 0
        self._raise_on_commit = fixtures.get("raise_on_commit", False)
        self._raise_on_rollback = fixtures.get("raise_on_rollback", False)
        self._raise_on_close = fixtures.get("raise_on_connection_close", False)
        self._raise_on_cursor_create = fixtures.get("raise_on_cursor_create", False)

    def cursor(self):
        if self._raise_on_cursor_create:
            raise RuntimeError("injected cursor creation failure")
        return self.cursor_obj

    def commit(self):
        if self._raise_on_commit:
            self._raise_on_commit = False
            raise RuntimeError("injected commit failure")
        self.commits += 1

    def rollback(self):
        if self._raise_on_rollback:
            self._raise_on_rollback = False
            raise RuntimeError("injected rollback failure")
        self.rollbacks += 1

    def close(self):
        if self._raise_on_close:
            self._raise_on_close = False
            raise RuntimeError("injected connection close failure")
        self.open = False
        self.closed_count += 1


def _segments(count: int, start_day: str = "2026-07-01", staff_start: int = 1):
    base = date.fromisoformat(start_day)
    normalized: list[dict[str, int | str]] = []
    for index in range(count):
        current = base + timedelta(days=index)
        normalized.append(
            {
                "staff_id": staff_start + index,
                "assigned_start_date": current.isoformat(),
                "assigned_end_date": current.isoformat(),
            }
        )
    return normalized


def _complete_result(segments):
    return {
        "feasibility": "complete",
        "complete_combinations": [
            [
                {
                    "segment_index": index,
                    "staff_id": segment["staff_id"],
                    "start_date": segment["assigned_start_date"],
                    "end_date": segment["assigned_end_date"],
                }
                for index, segment in enumerate(segments)
            ]
        ],
        "segment_candidates": [],
        "conflicts": [],
    }


def _to_cursor_segments(segments):
    return [
        {
            "plan_id": 1,
            "segment_order": i + 1,
            "staff_id": segment["staff_id"],
            "assigned_start_date": segment["assigned_start_date"],
            "assigned_end_date": segment["assigned_end_date"],
        }
        for i, segment in enumerate(segments)
    ]


def _assert_sql_is_parameterized(executed: list[tuple[str, tuple | None]], includes: list[str]):
    for statement, _params in executed:
        if statement.strip().upper().startswith(tuple(includes)):
            assert "%s" in statement


def _assert_selected_max_version(executed: list[tuple[str, tuple | None]], case_no: str):
    assert any(
        statement.startswith("SELECT MAX(version) AS max_version")
        and "FROM caregiver_matching_plans" in statement
        and statement_params == (case_no,)
        for statement, statement_params in executed
    )


def _assert_for_update_on_table(executed: list[tuple[str, tuple | None]], table_sql: str):
    assert any(table_sql in statement and "FOR UPDATE" in statement for statement, _ in executed)


def _assert_no_aggregate_for_active_lock(executed: list[tuple[str, tuple | None]]):
    for statement, _ in executed:
        if "caregiver_availability_locks" in statement:
            assert "COUNT(" not in statement
            assert "SUM(" not in statement


def _assert_execution_order(executed: list[tuple[str, tuple | None]], ordered_patterns: list[str]):
    positions: list[int] = []
    for pattern in ordered_patterns:
        position = next(
            idx
            for idx, (statement, _) in enumerate(executed)
            if pattern in statement
        )
        positions.append(position)
    assert positions == sorted(positions)


def test_create_matching_plan_version_created_for_1_2_3_4_segments(monkeypatch):
    for segment_count in (1, 2, 3, 4):
        segments = _segments(segment_count, staff_start=11)
        captured: dict[str, object] = {}

        def fake_search_segmented(*, case_no, segment_count, segment_drafts, as_of):
            captured["case_no"] = case_no
            captured["as_of"] = as_of
            captured["segment_drafts"] = segment_drafts
            captured["segment_count"] = segment_count
            return _complete_result(segments)

        monkeypatch.setattr(service, "search_segmented_caregiver_availability", fake_search_segmented)

        connection = MatchingPlanServiceConnection(
            {
                "order": {
                    "case_no": "C-001",
                    "status": "洽談中",
                    "start_date": segments[0]["assigned_start_date"],
                    "end_date": segments[-1]["assigned_end_date"],
                },
                "plans": [],
                "active_lock_count": 0,
                "segments": [],
                "plan_id": 2201,
            }
        )

        def fake_connection():
            return connection

        monkeypatch.setattr(service, "get_connection", fake_connection)

        result = service.create_matching_plan_version(
            case_no=" C-001 ",
            segments=segments,
            created_by=" admin ",
            as_of="2026-07-02",
        )

        assert result["result"] == "created"
        assert result["status"] == "proposed"
        assert result["plan_id"] == 2201
        assert result["version"] == 1
        assert result["case_no"] == "C-001"
        assert len(result["segments"]) == segment_count
        assert connection.commits == 1
        assert connection.rollbacks == 0
        assert connection.closed_count == 1
        assert connection.cursor_obj.closed_count == 1
        assert captured["case_no"] == "C-001"
        assert captured["as_of"] == "2026-07-02"
        assert captured["segment_count"] == segment_count
        assert captured["segment_drafts"] == [
            {"staff_id": segment["staff_id"], "start_date": segment["assigned_start_date"], "end_date": segment["assigned_end_date"]}
            for segment in segments
        ]
        _assert_sql_is_parameterized(connection.cursor_obj.executed, ["SELECT", "UPDATE", "INSERT"])
        _assert_for_update_on_table(connection.cursor_obj.executed, "WHERE o.case_no = %s")
        _assert_for_update_on_table(connection.cursor_obj.executed, "FROM caregiver_matching_plans")
        _assert_for_update_on_table(connection.cursor_obj.executed, "FROM caregiver_matching_plan_segments")
        _assert_for_update_on_table(connection.cursor_obj.executed, "FROM caregiver_availability_locks")
        _assert_for_update_on_table(connection.cursor_obj.executed, "FROM caregiver_availability_lock_days")
        _assert_execution_order(
            connection.cursor_obj.executed,
            [
                "FROM orders o",
                "FROM caregiver_matching_plans",
                "FROM caregiver_matching_plan_segments",
                "FROM caregiver_availability_locks",
                "FROM caregiver_availability_lock_days",
            ],
        )
        _assert_no_aggregate_for_active_lock(connection.cursor_obj.executed)
        _assert_selected_max_version(connection.cursor_obj.executed, "C-001")


def test_real_availability_helper_zero_based_payload_is_accepted(monkeypatch):
    segments = _segments(2, staff_start=141)

    def real_helper_search(**kwargs):
        result = derive_segment_availability(
            planned_start_date="2026-07-01",
            planned_end_date="2026-07-02",
            segment_count=kwargs["segment_count"],
            segment_drafts=kwargs["segment_drafts"],
            candidate_staff_ids=[141, 142],
            assignment_schedule_days=[],
            active_lock_days=[],
        )
        result["feasibility"] = (
            "complete" if result["complete_combinations"] else "partial"
        )
        return result

    connection = MatchingPlanServicePymysqlConnection(
        {
            "order": {
                "case_no": "C-030",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [],
            "segments": [],
            "active_lock_count": 0,
            "plan_id": 9701,
            "max_version": 0,
        }
    )
    monkeypatch.setattr(service, "search_segmented_caregiver_availability", real_helper_search)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.create_matching_plan_version(
        "C-030", segments, "admin", "2026-07-02"
    )

    assert result["result"] == "created"
    assert result["plan_id"] == 9701


def test_reusing_identical_active_proposed_plan_is_idempotent_no_commit(monkeypatch):
    segments = _segments(2, staff_start=21)
    cursor_segments = _to_cursor_segments(segments)
    plan_id = 3301
    captured: list[dict] = []

    def fake_search_segmented(**_kwargs):
        captured.append(_kwargs)
        return _complete_result(segments)

    monkeypatch.setattr(service, "search_segmented_caregiver_availability", fake_search_segmented)

    connection = MatchingPlanServiceConnection(
        {
            "order": {
                "case_no": "C-002",
                "status": "洽談中",
                "start_date": segments[0]["assigned_start_date"],
                "end_date": segments[-1]["assigned_end_date"],
            },
            "plans": [
                {
                    "id": plan_id,
                    "version": 3,
                    "status": "proposed",
                    "is_active": 1,
                }
            ],
            "segments": [
                {**entry, "plan_id": plan_id} for entry in cursor_segments
            ],
            "active_lock_count": 0,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.create_matching_plan_version(
        case_no="C-002",
        segments=segments,
        created_by="admin",
        as_of="2026-07-20",
    )

    assert result["result"] == "existing"
    assert result["version"] == 3
    assert result["plan_id"] == plan_id
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed_count == 1
    assert connection.cursor_obj.closed_count == 1
    assert len(captured) == 1
    assert all("INSERT INTO caregiver_matching_plans" not in s for s, _ in connection.cursor_obj.executed)
    assert all("UPDATE caregiver_matching_plans" not in s for s, _ in connection.cursor_obj.executed)


def test_modify_segments_creates_new_version_and_supersedes_old_draft(monkeypatch):
    new_segments = _segments(2, staff_start=31)
    old_segments = _segments(2, staff_start=99)
    connection = MatchingPlanServiceConnection(
        {
            "order": {
                "case_no": "C-003",
                "status": "洽談中",
                "start_date": old_segments[0]["assigned_start_date"],
                "end_date": old_segments[-1]["assigned_end_date"],
            },
            "plans": [
                {
                    "id": 4100,
                    "version": 4,
                    "status": "draft",
                    "is_active": 1,
                }
            ],
            "max_version": 4,
            "segments": [
                {**entry, "plan_id": 4100} for entry in _to_cursor_segments(old_segments)
            ],
            "active_lock_count": 0,
            "plan_id": 4101,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(new_segments),
    )

    result = service.create_matching_plan_version(
        case_no="C-003",
        segments=new_segments,
        created_by="ops",
        as_of="2026-07-20",
    )

    assert result["result"] == "created"
    assert result["version"] == 5
    assert result["plan_id"] == 4101
    assert any("UPDATE caregiver_matching_plans" in s for s, _ in connection.cursor_obj.executed)
    assert any("INSERT INTO caregiver_matching_plan_segments" in s for s, _ in connection.cursor_obj.executed)
    assert connection.commits == 1
    assert connection.closed_count == 1


@pytest.mark.parametrize(
    "segments, as_of",
    [
        (
            [
                {"staff_id": 1, "start_date": "2026-07-01", "end_date": "2026-07-02"},
                {"staff_id": 1, "start_date": "2026-07-03", "end_date": "2026-07-04"},
            ],
            "2026-07-02",
        ),
    ],
)
def test_input_normalization_validation_rejects_invalid_segments(monkeypatch, segments, as_of):
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not hit DB on validation failure"))
    with pytest.raises(ValueError):
        service.create_matching_plan_version("C-004", segments, "admin", as_of)


@pytest.mark.parametrize(
    "case_no, created_by, as_of, segments",
    [
        (None, "admin", "2026-07-02", _segments(2)),
        ("C-005", "", "2026-07-02", _segments(2)),
        ("C-006", "admin", "2026/07/02", _segments(2)),
        ("C-006A", "admin", None, _segments(2)),
        ("C-007", "admin", "2026-07-02", []),
        ("C-008", "admin", "2026-07-02", _segments(5)),
        ("C-009", "admin", "2026-07-02", [{"staff_id": True, "start_date": "2026-07-01", "end_date": "2026-07-02"}]),
        ("C-009A", "admin", "2026-07-02", [
            {"staff_id": 0, "start_date": "2026-07-01", "end_date": "2026-07-01"},
            {"staff_id": 2, "start_date": "2026-07-02", "end_date": "2026-07-02"},
        ]),
        ("C-009B", "admin", "2026-07-02", [
            {"staff_id": -1, "start_date": "2026-07-01", "end_date": "2026-07-01"},
            {"staff_id": 2, "start_date": "2026-07-02", "end_date": "2026-07-02"},
        ]),
        ("C-009C", "admin", "2026-07-02", [
            {"staff_id": "1", "start_date": "2026-07-01", "end_date": "2026-07-01"},
            {"staff_id": 2, "start_date": "2026-07-02", "end_date": "2026-07-02"},
        ]),
        ("C-009D", "admin", "2026-07-02", [
            {"staff_id": 1, "start_date": "2026-07-01", "end_date": "2026-07-02"},
            {"staff_id": 2, "start_date": "2026-07-02", "end_date": "2026-07-03"},
        ]),
        ("C-009E", "admin", "2026-07-02", [
            {"staff_id": 1, "start_date": "2026-07-01", "end_date": "2026-07-01"},
            {"staff_id": 2, "start_date": "2026-07-03", "end_date": "2026-07-03"},
        ]),
        (
            "C-010",
            "admin",
            "2026-07-02",
            [
                {
                    "staff_id": 1,
                    "start_date": "2026-07-02",
                    "end_date": "2026-07-01",
                },
                {
                    "staff_id": 2,
                    "start_date": "2026-07-03",
                    "end_date": "2026-07-04",
                },
            ],
        ),
        (
            "C-011",
            "admin",
            "2026-07-02",
            [
                {"staff_id": 1, "start_date": "2026-07-01", "end_date": "2026-07-01", "bad": 1},
                {"staff_id": 2, "start_date": "2026-07-02", "end_date": "2026-07-02"},
            ],
        ),
    ],
)
def test_input_validation_without_db(monkeypatch, case_no, created_by, as_of, segments):
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("should not hit DB on input validation"))
    with pytest.raises(ValueError):
        service.create_matching_plan_version(case_no, segments, created_by, as_of)


def test_partial_or_mismatch_availability_rejects_without_tx(monkeypatch):
    def fake_search(**_kwargs):
        return {
            "feasibility": "partial",
            "complete_combinations": [],
            "segment_candidates": [],
            "conflicts": [],
        }

    monkeypatch.setattr(service, "search_segmented_caregiver_availability", fake_search)
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("should not open tx"))
    with pytest.raises(ValueError, match="complete combination"):
        service.create_matching_plan_version(
            "C-012",
            _segments(2),
            "admin",
            "2026-07-02",
        )

    def fake_search_mismatch(**_kwargs):
        return {
            "feasibility": "complete",
            "complete_combinations": [
                [
                    {
                        "segment_index": 0,
                        "staff_id": 999,
                        "start_date": "2026-07-01",
                        "end_date": "2026-07-01",
                    }
                ],
            ],
            "segment_candidates": [],
            "conflicts": [],
        }

    monkeypatch.setattr(service, "search_segmented_caregiver_availability", fake_search_mismatch)
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("should not open tx"))
    with pytest.raises(ValueError, match="complete combination"):
        service.create_matching_plan_version(
            "C-013",
            _segments(2, staff_start=3),
            "admin",
            "2026-07-02",
        )


def test_conflicts_rejects_without_tx(monkeypatch):
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: {
            "feasibility": "complete",
            "complete_combinations": [
                [
                    {
                        "segment_index": 0,
                        "staff_id": 1,
                        "start_date": "2026-07-01",
                        "end_date": "2026-07-01",
                    }
                ]
            ],
            "segment_candidates": [],
            "conflicts": [
                {"reason": "staff_not_available", "segment_index": 0, "staff_id": 1, "work_date": "2026-07-01"}
            ],
        },
    )

    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("should not open tx"))
    with pytest.raises(ValueError, match="complete combination"):
        service.create_matching_plan_version("C-019", _segments(2), "admin", "2026-07-02")


def test_reject_when_case_not_found_or_not_in_negotiation(monkeypatch):
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(_segments(2)),
    )

    connection_not_found = MatchingPlanServiceConnection({"order": None})
    monkeypatch.setattr(service, "get_connection", lambda: connection_not_found)
    with pytest.raises(ValueError, match="case not found"):
        service.create_matching_plan_version("C-014", _segments(2), "admin", "2026-07-02")
    assert connection_not_found.rollbacks == 1
    assert connection_not_found.closed_count == 1

    connection_non_negotiation = MatchingPlanServiceConnection(
        {
            "order": {
                "case_no": "C-015",
                "status": "已成交",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection_non_negotiation)
    with pytest.raises(ValueError, match="negotiation"):
        service.create_matching_plan_version("C-015", _segments(2), "admin", "2026-07-02")
    assert connection_non_negotiation.rollbacks == 1
    assert connection_non_negotiation.closed_count == 1


def test_db_max_version_controls_next_version(monkeypatch):
    segments = _segments(2, staff_start=61)
    connection = MatchingPlanServiceConnection(
        {
            "order": {
                "case_no": "C-021",
                "status": "洽談中",
                "start_date": segments[0]["assigned_start_date"],
                "end_date": segments[-1]["assigned_end_date"],
            },
            "plans": [
                {
                    "id": 8100,
                    "version": 3,
                    "status": "draft",
                    "is_active": 1,
                }
            ],
            "segments": [],
            "active_lock_count": 0,
            "max_version": 12,
            "plan_id": 8101,
        }
    )

    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(segments),
    )

    result = service.create_matching_plan_version(
        case_no="C-021",
        segments=segments,
        created_by="admin",
        as_of="2026-07-02",
    )

    assert result["version"] == 13
    assert any(
        statement.startswith("SELECT MAX(version) AS max_version")
        and "FROM caregiver_matching_plans" in statement
        and params == ("C-021",)
        for statement, params in connection.cursor_obj.executed
    )
def test_reject_when_accepted_plan_or_active_lock_exists(monkeypatch):
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(_segments(2)),
    )

    connection_accepted = MatchingPlanServiceConnection(
        {
            "order": {
                "case_no": "C-016",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [
                {"id": 501, "version": 2, "status": "accepted", "is_active": None},
                {"id": 500, "version": 1, "status": "draft", "is_active": None},
            ],
            "active_lock_count": 0,
            "segments": [],
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection_accepted)
    with pytest.raises(ValueError, match="accepted"):
        service.create_matching_plan_version("C-016", _segments(2), "admin", "2026-07-02")
    assert connection_accepted.rollbacks == 1
    assert connection_accepted.closed_count == 1

    connection_locked = MatchingPlanServiceConnection(
        {
            "order": {
                "case_no": "C-017",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [],
            "segments": [],
            "active_lock_count": 1,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection_locked)
    with pytest.raises(ValueError, match="active availability lock"):
        service.create_matching_plan_version("C-017", _segments(2), "admin", "2026-07-02")
    assert connection_locked.rollbacks == 1
    assert connection_locked.closed_count == 1

    connection_locked_day_only = MatchingPlanServiceConnection(
        {
            "order": {
                "case_no": "C-025",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [],
            "segments": [],
            "active_lock_count": 0,
            "active_lock_day_count": 1,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection_locked_day_only)
    with pytest.raises(ValueError, match="active availability lock"):
        service.create_matching_plan_version("C-025", _segments(2), "admin", "2026-07-02")
    assert connection_locked_day_only.rollbacks == 1
    assert connection_locked_day_only.closed_count == 1


@pytest.mark.parametrize(
    "label,connection_options,raises_msg",
    [
        ("orders select", {"raise_on_statement": "SELECT o.case_no, o.status, o.start_date, o.end_date FROM orders o WHERE o.case_no = %s FOR UPDATE"}, "injected sql failure"),
        ("plans select", {"raise_on_statement": "SELECT id, version, status, is_active FROM caregiver_matching_plans WHERE case_no = %s ORDER BY version DESC FOR UPDATE"}, "injected sql failure"),
        ("locks select", {"raise_on_statement": "SELECT l.id, l.plan_id, l.status, l.is_active FROM caregiver_availability_locks l INNER JOIN caregiver_matching_plans p ON p.id = l.plan_id WHERE p.case_no = %s AND l.status = 'active' AND l.is_active = 1 FOR UPDATE"}, "injected sql failure"),
        ("segments select", {"raise_on_statement": "SELECT p.id AS plan_id, s.segment_order, s.staff_id, s.assigned_start_date, s.assigned_end_date FROM caregiver_matching_plan_segments s INNER JOIN caregiver_matching_plans p ON p.id = s.plan_id WHERE p.case_no = %s ORDER BY p.id, s.segment_order FOR UPDATE"}, "injected sql failure"),
        ("lock days select", {"raise_on_statement": "SELECT ld.id, ld.lock_id, ld.segment_id, ld.staff_id, ld.lock_date, ld.active_marker FROM caregiver_availability_lock_days ld INNER JOIN caregiver_availability_locks l ON l.id = ld.lock_id INNER JOIN caregiver_matching_plans p ON p.id = l.plan_id WHERE p.case_no = %s AND l.status = 'active' AND l.is_active = 1 AND ld.active_marker = 1 FOR UPDATE"}, "injected sql failure"),
        ("version select", {"raise_on_statement": "SELECT MAX(version) AS max_version FROM caregiver_matching_plans WHERE case_no = %s"}, "injected sql failure"),
        ("supersede update", {"raise_on_statement": "UPDATE caregiver_matching_plans SET status = 'superseded', is_active = NULL WHERE case_no = %s AND is_active = 1 AND status IN ('draft', 'proposed')"}, "injected sql failure"),
            ("plan insert", {"raise_on_statement": "INSERT INTO caregiver_matching_plans (case_no, version, status, is_active, start_date, end_date, created_by) VALUES (%s, %s, 'proposed', 1, %s, %s, %s)"}, "injected sql failure"),
        ("segment insert", {"raise_on_statement": "INSERT INTO caregiver_matching_plan_segments (plan_id, segment_order, staff_id, assigned_start_date, assigned_end_date) VALUES (%s, %s, %s, %s, %s)"}, "injected sql failure"),
        ("commit", {"raise_on_commit": True}, "injected commit failure"),
    ],
)
def test_injection_failure_rolls_back(monkeypatch, label, connection_options, raises_msg):
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(_segments(2, staff_start=81)),
    )
    connection = MatchingPlanServiceConnection(
        {
            "order": {
                "case_no": "C-018",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [
                {
                    "id": 6000,
                    "version": 1,
                    "status": "draft",
                    "is_active": 1,
                }
            ],
            "segments": [],
            "active_lock_count": 0,
            "plan_id": 6001,
            "max_version": 1,
            **connection_options,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    with pytest.raises(RuntimeError, match=raises_msg):
        service.create_matching_plan_version(
            "C-018", _segments(2, staff_start=81), "admin", "2026-07-02"
        )

    assert connection.commits == 0
    assert connection.closed_count == 1
    assert connection.rollbacks == 1


def test_cursor_close_failure_still_closes_connection(monkeypatch):
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(_segments(2, staff_start=91)),
    )
    connection = MatchingPlanServiceConnection(
        {
            "order": {
                "case_no": "C-022",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [],
            "segments": [],
            "active_lock_count": 0,
            "plan_id": 9101,
            "max_version": 0,
            "raise_on_cursor_close": True,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="injected cursor close failure"):
        service.create_matching_plan_version("C-022", _segments(2, staff_start=91), "admin", "2026-07-02")

    assert connection.closed_count == 1


def test_pymysql_connection_compatible_success(monkeypatch):
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(_segments(2, staff_start=101)),
    )
    connection = MatchingPlanServicePymysqlConnection(
        {
            "order": {
                "case_no": "C-023",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [],
            "segments": [],
            "active_lock_count": 0,
            "plan_id": 9201,
            "max_version": 0,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.create_matching_plan_version(
        "C-023", _segments(2, staff_start=101), "admin", "2026-07-02"
    )

    assert result["result"] == "created"
    assert result["plan_id"] == 9201
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed_count == 1
    assert connection.open is False
    assert connection.cursor_obj.closed_count == 1


def test_pymysql_connection_existing_returns_existing_and_rolls_back(monkeypatch):
    segments = _segments(2, staff_start=102)
    cursor_segments = [
        {**entry, "plan_id": 9301}
        for entry in _to_cursor_segments(segments)
    ]
    connection = MatchingPlanServicePymysqlConnection(
        {
            "order": {
                "case_no": "C-024",
                "status": "洽談中",
                "start_date": segments[0]["assigned_start_date"],
                "end_date": segments[-1]["assigned_end_date"],
            },
            "plans": [
                {
                    "id": 9301,
                    "version": 1,
                    "status": "proposed",
                    "is_active": 1,
                }
            ],
            "segments": cursor_segments,
            "active_lock_count": 0,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(segments),
    )

    result = service.create_matching_plan_version(
        "C-024", _segments(2, staff_start=102), "admin", "2026-07-02"
    )

    assert result["result"] == "existing"
    assert result["version"] == 1
    assert result["plan_id"] == 9301
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed_count == 1
    assert connection.open is False
    assert connection.cursor_obj.closed_count == 1


def test_pymysql_connection_failure_rolls_back(monkeypatch):
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(_segments(2, staff_start=104)),
    )
    connection = MatchingPlanServicePymysqlConnection(
        {
            "order": {
                "case_no": "C-025",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [
                {
                    "id": 9401,
                    "version": 1,
                    "status": "draft",
                    "is_active": 1,
                }
            ],
            "segments": [],
            "active_lock_count": 0,
            "plan_id": 9402,
            "max_version": 1,
            "raise_on_statement": "INSERT INTO caregiver_matching_plan_segments (plan_id, segment_order, staff_id, assigned_start_date, assigned_end_date) VALUES (%s, %s, %s, %s, %s)",
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="injected sql failure"):
        service.create_matching_plan_version(
            "C-025", _segments(2, staff_start=104), "admin", "2026-07-02"
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed_count == 1
    assert connection.open is False
    assert connection.cursor_obj.closed_count == 1


def test_pymysql_cursor_close_failure_still_closes_connection(monkeypatch):
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(_segments(2, staff_start=103)),
    )
    connection = MatchingPlanServicePymysqlConnection(
        {
            "order": {
                "case_no": "C-026",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [],
            "segments": [],
            "active_lock_count": 0,
            "plan_id": 9302,
            "max_version": 0,
            "raise_on_cursor_close": True,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="injected cursor close failure"):
        service.create_matching_plan_version(
            "C-026", _segments(2, staff_start=103), "admin", "2026-07-02"
        )

    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed_count == 1
    assert connection.open is False
    assert connection.cursor_obj.closed_count == 0


def test_pymysql_cursor_creation_failure_rolls_back_and_closes_connection(monkeypatch):
    segments = _segments(2, staff_start=111)
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(segments),
    )
    connection = MatchingPlanServicePymysqlConnection(
        {"raise_on_cursor_create": True}
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="injected cursor creation failure"):
        service.create_matching_plan_version(
            "C-027", segments, "admin", "2026-07-02"
        )

    assert connection.rollbacks == 1
    assert connection.closed_count == 1
    assert connection.open is False


def test_pymysql_connection_close_failure_is_reported(monkeypatch):
    segments = _segments(2, staff_start=121)
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(segments),
    )
    connection = MatchingPlanServicePymysqlConnection(
        {
            "order": {
                "case_no": "C-028",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [],
            "segments": [],
            "active_lock_count": 0,
            "plan_id": 9501,
            "max_version": 0,
            "raise_on_connection_close": True,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="injected connection close failure"):
        service.create_matching_plan_version(
            "C-028", segments, "admin", "2026-07-02"
        )

    assert connection.commits == 1
    assert connection.cursor_obj.closed_count == 1


def test_second_segment_insert_failure_rolls_back(monkeypatch):
    segments = _segments(2, staff_start=131)
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **_kwargs: _complete_result(segments),
    )
    connection = MatchingPlanServicePymysqlConnection(
        {
            "order": {
                "case_no": "C-029",
                "status": "洽談中",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
            "plans": [],
            "segments": [],
            "active_lock_count": 0,
            "plan_id": 9601,
            "max_version": 0,
            "raise_on_segment_insert_number": 2,
        }
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="injected segment insert failure"):
        service.create_matching_plan_version(
            "C-029", segments, "admin", "2026-07-02"
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed_count == 1
    assert connection.open is False
