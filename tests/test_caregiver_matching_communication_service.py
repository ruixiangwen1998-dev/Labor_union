from datetime import date, datetime

import pytest

from services import caregiver_matching_communication_service as service


class _Cursor:
    def __init__(self, *, existing_events=None):
        self.calls = []
        self._result = None
        self.rowcount = 1
        self.lastrowid = 100
        self.closed = 0
        self.existing_events = existing_events or []

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        compact = " ".join(sql.split()).lower()
        self._result = None
        self.rowcount = 1
        if compact.startswith("select p.id, p.case_no"):
            self._result = {
                "id": 7,
                "case_no": "CASE-1",
                "version": 1,
                "status": "proposed",
                "is_active": 1,
                "order_status": "洽談中",
                "client_line_user_id": "U-client",
            }
        elif compact.startswith("select id from caregiver_matching_plans"):
            self._result = {"id": 7}
        elif compact.startswith("select s.id as segment_id"):
            self._result = [
                {
                    "segment_id": 71,
                    "segment_order": 1,
                    "staff_id": 101,
                    "assigned_start_date": date(2026, 8, 1),
                    "assigned_end_date": date(2026, 8, 10),
                    "staff_name": "A",
                    "staff_line_user_id": "U-a",
                },
                {
                    "segment_id": 72,
                    "segment_order": 2,
                    "staff_id": 102,
                    "assigned_start_date": date(2026, 8, 11),
                    "assigned_end_date": date(2026, 8, 20),
                    "staff_name": "B",
                    "staff_line_user_id": "U-b",
                },
            ]
        elif compact.startswith("select id, segment_id, event_type"):
            self._result = self.existing_events
        elif compact.startswith("select id, event_key, plan_id"):
            self._result = []
        elif compact.startswith("select id, plan_id, segment_id, event_type"):
            self._result = None
        elif compact.startswith("select id as lock_id"):
            self._result = {
                "lock_id": 77,
                "plan_id": 7,
                "status": "active",
                "created_by": "admin",
                "created_at": datetime(2026, 7, 3),
            }
        elif compact.startswith("select deposit_receivable"):
            self._result = {
                "deposit_receivable": 1000,
                "deposit_received": 1000,
                "deposit_received_at": date(2026, 7, 4),
            }
        elif compact.startswith("select p.status, p.is_active"):
            self._result = {
                "status": "proposed",
                "is_active": 1,
                "order_status": "洽談中",
            }
        elif compact.startswith("insert into caregiver_matching_plan_events"):
            self.lastrowid += 1

    def fetchone(self):
        return self._result

    def fetchall(self):
        if self._result is None:
            return []
        return self._result if isinstance(self._result, list) else [self._result]

    def close(self):
        self.closed += 1


class _Connection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def test_contact_state_derives_latest_per_segment_willingness_and_send_history(
    monkeypatch,
):
    events = [
        {
            "id": 1,
            "segment_id": 71,
            "event_type": "info_1_sent",
            "event_key": "i1",
            "actor": "admin",
            "payload": '{"delivery_status":"queued"}',
            "occurred_at": datetime(2026, 7, 1),
        },
        {
            "id": 2,
            "segment_id": 71,
            "event_type": "willingness_changed",
            "event_key": "w1",
            "actor": "admin",
            "payload": '{"willingness":"willing"}',
            "occurred_at": datetime(2026, 7, 2),
        },
        {
            "id": 3,
            "segment_id": 72,
            "event_type": "willingness_changed",
            "event_key": "w2",
            "actor": "admin",
            "payload": '{"willingness":"pending"}',
            "occurred_at": datetime(2026, 7, 2),
        },
    ]
    cursor = _Cursor(existing_events=events)
    monkeypatch.setattr(service, "get_connection", lambda: _Connection(cursor))

    state = service.get_matching_plan_contact_state("CASE-1", 7)

    assert state["segments"][0]["info_1_sent"] is True
    assert state["segments"][0]["willingness"] == "willing"
    assert state["segments"][1]["willingness"] == "pending"
    assert state["all_willing"] is False


def test_active_matching_plan_state_reloads_lock_and_deposit(monkeypatch):
    cursor = _Cursor()
    monkeypatch.setattr(service, "get_connection", lambda: _Connection(cursor))

    state = service.get_active_matching_plan_state("CASE-1")

    assert state["plan"]["id"] == 7
    assert state["availability_lock"]["lock_id"] == 77
    assert state["deposit"]["deposit_received"] == 1000
    assert [segment["segment_id"] for segment in state["segments"]] == [71, 72]


def test_resume_delivery_is_individual_atomic_and_adds_multi_caregiver_note(
    monkeypatch,
):
    state = {
        "plan": {
            "case_no": "CASE-1",
            "status": "proposed",
            "is_active": 1,
            "client_line_user_id": "U-client",
        },
        "segments": [
            {
                "segment_id": 71,
                "segment_order": 1,
                "staff_id": 101,
                "assigned_start_date": "2026-08-01",
                "assigned_end_date": "2026-08-10",
                "willingness": "willing",
            },
            {
                "segment_id": 72,
                "segment_order": 2,
                "staff_id": 102,
                "assigned_start_date": "2026-08-11",
                "assigned_end_date": "2026-08-20",
                "willingness": "willing",
            },
        ],
        "all_willing": True,
    }
    cursor = _Cursor()
    connection = _Connection(cursor)
    tasks = []
    monkeypatch.setattr(
        service, "get_matching_plan_contact_state", lambda case_no, plan_id: state
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "enqueue_line_task",
        lambda cursor, **kwargs: tasks.append(kwargs) or len(tasks),
    )

    result = service.send_matching_plan_resumes(
        "CASE-1", 7, "請確認服務安排", "resume-batch", "admin"
    )

    assert result["status"] == "sent"
    assert len(tasks) == 2
    assert all(task["to_user_id"] == "U-client" for task in tasks)
    assert all("由多位月嫂共同完成" in task["message_content"] for task in tasks)
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert len(result["event_ids"]) == 2


def test_resume_delivery_gate_fails_before_any_write_when_one_caregiver_not_willing(
    monkeypatch,
):
    state = {
        "plan": {"client_line_user_id": "U-client"},
        "segments": [
            {
                "segment_id": 71,
                "staff_id": 101,
                "willingness": "pending",
            }
        ],
        "all_willing": False,
    }
    monkeypatch.setattr(
        service, "get_matching_plan_contact_state", lambda case_no, plan_id: state
    )
    monkeypatch.setattr(
        service,
        "get_connection",
        lambda: pytest.fail("database write must not start before willingness gate"),
    )

    with pytest.raises(ValueError, match="all caregivers must be willing"):
        service.send_matching_plan_resumes(
            "CASE-1", 7, "note", "resume-batch", "admin"
        )


def test_information_send_revalidates_latest_full_plan_before_queueing(monkeypatch):
    cursor = _Cursor()
    connection = _Connection(cursor)
    queued = []
    monkeypatch.setattr(
        service,
        "get_matching_plan_contact_state",
        lambda case_no, plan_id: {
            "plan": {
                "case_no": "CASE-1",
                "status": "proposed",
                "is_active": 1,
            },
            "segments": [
                {
                    "segment_id": 71,
                    "segment_order": 1,
                    "staff_id": 101,
                    "assigned_start_date": "2026-08-01",
                    "assigned_end_date": "2026-08-10",
                    "staff_line_user_id": "U-a",
                },
                {
                    "segment_id": 72,
                    "segment_order": 2,
                    "staff_id": 102,
                    "assigned_start_date": "2026-08-11",
                    "assigned_end_date": "2026-08-20",
                    "staff_line_user_id": "U-b",
                },
            ],
        },
    )
    monkeypatch.setattr(
        service,
        "search_segmented_caregiver_availability",
        lambda **kwargs: {"feasibility": "complete", "conflicts": []},
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "enqueue_line_task",
        lambda cursor, **kwargs: queued.append(kwargs) or 88,
    )

    result = service.send_matching_plan_information(
        "CASE-1", 7, 71, 1, "info-event", "admin"
    )

    assert result["line_task_id"] == 88
    assert queued[0]["to_user_id"] == "U-a"
    assert "2026-08-01～2026-08-10" in queued[0]["message_content"]
    assert connection.commits == 1
