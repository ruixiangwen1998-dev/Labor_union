from datetime import date

import pytest
from pydantic import ValidationError

from api.routes import assignment_schedule_rest_dates as route
from api.schemas.orders import AssignmentLeaveResolutionBatchApplyRequest
from services import assignment_schedule_rest_date_service as service
from services.admin_auth_service import AdminPrincipal


def _request():
    return {
        "contract_version": "assignment-leave-substitution-batch-apply/v1",
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "items": [
            {
                "original_schedule_id": 21,
                "work_date": "2026-08-01",
                "resolution_type": "defer_following_assignments",
                "substitute_staff_id": None,
            },
            {
                "original_schedule_id": 22,
                "work_date": "2026-08-02",
                "resolution_type": "substitute",
                "substitute_staff_id": 202,
            },
        ],
        "preview_fingerprint": "a" * 64,
        "batch_key": "batch-1",
        "actor": "admin",
        "reason": "multi-date leave",
    }


class _Cursor:
    def __init__(self, fail_on_event=False):
        self.calls = []
        self.rowcount = 1
        self.lastrowid = 70
        self.closed = 0
        self.fail_on_event = fail_on_event

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if (
            self.fail_on_event
            and "INSERT INTO assignment_schedule_leave_substitution_events" in sql
        ):
            raise RuntimeError("event insert failed")
        if "INSERT INTO assignment_schedule_leave_substitution_events" in sql:
            self.lastrowid += 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []

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


def _install_apply_dependencies(monkeypatch, connection):
    preview_request = {
        "contract_version": "assignment-leave-substitution-batch-preview/v1",
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "items": _request()["items"],
    }
    envelope = {
        "preview_request": preview_request,
        "requested_preview_fingerprint": "a" * 64,
        "batch_key": "batch-1",
        "actor": "admin",
        "reason": "multi-date leave",
        "replay_identity_seed": {
            "batch_key": "batch-1",
            "request_snapshot": preview_request,
            "preview_fingerprint": "a" * 64,
        },
    }
    monkeypatch.setattr(
        service,
        "canonicalize_assignment_leave_resolution_batch_apply_envelope",
        lambda request: envelope,
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "read_assignment_leave_resolution_batch_replay_snapshot",
        lambda cursor, batch_key, lock_rows: {
            "state": "absent",
            "header": None,
            "children": [],
        },
    )
    monkeypatch.setattr(
        service,
        "decide_assignment_leave_resolution_batch_replay",
        lambda snapshot, identity: {"status": "absent", "replay_result": None},
    )
    monkeypatch.setattr(
        service,
        "acquire_assignment_leave_resolution_batch_locked_facts",
        lambda cursor, request: {"locked": True},
    )
    monkeypatch.setattr(
        service,
        "authorize_assignment_leave_resolution_batch_apply",
        lambda request, fingerprint, identity, facts: {
            "status": "apply",
            "apply_authorization": {"authorized": True},
            "business_conflicts": None,
        },
    )
    monkeypatch.setattr(
        service,
        "build_assignment_leave_resolution_batch_mutation_command",
        lambda authorization, facts: {"mutation": True},
    )
    events = [
        {
            "batch_key": "batch-1",
            "batch_item_index": index,
            "case_no": "CASE-1",
            "original_assignment_id": 11,
            "original_schedule_id": 21 + index,
            "work_date": date(2026, 8, 1 + index).isoformat(),
            "resolution_type": (
                "defer_following_assignments" if index == 0 else "substitute"
            ),
            "substitute_assignment_id": None if index == 0 else 31,
            "event_key": f"event-{index}",
            "actor": "admin",
            "reason": "multi-date leave",
            "schedule_snapshot": {"batch": "batch-1"},
            "payroll_snapshot": {"target_hours": "40"},
        }
        for index in range(2)
    ]
    monkeypatch.setattr(
        service,
        "execute_assignment_leave_resolution_batch_mutations",
        lambda cursor, command: {
            "assignments": [{"id": 11}],
            "schedule_snapshot": {"batch": "batch-1"},
            "payroll_snapshots": [{"target_hours": "40"}] * 2,
            "pending_event_payloads": events,
        },
    )


def test_batch_apply_commits_header_and_all_children_once(monkeypatch):
    cursor = _Cursor()
    connection = _Connection(cursor)
    _install_apply_dependencies(monkeypatch, connection)

    result = service.apply_assignment_leave_resolution_batch(_request())

    sql = [" ".join(statement.split()) for statement, _ in cursor.calls]
    assert sum(
        "INSERT INTO assignment_schedule_leave_substitution_batches" in statement
        for statement in sql
    ) == 1
    assert sum(
        "INSERT INTO assignment_schedule_leave_substitution_events" in statement
        for statement in sql
    ) == 2
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert result["status"] == "applied"
    assert result["event_ids"] == [71, 72]


def test_batch_apply_rolls_back_everything_when_any_child_insert_fails(monkeypatch):
    cursor = _Cursor(fail_on_event=True)
    connection = _Connection(cursor)
    _install_apply_dependencies(monkeypatch, connection)

    with pytest.raises(RuntimeError, match="event insert failed"):
        service.apply_assignment_leave_resolution_batch(_request())

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_batch_apply_schema_is_strict_and_rejects_duplicate_dates():
    payload = _request()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        AssignmentLeaveResolutionBatchApplyRequest(**payload)

    payload = _request()
    payload["items"][1]["work_date"] = payload["items"][0]["work_date"]
    with pytest.raises(ValidationError, match="work_date must be unique"):
        AssignmentLeaveResolutionBatchApplyRequest(**payload)


def test_batch_apply_route_enforces_principal_actor_and_forwards_json(monkeypatch):
    captured = []
    monkeypatch.setattr(
        route,
        "apply_assignment_leave_resolution_batch",
        lambda payload: captured.append(payload)
        or {"status": "applied", "batch_key": "batch-1"},
    )
    req = AssignmentLeaveResolutionBatchApplyRequest(**_request())
    principal = AdminPrincipal(1, "admin", "Admin", "system_admin")

    response = route.apply_assignment_leave_resolution_batch_route(
        req, 11, principal
    )

    assert response.data["status"] == "applied"
    assert captured[0]["items"][0]["work_date"] == "2026-08-01"

    with pytest.raises(Exception) as error:
        route.apply_assignment_leave_resolution_batch_route(
            req,
            11,
            AdminPrincipal(2, "other", "Other", "system_admin"),
        )
    assert getattr(error.value, "status_code", None) == 403
