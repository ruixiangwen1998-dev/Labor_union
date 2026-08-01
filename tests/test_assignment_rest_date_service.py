"""
================================================================================
檔案名稱: tests/test_assignment_rest_date_service.py
功能說明: 驗證 AssignmentScheduleRestDateService 以 assignment_id 為專屬權屬獨立更新排休，防範跨指派刪除
================================================================================
"""

import ast
from copy import deepcopy
from datetime import date, datetime, timedelta

import inspect
import json
import pytest
from decimal import Decimal
from pydantic import ValidationError
from api.schemas.orders import (
    AssignmentLeaveResolutionApplyRequest,
    AssignmentLeaveResolutionPreviewRequest,
)
from services import assignment_schedule_rest_date_service as service
from services.assignment_schedule_rest_date_service import (
    AssignmentScheduleRestDateValidationError,
    _normalise_rest_dates,
    _as_positive_int,
    _as_rest_date_string,
    canonicalize_assignment_leave_resolution_batch_apply_envelope,
    AssignmentLeaveResolutionApplicationError,
    AssignmentLeaveResolutionDataIntegrityError,
    AssignmentLeaveResolutionInfrastructureError,
    acquire_assignment_leave_resolution_batch_locked_facts,
    authorize_assignment_leave_resolution_batch_apply,
    read_assignment_leave_resolution_batch_replay_snapshot,
    decide_assignment_leave_resolution_batch_replay,
    apply_assignment_leave_resolution,
    preview_assignment_leave_resolution,
    preview_assignment_leave_resolution_batch,
    AssignmentLeaveResolutionDomainError,
    save_assignment_rest_dates,
)
from services.assignment_schedule_leave_resolution_preview import (
    calculate_assignment_leave_resolution_batch_transition,
    canonicalize_assignment_leave_resolution_batch_request,
    compute_assignment_leave_resolution_batch_preview_from_snapshot,
    compute_assignment_leave_resolution_preview_from_snapshot,
    validate_assignment_leave_resolution_domain_transition,
)
from services import assignment_schedule_leave_resolution_preview as leave_preview
from services.multi_caregiver_schedule_read import (
    AssignmentScheduleConflictSnapshotDomainError,
)


def test_batch_mutation_never_infers_double_pay_from_holiday_defaults():
    source = inspect.getsource(
        service.execute_assignment_leave_resolution_batch_mutations
    )

    assert "is_double_pay_default" not in source
    assert "double_pay_by_date.setdefault(day, False)" in source
    assert 'double_pay_by_date[item["work_date"]] = item["is_double_pay"]' in source


@pytest.mark.parametrize(
    ("error_type", "expected_kind"),
    [
        (AssignmentLeaveResolutionApplicationError, "application"),
        (AssignmentLeaveResolutionDataIntegrityError, "data_integrity"),
    ],
)
def test_leave_resolution_typed_exception_has_exact_json_safe_copyable_fields(error_type, expected_kind):
    details = {"nested": [{"count": 1, "enabled": True}], "nullable": None}
    error = error_type(
        code="batch_key_request_identity_conflict",
        reason="batch request identity differs",
        details=details,
    )
    details["nested"][0]["count"] = 2

    assert set(error.as_dict()) == {"kind", "code", "reason", "details"}
    assert error.as_dict() == {
        "kind": expected_kind,
        "code": "batch_key_request_identity_conflict",
        "reason": "batch request identity differs",
        "details": {"nested": [{"count": 1, "enabled": True}], "nullable": None},
    }
    rendered = error.as_dict()
    rendered["details"]["nested"][0]["count"] = 3
    assert error.as_dict()["details"]["nested"][0]["count"] == 1


def test_leave_resolution_typed_exception_infrastructure_keeps_cause_server_only():
    cause = RuntimeError("driver secret must not reach response")
    error = AssignmentLeaveResolutionInfrastructureError(
        code="database_unavailable",
        reason="database unavailable",
        details={"operation": "batch_replay_read"},
        cause=cause,
    )

    assert error.__cause__ is cause
    assert error.as_dict() == {
        "kind": "infrastructure",
        "code": "database_unavailable",
        "reason": "database unavailable",
        "details": {"operation": "batch_replay_read"},
    }
    assert "cause" not in error.as_dict()
    assert "driver secret" not in str(error)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": "bad code", "reason": "stable", "details": {}},
        {"code": "stable_code", "reason": " ", "details": {}},
        {"code": "stable_code", "reason": "stable", "details": []},
        {"code": "stable_code", "reason": "stable", "details": {"bad": object()}},
    ],
)
def test_leave_resolution_typed_exception_rejects_invalid_response_fields(kwargs):
    with pytest.raises(ValueError):
        AssignmentLeaveResolutionApplicationError(**kwargs)


def test_leave_resolution_typed_exception_infrastructure_requires_exception_cause():
    with pytest.raises(ValueError):
        AssignmentLeaveResolutionInfrastructureError(
            code="database_unavailable",
            reason="database unavailable",
            details={},
            cause="not-an-exception",  # type: ignore[arg-type]
        )


def _batch_leave_resolution_apply_envelope_request():
    return {
        "contract_version": "assignment-leave-substitution-batch-apply/v1",
        "case_no": " CASE-1 ",
        "original_assignment_id": 11,
        "items": [
            {"original_schedule_id": 24, "work_date": "2026-08-02", "resolution_type": "substitute", "substitute_staff_id": 202},
            {"original_schedule_id": 22, "work_date": "2026-08-01", "resolution_type": "defer_following_assignments", "substitute_staff_id": None},
        ],
        "preview_fingerprint": "a" * 64,
        "batch_key": " batch-1 ",
        "actor": " supervisor ",
        "reason": " leave ",
    }


def test_batch_leave_resolution_apply_envelope_canonicalizes_exact_seed_and_defensive_copies():
    request = _batch_leave_resolution_apply_envelope_request()
    before = deepcopy(request)
    result = canonicalize_assignment_leave_resolution_batch_apply_envelope(request)

    assert set(result) == {"preview_request", "requested_preview_fingerprint", "batch_key", "actor", "reason", "replay_identity_seed"}
    expected_items = [
        {**request["items"][1], "is_double_pay": False},
        {**request["items"][0], "is_double_pay": False},
    ]
    assert result["preview_request"] == {
        "contract_version": "assignment-leave-substitution-batch-preview/v1",
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "items": expected_items,
    }
    assert result["replay_identity_seed"] == {"batch_key": "batch-1", "request_snapshot": result["preview_request"], "preview_fingerprint": "a" * 64}
    result["preview_request"]["items"][0]["work_date"] = "changed"
    assert result["replay_identity_seed"]["request_snapshot"]["items"][0]["work_date"] == "2026-08-01"
    assert request == before


@pytest.mark.parametrize("mutation", [
    lambda request: request.update({"event_key": "client"}),
    lambda request: request.update({"original_assignment_id": True}),
    lambda request: request.update({"preview_fingerprint": "A" * 64}),
    lambda request: request.update({"actor": "   "}),
    lambda request: request["items"][0].update({"batch_item_index": 0}),
    lambda request: request["items"][1].update({"work_date": "2026-08-02"}),
])
def test_batch_leave_resolution_apply_envelope_rejects_invalid_syntactic_input(mutation):
    request = _batch_leave_resolution_apply_envelope_request()
    mutation(request)
    with pytest.raises(AssignmentLeaveResolutionApplicationError) as error:
        canonicalize_assignment_leave_resolution_batch_apply_envelope(request)
    assert error.value.code == "invalid_batch_apply_envelope"


def test_batch_leave_resolution_apply_envelope_is_order_independent_and_capability_limited():
    request = _batch_leave_resolution_apply_envelope_request()
    reordered = dict(reversed(request.items()))
    reordered["items"] = list(reversed(request["items"]))
    assert canonicalize_assignment_leave_resolution_batch_apply_envelope(request) == canonicalize_assignment_leave_resolution_batch_apply_envelope(reordered)
    source = inspect.getsource(canonicalize_assignment_leave_resolution_batch_apply_envelope)
    for forbidden in ("get_connection", "cursor", "open(", "datetime.now", "os.environ"):
        assert forbidden not in source


def test_batch_leave_resolution_apply_envelope_keeps_audit_metadata_out_of_retry_identity():
    first = _batch_leave_resolution_apply_envelope_request()
    second = _batch_leave_resolution_apply_envelope_request()
    second.update({"actor": " another-admin ", "reason": " another-reason "})

    first_result = canonicalize_assignment_leave_resolution_batch_apply_envelope(first)
    second_result = canonicalize_assignment_leave_resolution_batch_apply_envelope(second)

    assert first_result["actor"] == "supervisor"
    assert second_result["actor"] == "another-admin"
    assert first_result["reason"] == "leave"
    assert second_result["reason"] == "another-reason"
    assert first_result["replay_identity_seed"] == second_result["replay_identity_seed"]


class _BatchLockedFactsCursor:
    def __init__(self, assignment_rows):
        self.assignment_rows = assignment_rows
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchone(self):
        return {"case_no": "CASE-1"}

    def fetchall(self):
        return deepcopy(self.assignment_rows)


def _batch_locked_facts_request(items):
    return {
        "contract_version": "assignment-leave-substitution-batch-preview/v1",
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "items": items,
    }


def _batch_locked_facts_snapshot(schedule_rows):
    return {
        "database_current_date": date(2026, 8, 1),
        "assignments": [{"id": 11, "case_no": "CASE-1", "staff_id": 101, "status": "active"}],
        "assignment_schedule_days": schedule_rows,
        "active_lock_days": [],
        "historical_facts": {"leave_substitution_events": [], "actual_hours_adjustments": [], "non_cancelled_payments": [], "active_settlements": []},
    }


def _install_batch_locked_facts_dependencies(monkeypatch, snapshot):
    observed = {}

    def mutex(cursor, staff_ids):
        observed["mutex"] = (cursor, list(staff_ids))
        return list(staff_ids)

    def snapshot_reader(cursor, case_no, extra_staff_ids, range_start, range_end, lock_rows):
        observed["snapshot"] = (cursor, case_no, list(extra_staff_ids), range_start, range_end, lock_rows)
        return deepcopy(snapshot)

    monkeypatch.setattr(service, "lock_staff_occupancy_mutex", mutex)
    monkeypatch.setattr(service, "get_case_schedule_conflict_snapshot_with_cursor", snapshot_reader)
    return observed


def test_batch_leave_resolution_apply_locked_facts_acquires_order_assignments_mutex_then_one_snapshot(monkeypatch):
    items = [
        {"original_schedule_id": 22, "work_date": "2026-08-01", "resolution_type": "defer_following_assignments", "substitute_staff_id": None},
        {"original_schedule_id": 23, "work_date": "2026-08-02", "resolution_type": "substitute", "substitute_staff_id": 303},
    ]
    schedules = [
        {"id": 22, "case_no": "CASE-1", "staff_id": 101, "assignment_id": 11, "work_date": date(2026, 8, 1)},
        {"id": 23, "case_no": "CASE-1", "staff_id": 101, "assignment_id": 11, "work_date": "2026-08-02"},
    ]
    cursor = _BatchLockedFactsCursor([
        {"id": 11, "case_no": "CASE-1", "staff_id": 101},
        {"id": 12, "case_no": "CASE-1", "staff_id": 202},
    ])
    observed = _install_batch_locked_facts_dependencies(monkeypatch, _batch_locked_facts_snapshot(schedules))

    result = acquire_assignment_leave_resolution_batch_locked_facts(cursor, _batch_locked_facts_request(items))

    assert cursor.calls == [
        ("SELECT case_no FROM orders WHERE case_no = %s FOR UPDATE", ("CASE-1",)),
        ("SELECT id, case_no, staff_id FROM case_staff_assignments WHERE case_no = %s ORDER BY id ASC FOR UPDATE", ("CASE-1",)),
    ]
    assert observed["mutex"] == (cursor, [101, 303])
    assert observed["snapshot"] == (cursor, "CASE-1", [303], "2026-08-01", "2026-08-03", True)
    assert result["lock_identity"] == {"case_no": "CASE-1", "staff_ids": [101, 303], "range_start": "2026-08-01", "range_end": "2026-08-03"}
    assert [row["id"] for row in result["original_assignment_schedule"]["schedule_days"]] == [22, 23]


def test_batch_leave_resolution_apply_locked_facts_uses_empty_extra_ids_for_defer_only(monkeypatch):
    item = {"original_schedule_id": 22, "work_date": "2026-08-01", "resolution_type": "defer_following_assignments", "substitute_staff_id": None}
    cursor = _BatchLockedFactsCursor([{"id": 11, "case_no": "CASE-1", "staff_id": 101}])
    observed = _install_batch_locked_facts_dependencies(monkeypatch, _batch_locked_facts_snapshot([
        {"id": 22, "case_no": "CASE-1", "staff_id": 101, "assignment_id": 11, "work_date": date(2026, 8, 1)}
    ]))

    acquire_assignment_leave_resolution_batch_locked_facts(cursor, _batch_locked_facts_request([item]))

    assert observed["mutex"] == (cursor, [101])
    assert observed["snapshot"][2] == []


def test_batch_leave_resolution_apply_locked_facts_deduplicates_substitute_mutex_ids(monkeypatch):
    items = [
        {"original_schedule_id": 22, "work_date": "2026-08-01", "resolution_type": "substitute", "substitute_staff_id": 303},
        {"original_schedule_id": 23, "work_date": "2026-08-02", "resolution_type": "substitute", "substitute_staff_id": 303},
    ]
    cursor = _BatchLockedFactsCursor([{"id": 11, "case_no": "CASE-1", "staff_id": 101}])
    observed = _install_batch_locked_facts_dependencies(monkeypatch, _batch_locked_facts_snapshot([
        {"id": 22, "case_no": "CASE-1", "staff_id": 101, "assignment_id": 11, "work_date": date(2026, 8, 1)},
        {"id": 23, "case_no": "CASE-1", "staff_id": 101, "assignment_id": 11, "work_date": date(2026, 8, 2)},
    ]))

    acquire_assignment_leave_resolution_batch_locked_facts(cursor, _batch_locked_facts_request(items))

    assert observed["mutex"] == (cursor, [101, 303])
    assert observed["snapshot"][2] == [303]


def test_batch_leave_resolution_apply_locked_facts_rejects_ownership_drift_from_snapshot(monkeypatch):
    cursor = _BatchLockedFactsCursor([{"id": 11, "case_no": "CASE-1", "staff_id": 101}])
    _install_batch_locked_facts_dependencies(monkeypatch, _batch_locked_facts_snapshot([
        {"id": 22, "case_no": "CASE-1", "staff_id": 101, "assignment_id": 11, "work_date": date(2026, 8, 2)}
    ]))
    request = _batch_locked_facts_request([
        {"original_schedule_id": 22, "work_date": "2026-08-01", "resolution_type": "defer_following_assignments", "substitute_staff_id": None}
    ])

    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError) as error:
        acquire_assignment_leave_resolution_batch_locked_facts(cursor, request)

    assert error.value.as_dict()["details"] == {"source": "conflict_snapshot"}


@pytest.mark.parametrize(
    ("dependency", "failure", "exception_type"),
    [
        ("mutex", ValueError("bad mutex facts"), AssignmentLeaveResolutionDataIntegrityError),
        ("snapshot", RuntimeError("database unavailable"), AssignmentLeaveResolutionInfrastructureError),
    ],
)
def test_batch_leave_resolution_apply_locked_facts_types_dependency_failures(monkeypatch, dependency, failure, exception_type):
    item = {"original_schedule_id": 22, "work_date": "2026-08-01", "resolution_type": "defer_following_assignments", "substitute_staff_id": None}
    cursor = _BatchLockedFactsCursor([{"id": 11, "case_no": "CASE-1", "staff_id": 101}])
    if dependency == "mutex":
        monkeypatch.setattr(service, "lock_staff_occupancy_mutex", lambda *_args: (_ for _ in ()).throw(failure))
    else:
        monkeypatch.setattr(service, "lock_staff_occupancy_mutex", lambda _cursor, staff_ids: staff_ids)
        monkeypatch.setattr(service, "get_case_schedule_conflict_snapshot_with_cursor", lambda *_args: (_ for _ in ()).throw(failure))

    with pytest.raises(exception_type) as error:
        acquire_assignment_leave_resolution_batch_locked_facts(cursor, _batch_locked_facts_request([item]))

    if exception_type is AssignmentLeaveResolutionInfrastructureError:
        assert error.value.__cause__ is failure


def test_batch_leave_resolution_apply_locked_facts_rejects_reversed_noncanonical_items_without_queries():
    cursor = _BatchLockedFactsCursor([])
    request = _batch_locked_facts_request([
        {"original_schedule_id": 23, "work_date": "2026-08-02", "resolution_type": "defer_following_assignments", "substitute_staff_id": None},
        {"original_schedule_id": 22, "work_date": "2026-08-01", "resolution_type": "defer_following_assignments", "substitute_staff_id": None},
    ])

    with pytest.raises(AssignmentLeaveResolutionApplicationError):
        acquire_assignment_leave_resolution_batch_locked_facts(cursor, request)

    assert cursor.calls == []


def test_batch_leave_resolution_apply_locked_facts_is_transaction_capability_limited():
    source = inspect.getsource(acquire_assignment_leave_resolution_batch_locked_facts)
    for forbidden in ("commit(", "rollback(", "close(", "INSERT ", "UPDATE ", "DELETE ", "get_connection", "batch_key", "preview_fingerprint", "authorization"):
        assert forbidden not in source


def _batch_authorization_locked_facts():
    return {
        "original_assignment_schedule": {"assignment": {"id": 11}, "schedule_days": [{"id": 22}]},
        "conflict_snapshot": {"facts": "locked"},
        "lock_identity": {"case_no": "CASE-1", "staff_ids": [101], "range_start": "2026-08-01", "range_end": "2026-08-02"},
    }


def _batch_authorization_preview(status="ready", fingerprint="a" * 64):
    return {
        "contract_version": "assignment-leave-substitution-batch-preview/v1",
        "canonical_intent": {"case_ref": "case:CASE-1", "items": [{"item_ref": 0}]},
        "double_pay_preferences": [{"item_ref": 0, "is_double_pay": False}],
        "service_plan_transition": {"before": {"locked": True}, "intent": {"locked": True}, "after": {"locked": True}, "impacts": {"total": 1}},
        "canonical_eligibility": {
            "transition_valid": status != "blocked",
            "applicable": status == "ready",
            "blocking_diagnostics": [{"code": "blocked"}] if status == "blocked" else [],
            "review_diagnostics": [{"code": "review"}] if status == "requires_review" else [],
        },
        "status": status,
        "requires_confirmation": status != "blocked",
        "preview_fingerprint": fingerprint,
    }


def _batch_authorization_inputs():
    return (
        _batch_locked_facts_request([
            {"original_schedule_id": 22, "work_date": "2026-08-01", "resolution_type": "defer_following_assignments", "substitute_staff_id": None}
        ]),
        "a" * 64,
        {"batch_key": "batch-1", "actor": "admin", "reason": "leave"},
        _batch_authorization_locked_facts(),
    )


@pytest.mark.parametrize("preview_status", ["blocked", "requires_review"])
def test_batch_leave_resolution_apply_authorization_decision_rejects_well_formed_business_conflicts(monkeypatch, preview_status):
    request, fingerprint, metadata, locked_facts = _batch_authorization_inputs()
    preview = _batch_authorization_preview(preview_status, fingerprint)
    calls = []
    monkeypatch.setattr(
        leave_preview,
        "compute_assignment_leave_resolution_batch_preview_from_snapshot",
        lambda *args: calls.append(args) or preview,
    )

    result = authorize_assignment_leave_resolution_batch_apply(request, fingerprint, metadata, locked_facts)

    assert calls == [(request, locked_facts["original_assignment_schedule"], locked_facts["conflict_snapshot"])]
    assert result == {
        "status": "rejected",
        "apply_authorization": None,
        "business_conflicts": {
            "status": preview_status,
            "blocking_diagnostics": preview["canonical_eligibility"]["blocking_diagnostics"],
            "review_diagnostics": preview["canonical_eligibility"]["review_diagnostics"],
        },
    }


def test_batch_leave_resolution_apply_authorization_decision_applies_only_ready_matching_fresh_preview(monkeypatch):
    request, fingerprint, metadata, locked_facts = _batch_authorization_inputs()
    preview = _batch_authorization_preview("ready", fingerprint)
    monkeypatch.setattr(
        leave_preview,
        "compute_assignment_leave_resolution_batch_preview_from_snapshot",
        lambda *_args: preview,
    )

    result = authorize_assignment_leave_resolution_batch_apply(request, fingerprint, metadata, locked_facts)

    assert set(result) == {"status", "apply_authorization", "business_conflicts"}
    assert result["status"] == "apply" and result["business_conflicts"] is None
    assert set(result["apply_authorization"]) == {"canonical_intent", "double_pay_preferences", "service_plan_transition", "canonical_eligibility", "preview_fingerprint", "canonical_apply_identity"}
    assert result["apply_authorization"]["canonical_apply_identity"] == metadata
    preview["service_plan_transition"]["impacts"]["total"] = 2
    metadata["actor"] = "changed"
    assert result["apply_authorization"]["service_plan_transition"]["impacts"] == {"total": 1}
    assert result["apply_authorization"]["canonical_apply_identity"]["actor"] == "admin"


def test_batch_leave_resolution_apply_authorization_decision_rejects_stale_fingerprint_normally(monkeypatch):
    request, _fingerprint, metadata, locked_facts = _batch_authorization_inputs()
    monkeypatch.setattr(
        leave_preview,
        "compute_assignment_leave_resolution_batch_preview_from_snapshot",
        lambda *_args: _batch_authorization_preview("ready", "b" * 64),
    )

    result = authorize_assignment_leave_resolution_batch_apply(request, "a" * 64, metadata, locked_facts)

    assert result["status"] == "rejected"
    assert result["apply_authorization"] is None
    assert result["business_conflicts"]["status"] == "stale_preview"


@pytest.mark.parametrize(
    ("locked_facts", "failure", "exception_type"),
    [
        ({}, None, AssignmentLeaveResolutionDataIntegrityError),
        (None, ValueError("malformed dependency"), AssignmentLeaveResolutionDataIntegrityError),
        (None, RuntimeError("unexpected dependency failure"), AssignmentLeaveResolutionInfrastructureError),
    ],
)
def test_batch_leave_resolution_apply_authorization_decision_types_malformed_locked_or_dependency_facts(monkeypatch, locked_facts, failure, exception_type):
    request, fingerprint, metadata, valid_locked_facts = _batch_authorization_inputs()
    if failure is not None:
        monkeypatch.setattr(
            leave_preview,
            "compute_assignment_leave_resolution_batch_preview_from_snapshot",
            lambda *_args: (_ for _ in ()).throw(failure),
        )
        locked_facts = valid_locked_facts

    with pytest.raises(exception_type) as error:
        authorize_assignment_leave_resolution_batch_apply(request, fingerprint, metadata, locked_facts)

    if exception_type is AssignmentLeaveResolutionInfrastructureError:
        assert error.value.__cause__ is failure


def test_batch_leave_resolution_apply_authorization_decision_is_pure_and_capability_limited():
    source = inspect.getsource(authorize_assignment_leave_resolution_batch_apply)
    for forbidden in ("cursor", "execute(", "get_connection", "commit(", "rollback(", "close(", "INSERT ", "UPDATE ", "DELETE ", "event_key", "schedule_snapshot", "payroll_snapshot"):
        assert forbidden not in source


class _BatchReplayCursor:
    def __init__(self, header, children): self.header, self.children, self.calls, self.step = header, children, [], 0
    def execute(self, sql, params): self.calls.append((sql, params)); self.step += 1
    def fetchone(self): return self.header
    def fetchall(self): return self.children


def _batch_replay_header():
    return {"batch_key": "batch-1", "case_no": "CASE-1", "preview_fingerprint": "a" * 64, "item_count": 1, "actor": "a", "reason": "r", "request_snapshot": '{"items":[{"original_schedule_id":22,"work_date":"2026-08-01","resolution_type":"defer_following_assignments","substitute_staff_id":null}],"original_assignment_id":11,"case_no":"CASE-1","contract_version":"assignment-leave-substitution-batch-preview/v1"}'}


def _batch_replay_child(index=0):
    return {"batch_key": "batch-1", "batch_item_index": index, "case_no": "CASE-1", "original_assignment_id": 11, "original_schedule_id": 22, "work_date": date(2026, 8, 1), "resolution_type": "defer_following_assignments", "substitute_assignment_id": None, "event_key": "e", "actor": "a", "reason": "r", "schedule_snapshot": {"z": [True, None, Decimal("1.20")]}, "payroll_snapshot": "{}"}


def _batch_replay_two_item_header():
    header = _batch_replay_header()
    request_snapshot = json.loads(header["request_snapshot"])
    request_snapshot["items"].append(
        {"original_schedule_id": 23, "work_date": "2026-08-02", "resolution_type": "defer_following_assignments", "substitute_staff_id": None}
    )
    header["item_count"] = 2
    header["request_snapshot"] = request_snapshot
    return header


def _batch_replay_second_child(index=1):
    child = _batch_replay_child(index)
    child.update({"original_schedule_id": 23, "work_date": "2026-08-02", "event_key": "e-2"})
    return child


def _batch_replay_identity_snapshot():
    header = _batch_replay_two_item_header()
    children = [_batch_replay_second_child(), _batch_replay_child()]
    snapshot = read_assignment_leave_resolution_batch_replay_snapshot(
        _BatchReplayCursor(header, children), "batch-1", False
    )
    snapshot["children"] = list(reversed(snapshot["children"]))
    return snapshot


def _batch_replay_requested_identity(snapshot):
    return {
        "batch_key": snapshot["header"]["batch_key"],
        "request_snapshot": deepcopy(snapshot["header"]["request_snapshot"]),
        "preview_fingerprint": snapshot["header"]["preview_fingerprint"],
    }


def test_batch_leave_resolution_batch_replay_identity_returns_exact_absent_decision():
    identity = _batch_replay_requested_identity(_batch_replay_identity_snapshot())
    result = decide_assignment_leave_resolution_batch_replay(
        {"state": "absent", "header": None, "children": []},
        identity,
    )

    assert result == {"status": "absent", "replay_result": None}


def test_batch_leave_resolution_batch_replay_identity_replays_header_only_with_defensive_ordered_events():
    snapshot = _batch_replay_identity_snapshot()
    identity = _batch_replay_requested_identity(snapshot)
    result = decide_assignment_leave_resolution_batch_replay(snapshot, identity)

    assert set(result) == {"status", "replay_result"}
    assert set(result["replay_result"]) == {"status", "batch", "events"}
    assert result["status"] == result["replay_result"]["status"] == "idempotent_replay"
    assert [event["batch_item_index"] for event in result["replay_result"]["events"]] == [0, 1]
    snapshot["header"]["actor"] = "changed"
    result["replay_result"]["events"][0]["event_key"] = "changed"
    assert result["replay_result"]["batch"]["actor"] == "a"
    assert snapshot["children"][0]["event_key"] == "e-2"


@pytest.mark.parametrize(
    ("field", "value", "expected_fields"),
    [
        ("request_snapshot", None, ["request_snapshot"]),
        ("preview_fingerprint", "b" * 64, ["preview_fingerprint"]),
        ("both", None, ["preview_fingerprint", "request_snapshot"]),
    ],
)
def test_batch_leave_resolution_batch_replay_identity_raises_minimal_transport_conflict(field, value, expected_fields):
    snapshot = _batch_replay_identity_snapshot()
    identity = _batch_replay_requested_identity(snapshot)
    if field == "request_snapshot":
        identity["request_snapshot"]["case_no"] = "OTHER"
    elif field == "both":
        identity["request_snapshot"]["case_no"] = "OTHER"
        identity["preview_fingerprint"] = "b" * 64
    else:
        identity[field] = value

    with pytest.raises(AssignmentLeaveResolutionApplicationError) as error:
        decide_assignment_leave_resolution_batch_replay(snapshot, identity)

    assert error.value.as_dict() == {
        "kind": "application",
        "code": "batch_key_request_identity_conflict",
        "reason": "batch request identity differs",
        "details": {"batch_key": "batch-1", "mismatched_fields": expected_fields},
    }


def test_batch_leave_resolution_batch_replay_identity_ignores_actor_reason_and_child_drift():
    snapshot = _batch_replay_identity_snapshot()
    snapshot["header"]["actor"] = "another-actor"
    snapshot["header"]["reason"] = "another-reason"
    snapshot["children"][0]["event_key"] = "another-event"
    snapshot["children"][0]["schedule_snapshot"] = {"changed": ["payload"]}

    assert decide_assignment_leave_resolution_batch_replay(
        snapshot, _batch_replay_requested_identity(snapshot)
    )["status"] == "idempotent_replay"


def test_batch_leave_resolution_batch_replay_identity_types_malformed_requested_identity():
    with pytest.raises(AssignmentLeaveResolutionApplicationError):
        decide_assignment_leave_resolution_batch_replay(
            {"state": "absent", "header": None, "children": []},
            {"batch_key": "batch-1"},
        )


@pytest.mark.parametrize(
    "snapshot",
    [
        {"state": "present", "header": None, "children": []},
        {"state": "absent", "header": None, "children": ["malformed"]},
    ],
)
def test_batch_leave_resolution_batch_replay_identity_types_malformed_dependency(snapshot):
    identity = _batch_replay_requested_identity(_batch_replay_identity_snapshot())
    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError):
        decide_assignment_leave_resolution_batch_replay(snapshot, identity)


def test_batch_leave_resolution_batch_replay_identity_is_capability_limited():
    source = inspect.getsource(decide_assignment_leave_resolution_batch_replay)
    for forbidden in ("get_connection", "cursor", "execute(", "open(", "datetime.now", "os.environ"):
        assert forbidden not in source


@pytest.mark.parametrize(
    "mutate",
    [
        lambda identity: identity.update({"preview_fingerprint": "A" * 64}),
        lambda identity: identity["request_snapshot"].update({"extra": True}),
        lambda identity: identity["request_snapshot"].update({"items": []}),
        lambda identity: identity["request_snapshot"]["items"][0].update({"resolution_type": "leave_only"}),
        lambda identity: identity["request_snapshot"].update({"items": list(reversed(identity["request_snapshot"]["items"]))}),
    ],
)
def test_batch_leave_resolution_batch_replay_identity_rejects_noncanonical_requested_identity(mutate):
    snapshot = _batch_replay_identity_snapshot()
    identity = _batch_replay_requested_identity(snapshot)
    mutate(identity)

    with pytest.raises(AssignmentLeaveResolutionApplicationError):
        decide_assignment_leave_resolution_batch_replay(snapshot, identity)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot["children"][0].update({"batch_key": "other"}),
        lambda snapshot: snapshot["children"][0].update({"case_no": "OTHER"}),
        lambda snapshot: snapshot["children"][0].update({"original_assignment_id": 12}),
        lambda snapshot: snapshot["children"][0].update({"work_date": "2026-8-2"}),
        lambda snapshot: snapshot["children"][0].update({"resolution_type": "invalid"}),
        lambda snapshot: snapshot["children"][0].update({"schedule_snapshot": {"z": {"b": 2, "a": 1}}}),
        lambda snapshot: snapshot["header"].update({"case_no": "OTHER"}),
    ],
)
def test_batch_leave_resolution_batch_replay_identity_rejects_noncanonical_persisted_snapshot(mutate):
    snapshot = _batch_replay_identity_snapshot()
    mutate(snapshot)

    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError):
        decide_assignment_leave_resolution_batch_replay(snapshot, _batch_replay_requested_identity(snapshot))


def test_batch_leave_resolution_batch_replay_identity_rejects_header_contract_drift():
    snapshot = _batch_replay_identity_snapshot()
    snapshot["header"]["contract_version"] = "assignment-leave-substitution-batch-apply/v1"

    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError) as error:
        decide_assignment_leave_resolution_batch_replay(snapshot, _batch_replay_requested_identity(snapshot))

    assert error.value.code == "invalid_batch_replay_snapshot"


def test_batch_leave_resolution_batch_replay_identity_rejects_request_item_count_drift():
    snapshot = _batch_replay_identity_snapshot()
    snapshot["header"]["request_snapshot"]["items"] = snapshot["header"]["request_snapshot"]["items"][:1]

    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError) as error:
        decide_assignment_leave_resolution_batch_replay(snapshot, _batch_replay_requested_identity(snapshot))

    assert error.value.code == "invalid_batch_replay_snapshot"


def test_batch_leave_resolution_batch_replay_snapshot_read_absent_does_not_query_children():
    cursor = _BatchReplayCursor(None, [])
    assert read_assignment_leave_resolution_batch_replay_snapshot(cursor, "batch-1", False) == {"state": "absent", "header": None, "children": []}
    assert len(cursor.calls) == 1 and "FOR UPDATE" not in cursor.calls[0][0]


def test_batch_leave_resolution_batch_replay_snapshot_read_canonicalizes_and_locks_existing_rows():
    cursor = _BatchReplayCursor(_batch_replay_header(), [_batch_replay_child()])
    result = read_assignment_leave_resolution_batch_replay_snapshot(cursor, "batch-1", True)
    assert result["state"] == "present"
    assert result["header"]["original_assignment_id"] == 11
    assert result["children"][0]["work_date"] == "2026-08-01"
    assert result["children"][0]["schedule_snapshot"] == {"z": [True, None, "1.2"]}
    assert len(cursor.calls) == 2 and all("FOR UPDATE" in call[0] for call in cursor.calls)


def test_batch_leave_resolution_batch_replay_snapshot_read_preserves_json_scalars_and_exact_sql():
    header = _batch_replay_header()
    child = _batch_replay_child()
    child["schedule_snapshot"] = {
        "z": [True, None, Decimal("10"), Decimal("10.0"), Decimal("0.010"), 7, "7"],
        "a": {"b": 2, "a": 1},
    }
    unlocked = _BatchReplayCursor(deepcopy(header), [deepcopy(child)])
    locked = _BatchReplayCursor(deepcopy(header), [deepcopy(child)])

    unlocked_result = read_assignment_leave_resolution_batch_replay_snapshot(unlocked, "batch-1", False)
    locked_result = read_assignment_leave_resolution_batch_replay_snapshot(locked, "batch-1", True)

    assert unlocked_result == locked_result
    assert unlocked.calls == [
        ("SELECT batch_key, case_no, preview_fingerprint, item_count, actor, reason, request_snapshot FROM assignment_schedule_leave_substitution_batches WHERE batch_key = %s", ("batch-1",)),
        ("SELECT batch_key, batch_item_index, case_no, original_assignment_id, original_schedule_id, work_date, resolution_type, substitute_assignment_id, event_key, actor, reason, schedule_snapshot, payroll_snapshot FROM assignment_schedule_leave_substitution_events WHERE batch_key = %s ORDER BY batch_item_index ASC", ("batch-1",)),
    ]
    assert locked.calls == [(sql + " FOR UPDATE", params) for sql, params in unlocked.calls]
    assert unlocked_result["children"][0]["schedule_snapshot"] == {
        "a": {"a": 1, "b": 2},
        "z": [True, None, 10, 10, "0.01", 7, "7"],
    }


def test_batch_leave_resolution_batch_replay_snapshot_read_sorts_reversed_two_children_by_ordinal():
    result = read_assignment_leave_resolution_batch_replay_snapshot(
        _BatchReplayCursor(_batch_replay_two_item_header(), [_batch_replay_second_child(), _batch_replay_child()]),
        "batch-1",
        False,
    )

    assert [child["batch_item_index"] for child in result["children"]] == [0, 1]


def test_batch_leave_resolution_batch_replay_snapshot_read_treats_children_as_execution_results():
    child = _batch_replay_child()
    child.update(
        {
            "original_schedule_id": 777,
            "work_date": "2026-08-03",
            "event_key": "generated-execution-event",
            "schedule_snapshot": {"execution": {"result": "changed"}},
            "payroll_snapshot": {"execution": {"amount": Decimal("12.50")}},
        }
    )

    result = read_assignment_leave_resolution_batch_replay_snapshot(
        _BatchReplayCursor(_batch_replay_header(), [child]), "batch-1", False
    )

    assert result["children"] == [
        {
            **child,
            "schedule_snapshot": {"execution": {"result": "changed"}},
            "payroll_snapshot": {"execution": {"amount": "12.5"}},
        }
    ]


def test_batch_leave_resolution_batch_replay_snapshot_read_rejects_distinct_extra_ordinal():
    extra_child = _batch_replay_second_child(2)
    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError):
        read_assignment_leave_resolution_batch_replay_snapshot(
            _BatchReplayCursor(_batch_replay_two_item_header(), [_batch_replay_child(), _batch_replay_second_child(), extra_child]),
            "batch-1",
            False,
        )


def test_batch_leave_resolution_batch_replay_snapshot_read_present_shape_and_iso_string_date_are_exact():
    child = _batch_replay_child()
    child["work_date"] = "2026-08-01"
    result = read_assignment_leave_resolution_batch_replay_snapshot(
        _BatchReplayCursor(_batch_replay_header(), [child]), "batch-1", False
    )

    assert set(result) == {"state", "header", "children"}
    assert set(result["header"]) == {"batch_key", "contract_version", "case_no", "original_assignment_id", "request_snapshot", "preview_fingerprint", "item_count", "actor", "reason"}
    assert set(result["children"][0]) == {"batch_key", "batch_item_index", "case_no", "original_assignment_id", "original_schedule_id", "work_date", "resolution_type", "substitute_assignment_id", "event_key", "actor", "reason", "schedule_snapshot", "payroll_snapshot"}
    assert result["children"][0]["work_date"] == "2026-08-01"


@pytest.mark.parametrize("method", ["fetchone", "fetchall"])
def test_batch_leave_resolution_batch_replay_snapshot_read_propagates_fetch_exceptions(method):
    class _FetchFailureCursor(_BatchReplayCursor):
        def fetchone(self):
            if method == "fetchone":
                raise RuntimeError("fetchone database failure")
            return super().fetchone()

        def fetchall(self):
            if method == "fetchall":
                raise RuntimeError("fetchall database failure")
            return super().fetchall()

    cursor = _FetchFailureCursor(_batch_replay_header(), [_batch_replay_child()])
    with pytest.raises(AssignmentLeaveResolutionInfrastructureError) as error:
        read_assignment_leave_resolution_batch_replay_snapshot(cursor, "batch-1", False)
    assert str(error.value.__cause__) == f"{method} database failure"
    assert error.value.as_dict() == {
        "kind": "infrastructure",
        "code": "batch_replay_snapshot_read_unavailable",
        "reason": "batch replay snapshot read is unavailable",
        "details": {"operation": "batch_replay_snapshot_read"},
    }


@pytest.mark.parametrize("mutate", [
    lambda header: header.update({"item_count": 2}),
    lambda header: header.update({"request_snapshot": {"contract_version": "assignment-leave-substitution-batch-preview/v1", "case_no": "CASE-1", "original_assignment_id": 11, "items": []}}),
    lambda header: header.update({"request_snapshot": {**json.loads(_batch_replay_header()["request_snapshot"]), "extra": True}}),
    lambda header: header.update({"request_snapshot": {key: value for key, value in json.loads(_batch_replay_header()["request_snapshot"]).items() if key != "case_no"}}),
    lambda header: header.update({"request_snapshot": {**json.loads(_batch_replay_header()["request_snapshot"]), "original_assignment_id": True}}),
    lambda header: header.update({"request_snapshot": {**json.loads(_batch_replay_header()["request_snapshot"]), "items": [{"original_schedule_id": 23, "work_date": "2026-08-02", "resolution_type": "defer_following_assignments", "substitute_staff_id": None}, *json.loads(_batch_replay_header()["request_snapshot"])["items"]]}, "item_count": 2}),
])
def test_batch_leave_resolution_batch_replay_snapshot_read_rejects_request_item_count_or_shape(mutate):
    header = _batch_replay_header()
    mutate(header)
    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError):
        read_assignment_leave_resolution_batch_replay_snapshot(_BatchReplayCursor(header, [_batch_replay_child()]), "batch-1", False)


@pytest.mark.parametrize("field,value", [
    ("original_assignment_id", 12),
    ("batch_key", "other-batch"),
    ("work_date", datetime(2026, 8, 1)),
])
def test_batch_leave_resolution_batch_replay_snapshot_read_rejects_child_linkage_and_date(field, value):
    child = _batch_replay_child()
    child[field] = value
    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError):
        read_assignment_leave_resolution_batch_replay_snapshot(_BatchReplayCursor(_batch_replay_header(), [child]), "batch-1", False)


@pytest.mark.parametrize("snapshot", [
    "not-json",
    "[]",
    b"{}",
    {"value": float("nan")},
    {"value": object()},
])
def test_batch_leave_resolution_batch_replay_snapshot_read_rejects_invalid_json_payloads(snapshot):
    child = _batch_replay_child()
    child["schedule_snapshot"] = snapshot
    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError):
        read_assignment_leave_resolution_batch_replay_snapshot(_BatchReplayCursor(_batch_replay_header(), [child]), "batch-1", False)


def test_batch_leave_resolution_batch_replay_snapshot_read_returns_defensive_json_copies_and_propagates_db_errors():
    child = _batch_replay_child()
    source_payload = dict(nested=[{"value": Decimal("10")}])
    child["schedule_snapshot"] = source_payload
    cursor = _BatchReplayCursor(_batch_replay_header(), [child])
    snapshot = read_assignment_leave_resolution_batch_replay_snapshot(cursor, "batch-1", False)
    source_payload["nested"][0]["value"] = Decimal("11")
    assert snapshot["children"][0]["schedule_snapshot"] == {"nested": [{"value": 10}]}
    snapshot["children"][0]["schedule_snapshot"]["nested"][0]["value"] = 12
    assert source_payload["nested"][0]["value"] == Decimal("11")

    class _FailingCursor(_BatchReplayCursor):
        def execute(self, sql, params):
            raise RuntimeError("database unavailable")

    with pytest.raises(AssignmentLeaveResolutionInfrastructureError) as error:
        read_assignment_leave_resolution_batch_replay_snapshot(_FailingCursor(None, []), "batch-1", False)
    assert str(error.value.__cause__) == "database unavailable"


@pytest.mark.parametrize("children", [[], [_batch_replay_child(1)], [_batch_replay_child(0), _batch_replay_child(0)]])
def test_batch_leave_resolution_batch_replay_snapshot_read_rejects_bad_ordinals(children):
    cursor = _BatchReplayCursor(_batch_replay_header(), children)
    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError):
        read_assignment_leave_resolution_batch_replay_snapshot(cursor, "batch-1", False)


@pytest.mark.parametrize("mutate", [
    lambda header, child: header.update({"preview_fingerprint": "A" * 64}),
    lambda header, child: child.update({"case_no": "OTHER"}),
    lambda header, child: child.update({"event_key": " "}),
    lambda header, child: child.update({"resolution_type": "substitute", "substitute_assignment_id": 11}),
    lambda header, child: child.update({"work_date": datetime(2026, 8, 1)}),
    lambda header, child: child.update({"schedule_snapshot": {1: "bad"}}),
    lambda header, child: child.update({"payroll_snapshot": {"x": float("inf")}}),
])
def test_batch_leave_resolution_batch_replay_snapshot_read_rejects_adversarial_identity_and_json(mutate):
    header, child = _batch_replay_header(), _batch_replay_child()
    mutate(header, child)
    with pytest.raises(AssignmentLeaveResolutionDataIntegrityError):
        read_assignment_leave_resolution_batch_replay_snapshot(_BatchReplayCursor(header, [child]), "batch-1", False)


@pytest.mark.parametrize("value", [False, True, 0, 1])
def test_batch_leave_resolution_batch_replay_snapshot_read_requires_strict_bool(value):
    cursor = _BatchReplayCursor(None, [])
    if type(value) is bool:
        read_assignment_leave_resolution_batch_replay_snapshot(cursor, "batch-1", value)
    else:
        with pytest.raises(AssignmentLeaveResolutionApplicationError) as error:
            read_assignment_leave_resolution_batch_replay_snapshot(cursor, "batch-1", value)
        assert error.value.as_dict() == {
            "kind": "application",
            "code": "invalid_batch_replay_read_request",
            "reason": "batch replay read request is invalid",
            "details": {"field": "lock_rows"},
        }


@pytest.mark.parametrize(
    ("cursor", "batch_key", "field"),
    [
        (None, "batch-1", "cursor"),
        (_BatchReplayCursor(None, []), " batch-1", "batch_key"),
    ],
)
def test_batch_leave_resolution_batch_replay_snapshot_read_types_malformed_transport_inputs(
    cursor, batch_key, field
):
    with pytest.raises(AssignmentLeaveResolutionApplicationError) as error:
        read_assignment_leave_resolution_batch_replay_snapshot(cursor, batch_key, False)

    assert error.value.as_dict() == {
        "kind": "application",
        "code": "invalid_batch_replay_read_request",
        "reason": "batch replay read request is invalid",
        "details": {"field": field},
    }


def _leave_resolution_preview_facts(*, historical=False, locked=False):
    work_date = date(2026, 7, 1) if historical else date(2026, 8, 1)
    assignment = {
        "id": 11,
        "case_no": "CASE-1",
        "staff_id": 101,
        "status": "active",
        "assigned_start_date": work_date,
        "assigned_end_date": work_date,
        "planned_hours": 8,
        "actual_hours": 8,
        "service_hours_per_day": 8,
    }
    read_snapshot = {
        "assignment": assignment,
        "schedule_days": [
            {
                "id": 22,
                "assignment_id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "work_date": work_date,
                "is_work_day": True,
                "is_double_pay": False,
                "notes": None,
                "requires_review": False,
            }
        ],
    }
    conflict_snapshot = {
        "database_current_date": date(2026, 7, 15),
        "assignments": [
            {
                "id": assignment["id"],
                "case_no": assignment["case_no"],
                "staff_id": assignment["staff_id"],
                "status": assignment["status"],
                "assigned_start_date": assignment["assigned_start_date"],
                "assigned_end_date": assignment["assigned_end_date"],
                "planned_hours": assignment["planned_hours"],
                "actual_hours": assignment["actual_hours"],
            }
        ],
        "assignment_schedule_days": [
            {
                "id": 22,
                "assignment_id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "work_date": work_date,
                "is_work_day": True,
                "is_double_pay": False,
                "notes": None,
                "requires_review": False,
            }
        ],
        "active_lock_days": [],
        "historical_facts": {
            "leave_substitution_events": [],
            "actual_hours_adjustments": [],
            "non_cancelled_payments": (
                [
                    {
                        "id": 90,
                        "case_no": "CASE-1",
                        "assignment_id": 11,
                        "payment_status": "posted",
                    }
                ]
                if locked
                else []
            ),
            "active_settlements": [],
        },
    }
    return work_date, read_snapshot, conflict_snapshot


def test_leave_resolution_schema_accepts_intent_only_requests():
    preview = AssignmentLeaveResolutionPreviewRequest(
        case_no=" CASE-1 ",
        original_assignment_id=11,
        original_schedule_id=22,
        work_date="2026-07-01",
        resolution_type="substitute",
        substitute_staff_id=202,
    )
    assert preview.case_no == "CASE-1"
    assert preview.work_date == date(2026, 7, 1)

    apply_request = AssignmentLeaveResolutionApplyRequest(
        case_no="CASE-1",
        original_assignment_id=11,
        original_schedule_id=22,
        work_date="2026-07-01",
        resolution_type="defer_following_assignments",
        preview_fingerprint="a" * 64,
        event_key=" leave-11-22 ",
        actor=" admin ",
        reason=" single-day leave ",
    )
    assert apply_request.event_key == "leave-11-22"
    assert apply_request.actor == "admin"
    assert apply_request.reason == "single-day leave"


def test_leave_resolution_schema_requires_resolution_specific_staff():
    with pytest.raises(ValidationError, match="substitute_staff_id is required"):
        AssignmentLeaveResolutionPreviewRequest(
            case_no="CASE-1",
            original_assignment_id=11,
            original_schedule_id=22,
            work_date="2026-07-01",
            resolution_type="substitute",
        )

    with pytest.raises(ValidationError, match="substitute_staff_id is not allowed"):
        AssignmentLeaveResolutionPreviewRequest(
            case_no="CASE-1",
            original_assignment_id=11,
            original_schedule_id=22,
            work_date="2026-07-01",
            resolution_type="defer_following_assignments",
            substitute_staff_id=202,
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "original_assignment_id",
        "original_schedule_id",
        "substitute_staff_id",
    ],
)
def test_leave_resolution_schema_rejects_bool_ids(field_name):
    request = {
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "original_schedule_id": 22,
        "work_date": "2026-07-01",
        "resolution_type": "substitute",
        "substitute_staff_id": 202,
    }
    request[field_name] = True

    with pytest.raises(ValidationError):
        AssignmentLeaveResolutionPreviewRequest(**request)


def test_leave_resolution_schema_forbids_server_derived_facts():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AssignmentLeaveResolutionApplyRequest(
            case_no="CASE-1",
            original_assignment_id=11,
            original_schedule_id=22,
            work_date="2026-07-01",
            resolution_type="defer_following_assignments",
            preview_fingerprint="b" * 64,
            event_key="leave-11-22",
            actor="admin",
            reason="single-day leave",
            historical_fact_state="unlocked",
        )


def test_leave_resolution_preview_substitute_is_read_only_and_ready(monkeypatch):
    work_date, read_snapshot, conflict_snapshot = _leave_resolution_preview_facts()
    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: conflict_snapshot,
    )

    result = preview_assignment_leave_resolution(
        "CASE-1", 11, 22, work_date.isoformat(), "substitute", 202
    )

    assert result["status"] == "ready"
    assert result["historical_fact_state"] == "bootstrap"
    assert result["required_hours"] == result["provisional_actual_hours"] == 8
    created = result["assignment_transition_plan"]["created"]
    assert len(created) == 1
    assert created[0]["staff_id"] == 202
    assert created[0]["kind"] == "single_day_substitute"
    assert created[0]["original_assignment_id"] == 11
    assert result["requires_confirmation"] is True
    assert len(result["preview_fingerprint"]) == 64
    repeated = preview_assignment_leave_resolution(
        "CASE-1", 11, 22, work_date.isoformat(), "substitute", 202
    )
    assert repeated["preview_fingerprint"] == result["preview_fingerprint"]


def test_leave_resolution_preview_delegates_canonical_facts_to_snapshot_helper(
    monkeypatch,
):
    import services.assignment_schedule_leave_resolution_preview as preview_service

    work_date, read_snapshot, conflict_snapshot = _leave_resolution_preview_facts()
    captured = {}
    expected = {"status": "ready", "preview_fingerprint": "f" * 64}

    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: conflict_snapshot,
    )

    def compute(request, original_facts, case_facts):
        captured["request"] = request
        captured["original_facts"] = original_facts
        captured["case_facts"] = case_facts
        return expected

    monkeypatch.setattr(
        preview_service,
        "compute_assignment_leave_resolution_preview_from_snapshot",
        compute,
    )

    result = preview_assignment_leave_resolution(
        " CASE-1 ", 11, 22, work_date.isoformat(), "substitute", 202
    )

    assert result is expected
    assert captured == {
        "request": {
            "case_no": "CASE-1",
            "original_assignment_id": 11,
            "original_schedule_id": 22,
            "work_date": work_date.isoformat(),
            "resolution_type": "substitute",
            "substitute_staff_id": 202,
        },
        "original_facts": read_snapshot,
        "case_facts": conflict_snapshot,
    }


def test_leave_resolution_preview_historical_state_is_server_derived(monkeypatch):
    work_date, read_snapshot, conflict_snapshot = _leave_resolution_preview_facts(
        historical=True
    )
    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: conflict_snapshot,
    )

    result = preview_assignment_leave_resolution(
        "CASE-1", 11, 22, work_date.isoformat(), "substitute", 202
    )

    assert result["status"] == "requires_review"
    assert result["historical_fact_state"] == "unlocked"
    assert result["requires_audit"] is True
    assert result["review_reasons"] == ["historical_audit_required"]


def test_leave_resolution_preview_blocks_locked_history(monkeypatch):
    work_date, read_snapshot, conflict_snapshot = _leave_resolution_preview_facts(
        historical=True, locked=True
    )
    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: conflict_snapshot,
    )

    result = preview_assignment_leave_resolution(
        "CASE-1", 11, 22, work_date.isoformat(), "substitute", 202
    )

    assert result["status"] == "blocked"
    assert result["historical_fact_state"] == "locked"
    assert result["blocking_reasons"] == ["historical_facts_locked"]


def test_leave_resolution_preview_maps_conflict_snapshot_case_not_found(monkeypatch):
    work_date, read_snapshot, _ = _leave_resolution_preview_facts()
    expected_details = {"case_no": "CASE-1"}

    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: (
            (_ for _ in ()).throw(
                AssignmentScheduleConflictSnapshotDomainError(
                    "not_found",
                    "case_not_found",
                    expected_details,
                )
            )
        ),
    )

    with pytest.raises(AssignmentLeaveResolutionDomainError) as error:
        preview_assignment_leave_resolution(
            "CASE-1",
            11,
            22,
            work_date.isoformat(),
            "substitute",
            202,
        )

    assert error.value.category == "not_found"
    assert error.value.code == "case_not_found"
    assert error.value.reason == "case does not exist"
    assert error.value.details == expected_details


def test_leave_resolution_preview_maps_conflict_snapshot_identity_changed(monkeypatch):
    work_date, read_snapshot, _ = _leave_resolution_preview_facts()
    expected_details = {
        "case_no": "CASE-1",
        "before": ({"assignment_id": 11, "staff_id": 101},),
        "after": ({"assignment_id": 11, "staff_id": 102},),
    }

    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: (
            (_ for _ in ()).throw(
                AssignmentScheduleConflictSnapshotDomainError(
                    "conflict",
                    "assignment_identity_changed_during_snapshot",
                    {
                        "case_no": "CASE-1",
                        "before": [
                            {"assignment_id": 11, "staff_id": 101},
                        ],
                        "after": [{"assignment_id": 11, "staff_id": 102}],
                    },
                )
            )
        ),
    )

    with pytest.raises(AssignmentLeaveResolutionDomainError) as error:
        preview_assignment_leave_resolution(
            "CASE-1",
            11,
            22,
            work_date.isoformat(),
            "substitute",
            202,
        )

    assert error.value.category == "conflict"
    assert error.value.code == "assignment_identity_changed_during_snapshot"
    assert (
        error.value.reason
        == "assignment identity changed while reading conflict snapshot"
    )
    assert error.value.details == expected_details


def test_leave_resolution_preview_does_not_parse_dependency_valueerror(monkeypatch):
    work_date, read_snapshot, _ = _leave_resolution_preview_facts()

    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: (
            (_ for _ in ()).throw(ValueError("case does not exist"))
        ),
    )

    with pytest.raises(ValueError, match="case does not exist"):
        preview_assignment_leave_resolution(
            "CASE-1",
            11,
            22,
            work_date.isoformat(),
            "substitute",
            202,
        )


def test_leave_resolution_preview_missing_original_schedule_returns_not_found(monkeypatch):
    work_date, read_snapshot, conflict_snapshot = _leave_resolution_preview_facts()
    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: conflict_snapshot,
    )

    with pytest.raises(AssignmentLeaveResolutionDomainError) as error:
        preview_assignment_leave_resolution(
            "CASE-1", 11, 99, work_date.isoformat(), "substitute", 202
        )

    assert error.value.category == "not_found"
    assert error.value.code == "original_schedule_not_found"


def test_leave_resolution_preview_defer_counts_work_days_and_locks_all_shifted_assignments(
    monkeypatch,
):
    leave_date = date(2026, 7, 1)
    original = {
        "id": 11,
        "case_no": "CASE-1",
        "staff_id": 101,
        "status": "active",
        "assigned_start_date": leave_date,
        "assigned_end_date": leave_date,
        "planned_hours": 8,
        "actual_hours": 8,
        "service_hours_per_day": 8,
    }
    following = {
        "id": 12,
        "case_no": "CASE-1",
        "staff_id": 102,
        "status": "active",
        "assigned_start_date": date(2026, 7, 2),
        "assigned_end_date": date(2026, 7, 2),
        "planned_hours": 8,
        "actual_hours": 8,
        "service_hours_per_day": 8,
    }
    read_snapshot = {
        "assignment": original,
        "schedule_days": [
            {
                "id": 22,
                "assignment_id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "work_date": leave_date,
                "is_work_day": True,
            }
        ],
    }

    def snapshot_with(payment_assignment_id=None):
        return {
            "database_current_date": date(2026, 7, 15),
            "assignments": [
                {k: v for k, v in original.items() if k != "service_hours_per_day"},
                {k: v for k, v in following.items() if k != "service_hours_per_day"},
            ],
            "assignment_schedule_days": [
            {
                "id": 22,
                "assignment_id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "work_date": leave_date,
                "is_work_day": True,
                "is_double_pay": False,
                "notes": None,
                "requires_review": False,
            }
            ],
            "active_lock_days": [],
            "historical_facts": {
                "leave_substitution_events": [],
                "actual_hours_adjustments": [],
                "non_cancelled_payments": (
                    [
                        {
                            "id": 90,
                            "case_no": "CASE-1",
                            "assignment_id": payment_assignment_id,
                            "payment_status": "posted",
                        }
                    ]
                    if payment_assignment_id is not None
                    else []
                ),
                "active_settlements": [],
            },
        }

    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: snapshot_with(),
    )
    ready = preview_assignment_leave_resolution(
        "CASE-1",
        11,
        22,
        leave_date.isoformat(),
        "defer_following_assignments",
    )
    assert ready["status"] == "requires_review"
    assert ready["required_hours"] == ready["provisional_actual_hours"] == 16
    assert "order_service_hours_mismatch" not in ready["blocking_reasons"]

    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: snapshot_with(12),
    )
    locked_result = preview_assignment_leave_resolution(
        "CASE-1",
        11,
        22,
        leave_date.isoformat(),
        "defer_following_assignments",
    )

    assert locked_result["status"] == "blocked"
    assert locked_result["historical_fact_state"] == "locked"
    assert locked_result["blocking_reasons"] == ["historical_facts_locked"]


def test_leave_resolution_preview_rejects_imprecise_ownership(monkeypatch):
    work_date, read_snapshot, _ = _leave_resolution_preview_facts()
    read_snapshot["schedule_days"][0]["assignment_id"] = 12
    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)

    with pytest.raises(AssignmentLeaveResolutionDomainError, match="ownership mismatch") as error:
        preview_assignment_leave_resolution(
            "CASE-1", 11, 22, work_date.isoformat(), "substitute", 202
        )
    assert error.value.category == "validation_error"
    assert error.value.code == "schedule_ownership_mismatch"
    assert error.value.reason == "original schedule ownership mismatch"


def test_leave_resolution_preview_conflict_when_availability_conflict(monkeypatch):
    work_date, read_snapshot, conflict_snapshot = _leave_resolution_preview_facts()
    conflict_snapshot["active_lock_days"] = [
        {
            "id": 1000,
            "lock_id": 1001,
            "plan_id": 1002,
            "case_no": "CASE-1",
            "segment_id": 1,
            "staff_id": 202,
            "lock_date": work_date,
        }
    ]
    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: conflict_snapshot,
    )

    result = preview_assignment_leave_resolution(
        "CASE-1",
        11,
        22,
        work_date.isoformat(),
        "substitute",
        202,
    )

    assert result["status"] == "blocked"
    assert result["blocking_reasons"] == ["availability_conflict"]


def test_leave_resolution_preview_preserves_unknown_conflict_snapshot_code(monkeypatch):
    work_date, read_snapshot, _ = _leave_resolution_preview_facts()
    original_allowed = set(AssignmentScheduleConflictSnapshotDomainError._ALLOWED_CODES)
    monkeypatch.setattr(
        AssignmentScheduleConflictSnapshotDomainError,
        "_ALLOWED_CODES",
        original_allowed
        | {("conflict", "assignment_visibility_changed_during_snapshot")},
    )
    expected_details = {
        "case_no": "CASE-1",
        "before": [{"assignment_id": 11, "staff_id": 101}],
        "after": [{"assignment_id": 11, "staff_id": 102}],
    }
    expected_error = AssignmentScheduleConflictSnapshotDomainError(
        "conflict",
        "assignment_visibility_changed_during_snapshot",
        expected_details,
    )

    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: (
            (_ for _ in ()).throw(expected_error)
        ),
    )

    with pytest.raises(AssignmentScheduleConflictSnapshotDomainError) as error:
        preview_assignment_leave_resolution(
            "CASE-1",
            11,
            22,
            work_date.isoformat(),
            "substitute",
            202,
        )

    assert error.value.category == "conflict"
    assert error.value.code == "assignment_visibility_changed_during_snapshot"
    assert error.value.details == expected_details


def test_leave_resolution_preview_returns_blocked_without_domain_mapping(monkeypatch):
    work_date, read_snapshot, conflict_snapshot = _leave_resolution_preview_facts()
    expected = {
        "status": "blocked",
        "historical_fact_state": "unlocked",
        "blocking_reasons": ["assignment_transition_conflict"],
        "assignment_transition_conflicts": [{"code": "assignment_row_limit_exceeded"}],
        "availability_conflicts": [],
        "review_reasons": [],
        "requires_confirmation": False,
    }

    monkeypatch.setattr(service, "get_assignment_schedule", lambda assignment_id: read_snapshot)
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: conflict_snapshot,
    )
    monkeypatch.setattr(
        "services.assignment_schedule_leave_resolution_preview.compute_assignment_leave_resolution_preview_from_snapshot",
        lambda *_args, **_kwargs: expected,
    )

    result = preview_assignment_leave_resolution(
        "CASE-1",
        11,
        22,
        work_date.isoformat(),
        "substitute",
        202,
    )

    assert result == expected


def test_batch_leave_resolution_preview_orchestration_maps_case_not_found_dependency_error(
    monkeypatch,
):
    request, _, _snapshot = _batch_leave_resolution_request_fixtures()
    expected_details = {"case_no": "CASE-1"}
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: (
            (_ for _ in ()).throw(
                AssignmentScheduleConflictSnapshotDomainError(
                    "not_found",
                    "case_not_found",
                    expected_details,
                )
            )
        ),
    )

    with pytest.raises(AssignmentLeaveResolutionDomainError) as error:
        preview_assignment_leave_resolution_batch(request)

    assert error.value.category == "not_found"
    assert error.value.code == "case_not_found"
    assert error.value.reason == "case does not exist"
    assert error.value.details == expected_details


def test_batch_leave_resolution_preview_orchestration_maps_identity_changed_dependency_error(
    monkeypatch,
):
    request, _, _snapshot = _batch_leave_resolution_request_fixtures()
    expected_dependency_details = {
        "case_no": "CASE-1",
        "before": [{"assignment_id": 11, "staff_id": 101}],
        "after": [{"assignment_id": 11, "staff_id": 102}],
    }
    expected_details = {
        "case_no": "CASE-1",
        "before": ({"assignment_id": 11, "staff_id": 101},),
        "after": ({"assignment_id": 11, "staff_id": 102},),
    }
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: (
            (_ for _ in ()).throw(
                AssignmentScheduleConflictSnapshotDomainError(
                    "conflict",
                    "assignment_identity_changed_during_snapshot",
                    expected_dependency_details,
                )
            )
        ),
    )

    with pytest.raises(AssignmentLeaveResolutionDomainError) as error:
        preview_assignment_leave_resolution_batch(request)

    assert error.value.category == "conflict"
    assert error.value.code == "assignment_identity_changed_during_snapshot"
    assert (
        error.value.reason
        == "assignment identity changed while reading conflict snapshot"
    )
    assert error.value.details == expected_details


def test_batch_leave_resolution_preview_orchestration_original_assignment_not_found(
    monkeypatch,
):
    request, _original, snapshot = _batch_leave_resolution_request_fixtures()
    request["original_assignment_id"] = 99
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: snapshot,
    )

    with pytest.raises(AssignmentLeaveResolutionDomainError) as error:
        preview_assignment_leave_resolution_batch(request)

    assert error.value.category == "not_found"
    assert error.value.code == "original_assignment_not_found"
    assert error.value.reason == "original assignment does not exist"
    assert error.value.details == {
        "case_no": "CASE-1",
        "original_assignment_id": 99,
    }


def test_batch_leave_resolution_preview_orchestration_preserves_unknown_dependency_error(
    monkeypatch,
):
    request, _, _snapshot = _batch_leave_resolution_request_fixtures()
    original_codes = set(AssignmentScheduleConflictSnapshotDomainError._ALLOWED_CODES)
    monkeypatch.setattr(
        AssignmentScheduleConflictSnapshotDomainError,
        "_ALLOWED_CODES",
        original_codes | {("conflict", "assignment_visibility_changed_during_snapshot")},
    )
    expected_error = AssignmentScheduleConflictSnapshotDomainError(
        "conflict",
        "assignment_visibility_changed_during_snapshot",
        {
            "case_no": "CASE-1",
            "before": [{"assignment_id": 11, "staff_id": 101}],
            "after": [{"assignment_id": 11, "staff_id": 102}],
        },
    )
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: (
            (_ for _ in ()).throw(expected_error)
        ),
    )

    with pytest.raises(AssignmentScheduleConflictSnapshotDomainError) as error:
        preview_assignment_leave_resolution_batch(request)

    assert error.value.code == "assignment_visibility_changed_during_snapshot"
    assert error.value.details == expected_error.as_dict()["details"]


def test_batch_leave_resolution_preview_orchestration_preserves_dependency_error_contract_violation(
    monkeypatch,
):
    request, _, _snapshot = _batch_leave_resolution_request_fixtures()
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: (
            (_ for _ in ()).throw(ValueError("db unavailable"))
        ),
    )

    with pytest.raises(ValueError, match="db unavailable"):
        preview_assignment_leave_resolution_batch(request)


def test_batch_leave_resolution_preview_orchestration_delegates_once_and_preserves_result(
    monkeypatch,
):
    request, _, snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = [
        {
            "original_schedule_id": 24,
            "work_date": "2026-08-03",
            "resolution_type": "substitute",
            "substitute_staff_id": 303,
        },
        {
            "original_schedule_id": 22,
            "work_date": "2026-08-01",
            "resolution_type": "defer_following_assignments",
            "substitute_staff_id": None,
        },
    ]
    expected_snapshot = {
        "database_current_date": date(2026, 8, 15),
        "assignments": [
            {
                "id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "status": "active",
                "assigned_start_date": date(2026, 8, 1),
                "assigned_end_date": date(2026, 8, 10),
                "planned_hours": 80,
                "actual_hours": 80,
            }
        ],
        "assignment_schedule_days": [
            {
                "id": 22,
                "assignment_id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "work_date": date(2026, 8, 1),
                "is_work_day": True,
                "is_double_pay": False,
                "notes": None,
                "requires_review": False,
            },
            {
                "id": 23,
                "assignment_id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "work_date": date(2026, 8, 3),
                "is_work_day": True,
                "is_double_pay": False,
                "notes": None,
                "requires_review": False,
            },
            {
                "id": 24,
                "assignment_id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "work_date": date(2026, 8, 2),
                "is_work_day": True,
                "is_double_pay": False,
                "notes": None,
                "requires_review": False,
            },
        ],
        "active_lock_days": [],
        "historical_facts": {
            "leave_substitution_events": [],
            "actual_hours_adjustments": [],
            "non_cancelled_payments": [],
            "active_settlements": [],
        },
    }
    captured = {}
    compute_result = {
        "status": "blocked",
        "historical_fact_state": "unlocked",
        "blocking_reasons": ["availability_conflict"],
        "assignment_transition_conflicts": [],
        "review_reasons": [],
        "requires_confirmation": False,
        "preview_fingerprint": "f" * 64,
    }

    def snapshot(case_no, staff_ids, range_start, range_end):
        captured["snapshot_args"] = (case_no, staff_ids, range_start, range_end)
        return expected_snapshot

    def compute_batch(request_payload, original_assignment_snapshot, conflict_snapshot):
        captured["compute_args"] = (request_payload, original_assignment_snapshot, conflict_snapshot)
        return compute_result

    monkeypatch.setattr(service, "get_case_schedule_conflict_snapshot", snapshot)
    monkeypatch.setattr(
        leave_preview,
        "compute_assignment_leave_resolution_batch_preview_from_snapshot",
        compute_batch,
    )

    result = preview_assignment_leave_resolution_batch(request)

    assert result == compute_result
    assert captured["snapshot_args"][0] == "CASE-1"
    assert captured["snapshot_args"][1] == [303]
    assert captured["snapshot_args"][2] == "2026-08-01"
    assert captured["snapshot_args"][3] == "2026-08-04"
    assert captured["compute_args"][0] == request
    assert captured["compute_args"][1] == {
        "assignment": expected_snapshot["assignments"][0],
        "schedule_days": expected_snapshot["assignment_schedule_days"],
    }
    assert captured["compute_args"][2] == expected_snapshot


def test_batch_leave_resolution_preview_orchestration_all_defer_items_keep_snapshot_staff_ids_empty(
    monkeypatch,
):
    request, _, snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = [
        {
            "original_schedule_id": 24,
            "work_date": "2026-08-02",
            "resolution_type": "defer_following_assignments",
            "substitute_staff_id": None,
        },
        {
            "original_schedule_id": 22,
            "work_date": "2026-08-01",
            "resolution_type": "defer_following_assignments",
            "substitute_staff_id": None,
        },
    ]
    captured = {}

    def snapshot_reader(case_no, staff_ids, range_start, range_end):
        captured["snapshot_args"] = (case_no, staff_ids, range_start, range_end)
        return snapshot

    monkeypatch.setattr(service, "get_case_schedule_conflict_snapshot", snapshot_reader)
    monkeypatch.setattr(
        leave_preview,
        "compute_assignment_leave_resolution_batch_preview_from_snapshot",
        lambda *_args, **_kwargs: {"status": "ready"},
    )

    preview_assignment_leave_resolution_batch(request)

    assert captured["snapshot_args"][1] == []


def test_batch_leave_resolution_preview_orchestration_preserves_substitute_staff_order_and_duplicates(
    monkeypatch,
):
    request, _, snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = [
        {
            "original_schedule_id": 22,
            "work_date": "2026-08-01",
            "resolution_type": "substitute",
            "substitute_staff_id": 303,
        },
        {
            "original_schedule_id": 24,
            "work_date": "2026-08-02",
            "resolution_type": "substitute",
            "substitute_staff_id": 202,
        },
        {
            "original_schedule_id": 24,
            "work_date": "2026-08-03",
            "resolution_type": "substitute",
            "substitute_staff_id": 303,
        },
    ]
    captured = {}

    def snapshot_reader(case_no, staff_ids, range_start, range_end):
        captured["snapshot_args"] = (case_no, staff_ids, range_start, range_end)
        return snapshot

    monkeypatch.setattr(service, "get_case_schedule_conflict_snapshot", snapshot_reader)
    monkeypatch.setattr(
        leave_preview,
        "compute_assignment_leave_resolution_batch_preview_from_snapshot",
        lambda *_args, **_kwargs: {"status": "ready"},
    )

    preview_assignment_leave_resolution_batch(request)

    assert captured["snapshot_args"][1] == [303, 202, 303]


def test_batch_leave_resolution_preview_orchestration_preserves_dependency_exception(monkeypatch):
    request, _, snapshot = _batch_leave_resolution_request_fixtures()
    expected_error = RuntimeError("dependency failure")
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda case_no, staff_ids, range_start, range_end: snapshot,
    )
    monkeypatch.setattr(
        leave_preview,
        "compute_assignment_leave_resolution_batch_preview_from_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(expected_error),
    )

    with pytest.raises(RuntimeError) as error:
        preview_assignment_leave_resolution_batch(request)

    assert error.value is expected_error


def test_batch_leave_resolution_preview_orchestration_rejects_server_derived_top_level_field(monkeypatch):
    request, _original, _snapshot = _batch_leave_resolution_request_fixtures()
    request["preview_fingerprint"] = "f" * 64
    monkeypatch.setattr(
        service,
        "get_case_schedule_conflict_snapshot",
        lambda *_args: pytest.fail("snapshot must not be read for an invalid envelope"),
    )

    with pytest.raises(AssignmentLeaveResolutionDomainError) as error:
        preview_assignment_leave_resolution_batch(request)

    assert error.value.category == "validation_error"
    assert error.value.code == "invalid_request"
    assert error.value.details == {"field": "request"}


def _leave_resolution_preview_from_snapshot_request(work_date, resolution, staff=None):
    return {
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "original_schedule_id": 22,
        "work_date": work_date.isoformat(),
        "resolution_type": resolution,
        "substitute_staff_id": staff,
    }


def _batch_leave_resolution_request_schedule_facts():
    assignment = {
        "id": 11,
        "case_no": "CASE-1",
        "staff_id": 101,
        "status": "active",
        "assigned_start_date": date(2026, 8, 1),
        "assigned_end_date": date(2026, 8, 10),
        "planned_hours": 8,
        "actual_hours": 80,
        "service_hours_per_day": 8,
    }
    schedule_days = [
        {
            "id": 22,
            "assignment_id": 11,
            "case_no": "CASE-1",
            "staff_id": 101,
            "work_date": date(2026, 8, 1),
            "is_work_day": True,
        },
        {
            "id": 23,
            "assignment_id": 11,
            "case_no": "CASE-1",
            "staff_id": 101,
            "work_date": date(2026, 8, 3),
            "is_work_day": True,
        },
        {
            "id": 24,
            "assignment_id": 11,
            "case_no": "CASE-1",
            "staff_id": 101,
            "work_date": date(2026, 8, 2),
            "is_work_day": True,
        },
    ]
    original = {
        "assignment": assignment,
        "schedule_days": schedule_days,
    }
    snapshot = {
        "assignments": [dict(assignment)],
        "assignment_schedule_days": [dict(row) for row in schedule_days],
    }
    return original, snapshot


def _batch_leave_resolution_request_fixtures():
    original_snapshot, snapshot = _batch_leave_resolution_request_schedule_facts()
    return {
        "contract_version": "assignment-leave-substitution-batch-preview/v1",
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "items": [
            {
                "original_schedule_id": 24,
                "work_date": "2026-08-02",
                "resolution_type": "substitute",
                "substitute_staff_id": 202,
            },
            {
                "original_schedule_id": 22,
                "work_date": "2026-08-01",
                "resolution_type": "defer_following_assignments",
                "substitute_staff_id": None,
            },
        ],
    }, original_snapshot, snapshot


def test_batch_leave_resolution_request_canonicalization_is_deterministic_sorted_and_indexed():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    first = canonicalize_assignment_leave_resolution_batch_request(
        request, original_snapshot, conflict_snapshot
    )
    second = canonicalize_assignment_leave_resolution_batch_request(
        request, original_snapshot, conflict_snapshot
    )
    assert first == second

    canonical_intent = first["canonical_batch_intent"]
    canonical_lineage = first["item_lineage"]["items"]
    assert canonical_intent["contract_version"] == "assignment-leave-substitution-batch-preview/v1"
    assert canonical_intent["case_no"] == "CASE-1"
    assert canonical_intent["original_assignment_id"] == 11
    assert canonical_intent["items"][0]["batch_item_index"] == 0
    assert canonical_intent["items"][0]["original_schedule_id"] == 22
    assert canonical_intent["items"][0]["work_date"] == "2026-08-01"
    assert canonical_intent["items"][1]["batch_item_index"] == 1
    assert canonical_intent["items"][1]["original_schedule_id"] == 24
    assert canonical_intent["items"][1]["work_date"] == "2026-08-02"
    assert [row["batch_item_index"] for row in canonical_lineage] == [0, 1]
    assert canonical_lineage[0]["original_staff_id"] == 101
    assert canonical_lineage[1]["original_schedule_id"] == 24


def test_batch_leave_resolution_request_canonicalization_rejects_unknown_fields():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["unknown"] = "x"
    with pytest.raises(ValueError, match="unsupported"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"][0]["batch_item_index"] = 0
    with pytest.raises(ValueError, match="unsupported"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )


def test_batch_leave_resolution_request_canonicalization_rejects_duplicate_schedule_or_date():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = [
        {
            "original_schedule_id": 22,
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
    ]
    with pytest.raises(ValueError, match="duplicate original_schedule_id in items"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = [
        {
            "original_schedule_id": 23,
            "work_date": "2026-08-01",
            "resolution_type": "defer_following_assignments",
            "substitute_staff_id": None,
        },
        {
            "original_schedule_id": 24,
            "work_date": "2026-08-01",
            "resolution_type": "substitute",
            "substitute_staff_id": 202,
        },
    ]
    with pytest.raises(ValueError, match="duplicate work_date in items"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"][1]["original_schedule_id"] = request["items"][0][
        "original_schedule_id"
    ]
    request["items"][1]["work_date"] = request["items"][0]["work_date"]
    with pytest.raises(ValueError, match="duplicate original_schedule_id in items"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )


def test_batch_leave_resolution_request_canonicalization_rejects_invalid_resolution_and_staff_combo():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = [
        {
            "original_schedule_id": 22,
            "work_date": "2026-08-01",
            "resolution_type": "substitute",
            "substitute_staff_id": None,
        }
    ]
    with pytest.raises(ValueError, match="substitute_staff_id must be"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = [
        {
            "original_schedule_id": 22,
            "work_date": "2026-08-01",
            "resolution_type": "defer_following_assignments",
            "substitute_staff_id": 202,
        }
    ]
    with pytest.raises(ValueError, match="must be null when deferring assignments"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"][0]["resolution_type"] = []
    with pytest.raises(
        ValueError,
        match="resolution_type must be defer_following_assignments or substitute",
    ):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )


def test_batch_leave_resolution_request_canonicalization_rejects_request_and_item_shape_issues():
    _, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    with pytest.raises(ValueError, match="request must be a mapping"):
        canonicalize_assignment_leave_resolution_batch_request(
            [],
            original_snapshot,
            conflict_snapshot,
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = "not-a-list"
    with pytest.raises(ValueError, match="items must be a non-empty list"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = []
    with pytest.raises(ValueError, match="items must be a non-empty list"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = ["not-mapping"]
    with pytest.raises(ValueError, match="items\\[0\\] must be a mapping"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    del request["case_no"]
    with pytest.raises(ValueError, match="unsupported or missing fields"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    del request["items"][0]["work_date"]
    with pytest.raises(ValueError, match="unsupported or missing fields"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, _, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    with pytest.raises(ValueError, match="original_assignment_schedule must be a mapping"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, [], conflict_snapshot
        )

    request, original_snapshot, _ = _batch_leave_resolution_request_fixtures()
    with pytest.raises(ValueError, match="conflict_snapshot must be a mapping"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, []
        )


def test_batch_leave_resolution_request_canonicalization_rejects_exact_contract_version():
    for invalid_version in (
        "bad-contract-version",
        " assignment-leave-substitution-batch-preview/v1",
        "assignment-leave-substitution-batch-preview/v1 ",
    ):
        request, original_snapshot, conflict_snapshot = (
            _batch_leave_resolution_request_fixtures()
        )
        request["contract_version"] = invalid_version
        with pytest.raises(
            ValueError,
            match="contract_version must be assignment-leave-substitution-batch-preview/v1",
        ):
            canonicalize_assignment_leave_resolution_batch_request(
                request, original_snapshot, conflict_snapshot
            )


def test_batch_leave_resolution_request_canonicalization_trims_case_no_and_rejects_empty():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["case_no"] = "  CASE-1  "
    canonical = canonicalize_assignment_leave_resolution_batch_request(
        request, original_snapshot, conflict_snapshot
    )
    assert canonical["canonical_batch_intent"]["case_no"] == "CASE-1"

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["case_no"] = "   "
    with pytest.raises(ValueError, match="case_no must be a non-empty string"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )


def test_batch_leave_resolution_request_canonicalization_rejects_bool_and_bad_ids():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["original_assignment_id"] = True
    with pytest.raises(ValueError, match="original_assignment_id must be a positive integer"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"][0]["original_schedule_id"] = True
    with pytest.raises(ValueError, match="original_schedule_id must be a positive integer"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"][0]["substitute_staff_id"] = True
    with pytest.raises(ValueError, match="substitute_staff_id must be a positive integer"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    for invalid_assignment_id in (0, -1, "11"):
        request, original_snapshot, conflict_snapshot = (
            _batch_leave_resolution_request_fixtures()
        )
        request["original_assignment_id"] = invalid_assignment_id
        with pytest.raises(
            ValueError,
            match="original_assignment_id must be a positive integer",
        ):
            canonicalize_assignment_leave_resolution_batch_request(
                request, original_snapshot, conflict_snapshot
            )


def test_batch_leave_resolution_request_canonicalization_rejects_untrimmed_and_nonstr_dates():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"][0]["work_date"] = " 2026-08-02 "
    with pytest.raises(ValueError, match="work_date must be YYYY-MM-DD"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"][0]["work_date"] = date(2026, 8, 2)
    with pytest.raises(ValueError, match="work_date must be YYYY-MM-DD"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    for invalid_work_date in ("2026-8-02", "2026-02-30", True):
        request, original_snapshot, conflict_snapshot = (
            _batch_leave_resolution_request_fixtures()
        )
        request["items"][0]["work_date"] = invalid_work_date
        with pytest.raises(ValueError, match="work_date must be YYYY-MM-DD"):
            canonicalize_assignment_leave_resolution_batch_request(
                request, original_snapshot, conflict_snapshot
            )


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "batch_key",
        "actor",
        "reason",
        "preview_fingerprint",
        "event_key",
        "historical_fact_state",
        "provisional_plan",
    ],
)
def test_batch_leave_resolution_request_canonicalization_rejects_server_derived_request_fields(forbidden_field):
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request[forbidden_field] = "v"
    with pytest.raises(ValueError, match="unsupported or missing fields"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )


def test_batch_leave_resolution_request_canonicalization_rejects_server_derived_item_fields():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"][0]["batch_item_index"] = 0
    with pytest.raises(ValueError, match="item contains unsupported or missing fields"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )


def test_batch_leave_resolution_request_canonicalization_rejects_ownership_mismatch_paths():
    mismatch_cases = [
        ("request", ("case_no",), "CASE-2", "original assignment ownership mismatch"),
        (
            "request",
            ("original_assignment_id",),
            12,
            "original assignment ownership mismatch",
        ),
        (
            "original",
            ("assignment", "id"),
            12,
            "original assignment ownership mismatch",
        ),
        (
            "original",
            ("assignment", "case_no"),
            "CASE-2",
            "original assignment ownership mismatch",
        ),
        (
            "original",
            ("assignment", "staff_id"),
            999,
            "original schedule ownership mismatch",
        ),
        (
            "conflict",
            ("assignments", 0, "id"),
            12,
            "original assignment ownership mismatch",
        ),
        (
            "conflict",
            ("assignments", 0, "case_no"),
            "CASE-2",
            "original assignment ownership mismatch",
        ),
        (
            "conflict",
            ("assignments", 0, "staff_id"),
            999,
            "original assignment ownership mismatch",
        ),
        (
            "original",
            ("schedule_days", 0, "assignment_id"),
            12,
            "original_schedule_id does not belong",
        ),
        (
            "conflict",
            ("assignment_schedule_days", 0, "assignment_id"),
            12,
            "original_schedule_id does not belong",
        ),
        (
            "original",
            ("schedule_days", 0, "case_no"),
            "CASE-2",
            "original schedule ownership mismatch",
        ),
        (
            "conflict",
            ("assignment_schedule_days", 0, "case_no"),
            "CASE-2",
            "original schedule ownership mismatch",
        ),
        (
            "original",
            ("schedule_days", 0, "staff_id"),
            999,
            "original schedule ownership mismatch",
        ),
        (
            "conflict",
            ("assignment_schedule_days", 0, "staff_id"),
            999,
            "original schedule ownership mismatch",
        ),
        (
            "original",
            ("schedule_days", 0, "work_date"),
            date(2026, 8, 4),
            "original schedule ownership mismatch",
        ),
        (
            "conflict",
            ("assignment_schedule_days", 0, "work_date"),
            date(2026, 8, 4),
            "original schedule ownership mismatch",
        ),
    ]
    for target_name, path, value, error_match in mismatch_cases:
        request, original_snapshot, conflict_snapshot = (
            _batch_leave_resolution_request_fixtures()
        )
        target = {
            "request": request,
            "original": original_snapshot,
            "conflict": conflict_snapshot,
        }[target_name]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError, match=error_match):
            canonicalize_assignment_leave_resolution_batch_request(
                request, original_snapshot, conflict_snapshot
            )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    conflict_snapshot["assignments"].append(dict(conflict_snapshot["assignments"][0]))
    with pytest.raises(ValueError, match="original assignment ownership mismatch"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    original_snapshot["schedule_days"].append(dict(original_snapshot["schedule_days"][0]))
    with pytest.raises(ValueError, match="original_schedule_id does not belong"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    conflict_snapshot["assignment_schedule_days"].append(
        dict(conflict_snapshot["assignment_schedule_days"][0])
    )
    with pytest.raises(ValueError, match="original_schedule_id does not belong"):
        canonicalize_assignment_leave_resolution_batch_request(
            request, original_snapshot, conflict_snapshot
        )


def test_batch_leave_resolution_request_canonicalization_rejects_unreferenced_target_schedule_snapshot_drift():
    drift_cases = [
        ("original", "duplicate", None, None),
        ("conflict", "duplicate", None, None),
        ("original", "set", "id", True),
        ("conflict", "set", "id", True),
        ("original", "set", "assignment_id", 12),
        ("conflict", "set", "assignment_id", 12),
        ("original", "set", "assignment_id", True),
        ("conflict", "set", "assignment_id", True),
        ("original", "set", "case_no", "CASE-2"),
        ("conflict", "set", "case_no", "CASE-2"),
        ("original", "set", "staff_id", 999),
        ("conflict", "set", "staff_id", 999),
        ("original", "set", "staff_id", True),
        ("conflict", "set", "staff_id", True),
        ("original", "set", "work_date", date(2026, 8, 4)),
        ("conflict", "set", "work_date", date(2026, 8, 4)),
        ("original", "set", "work_date", " 2026-08-03 "),
        ("conflict", "set", "work_date", " 2026-08-03 "),
        ("original", "remove", None, None),
        ("conflict", "remove", None, None),
    ]
    for target_name, operation, field_name, value in drift_cases:
        request, original_snapshot, conflict_snapshot = (
            _batch_leave_resolution_request_fixtures()
        )
        request["items"] = [
            dict(item)
            for item in request["items"]
            if item["original_schedule_id"] == 22
        ]
        rows = (
            original_snapshot["schedule_days"]
            if target_name == "original"
            else conflict_snapshot["assignment_schedule_days"]
        )
        unreferenced_row = next(row for row in rows if row["id"] == 23)
        if operation == "duplicate":
            rows.append(dict(unreferenced_row))
        elif operation == "remove":
            rows.remove(unreferenced_row)
        else:
            unreferenced_row[field_name] = value

        with pytest.raises(ValueError):
            canonicalize_assignment_leave_resolution_batch_request(
                request, original_snapshot, conflict_snapshot
            )


def test_batch_leave_resolution_request_canonicalization_allows_unrelated_assignment_snapshot_rows():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = [
        dict(item)
        for item in request["items"]
        if item["original_schedule_id"] == 22
    ]
    conflict_snapshot["assignments"].append(
        {
            "id": 12,
            "case_no": "OTHER-CASE",
            "staff_id": 303,
        }
    )
    conflict_snapshot["assignment_schedule_days"].append(
        {
            "id": 90,
            "assignment_id": 12,
            "case_no": "OTHER-CASE",
            "staff_id": 303,
            "work_date": date(2026, 9, 1),
            "is_work_day": True,
        }
    )

    canonical = canonicalize_assignment_leave_resolution_batch_request(
        request, original_snapshot, conflict_snapshot
    )
    assert [
        item["original_schedule_id"]
        for item in canonical["canonical_batch_intent"]["items"]
    ] == [22]
    assert canonical["item_lineage"]["items"][0]["original_schedule_id"] == 22


def test_batch_leave_resolution_request_canonicalization_stable_under_input_order_changes():
    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request["items"] = [
        {
            "original_schedule_id": 24,
            "work_date": "2026-08-02",
            "resolution_type": "defer_following_assignments",
            "substitute_staff_id": None,
        },
        {
            "original_schedule_id": 23,
            "work_date": "2026-08-03",
            "resolution_type": "substitute",
            "substitute_staff_id": 202,
        },
        {
            "original_schedule_id": 22,
            "work_date": "2026-08-01",
            "resolution_type": "defer_following_assignments",
            "substitute_staff_id": None,
        },
    ]
    conflict_snapshot["assignments"].append(
        {
            "id": 12,
            "case_no": "CASE-1",
            "staff_id": 303,
        }
    )
    conflict_snapshot["assignment_schedule_days"].append(
        {
            "id": 90,
            "assignment_id": 12,
            "case_no": "CASE-1",
            "staff_id": 303,
            "work_date": date(2026, 8, 4),
            "is_work_day": True,
        }
    )
    baseline = canonicalize_assignment_leave_resolution_batch_request(
        request, original_snapshot, conflict_snapshot
    )

    request = dict(reversed(list(request.items())))
    request["items"] = [
        dict(reversed(list(item.items()))) for item in reversed(request["items"])
    ]
    original_snapshot = dict(reversed(list(original_snapshot.items())))
    original_snapshot["assignment"] = dict(
        reversed(list(original_snapshot["assignment"].items()))
    )
    original_snapshot["schedule_days"] = [
        dict(reversed(list(row.items())))
        for row in reversed(original_snapshot["schedule_days"])
    ]
    conflict_snapshot = dict(reversed(list(conflict_snapshot.items())))
    conflict_snapshot["assignments"] = [
        dict(reversed(list(row.items())))
        for row in reversed(conflict_snapshot["assignments"])
    ]
    conflict_snapshot["assignment_schedule_days"] = [
        dict(reversed(list(row.items())))
        for row in reversed(conflict_snapshot["assignment_schedule_days"])
    ]

    reordered = canonicalize_assignment_leave_resolution_batch_request(
        request, original_snapshot, conflict_snapshot
    )
    assert reordered == baseline
    assert [
        item["original_schedule_id"]
        for item in reordered["canonical_batch_intent"]["items"]
    ] == [
        22,
        24,
        23,
    ]
    assert [
        row["batch_item_index"]
        for row in reordered["canonical_batch_intent"]["items"]
    ] == [
        0,
        1,
        2,
    ]
    assert [
        (
            row["batch_item_index"],
            row["original_assignment_id"],
            row["original_schedule_id"],
            row["original_staff_id"],
            row["work_date"],
        )
        for row in reordered["item_lineage"]["items"]
    ] == [
        (0, 11, 22, 101, "2026-08-01"),
        (1, 11, 24, 101, "2026-08-02"),
        (2, 11, 23, 101, "2026-08-03"),
    ]


def test_batch_leave_resolution_request_canonicalization_keeps_inputs_immutable():
    import copy

    request, original_snapshot, conflict_snapshot = _batch_leave_resolution_request_fixtures()
    request_copy = copy.deepcopy(request)
    original_copy = copy.deepcopy(original_snapshot)
    snapshot_copy = copy.deepcopy(conflict_snapshot)

    result = canonicalize_assignment_leave_resolution_batch_request(
        request, original_snapshot, conflict_snapshot
    )
    assert request == request_copy
    assert original_snapshot == original_copy
    assert conflict_snapshot == snapshot_copy

    result["canonical_batch_intent"]["items"][0]["resolution_type"] = "corrupt"
    assert request["items"][0]["resolution_type"] != "corrupt"


def _batch_leave_resolution_transition_facts():
    assignment = {
        "id": 11,
        "case_no": "CASE-1",
        "staff_id": 101,
        "status": "active",
        "assigned_start_date": date(2026, 8, 1),
        "assigned_end_date": date(2026, 8, 5),
        "planned_hours": 40,
        "actual_hours": 40,
        "service_hours_per_day": 8,
    }
    schedule_days = [
        {
            "id": 20 + day,
            "assignment_id": 11,
            "case_no": "CASE-1",
            "staff_id": 101,
            "work_date": date(2026, 8, day),
            "is_work_day": True,
        }
        for day in range(1, 6)
    ]
    original = {"assignment": assignment, "schedule_days": schedule_days}
    snapshot = {
        "database_current_date": date(2026, 7, 15),
        "assignments": [dict(assignment)],
        "assignment_schedule_days": [
            {**row, "requires_review": False} for row in schedule_days
        ],
        "active_lock_days": [],
        "historical_facts": {
            "leave_substitution_events": [],
            "actual_hours_adjustments": [],
            "non_cancelled_payments": [],
            "active_settlements": [],
        },
    }
    return original, snapshot


def _batch_leave_resolution_transition_input(items):
    original, snapshot = _batch_leave_resolution_transition_facts()
    canonical = canonicalize_assignment_leave_resolution_batch_request(
        {
            "contract_version": "assignment-leave-substitution-batch-preview/v1",
            "case_no": "CASE-1",
            "original_assignment_id": 11,
            "items": items,
        },
        original,
        snapshot,
    )
    return canonical, original, snapshot


def _domain_rules_adapter_fixtures():
    before_service_plan = {
        "segments": [
            {
                "segment_ref": "current:0",
                "caregiver_ref": 101,
                "status": "active",
                "service_period": {"start": date(2026, 8, 1), "end": date(2026, 8, 3)},
                "segment_kind": "formal",
                "lineage": {
                    "original_segment_ref": None,
                    "substitution_service_day": None,
                },
            }
        ],
        "daily_ownership": [
            {"service_day": date(2026, 8, 1), "segment_ref": "current:0", "caregiver_ref": 101},
            {"service_day": date(2026, 8, 2), "segment_ref": "current:0", "caregiver_ref": 101},
            {"service_day": date(2026, 8, 3), "segment_ref": "current:0", "caregiver_ref": 101},
        ],
        "service_period": {"start": date(2026, 8, 1), "end": date(2026, 8, 3)},
    }
    canonical_leave_intent = {
        "original_segment_ref": "current:0",
        "items": [
            {
                "item_ref": 0,
                "service_day": date(2026, 8, 2),
                "resolution": "substitute",
                "substitute_caregiver_ref": 202,
            }
        ],
    }
    candidate_after_service_plan = {
        "segments": [
            {
                "segment_ref": "current:0",
                "caregiver_ref": 101,
                "status": "active",
                "service_period": {"start": date(2026, 8, 1), "end": date(2026, 8, 3)},
                "segment_kind": "formal",
                "lineage": {
                    "original_segment_ref": None,
                    "substitution_service_day": None,
                },
            },
            {
                "segment_ref": "substitute:0",
                "caregiver_ref": 202,
                "status": "active",
                "service_period": {"start": date(2026, 8, 2), "end": date(2026, 8, 2)},
                "segment_kind": "single_day_substitute",
                "lineage": {
                    "original_segment_ref": "current:0",
                    "substitution_service_day": date(2026, 8, 2),
                },
            },
        ],
        "daily_ownership": [
            {"service_day": date(2026, 8, 1), "segment_ref": "current:0", "caregiver_ref": 101},
            {"service_day": date(2026, 8, 2), "segment_ref": "substitute:0", "caregiver_ref": 202},
            {"service_day": date(2026, 8, 3), "segment_ref": "current:0", "caregiver_ref": 101},
        ],
        "service_period": {"start": date(2026, 8, 1), "end": date(2026, 8, 3)},
    }
    return before_service_plan, canonical_leave_intent, candidate_after_service_plan


def _domain_rules_adapter_success_transition_payload():
    before_assignment = {
        "id": 1,
        "case_no": "__pure_domain_case__",
        "staff_id": 1,
        "status": "active",
        "assigned_start_date": date(2026, 8, 1),
        "assigned_end_date": date(2026, 8, 3),
        "kind": "formal",
        "original_assignment_id": None,
        "substitution_work_date": None,
    }
    substitute_assignment = {
        "id": "__substitute__:0",
        "case_no": "__pure_domain_case__",
        "staff_id": 2,
        "status": "active",
        "assigned_start_date": date(2026, 8, 2),
        "assigned_end_date": date(2026, 8, 2),
        "kind": "single_day_substitute",
        "original_assignment_id": 1,
        "substitution_work_date": date(2026, 8, 2),
    }
    return before_assignment, substitute_assignment, {
        "case_no": "__pure_domain_case__",
        "operation_kind": "batch_leave_resolution",
        "historical_fact_state": "bootstrap",
        "requires_audit": False,
        "effective_date": date(2026, 8, 2),
        "current_case_start_date": date(2026, 8, 1),
        "current_case_end_date": date(2026, 8, 3),
        "proposed_case_start_date": date(2026, 8, 1),
        "proposed_case_end_date": date(2026, 8, 3),
        "removed_future_dates": [],
        "before_assignments": [before_assignment],
        "after_assignments": [before_assignment, substitute_assignment],
        "created": [substitute_assignment],
        "retained": [before_assignment],
        "truncated": [],
        "cancelled": [],
        "facts": {
            "created": [substitute_assignment],
            "retained": [before_assignment],
            "truncated": [],
            "cancelled": [],
        },
        "ownership_by_date": {
            "2026-08-01": 1,
            "2026-08-02": "__substitute__:0",
            "2026-08-03": 1,
        },
    }


def test_batch_leave_resolution_domain_rules_adapter_success_uses_temporary_id_mapping(monkeypatch):
    before_service_plan, canonical_leave_intent, candidate_after_service_plan = (
        _domain_rules_adapter_fixtures()
    )
    _, _, transition_payload = _domain_rules_adapter_success_transition_payload()

    def validate_assignment_plan_transition(**kwargs):
        assert kwargs["case_no"] == "__pure_domain_case__"
        assert kwargs["operation_kind"] == "batch_leave_resolution"
        assert kwargs["current_case_end_date"] == date(2026, 8, 3)
        assert kwargs["proposed_case_end_date"] == date(2026, 8, 3)
        return transition_payload

    monkeypatch.setattr(
        leave_preview,
        "validate_assignment_plan_transition",
        validate_assignment_plan_transition,
    )

    result = validate_assignment_leave_resolution_domain_transition(
        case_ref="CASE-1",
        database_current_date=date(2026, 8, 1),
        historical_fact_state="bootstrap",
        before_service_plan=before_service_plan,
        canonical_leave_intent=canonical_leave_intent,
        candidate_after_service_plan=candidate_after_service_plan,
    )

    assert result["valid"] is True
    assert result["after_service_plan"] == {
        "segments": [
            {
                "segment_ref": "current:0",
                "caregiver_ref": 101,
                "status": "active",
                "service_period": {"start": date(2026, 8, 1), "end": date(2026, 8, 3)},
                "segment_kind": "formal",
                "lineage": {"original_segment_ref": None, "substitution_service_day": None},
            },
            {
                "segment_ref": "substitute:0",
                "caregiver_ref": 202,
                "status": "active",
                "service_period": {"start": date(2026, 8, 2), "end": date(2026, 8, 2)},
                "segment_kind": "single_day_substitute",
                "lineage": {"original_segment_ref": "current:0", "substitution_service_day": date(2026, 8, 2)},
            },
        ],
        "daily_ownership": [
            {"service_day": date(2026, 8, 1), "segment_ref": "current:0", "caregiver_ref": 101},
            {"service_day": date(2026, 8, 2), "segment_ref": "substitute:0", "caregiver_ref": 202},
            {"service_day": date(2026, 8, 3), "segment_ref": "current:0", "caregiver_ref": 101},
        ],
        "service_period": {"start": date(2026, 8, 1), "end": date(2026, 8, 3)},
    }


def test_batch_leave_resolution_domain_rules_adapter_rejects_before_assignment_identity_mismatch(monkeypatch):
    before_service_plan, canonical_leave_intent, candidate_after_service_plan = (
        _domain_rules_adapter_fixtures()
    )
    before_assignment, substitute_assignment, transition_payload = (
        _domain_rules_adapter_success_transition_payload()
    )
    transition_payload["before_assignments"] = [
        dict(before_assignment, id=999, staff_id=3),
        dict(substitute_assignment),
    ]

    def validate_assignment_plan_transition(**_kwargs):
        return transition_payload

    monkeypatch.setattr(
        leave_preview,
        "validate_assignment_plan_transition",
        validate_assignment_plan_transition,
    )

    with pytest.raises(
        ValueError,
        match="dependency before_assignments must equal the projected plan rows",
    ):
        validate_assignment_leave_resolution_domain_transition(
            case_ref="CASE-1",
            database_current_date=date(2026, 8, 1),
            historical_fact_state="bootstrap",
            before_service_plan=before_service_plan,
            canonical_leave_intent=canonical_leave_intent,
            candidate_after_service_plan=candidate_after_service_plan,
        )


def test_batch_leave_resolution_domain_rules_adapter_rejects_unknown_after_assignment_identity(monkeypatch):
    before_service_plan, canonical_leave_intent, candidate_after_service_plan = (
        _domain_rules_adapter_fixtures()
    )
    _, _, transition_payload = _domain_rules_adapter_success_transition_payload()
    transition_payload["after_assignments"] = [
        {
            "id": 1,
            "case_no": "__pure_domain_case__",
            "staff_id": 1,
            "status": "active",
            "assigned_start_date": date(2026, 8, 1),
            "assigned_end_date": date(2026, 8, 3),
            "kind": "formal",
            "original_assignment_id": None,
            "substitution_work_date": None,
        },
        {
            "id": "__unknown__",
            "case_no": "__pure_domain_case__",
            "staff_id": 2,
            "status": "active",
            "assigned_start_date": date(2026, 8, 2),
            "assigned_end_date": date(2026, 8, 2),
            "kind": "single_day_substitute",
            "original_assignment_id": 1,
            "substitution_work_date": date(2026, 8, 2),
        },
    ]

    def validate_assignment_plan_transition(**_kwargs):
        return transition_payload

    monkeypatch.setattr(
        leave_preview,
        "validate_assignment_plan_transition",
        validate_assignment_plan_transition,
    )

    with pytest.raises(
        ValueError,
        match="dependency after_assignments must equal the projected plan rows",
    ):
        validate_assignment_leave_resolution_domain_transition(
            case_ref="CASE-1",
            database_current_date=date(2026, 8, 1),
            historical_fact_state="bootstrap",
            before_service_plan=before_service_plan,
            canonical_leave_intent=canonical_leave_intent,
            candidate_after_service_plan=candidate_after_service_plan,
        )


def test_batch_leave_resolution_domain_rules_adapter_rejects_conflict_codes_and_reverses_ids(monkeypatch):
    before_service_plan, canonical_leave_intent, candidate_after_service_plan = (
        _domain_rules_adapter_fixtures()
    )

    def validate_assignment_plan_transition(**_kwargs):
        conflict = leave_preview.AssignmentPlanTransitionConflict(
            "batch_substitute_date_duplicate"
        )
        conflict.details = {
            "assignment_ids": [1, "__substitute__:0"],
            "field": "segment_ref",
        }
        raise conflict

    monkeypatch.setattr(
        leave_preview,
        "validate_assignment_plan_transition",
        validate_assignment_plan_transition,
    )

    result = validate_assignment_leave_resolution_domain_transition(
        case_ref="CASE-1",
        database_current_date=date(2026, 8, 1),
        historical_fact_state="bootstrap",
        before_service_plan=before_service_plan,
        canonical_leave_intent=canonical_leave_intent,
        candidate_after_service_plan=candidate_after_service_plan,
    )

    assert result["valid"] is False
    assert result["after_service_plan"] is None
    assert result["transition_diagnostics"] == [
        {
            "code": "batch_substitute_date_duplicate",
            "scope_ref": "transition",
            "facts": {
                "segment_refs": ["current:0", "substitute:0"],
                "field": "segment_ref",
            },
        }
    ]


def test_batch_leave_resolution_domain_rules_adapter_rejects_candidate_staff_not_in_intent():
    before_service_plan, canonical_leave_intent, candidate_after_service_plan = (
        _domain_rules_adapter_fixtures()
    )
    canonical_leave_intent = {
        "original_segment_ref": "current:0",
        "items": [
            {
                "item_ref": 0,
                "service_day": date(2026, 8, 2),
                "resolution": "defer",
                "substitute_caregiver_ref": None,
            }
        ],
    }

    with pytest.raises(
        ValueError,
        match="candidate_after_service_plan caregiver refs must match before refs and substitute intent refs",
    ):
        validate_assignment_leave_resolution_domain_transition(
            case_ref="CASE-1",
            database_current_date=date(2026, 8, 1),
            historical_fact_state="bootstrap",
            before_service_plan=before_service_plan,
            canonical_leave_intent=canonical_leave_intent,
            candidate_after_service_plan=candidate_after_service_plan,
        )


def _domain_rules_adapter_echo_transition(kwargs, ownership_by_date):
    before = kwargs["current_assignments"]
    after = kwargs["proposed_assignments"]
    before_by_id = {row["id"]: row for row in before}
    facts = {"created": [], "retained": [], "truncated": [], "cancelled": []}
    for row in after:
        prior = before_by_id.get(row["id"])
        if prior is None:
            facts["created"].append(row)
        elif row["status"] == "cancelled":
            facts["cancelled"].append({"before": prior, "after": row})
        elif row == prior:
            facts["retained"].append(row)
        else:
            facts["truncated"].append({"before": prior, "after": row})
    return {
        "case_no": kwargs["case_no"],
        "operation_kind": kwargs["operation_kind"],
        "historical_fact_state": kwargs["historical_fact_state"],
        "requires_audit": False,
        "effective_date": kwargs["effective_date"],
        "current_case_start_date": kwargs["current_case_start_date"],
        "current_case_end_date": kwargs["current_case_end_date"],
        "proposed_case_start_date": kwargs["proposed_case_start_date"],
        "proposed_case_end_date": kwargs["proposed_case_end_date"],
        "removed_future_dates": [],
        "before_assignments": before,
        "after_assignments": after,
        **facts,
        "facts": facts,
        "ownership_by_date": ownership_by_date,
    }


@pytest.mark.parametrize(
    ("original_ref", "first_substitute", "second_substitute"),
    [(True, 1, "1"), (False, 0, "0")],
    ids=["true-int-string", "false-int-string"],
)
def test_batch_leave_resolution_domain_rules_adapter_uses_typed_canonical_caregiver_order(
    monkeypatch, original_ref, first_substitute, second_substitute
):
    before, intent, candidate = _domain_rules_adapter_fixtures()
    before["segments"][0]["caregiver_ref"] = original_ref
    for row in before["daily_ownership"]:
        row["caregiver_ref"] = original_ref
    before["service_period"]["end"] = date(2026, 8, 3)
    intent["items"] = [
        {
            "item_ref": 0,
            "service_day": date(2026, 8, 1),
            "resolution": "substitute",
            "substitute_caregiver_ref": first_substitute,
        },
        {
            "item_ref": 1,
            "service_day": date(2026, 8, 2),
            "resolution": "substitute",
            "substitute_caregiver_ref": second_substitute,
        },
    ]
    candidate["segments"][0]["caregiver_ref"] = original_ref
    candidate["segments"] = [
        candidate["segments"][0],
        {
            "segment_ref": "substitute:0",
            "caregiver_ref": first_substitute,
            "status": "active",
            "service_period": {"start": date(2026, 8, 1), "end": date(2026, 8, 1)},
            "segment_kind": "single_day_substitute",
            "lineage": {
                "original_segment_ref": "current:0",
                "substitution_service_day": date(2026, 8, 1),
            },
        },
        {
            "segment_ref": "substitute:1",
            "caregiver_ref": second_substitute,
            "status": "active",
            "service_period": {"start": date(2026, 8, 2), "end": date(2026, 8, 2)},
            "segment_kind": "single_day_substitute",
            "lineage": {
                "original_segment_ref": "current:0",
                "substitution_service_day": date(2026, 8, 2),
            },
        },
    ]
    candidate["daily_ownership"] = [
        {"service_day": date(2026, 8, 1), "segment_ref": "substitute:0", "caregiver_ref": first_substitute},
        {"service_day": date(2026, 8, 2), "segment_ref": "substitute:1", "caregiver_ref": second_substitute},
        {"service_day": date(2026, 8, 3), "segment_ref": "current:0", "caregiver_ref": original_ref},
    ]
    calls = []

    def validate_assignment_plan_transition(**kwargs):
        calls.append(kwargs)
        return _domain_rules_adapter_echo_transition(
            kwargs,
            {
                "2026-08-01": "__substitute__:0",
                "2026-08-02": "__substitute__:1",
                "2026-08-03": 1,
            },
        )

    monkeypatch.setattr(
        leave_preview, "validate_assignment_plan_transition", validate_assignment_plan_transition
    )
    result = validate_assignment_leave_resolution_domain_transition(
        case_ref="CASE-1",
        database_current_date=date(2026, 8, 1),
        historical_fact_state="bootstrap",
        before_service_plan=before,
        canonical_leave_intent=intent,
        candidate_after_service_plan=candidate,
    )

    staff_by_rule_id = {row["id"]: row["staff_id"] for row in calls[0]["proposed_assignments"]}
    assert staff_by_rule_id == {1: 1, "__substitute__:0": 2, "__substitute__:1": 3}
    assert result["after_service_plan"]["daily_ownership"] == candidate["daily_ownership"]


@pytest.mark.parametrize("drift", ["top_level", "facts", "row", "after", "ownership"])
def test_batch_leave_resolution_domain_rules_adapter_rejects_exact_contract_drift(
    monkeypatch, drift
):
    before, intent, candidate = _domain_rules_adapter_fixtures()
    _, _, payload = _domain_rules_adapter_success_transition_payload()
    if drift == "top_level":
        payload["unexpected"] = None
    elif drift == "facts":
        del payload["facts"]["created"]
    elif drift == "row":
        payload["after_assignments"][0]["unexpected"] = None
    elif drift == "after":
        payload["after_assignments"][0]["assigned_end_date"] = date(2026, 8, 2)
    else:
        payload["ownership_by_date"]["2026-08-02"] = 1
    monkeypatch.setattr(leave_preview, "validate_assignment_plan_transition", lambda **_kwargs: payload)

    with pytest.raises(ValueError):
        validate_assignment_leave_resolution_domain_transition(
            case_ref="CASE-1",
            database_current_date=date(2026, 8, 1),
            historical_fact_state="bootstrap",
            before_service_plan=before,
            canonical_leave_intent=intent,
            candidate_after_service_plan=candidate,
        )


@pytest.mark.parametrize(
    ("code", "details", "valid"),
    [
        ("batch_substitute_date_duplicate", {"assignment_ids": [1, "__substitute__:0"]}, False),
        ("unknown_code", {}, None),
        ("batch_substitute_date_duplicate", {"unexpected": 1}, None),
        ("batch_leave_target_mismatch", {"reason": "__substitute__:0"}, None),
    ],
    ids=["allowlisted", "unknown-code", "unknown-detail", "temporary-leak"],
)
def test_batch_leave_resolution_domain_rules_adapter_reverses_only_allowlisted_conflicts(
    monkeypatch, code, details, valid
):
    before, intent, candidate = _domain_rules_adapter_fixtures()

    def validate_assignment_plan_transition(**_kwargs):
        raise leave_preview.AssignmentPlanTransitionConflict(code, details)

    monkeypatch.setattr(leave_preview, "validate_assignment_plan_transition", validate_assignment_plan_transition)
    if valid is None:
        with pytest.raises(ValueError):
            validate_assignment_leave_resolution_domain_transition(
                case_ref="CASE-1",
                database_current_date=date(2026, 8, 1),
                historical_fact_state="bootstrap",
                before_service_plan=before,
                canonical_leave_intent=intent,
                candidate_after_service_plan=candidate,
            )
    else:
        result = validate_assignment_leave_resolution_domain_transition(
            case_ref="CASE-1",
            database_current_date=date(2026, 8, 1),
            historical_fact_state="bootstrap",
            before_service_plan=before,
            canonical_leave_intent=intent,
            candidate_after_service_plan=candidate,
        )
        assert result["valid"] is valid
        assert result["after_service_plan"] is None


def test_batch_leave_resolution_domain_rules_adapter_is_immutable_and_capability_limited(monkeypatch):
    before, intent, candidate = _domain_rules_adapter_fixtures()
    original_before, original_intent, original_candidate = (
        deepcopy(before),
        deepcopy(intent),
        deepcopy(candidate),
    )
    _, _, payload = _domain_rules_adapter_success_transition_payload()
    monkeypatch.setattr(leave_preview, "validate_assignment_plan_transition", lambda **_kwargs: payload)

    validate_assignment_leave_resolution_domain_transition(
        case_ref="CASE-1",
        database_current_date=date(2026, 8, 1),
        historical_fact_state="bootstrap",
        before_service_plan=before,
        canonical_leave_intent=intent,
        candidate_after_service_plan=candidate,
    )

    assert (before, intent, candidate) == (original_before, original_intent, original_candidate)
    source = inspect.getsource(validate_assignment_leave_resolution_domain_transition)
    for forbidden in ("get_connection", "open(", "datetime.now", "os.environ", "subprocess"):
        assert forbidden not in source


def _batch_leave_resolution_pure_domain_fixtures(items, *, occupancy=(), state="bootstrap", protected=()):
    before = {
        "segments": [
            {
                "segment_ref": "current:0",
                "caregiver_ref": 101,
                "status": "active",
                "service_period": {"start": date(2026, 8, 1), "end": date(2026, 8, 3)},
                "segment_kind": "formal",
                "lineage": {"original_segment_ref": None, "substitution_service_day": None},
            }
        ],
        "daily_ownership": [
            {"service_day": date(2026, 8, day), "segment_ref": "current:0", "caregiver_ref": 101}
            for day in range(1, 4)
        ],
        "service_period": {"start": date(2026, 8, 1), "end": date(2026, 8, 3)},
        "service_commitment": {
            "required_service_days": 3,
            "hours_per_service_day": Decimal("8"),
            "required_total_hours": Decimal("24"),
        },
    }
    canonical_items = [
        {
            "item_ref": index,
            "service_day": day,
            "resolution": resolution,
            "substitute_caregiver_ref": substitute,
        }
        for index, (day, resolution, substitute) in enumerate(items)
    ]
    intent = {"case_ref": "CASE-PURE", "original_segment_ref": "current:0", "items": canonical_items}
    lineage = [
        {"item_ref": item["item_ref"], "original_service_day_ref": f"service-day:{item['service_day'].isoformat()}"}
        for item in canonical_items
    ]
    eligibility = {
        "database_current_date": date(2026, 7, 1),
        "historical_protection": {"state": state, "protected_segment_refs": list(protected)},
        "occupancy": list(occupancy),
    }
    return intent, lineage, before, eligibility


def _install_pure_domain_adapter(monkeypatch, *, invalid=False):
    calls = []

    def adapter(**kwargs):
        calls.append(deepcopy(kwargs))
        if invalid:
            return {
                "valid": False,
                "after_service_plan": None,
                "transition_diagnostics": [
                    {"code": "batch_leave_target_mismatch", "scope_ref": "current:0", "facts": {"segment_ref": "current:0"}}
                ],
            }
        return {
            "valid": True,
            "after_service_plan": deepcopy(kwargs["candidate_after_service_plan"]),
            "transition_diagnostics": [],
        }

    monkeypatch.setattr(leave_preview, "validate_assignment_leave_resolution_domain_transition", adapter)
    return calls


def test_batch_leave_resolution_pure_domain_transition_substitute_projects_opaque_ownership_and_impacts(monkeypatch):
    intent, lineage, before, eligibility = _batch_leave_resolution_pure_domain_fixtures(
        [(date(2026, 8, 2), "substitute", 202)]
    )
    calls = _install_pure_domain_adapter(monkeypatch)

    result = calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=intent, item_lineage=lineage, before_service_plan=before, eligibility_facts=eligibility
    )

    transition = result["service_plan_transition"]
    assert set(result) == {"canonical_intent", "service_plan_transition", "canonical_eligibility"}
    assert transition["after"]["daily_ownership"] == [
        {"service_day": date(2026, 8, 1), "segment_ref": "current:0", "caregiver_ref": 101},
        {"service_day": date(2026, 8, 2), "segment_ref": "substitute:0", "caregiver_ref": 202},
        {"service_day": date(2026, 8, 3), "segment_ref": "derived:0", "caregiver_ref": 101},
    ]
    assert [row["segment_ref"] for row in transition["after"]["segments"]] == ["current:0", "derived:0", "substitute:0"]
    assert transition["impacts"]["total"] == {
        "before_service_days": 3, "after_service_days": 3, "delta_service_days": 0,
        "before_service_hours": Decimal("24"), "after_service_hours": Decimal("24"), "delta_service_hours": Decimal("0"),
        "required_service_days": 3, "required_total_hours": Decimal("24"),
    }
    assert result["canonical_eligibility"] == {
        "transition_valid": True, "applicable": True, "blocking_diagnostics": [], "review_diagnostics": []
    }
    assert calls[0]["canonical_leave_intent"] == {"original_segment_ref": "current:0", "items": intent["items"]}


@pytest.mark.parametrize("defer_count", [1, 2, 3])
def test_batch_leave_resolution_pure_domain_transition_defers_once_and_preserves_commitment(monkeypatch, defer_count):
    intent, lineage, before, eligibility = _batch_leave_resolution_pure_domain_fixtures(
        [(date(2026, 8, day), "defer", None) for day in range(1, defer_count + 1)]
    )
    _install_pure_domain_adapter(monkeypatch)

    result = calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=intent, item_lineage=lineage, before_service_plan=before, eligibility_facts=eligibility
    )

    after = result["service_plan_transition"]["after"]
    assert after["service_period"]["end"] == date(2026, 8, 3 + defer_count)
    assert len(after["daily_ownership"]) == 3 + defer_count
    assert result["service_plan_transition"]["impacts"]["total"]["after_service_days"] == 3
    assert result["canonical_eligibility"]["applicable"] is True


def test_batch_leave_resolution_pure_domain_transition_mixed_and_valid_but_blocked_retains_after(monkeypatch):
    occupancy = [
        {"caregiver_ref": 202, "service_day": date(2026, 8, 1), "source_kind": "waiting_deposit_lock", "source_segment_ref": None}
    ]
    intent, lineage, before, eligibility = _batch_leave_resolution_pure_domain_fixtures(
        [(date(2026, 8, 1), "substitute", 202), (date(2026, 8, 2), "defer", None)], occupancy=occupancy
    )
    _install_pure_domain_adapter(monkeypatch)

    result = calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=intent, item_lineage=lineage, before_service_plan=before, eligibility_facts=eligibility
    )

    assert result["service_plan_transition"]["after"] is not None
    assert result["canonical_eligibility"]["transition_valid"] is True
    assert result["canonical_eligibility"]["applicable"] is False
    assert result["canonical_eligibility"]["blocking_diagnostics"][0]["code"] == "waiting_deposit_lock_conflict"


@pytest.mark.parametrize(
    ("source_kind", "state", "protected", "code", "applicable"),
    [
        ("formal_service", "bootstrap", (), "formal_service_conflict", False),
        ("legacy_unresolved", "bootstrap", (), "legacy_ownership_requires_review", True),
        (None, "locked", ("current:0",), "historical_ownership_locked", False),
    ],
)
def test_batch_leave_resolution_pure_domain_transition_routes_eligibility_diagnostics(
    monkeypatch, source_kind, state, protected, code, applicable
):
    occupancy = [] if source_kind is None else [
        {"caregiver_ref": 202, "service_day": date(2026, 8, 2), "source_kind": source_kind, "source_segment_ref": None}
    ]
    intent, lineage, before, eligibility = _batch_leave_resolution_pure_domain_fixtures(
        [(date(2026, 8, 2), "substitute", 202)], occupancy=occupancy, state=state, protected=protected
    )
    _install_pure_domain_adapter(monkeypatch)

    result = calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=intent, item_lineage=lineage, before_service_plan=before, eligibility_facts=eligibility
    )

    diagnostics = result["canonical_eligibility"]["blocking_diagnostics"] + result["canonical_eligibility"]["review_diagnostics"]
    assert result["canonical_eligibility"]["applicable"] is applicable
    assert [diagnostic["code"] for diagnostic in diagnostics] == [code]
    assert result["service_plan_transition"]["after"] is not None


def test_batch_leave_resolution_pure_domain_transition_invalid_adapter_has_null_after_and_impacts(monkeypatch):
    intent, lineage, before, eligibility = _batch_leave_resolution_pure_domain_fixtures(
        [(date(2026, 8, 2), "substitute", 202)]
    )
    _install_pure_domain_adapter(monkeypatch, invalid=True)

    result = calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=intent, item_lineage=lineage, before_service_plan=before, eligibility_facts=eligibility
    )

    assert result["service_plan_transition"]["after"] is None
    assert result["service_plan_transition"]["impacts"] is None
    assert result["canonical_eligibility"]["transition_valid"] is False
    assert result["canonical_eligibility"]["applicable"] is False


def test_batch_leave_resolution_pure_domain_transition_rejects_contract_drift_and_is_immutable(monkeypatch):
    intent, lineage, before, eligibility = _batch_leave_resolution_pure_domain_fixtures(
        [(date(2026, 8, 2), "substitute", 202)]
    )
    original = deepcopy((intent, lineage, before, eligibility))
    _install_pure_domain_adapter(monkeypatch)
    with pytest.raises(TypeError):
        calculate_assignment_leave_resolution_batch_transition(intent, lineage, before, eligibility)
    calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=intent, item_lineage=lineage, before_service_plan=before, eligibility_facts=eligibility
    )
    assert (intent, lineage, before, eligibility) == original
    broken_lineage = deepcopy(lineage)
    broken_lineage[0]["original_service_day_ref"] = "service-day:2026-08-03"
    with pytest.raises(ValueError, match="item_lineage original_service_day_ref"):
        calculate_assignment_leave_resolution_batch_transition(
            canonical_intent=intent, item_lineage=broken_lineage, before_service_plan=before, eligibility_facts=eligibility
        )
    malformed_occupancy = deepcopy(eligibility)
    malformed_occupancy["occupancy"] = [{"caregiver_ref": 202}]
    with pytest.raises(ValueError, match="occupancy item must contain exact keys"):
        calculate_assignment_leave_resolution_batch_transition(
            canonical_intent=intent, item_lineage=lineage, before_service_plan=before, eligibility_facts=malformed_occupancy
        )


def test_batch_leave_resolution_pure_domain_transition_allows_same_caregiver_fragments_and_flags_commitment(monkeypatch):
    intent, lineage, before, eligibility = _batch_leave_resolution_pure_domain_fixtures(
        [(date(2026, 8, 2), "substitute", 101)]
    )
    intent["case_ref"] = True
    before["service_commitment"] = {
        "required_service_days": 4,
        "hours_per_service_day": Decimal("8"),
        "required_total_hours": Decimal("32"),
    }
    _install_pure_domain_adapter(monkeypatch)

    result = calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=intent, item_lineage=lineage, before_service_plan=before, eligibility_facts=eligibility
    )

    impacts = result["service_plan_transition"]["impacts"]
    assert [row["caregiver_ref"] for row in impacts["per_caregiver"]] == [101]
    assert result["service_plan_transition"]["after"] is not None
    assert result["canonical_eligibility"]["applicable"] is False
    assert result["canonical_eligibility"]["blocking_diagnostics"] == [
        {
            "code": "service_commitment_mismatch",
            "scope_ref": "transition",
            "facts": {
                "expected_service_days": 4, "actual_service_days": 3,
                "expected_service_hours": "32", "actual_service_hours": "24",
            },
        }
    ]


def test_batch_leave_resolution_pure_domain_transition_is_deterministic_and_capability_limited(monkeypatch):
    intent, lineage, before, eligibility = _batch_leave_resolution_pure_domain_fixtures(
        [(date(2026, 8, 1), "substitute", 202), (date(2026, 8, 2), "defer", None)]
    )
    _install_pure_domain_adapter(monkeypatch)
    first = calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=intent, item_lineage=lineage, before_service_plan=before, eligibility_facts=eligibility
    )
    second = calculate_assignment_leave_resolution_batch_transition(
        canonical_intent=deepcopy(intent), item_lineage=deepcopy(lineage), before_service_plan=deepcopy(before), eligibility_facts=deepcopy(eligibility)
    )
    assert first == second
    source = inspect.getsource(calculate_assignment_leave_resolution_batch_transition)
    module = ast.parse(inspect.getsource(leave_preview))
    definitions = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "calculate_assignment_leave_resolution_batch_transition"
    ]
    assert len(definitions) == 1
    for forbidden in (
        "get_connection",
        "_extract_blocked_days",
        "validate_assignment_plan_transition",
        "open(",
        "datetime.now",
        "canonical_batch_intent",
        "original_assignment_schedule",
        "conflict_snapshot",
        "batch-substitute-",
        "assignment_transition_plan",
        "schedule_change_plan",
    ):
        assert forbidden not in source


def _batch_preview_dependency_results(*, blocking_reasons=(), review_reasons=()):
    original, snapshot = _batch_leave_resolution_transition_facts()
    snapshot["assignments"] = [
        {
            key: value
            for key, value in snapshot["assignments"][0].items()
            if key != "service_hours_per_day"
        }
    ]
    snapshot["assignment_schedule_days"] = [
        {
            **row,
            "is_double_pay": False,
            "notes": None,
            "requires_review": False,
        }
        for row in snapshot["assignment_schedule_days"]
    ]
    request = {
        "contract_version": "assignment-leave-substitution-batch-preview/v1",
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "items": [
            {
                "original_schedule_id": 21,
                "work_date": "2026-08-01",
                "resolution_type": "substitute",
                "substitute_staff_id": 202,
                "is_double_pay": False,
            }
        ],
    }
    blocking_diagnostics = [
        {"code": code, "scope_ref": "transition", "facts": {}}
        for code in blocking_reasons
    ]
    review_diagnostics = [
        {"code": code, "scope_ref": "transition", "facts": {}}
        for code in review_reasons
    ]

    def transition(**kwargs):
        return {
            "canonical_intent": deepcopy(kwargs["canonical_intent"]),
            "service_plan_transition": {
                "before": deepcopy(kwargs["before_service_plan"]),
                "intent": deepcopy(kwargs["canonical_intent"]),
                "after": deepcopy(kwargs["before_service_plan"]),
                "impacts": {
                    "per_caregiver": [],
                    "total": {
                        "before_service_hours": Decimal("40"),
                        "after_service_hours": Decimal("40"),
                    },
                },
            },
            "canonical_eligibility": {
                "transition_valid": True,
                "applicable": not blocking_diagnostics,
                "blocking_diagnostics": deepcopy(blocking_diagnostics),
                "review_diagnostics": deepcopy(review_diagnostics),
            },
        }

    return request, original, snapshot, transition


def test_batch_leave_resolution_preview_from_pure_transition_projects_opaque_facts_and_retains_blocked_after(
    monkeypatch,
):
    original, snapshot = _batch_leave_resolution_transition_facts()
    snapshot["assignments"] = [
        {key: value for key, value in snapshot["assignments"][0].items() if key != "service_hours_per_day"}
    ]
    snapshot["assignment_schedule_days"] = [
        {**row, "is_double_pay": False, "notes": None, "requires_review": False}
        for row in snapshot["assignment_schedule_days"]
    ]
    request = {
        "contract_version": "assignment-leave-substitution-batch-preview/v1",
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "items": [{"original_schedule_id": 21, "work_date": "2026-08-01", "resolution_type": "substitute", "substitute_staff_id": 202}],
    }
    captured = {}

    def transition(**kwargs):
        captured.update(deepcopy(kwargs))
        return {
            "canonical_intent": deepcopy(kwargs["canonical_intent"]),
            "service_plan_transition": {
                "before": deepcopy(kwargs["before_service_plan"]),
                "intent": deepcopy(kwargs["canonical_intent"]),
                "after": deepcopy(kwargs["before_service_plan"]),
                "impacts": {"per_caregiver": [], "total": {}},
            },
            "canonical_eligibility": {
                "transition_valid": True,
                "applicable": False,
                "blocking_diagnostics": [{"code": "formal_service_conflict", "scope_ref": "transition", "facts": {}}],
                "review_diagnostics": [],
            },
        }

    monkeypatch.setattr(leave_preview, "calculate_assignment_leave_resolution_batch_transition", transition)
    result = compute_assignment_leave_resolution_batch_preview_from_snapshot(request, original, snapshot)

    assert set(captured) == {"canonical_intent", "item_lineage", "before_service_plan", "eligibility_facts"}
    assert captured["canonical_intent"]["original_segment_ref"] == "current:0"
    assert captured["canonical_intent"]["items"][0]["substitute_caregiver_ref"] == "caregiver:1"
    assert captured["item_lineage"] == [{"item_ref": 0, "original_service_day_ref": "service-day:2026-08-01"}]
    assert result["status"] == "blocked"
    assert result["requires_confirmation"] is False
    assert result["service_plan_transition"]["after"] is not None
    payload = json.dumps(captured, default=str, sort_keys=True)
    assert '"11"' not in payload and '"101"' not in payload and '"202"' not in payload
    assert result["preview_fingerprint"] == result["preview_fingerprint"].lower()


def test_batch_leave_resolution_preview_from_pure_transition_handles_unseen_lock_staff_and_reordered_snapshot(
    monkeypatch,
):
    original, snapshot = _batch_leave_resolution_transition_facts()
    snapshot["assignments"] = [{key: value for key, value in snapshot["assignments"][0].items() if key != "service_hours_per_day"}]
    snapshot["assignment_schedule_days"] = [{**row, "is_double_pay": False, "notes": None, "requires_review": False} for row in snapshot["assignment_schedule_days"]]
    snapshot["active_lock_days"] = [{"id": 1, "lock_id": 2, "plan_id": 3, "case_no": "CASE-1", "segment_id": 4, "staff_id": 404, "lock_date": date(2026, 8, 1)}]
    request = {"contract_version": "assignment-leave-substitution-batch-preview/v1", "case_no": "CASE-1", "original_assignment_id": 11, "items": [{"original_schedule_id": 21, "work_date": "2026-08-01", "resolution_type": "substitute", "substitute_staff_id": 202}]}
    captured = []

    def transition(**kwargs):
        captured.append(deepcopy(kwargs))
        return {"canonical_intent": deepcopy(kwargs["canonical_intent"]), "service_plan_transition": {"before": deepcopy(kwargs["before_service_plan"]), "intent": deepcopy(kwargs["canonical_intent"]), "after": deepcopy(kwargs["before_service_plan"]), "impacts": {"per_caregiver": [], "total": {}}}, "canonical_eligibility": {"transition_valid": True, "applicable": False, "blocking_diagnostics": [{"code": "waiting_deposit_lock_conflict", "scope_ref": "transition", "facts": {}}], "review_diagnostics": []}}

    monkeypatch.setattr(leave_preview, "calculate_assignment_leave_resolution_batch_transition", transition)
    before = deepcopy((request, original, snapshot))
    first = compute_assignment_leave_resolution_batch_preview_from_snapshot(request, original, snapshot)
    reordered = deepcopy(snapshot)
    reordered["assignments"].reverse()
    reordered["assignment_schedule_days"].reverse()
    reordered["active_lock_days"].reverse()
    second = compute_assignment_leave_resolution_batch_preview_from_snapshot(dict(reversed(request.items())), dict(reversed(original.items())), reordered)

    assert first == second
    assert (request, original, snapshot) == before
    lock_occupancy = next(row for row in captured[0]["eligibility_facts"]["occupancy"] if row["source_kind"] == "waiting_deposit_lock")
    assert lock_occupancy["caregiver_ref"] == "caregiver:2"


@pytest.mark.parametrize("field", ["active_lock_days", "assignment_schedule_days", "historical_facts"])
def test_batch_leave_resolution_preview_from_pure_transition_fails_closed_for_malformed_unselected_snapshot_rows(field):
    original, snapshot = _batch_leave_resolution_transition_facts()
    snapshot["assignments"] = [{key: value for key, value in snapshot["assignments"][0].items() if key != "service_hours_per_day"}]
    snapshot["assignment_schedule_days"] = [{**row, "is_double_pay": False, "notes": None, "requires_review": False} for row in snapshot["assignment_schedule_days"]]
    if field == "active_lock_days":
        snapshot[field] = [{"id": 1, "lock_id": 2, "plan_id": 3, "case_no": "CASE-1", "segment_id": 4, "staff_id": True, "lock_date": date(2026, 8, 1)}]
    elif field == "assignment_schedule_days":
        snapshot[field].append({"id": 99})
    else:
        snapshot[field]["non_cancelled_payments"] = [{"assignment_id": True}]
    request = {"contract_version": "assignment-leave-substitution-batch-preview/v1", "case_no": "CASE-1", "original_assignment_id": 11, "items": [{"original_schedule_id": 21, "work_date": "2026-08-01", "resolution_type": "defer_following_assignments", "substitute_staff_id": None}]}
    with pytest.raises(ValueError):
        compute_assignment_leave_resolution_batch_preview_from_snapshot(request, original, snapshot)


def test_batch_leave_resolution_preview_from_pure_transition_rejects_invalid_after_contract(monkeypatch):
    original, snapshot = _batch_leave_resolution_transition_facts()
    snapshot["assignments"] = [{key: value for key, value in snapshot["assignments"][0].items() if key != "service_hours_per_day"}]
    snapshot["assignment_schedule_days"] = [{**row, "is_double_pay": False, "notes": None, "requires_review": False} for row in snapshot["assignment_schedule_days"]]
    request = {"contract_version": "assignment-leave-substitution-batch-preview/v1", "case_no": "CASE-1", "original_assignment_id": 11, "items": [{"original_schedule_id": 21, "work_date": "2026-08-01", "resolution_type": "defer_following_assignments", "substitute_staff_id": None}]}
    monkeypatch.setattr(leave_preview, "calculate_assignment_leave_resolution_batch_transition", lambda **kwargs: {"canonical_intent": kwargs["canonical_intent"], "service_plan_transition": {"before": {}, "intent": {}, "after": {}, "impacts": {}}, "canonical_eligibility": {"transition_valid": False, "applicable": False, "blocking_diagnostics": [], "review_diagnostics": []}})
    with pytest.raises(ValueError, match="invalid transition"):
        compute_assignment_leave_resolution_batch_preview_from_snapshot(request, original, snapshot)


@pytest.mark.parametrize(
    ("blocking_reasons", "review_reasons", "expected_status", "confirmation"),
    [
        (["formal_service_conflict"], ["historical_change_requires_review"], "blocked", False),
        ([], ["historical_change_requires_review"], "requires_review", True),
        ([], [], "ready", True),
    ],
)
def test_batch_leave_resolution_preview_from_snapshot_aggregates_status_and_delegates_once(
    monkeypatch, blocking_reasons, review_reasons, expected_status, confirmation
):
    request, original, snapshot, transition = _batch_preview_dependency_results(
        blocking_reasons=blocking_reasons,
        review_reasons=review_reasons,
    )
    calls = []
    real_canonicalize = leave_preview.canonicalize_assignment_leave_resolution_batch_request

    def canonicalize(request, original_assignment_schedule, conflict_snapshot):
        calls.append(("canonicalize", request, original_assignment_schedule, conflict_snapshot))
        return real_canonicalize(request, original_assignment_schedule, conflict_snapshot)

    def calculate(**kwargs):
        calls.append(("calculate", deepcopy(kwargs)))
        return transition(**kwargs)

    monkeypatch.setattr(
        leave_preview,
        "canonicalize_assignment_leave_resolution_batch_request",
        canonicalize,
    )
    monkeypatch.setattr(
        leave_preview,
        "calculate_assignment_leave_resolution_batch_transition",
        calculate,
    )
    result = compute_assignment_leave_resolution_batch_preview_from_snapshot(
        request, original, snapshot
    )

    assert result["status"] == expected_status
    assert result["requires_confirmation"] is confirmation
    assert set(result) == {
        "contract_version",
        "canonical_intent",
        "double_pay_preferences",
        "service_plan_transition",
        "canonical_eligibility",
        "status",
        "requires_confirmation",
        "preview_fingerprint",
    }
    assert [item["code"] for item in result["canonical_eligibility"]["blocking_diagnostics"]] == blocking_reasons
    assert [item["code"] for item in result["canonical_eligibility"]["review_diagnostics"]] == review_reasons
    assert result["double_pay_preferences"] == [{"item_ref": 0, "is_double_pay": False}]
    assert [call[0] for call in calls] == ["canonicalize", "calculate"]
    assert set(calls[1][1]) == {
        "canonical_intent",
        "item_lineage",
        "before_service_plan",
        "eligibility_facts",
    }


def test_batch_leave_resolution_preview_from_snapshot_fingerprint_is_complete_and_rejects_contract_extras(
    monkeypatch,
):
    request, original, snapshot, transition = _batch_preview_dependency_results()
    variation = {"case_ref": None, "after_hours": Decimal("40"), "review": False, "extra": False}

    def calculate(**kwargs):
        result = transition(**kwargs)
        if variation["case_ref"] is not None:
            result["canonical_intent"]["case_ref"] = variation["case_ref"]
        result["service_plan_transition"]["impacts"]["total"]["after_service_hours"] = variation["after_hours"]
        if variation["review"]:
            result["canonical_eligibility"]["review_diagnostics"] = [
                {
                    "code": "historical_change_requires_review",
                    "scope_ref": "transition",
                    "facts": {},
                }
            ]
        if variation["extra"]:
            result["display_text"] = "代班預覽"
        return result

    monkeypatch.setattr(
        leave_preview,
        "calculate_assignment_leave_resolution_batch_transition",
        calculate,
    )

    baseline = compute_assignment_leave_resolution_batch_preview_from_snapshot(
        request, original, snapshot
    )

    changed_double_pay_request = deepcopy(request)
    changed_double_pay_request["items"][0]["is_double_pay"] = True
    changed_double_pay = compute_assignment_leave_resolution_batch_preview_from_snapshot(
        changed_double_pay_request, original, snapshot
    )
    assert changed_double_pay["preview_fingerprint"] != baseline["preview_fingerprint"]

    variation["case_ref"] = "case:CASE-1-v2"
    changed_intent = compute_assignment_leave_resolution_batch_preview_from_snapshot(
        request, original, snapshot
    )
    assert changed_intent["preview_fingerprint"] != baseline["preview_fingerprint"]

    variation["case_ref"] = None
    variation["after_hours"] = Decimal("39")
    changed_transition = compute_assignment_leave_resolution_batch_preview_from_snapshot(
        request, original, snapshot
    )
    assert changed_transition["preview_fingerprint"] != baseline["preview_fingerprint"]

    variation["after_hours"] = Decimal("40")
    variation["review"] = True
    changed_eligibility = compute_assignment_leave_resolution_batch_preview_from_snapshot(
        request, original, snapshot
    )
    assert changed_eligibility["preview_fingerprint"] != baseline["preview_fingerprint"]
    assert changed_eligibility["status"] == "requires_review"

    variation["review"] = False
    variation["extra"] = True
    with pytest.raises(ValueError, match="transition returned an invalid contract"):
        compute_assignment_leave_resolution_batch_preview_from_snapshot(
            request, original, snapshot
        )

    assert baseline["preview_fingerprint"] == baseline["preview_fingerprint"].lower()
    assert len(baseline["preview_fingerprint"]) == 64


def test_batch_leave_resolution_preview_from_snapshot_propagates_transition_dependency_exception(
    monkeypatch,
):
    request, original, snapshot, _transition = _batch_preview_dependency_results()
    calls = []
    expected_error = RuntimeError("dependency failure")

    def calculate(**kwargs):
        calls.append(deepcopy(kwargs))
        raise expected_error
    monkeypatch.setattr(
        leave_preview,
        "calculate_assignment_leave_resolution_batch_transition",
        calculate,
    )

    with pytest.raises(RuntimeError) as exc_info:
        compute_assignment_leave_resolution_batch_preview_from_snapshot(
            request, original, snapshot
        )
    assert exc_info.value is expected_error
    assert len(calls) == 1
    assert set(calls[0]) == {
        "canonical_intent",
        "item_lineage",
        "before_service_plan",
        "eligibility_facts",
    }


def test_leave_resolution_preview_from_snapshot_is_pure_deterministic_substitute():
    import copy

    work_date, original, snapshot = _leave_resolution_preview_facts()
    request = _leave_resolution_preview_from_snapshot_request(
        work_date, "substitute", 202
    )
    before = copy.deepcopy((request, original, snapshot))

    first = compute_assignment_leave_resolution_preview_from_snapshot(
        request, original, snapshot
    )
    second = compute_assignment_leave_resolution_preview_from_snapshot(
        request, original, snapshot
    )

    assert first == second
    assert (request, original, snapshot) == before
    assert first["status"] == "ready"
    assert first["historical_fact_state"] == "bootstrap"
    assert first["required_hours"] == first["provisional_actual_hours"] == 8
    assert first["assignment_transition_plan"]["created"][0]["staff_id"] == 202
    assert len(first["preview_fingerprint"]) == 64


def test_leave_resolution_preview_from_snapshot_rejects_invalid_assignment_snapshot_keys():
    work_date, original, snapshot = _leave_resolution_preview_facts()
    snapshot["assignments"][0]["service_hours_per_day"] = 8

    with pytest.raises(ValueError, match="must contain exact fields"):
        compute_assignment_leave_resolution_preview_from_snapshot(
            _leave_resolution_preview_from_snapshot_request(
                work_date, "substitute", 202
            ),
            original,
            snapshot,
        )


@pytest.mark.parametrize(
    ("collection", "row"),
    [
        (
            "leave_substitution_events",
            {
                "id": 92,
                "case_no": "CASE-1",
                "original_assignment_id": 11,
                "original_schedule_id": 22,
                "work_date": date(2026, 8, 1),
                "resolution_type": "substitute",
                "substitute_assignment_id": None,
                "event_key": "ev-dup",
                "occurred_at": "2026-08-01T00:00:00",
            },
        ),
        (
            "actual_hours_adjustments",
            {
                "id": 93,
                "case_no": "CASE-1",
                "assignment_id": 11,
                "original_hours": 8,
                "adjusted_hours": 8,
                "reason": "audit",
                "adjusted_at": "2026-08-01T00:00:00",
            },
        ),
        (
            "non_cancelled_payments",
            {
                "id": 94,
                "case_no": "CASE-1",
                "assignment_id": 11,
                "payment_status": "posted",
            },
        ),
        (
            "active_settlements",
            {
                "id": 95,
                "case_no": "CASE-1",
                "assignment_id": 11,
                "settlement_id": 701,
                "status": "pending",
            },
        ),
    ],
    ids=[
        "leave_substitution_events",
        "actual_hours_adjustments",
        "non_cancelled_payments",
        "active_settlements",
    ],
)
def test_leave_resolution_preview_from_snapshot_rejects_duplicate_historical_ids(collection, row):
    work_date, original, snapshot = _leave_resolution_preview_facts()
    snapshot["historical_facts"][collection] = [
        dict(row),
        dict(row),
    ]

    expected = (
        "historical_facts.leave_substitution_events contains duplicate id"
        if collection == "leave_substitution_events"
        else (
            "historical_facts.actual_hours_adjustments contains duplicate id"
            if collection == "actual_hours_adjustments"
            else (
                "historical_facts.non_cancelled_payments contains duplicate id"
                if collection == "non_cancelled_payments"
                else "historical_facts.active_settlements contains duplicate id"
            )
        )
    )

    with pytest.raises(ValueError, match=expected):
        compute_assignment_leave_resolution_preview_from_snapshot(
            _leave_resolution_preview_from_snapshot_request(
                work_date, "substitute", 202
            ),
            original,
            snapshot,
        )


def test_leave_resolution_preview_from_snapshot_rejects_duplicate_active_lock_ids():
    work_date, original, snapshot = _leave_resolution_preview_facts()
    snapshot["active_lock_days"] = [
        {
            "id": 100,
            "lock_id": 101,
            "plan_id": 201,
            "case_no": "CASE-1",
            "segment_id": 1,
            "staff_id": 101,
            "lock_date": date(2026, 8, 1),
        },
        {
            "id": 100,
            "lock_id": 102,
            "plan_id": 202,
            "case_no": "CASE-1",
            "segment_id": 2,
            "staff_id": 101,
            "lock_date": date(2026, 8, 2),
        },
    ]

    with pytest.raises(
        ValueError, match="conflict_snapshot.active_lock_days contains duplicate id"
    ):
        compute_assignment_leave_resolution_preview_from_snapshot(
            _leave_resolution_preview_from_snapshot_request(
                work_date, "substitute", 202
            ),
            original,
            snapshot,
        )


def test_leave_resolution_preview_from_snapshot_rejects_schedule_lineage_mismatch_on_event():
    work_date, original, snapshot = _leave_resolution_preview_facts()
    snapshot["historical_facts"]["leave_substitution_events"] = [
        {
            "id": 99,
            "case_no": "CASE-1",
            "original_assignment_id": 11,
            "original_schedule_id": 22,
            "work_date": date(2026, 8, 2),
            "resolution_type": "substitute",
            "substitute_assignment_id": 202,
            "event_key": "ev-mismatch",
            "occurred_at": "2026-08-02T00:00:00",
        }
    ]

    with pytest.raises(
        ValueError, match="historical_facts.leave_substitution_events schedule lineage mismatch"
    ):
        compute_assignment_leave_resolution_preview_from_snapshot(
            _leave_resolution_preview_from_snapshot_request(
                work_date, "substitute", 202
            ),
            original,
            snapshot,
        )


def test_leave_resolution_preview_from_snapshot_rejects_assignment_schedule_staff_ownership_mismatch():
    work_date, original, snapshot = _leave_resolution_preview_facts()
    snapshot["assignment_schedule_days"][0]["staff_id"] = 999

    with pytest.raises(
        ValueError,
        match="conflict_snapshot.assignment_schedule_days staff ownership mismatch",
    ):
        compute_assignment_leave_resolution_preview_from_snapshot(
            _leave_resolution_preview_from_snapshot_request(
                work_date, "substitute", 202
            ),
            original,
            snapshot,
        )


def test_leave_resolution_preview_from_snapshot_defer_counts_makeup_once():
    leave_date = date(2026, 8, 1)
    _, original, snapshot = _leave_resolution_preview_facts()
    original["assignment"]["assigned_start_date"] = leave_date
    original["assignment"]["assigned_end_date"] = leave_date
    original["assignment"]["actual_hours"] = 8
    original["schedule_days"][0]["work_date"] = leave_date
    snapshot["database_current_date"] = date(2026, 7, 15)
    snapshot["assignments"][0].update(
        {
            key: original["assignment"][key]
            for key in [
                "id",
                "case_no",
                "staff_id",
                "status",
                "assigned_start_date",
                "assigned_end_date",
                "planned_hours",
                "actual_hours",
            ]
        }
    )
    snapshot["assignment_schedule_days"][0]["work_date"] = leave_date
    following = {
        **snapshot["assignments"][0],
        "id": 12,
        "staff_id": 102,
        "assigned_start_date": date(2026, 8, 2),
        "assigned_end_date": date(2026, 8, 2),
    }
    snapshot["assignments"].append(following)

    result = compute_assignment_leave_resolution_preview_from_snapshot(
        _leave_resolution_preview_from_snapshot_request(
            leave_date, "defer_following_assignments"
        ),
        original,
        snapshot,
    )

    assert result["status"] == "ready"
    assert result["required_hours"] == result["provisional_actual_hours"] == 16
    after = result["assignment_transition_plan"]["after_assignments"]
    assert next(row for row in after if row["id"] == 12)["assigned_start_date"] == date(
        2026, 8, 3
    )


def test_leave_resolution_preview_from_snapshot_historical_locked_is_blocked():
    work_date, original, snapshot = _leave_resolution_preview_facts(
        historical=True, locked=True
    )

    result = compute_assignment_leave_resolution_preview_from_snapshot(
        _leave_resolution_preview_from_snapshot_request(
            work_date, "substitute", 202
        ),
        original,
        snapshot,
    )

    assert result["status"] == "blocked"
    assert result["historical_fact_state"] == "locked"
    assert result["blocking_reasons"] == ["historical_facts_locked"]


def test_leave_resolution_preview_from_snapshot_settlements_also_lock_historical_snapshot():
    work_date, original, snapshot = _leave_resolution_preview_facts(historical=True)

    snapshot["historical_facts"]["non_cancelled_payments"] = []
    snapshot["historical_facts"]["active_settlements"] = [
        {
            "id": 91,
            "case_no": "CASE-1",
            "assignment_id": 11,
            "settlement_id": 701,
            "status": "pending",
        }
    ]

    result = compute_assignment_leave_resolution_preview_from_snapshot(
        _leave_resolution_preview_from_snapshot_request(
            work_date, "substitute", 202
        ),
        original,
        snapshot,
    )

    assert result["status"] == "blocked"
    assert result["historical_fact_state"] == "locked"
    assert result["blocking_reasons"] == ["historical_facts_locked"]


def test_leave_resolution_preview_from_snapshot_rejects_conflict_details_with_non_string_keys(
    monkeypatch,
):
    work_date, original, snapshot = _leave_resolution_preview_facts()

    def validate(*_args, **_kwargs):
        conflict = leave_preview.AssignmentPlanTransitionConflict(
            "assignment_row_limit_exceeded",
        )
        conflict.details = {1: "invalid-key-type"}
        raise conflict

    monkeypatch.setattr(leave_preview, "validate_assignment_plan_transition", validate)

    with pytest.raises(ValueError, match="history details must use string keys"):
        compute_assignment_leave_resolution_preview_from_snapshot(
            _leave_resolution_preview_from_snapshot_request(
                work_date, "substitute", 202
            ),
            original,
            snapshot,
        )


def test_leave_resolution_preview_from_snapshot_rejects_conflict_details_with_cycles(
    monkeypatch,
):
    work_date, original, snapshot = _leave_resolution_preview_facts()

    def validate(*_args, **_kwargs):
        conflict = leave_preview.AssignmentPlanTransitionConflict(
            "assignment_row_limit_exceeded",
        )
        recursive = {}
        recursive["details"] = recursive
        conflict.details = recursive
        raise conflict

    monkeypatch.setattr(leave_preview, "validate_assignment_plan_transition", validate)

    with pytest.raises(ValueError, match="must be JSON-safe"):
        compute_assignment_leave_resolution_preview_from_snapshot(
            _leave_resolution_preview_from_snapshot_request(
                work_date, "substitute", 202
            ),
            original,
            snapshot,
        )


@pytest.mark.parametrize("invalid_details", [None, [], "not-a-mapping"])
def test_leave_resolution_preview_from_snapshot_rejects_non_mapping_conflict_details(
    monkeypatch,
    invalid_details,
):
    work_date, original, snapshot = _leave_resolution_preview_facts()

    def validate(*_args, **_kwargs):
        conflict = leave_preview.AssignmentPlanTransitionConflict(
            "assignment_row_limit_exceeded",
        )
        conflict.details = invalid_details
        raise conflict

    monkeypatch.setattr(leave_preview, "validate_assignment_plan_transition", validate)

    with pytest.raises(
        ValueError,
        match="AssignmentPlanTransitionConflict details must be a mapping",
    ):
        compute_assignment_leave_resolution_preview_from_snapshot(
            _leave_resolution_preview_from_snapshot_request(
                work_date, "substitute", 202
            ),
            original,
            snapshot,
        )


def test_leave_resolution_preview_from_snapshot_requires_exact_owned_schedule():
    work_date, original, snapshot = _leave_resolution_preview_facts(
        historical=True
    )
    original["schedule_days"] = []

    with pytest.raises(
        ValueError,
        match="original_schedule_id does not belong to original_assignment_id",
    ):
        compute_assignment_leave_resolution_preview_from_snapshot(
            _leave_resolution_preview_from_snapshot_request(
                work_date, "substitute", 202
            ),
            original,
            snapshot,
        )


class _ApplyPreflightCursor:
    def __init__(self, existing_event=None):
        self.calls = []
        self._result = None
        self.existing_event = existing_event

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        normalised = " ".join(sql.split()).lower()
        if "from orders" in normalised:
            self._result = {
                "case_no": "CASE-1",
                "service_hours_per_day": 8,
            }
        elif "from case_staff_assignments" in normalised:
            self._result = [
                {
                    "id": 11,
                    "staff_id": 101,
                    "assigned_start_date": date(2026, 8, 1),
                    "assigned_end_date": date(2026, 8, 1),
                }
            ]
        elif "where event_key" in normalised:
            self._result = self.existing_event
        else:
            raise AssertionError(f"unexpected SQL: {normalised}")

    def fetchone(self):
        if isinstance(self._result, list):
            return self._result[0] if self._result else None
        return self._result

    def fetchall(self):
        if isinstance(self._result, list):
            return self._result
        return [] if self._result is None else [self._result]


def _leave_resolution_apply_request(fingerprint="a" * 64):
    return {
        "case_no": " CASE-1 ",
        "original_assignment_id": 11,
        "original_schedule_id": 22,
        "work_date": "2026-08-01",
        "resolution_type": "substitute",
        "substitute_staff_id": 202,
        "preview_fingerprint": fingerprint,
        "event_key": " leave-11-22 ",
        "actor": " admin ",
        "reason": " single-day leave ",
    }


def _install_apply_preflight_dependencies(
    monkeypatch, snapshot, preview, *, locked_staff_ids=(101, 202)
):
    observed = {}

    def lock_mutex(cursor, staff_ids):
        observed["mutex"] = (cursor, staff_ids)
        return list(locked_staff_ids)

    def locked_snapshot(cursor, case_no, staff_ids, start, end, lock_rows):
        observed["snapshot"] = (
            cursor,
            case_no,
            staff_ids,
            start,
            end,
            lock_rows,
        )
        return snapshot

    monkeypatch.setattr(
        "services.staff_occupancy_mutex_service.lock_staff_occupancy_mutex",
        lock_mutex,
    )
    monkeypatch.setattr(
        "services.multi_caregiver_schedule_read.get_case_schedule_conflict_snapshot_with_cursor",
        locked_snapshot,
    )
    monkeypatch.setattr(
        "services.assignment_schedule_leave_resolution_preview.compute_assignment_leave_resolution_preview_from_snapshot",
        lambda request, original, case_snapshot: preview,
    )
    return observed


def test_leave_resolution_apply_preflight_locks_and_returns_canonical_command(
    monkeypatch,
):
    _, original, snapshot = _leave_resolution_preview_facts()
    preview = compute_assignment_leave_resolution_preview_from_snapshot(
        _leave_resolution_preview_from_snapshot_request(
            date(2026, 8, 1), "substitute", 202
        ),
        original,
        snapshot,
    )
    request = _leave_resolution_apply_request(preview["preview_fingerprint"])
    cursor = _ApplyPreflightCursor()
    observed = _install_apply_preflight_dependencies(monkeypatch, snapshot, preview)

    result = service.prepare_assignment_leave_resolution_apply(cursor, request)

    assert result["status"] == "apply"
    assert result["mutation_command"]["request_identity"]["event_key"] == "leave-11-22"
    assert observed["mutex"] == (cursor, [101, 202])
    assert observed["snapshot"] == (
        cursor,
        "CASE-1",
        [101, 202],
        "2026-08-01",
        "2026-08-02",
        True,
    )
    sql = [" ".join(statement.split()).lower() for statement, _ in cursor.calls]
    assert "from orders" in sql[0]
    assert "from case_staff_assignments" in sql[1]
    assert "where event_key" in sql[-1]
    assert all(
        statement.split()[0] not in {"insert", "update", "delete", "replace"}
        for statement in sql
    )
    assert not hasattr(cursor, "commit")
    assert not hasattr(cursor, "rollback")
    assert not hasattr(cursor, "close")


def test_leave_resolution_apply_preflight_allows_confirmed_historical_unlocked(
    monkeypatch,
):
    _, _, snapshot = _leave_resolution_preview_facts(historical=True)
    preview = {
        "status": "requires_review",
        "historical_fact_state": "unlocked",
        "requires_confirmation": True,
        "requires_audit": True,
        "preview_fingerprint": "b" * 64,
        "assignment_transition_plan": {"after_assignments": []},
        "assignment_service_impacts": [],
    }
    cursor = _ApplyPreflightCursor()
    _install_apply_preflight_dependencies(monkeypatch, snapshot, preview)

    result = service.prepare_assignment_leave_resolution_apply(
        cursor, _leave_resolution_apply_request("b" * 64)
    )

    assert result["status"] == "apply"
    assert result["mutation_command"]["requires_audit"] is True


@pytest.mark.parametrize(
    ("preview", "fingerprint", "reason"),
    [
        (
            {
                "status": "blocked",
                "historical_fact_state": "locked",
                "requires_confirmation": False,
                "preview_fingerprint": "c" * 64,
            },
            "c" * 64,
            "preview_not_ready",
        ),
        (
            {
                "status": "ready",
                "historical_fact_state": "bootstrap",
                "requires_confirmation": True,
                "preview_fingerprint": "d" * 64,
            },
            "e" * 64,
            "preview_fingerprint_mismatch",
        ),
    ],
)
def test_leave_resolution_apply_preflight_rejects_locked_or_stale(
    monkeypatch, preview, fingerprint, reason
):
    _, _, snapshot = _leave_resolution_preview_facts()
    cursor = _ApplyPreflightCursor()
    _install_apply_preflight_dependencies(monkeypatch, snapshot, preview)

    result = service.prepare_assignment_leave_resolution_apply(
        cursor, _leave_resolution_apply_request(fingerprint)
    )

    assert result["status"] == "rejected"
    assert result["reason"] == reason
    assert result["mutation_command"] is None


def test_leave_resolution_apply_preflight_event_key_identity_is_strict(monkeypatch):
    _, _, snapshot = _leave_resolution_preview_facts()
    preview = {
        "status": "ready",
        "historical_fact_state": "bootstrap",
        "requires_confirmation": True,
        "preview_fingerprint": "a" * 64,
    }
    canonical_identity = {
        **{
            key: value.strip() if isinstance(value, str) else value
            for key, value in _leave_resolution_apply_request().items()
        },
    }
    existing = {
        "id": 99,
        "schedule_snapshot": {"request_identity": canonical_identity},
    }
    cursor = _ApplyPreflightCursor(existing)
    _install_apply_preflight_dependencies(monkeypatch, snapshot, preview)

    replay = service.prepare_assignment_leave_resolution_apply(
        cursor, _leave_resolution_apply_request()
    )
    assert replay["status"] == "idempotent_replay"
    assert replay["mutation_command"] is None

    existing["schedule_snapshot"]["request_identity"]["reason"] = "different"
    with pytest.raises(ValueError, match="different request identity"):
        service.prepare_assignment_leave_resolution_apply(
            _ApplyPreflightCursor(existing), _leave_resolution_apply_request()
        )


class _ApplyMutationCursor:
    def __init__(self):
        self.calls = []
        self._result = None
        self.lastrowid = 100
        self.rowcount = 0
        self.assignments = {
            11: {
                "id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "status": "active",
                "actual_hours": Decimal("8"),
                "assigned_start_date": date(2026, 8, 1),
                "assigned_end_date": date(2026, 8, 1),
            }
        }
        self.schedule_rows = {
            (11, date(2026, 8, 1)): {
                "id": 22,
                "assignment_id": 11,
                "case_no": "CASE-1",
                "staff_id": 101,
                "work_date": date(2026, 8, 1),
                "is_work_day": True,
                "is_double_pay": False,
            }
        }

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self._result = None
        self.rowcount = 0
        normalised = " ".join(sql.split()).lower()
        if normalised.startswith("select id, staff_id, hourly_rate"):
            self._result = {
                "id": 11,
                "staff_id": 101,
                "hourly_rate": Decimal("100"),
                "floor_fee_allocated": Decimal("10"),
            }
            self.rowcount = 1
        elif normalised.startswith("insert into case_staff_assignments"):
            self.lastrowid += 1
            assignment_id = self.lastrowid
            self.assignments[assignment_id] = {
                "id": assignment_id,
                "case_no": params[0],
                "staff_id": params[1],
                "assignment_sequence": params[2],
                "status": "active",
                "actual_hours": params[6],
                "assigned_start_date": params[3],
                "assigned_end_date": params[4],
            }
            self._result = None
            self.rowcount = 1
        elif normalised.startswith("update case_staff_assignments set assignment_sequence"):
            target_id = params[5]
            if target_id not in self.assignments:
                self.assignments[target_id] = {
                    "id": target_id,
                    "case_no": params[6],
                    "staff_id": 101,
                    "assignment_sequence": params[0],
                    "status": "active",
                    "actual_hours": Decimal("0.00"),
                }
            self.assignments[target_id].update(
                {
                    "assignment_sequence": params[0],
                    "assigned_start_date": params[1],
                    "assigned_end_date": params[2],
                    "actual_hours": params[3],
                    "status": params[4],
                }
            )
            self.rowcount = 1
            self._result = None
        elif normalised.startswith("update case_staff_assignments set actual_hours = 0.00"):
            target_id = params[0]
            if target_id not in self.assignments:
                self.assignments[target_id] = {
                    "id": target_id,
                    "case_no": params[1],
                    "staff_id": 101,
                    "status": "active",
                    "actual_hours": Decimal("0.00"),
                }
            self.assignments[target_id].update(
                {"actual_hours": Decimal("0.00"), "status": "cancelled"}
            )
            self.rowcount = 1
            self._result = None
        elif normalised.startswith("update staff_schedule set is_work_day = false, is_double_pay = false"):
            if "where id = %s and assignment_id = %s and case_no = %s and work_date = %s" in normalised:
                schedule_id = params[0]
                assignment_id = params[1]
                case_no = params[2]
                work_date = params[3]
                for key, row in self.schedule_rows.items():
                    if row["id"] == schedule_id and row["assignment_id"] == assignment_id and row["case_no"] == case_no and row["work_date"] == work_date:
                        row["is_work_day"] = False
                        row["is_double_pay"] = False
                        self.rowcount = 1
                self._result = None
            elif "where assignment_id = %s and case_no = %s and work_date between %s and %s" in normalised:
                assignment_id = params[0]
                case_no = params[1]
                date_start = params[2]
                date_end = params[3]
                for row in self.schedule_rows.values():
                    if (
                        row["assignment_id"] == assignment_id
                        and row["case_no"] == case_no
                        and date_start <= row["work_date"] <= date_end
                    ):
                        row["is_work_day"] = False
                        row["is_double_pay"] = False
                        self.rowcount = 1
                self._result = None
        elif normalised.startswith("insert into staff_schedule"):
            assignment_id, case_no, staff_id, work_date = params[0], params[1], params[2], params[3]
            schedule_key = (assignment_id, work_date)
            existing = self.schedule_rows.get(schedule_key)
            if existing is None:
                self.schedule_rows[schedule_key] = {
                    "id": self.lastrowid + 1,
                    "assignment_id": assignment_id,
                    "case_no": case_no,
                    "staff_id": staff_id,
                    "work_date": work_date,
                    "is_work_day": True,
                    "is_double_pay": False,
                }
            else:
                existing["staff_id"] = staff_id
                existing["is_work_day"] = True
                existing["is_double_pay"] = False
                existing["assignment_id"] = assignment_id
            self.rowcount = 1
            self._result = None
        elif normalised.startswith("select id, is_work_day, is_double_pay") and "from staff_schedule" in normalised:
            if len(params) == 4:
                schedule_id = params[0]
                assignment_id = params[1]
                case_no = params[2]
                work_date = params[3]
                rows = [
                    row
                    for row in self.schedule_rows.values()
                    if row["id"] == schedule_id
                    and row["assignment_id"] == assignment_id
                    and row["case_no"] == case_no
                    and row["work_date"] == work_date
                ]
                self._result = rows[0] if rows else None
                self.rowcount = 1 if rows else 0
            elif "where id = %s and case_no = %s" in normalised and "and work_date between" not in normalised:
                schedule_id = params[0]
                case_no = params[1]
                rows = [
                    row
                    for row in self.schedule_rows.values()
                    if row["id"] == schedule_id and row["case_no"] == case_no
                ]
                self._result = rows[0] if rows else None
                self.rowcount = 1 if rows else 0
            else:
                self._result = None
        elif normalised.startswith("select id, staff_id, assignment_sequence") and "from case_staff_assignments" in normalised:
            case_no = params[0]
            rows = [
                dict(row)
                for row in self.assignments.values()
                if row["case_no"] == case_no and row["status"] != "cancelled"
            ]
            self._result = rows
            self.rowcount = len(rows)
        elif normalised.startswith("select assignment_id, staff_id, work_date, is_work_day, is_double_pay") and "from staff_schedule" in normalised:
            case_no = params[0]
            date_start = params[1]
            date_end = params[2]
            rows = [
                dict(row)
                for row in self.schedule_rows.values()
                if row["case_no"] == case_no
                and date_start <= row["work_date"] <= date_end
            ]
            self._result = rows
            self.rowcount = len(rows)
        else:
            self._result = None
        return None

    def fetchone(self):
        return self._result

    def fetchall(self):
        if self._result is None:
            return []
        if isinstance(self._result, list):
            return self._result
        return [self._result]


def _leave_resolution_apply_mutation_command(resolution):
    work_date, original, snapshot = _leave_resolution_preview_facts()
    preview = compute_assignment_leave_resolution_preview_from_snapshot(
        _leave_resolution_preview_from_snapshot_request(
            work_date, resolution, 202 if resolution == "substitute" else None
        ),
        original,
        snapshot,
    )
    return {
        "request_identity": {
            **_leave_resolution_preview_from_snapshot_request(
                work_date, resolution, 202 if resolution == "substitute" else None
            ),
            "preview_fingerprint": preview["preview_fingerprint"],
            "event_key": f"event-{resolution}",
            "actor": "admin",
            "reason": "leave",
        },
        "assignment_transition_plan": preview["assignment_transition_plan"],
        "assignment_service_impacts": preview["assignment_service_impacts"],
        "requires_audit": preview["requires_audit"],
    }


@pytest.mark.parametrize("resolution", ["defer_following_assignments", "substitute"])
def test_leave_resolution_apply_mutation_writes_canonical_rows(
    monkeypatch, resolution
):
    cursor = _ApplyMutationCursor()
    observed = {}

    def reconcile(owned_cursor, case_no, pending_substitution_event=None):
        observed["args"] = (owned_cursor, case_no, pending_substitution_event)
        return {
            "errors": [],
            "can_create_staff_payments": True,
            "target_hours": sum(
                Decimal(str(row["actual_hours"]))
                for row in cursor.assignments.values()
                if row["status"] != "cancelled"
            ),
            "assignments": [],
            "floor_fee_total": Decimal("10.00"),
        }

    monkeypatch.setattr(
        "services.assignment_payroll_reconciliation_service.reconcile_assignment_payroll_with_cursor",
        reconcile,
    )

    result = service.execute_assignment_leave_resolution_mutations(
        cursor, _leave_resolution_apply_mutation_command(resolution)
    )

    assert result["pending_event_payload"]["resolution_type"] == resolution
    assert result["schedules"]["original_schedule"]["is_work_day"] is False
    assert result["schedules"]["original_schedule"]["is_double_pay"] is False
    sql = [" ".join(statement.split()).lower() for statement, _ in cursor.calls]
    assert any(statement.startswith("update staff_schedule") for statement in sql)
    assert all("assignment_schedule_leave_substitution_events" not in statement for statement in sql)
    assert all("staff_payments" not in statement for statement in sql)
    assert not hasattr(cursor, "commit")
    assert not hasattr(cursor, "rollback")
    if resolution == "substitute":
        pending = observed["args"][2]
        assert pending["original_assignment_id"] == 11
        assert pending["substitute_assignment_id"] == 101
        assert pending["prefix_assignment_id"] is None
        assert pending["suffix_assignment_id"] is None
        assert result["pending_event_payload"]["substitute_assignment_id"] == 101
    else:
        assert observed["args"][2] is None


def test_leave_resolution_apply_mutation_accepts_str_ownership_with_int_row_ids(
    monkeypatch,
):
    cursor = _ApplyMutationCursor()

    def reconcile(owned_cursor, case_no, pending_substitution_event=None):
        return {
            "errors": [],
            "can_create_staff_payments": True,
            "target_hours": sum(
                Decimal(str(row["actual_hours"]))
                for row in cursor.assignments.values()
                if row["status"] != "cancelled"
            ),
            "assignments": [],
            "floor_fee_total": Decimal("10.00"),
        }

    monkeypatch.setattr(
        "services.assignment_payroll_reconciliation_service.reconcile_assignment_payroll_with_cursor",
        reconcile,
    )

    command = _leave_resolution_apply_mutation_command("defer_following_assignments")
    for row in command["assignment_transition_plan"]["after_assignments"]:
        if isinstance(row.get("id"), str) and row["id"].isdigit():
            row["id"] = int(row["id"])
    for impact in command["assignment_service_impacts"]:
        if (
            isinstance(impact.get("assignment_id"), str)
            and impact["assignment_id"].isdigit()
        ):
            impact["assignment_id"] = int(impact["assignment_id"])
    command["assignment_transition_plan"]["ownership_by_date"] = {
        key: str(value)
        for key, value in command["assignment_transition_plan"]["ownership_by_date"].items()
    }

    result = service.execute_assignment_leave_resolution_mutations(
        cursor,
        command,
    )

    assert result["pending_event_payload"]["resolution_type"] == "defer_following_assignments"
    assert result["schedules"]["original_schedule"]["is_work_day"] is False


def test_leave_resolution_apply_mutation_rejects_bool_and_nonnumeric_identifiers(monkeypatch):
    cursor = _ApplyMutationCursor()

    def reconcile(owned_cursor, case_no, pending_substitution_event=None):
        return {
            "errors": [],
            "can_create_staff_payments": True,
            "target_hours": sum(
                Decimal(str(row["actual_hours"]))
                for row in cursor.assignments.values()
                if row["status"] != "cancelled"
            ),
            "assignments": [],
            "floor_fee_total": Decimal("10.00"),
        }

    monkeypatch.setattr(
        "services.assignment_payroll_reconciliation_service.reconcile_assignment_payroll_with_cursor",
        reconcile,
    )

    command = _leave_resolution_apply_mutation_command("defer_following_assignments")
    command["assignment_transition_plan"]["after_assignments"][0]["id"] = True
    with pytest.raises(ValueError, match="canonical assignment id is invalid"):
        service.execute_assignment_leave_resolution_mutations(cursor, command)

    command = _leave_resolution_apply_mutation_command("substitute")
    command["assignment_transition_plan"]["ownership_by_date"]["2026-08-01"] = "not-a-number"
    with pytest.raises(ValueError, match="unknown"):
        service.execute_assignment_leave_resolution_mutations(
            _ApplyMutationCursor(),
            command,
        )


def test_leave_resolution_apply_mutation_cleanup_and_staff_schedule_upsert(monkeypatch):
    cursor = _ApplyMutationCursor()

    def reconcile(owned_cursor, case_no, pending_substitution_event=None):
        return {
            "errors": [],
            "can_create_staff_payments": True,
            "target_hours": sum(
                Decimal(str(row["actual_hours"]))
                for row in cursor.assignments.values()
                if row["status"] != "cancelled"
            ),
            "assignments": [],
            "floor_fee_total": Decimal("10.00"),
        }

    monkeypatch.setattr(
        "services.assignment_payroll_reconciliation_service.reconcile_assignment_payroll_with_cursor",
        reconcile,
    )

    result = service.execute_assignment_leave_resolution_mutations(
        cursor, _leave_resolution_apply_mutation_command("defer_following_assignments")
    )

    sql = [" ".join(statement.split()).lower() for statement, _ in cursor.calls]
    assert any(
        "update staff_schedule set is_work_day = false, is_double_pay = false" in statement
        and "between" in statement
        and "assignment_id" in statement
        for statement in sql
    )
    assert any(
        "insert into staff_schedule" in statement
        and "staff_id = values(staff_id)" in statement
        and "is_double_pay = false" in statement
        for statement in sql
    )
    assert result["schedules"]["original_schedule"]["is_work_day"] is False


def test_leave_resolution_apply_mutation_rejects_extra_case_assignment(monkeypatch):
    cursor = _ApplyMutationCursor()
    cursor.assignments[999] = {
        "id": 999,
        "case_no": "CASE-1",
        "staff_id": 999,
        "assignment_sequence": 4,
        "assigned_start_date": date(2026, 8, 9),
        "assigned_end_date": date(2026, 8, 9),
        "status": "active",
        "actual_hours": Decimal("8"),
    }
    monkeypatch.setattr(
        "services.assignment_payroll_reconciliation_service.reconcile_assignment_payroll_with_cursor",
        lambda *args, **kwargs: pytest.fail("reconciliation must not run"),
    )

    with pytest.raises(ValueError, match="id set changed"):
        service.execute_assignment_leave_resolution_mutations(
            cursor,
            _leave_resolution_apply_mutation_command("defer_following_assignments"),
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("staff_id", 999),
        ("status", "review"),
        ("assignment_sequence", 9),
        ("assigned_start_date", date(2026, 7, 31)),
        ("assigned_end_date", date(2026, 8, 9)),
        ("actual_hours", Decimal("7.5")),
    ],
)
def test_leave_resolution_apply_mutation_rejects_assignment_readback_mismatch(
    monkeypatch, field, replacement
):
    class MismatchCursor(_ApplyMutationCursor):
        def execute(self, sql, params=()):
            result = super().execute(sql, params)
            normalised = " ".join(sql.split()).lower()
            if (
                normalised.startswith("select id, staff_id, assignment_sequence")
                and self._result
            ):
                self._result[0][field] = replacement
            return result

    monkeypatch.setattr(
        "services.assignment_payroll_reconciliation_service.reconcile_assignment_payroll_with_cursor",
        lambda *args, **kwargs: pytest.fail("reconciliation must not run"),
    )

    with pytest.raises(ValueError, match="does not match final plan"):
        service.execute_assignment_leave_resolution_mutations(
            MismatchCursor(),
            _leave_resolution_apply_mutation_command("defer_following_assignments"),
        )


def test_leave_resolution_apply_mutation_rejects_reconciliation_target_mismatch(
    monkeypatch,
):
    monkeypatch.setattr(
        "services.assignment_payroll_reconciliation_service.reconcile_assignment_payroll_with_cursor",
        lambda *args, **kwargs: {
            "errors": [],
            "can_create_staff_payments": True,
            "target_hours": Decimal("999"),
        },
    )

    with pytest.raises(ValueError, match="target hours mismatch"):
        service.execute_assignment_leave_resolution_mutations(
            _ApplyMutationCursor(),
            _leave_resolution_apply_mutation_command("defer_following_assignments"),
        )


def test_leave_resolution_apply_mutation_snapshots_verified_db_readback(monkeypatch):
    cursor = _ApplyMutationCursor()

    def reconcile(owned_cursor, case_no, pending_substitution_event=None):
        return {
            "errors": [],
            "can_create_staff_payments": True,
            "target_hours": sum(
                Decimal(str(row["actual_hours"]))
                for row in cursor.assignments.values()
                if row["status"] != "cancelled"
            ),
        }

    monkeypatch.setattr(
        "services.assignment_payroll_reconciliation_service.reconcile_assignment_payroll_with_cursor",
        reconcile,
    )
    result = service.execute_assignment_leave_resolution_mutations(
        cursor,
        _leave_resolution_apply_mutation_command("substitute"),
    )

    expected = [
        {
            "id": row["id"],
            "case_no": row["case_no"],
            "staff_id": row["staff_id"],
            "assignment_sequence": row["assignment_sequence"],
            "assigned_start_date": row["assigned_start_date"],
            "assigned_end_date": row["assigned_end_date"],
            "status": row["status"],
            "actual_hours": str(Decimal(str(row["actual_hours"]))),
        }
        for row in cursor.assignments.values()
        if row["status"] != "cancelled"
    ]
    assert result["assignments"] == expected
    assert result["schedules"]["assignments"] == expected
    assert result["actual_hours"] == {
        row["id"]: row["actual_hours"] for row in expected
    }


def test_leave_resolution_apply_mutation_rejects_fifth_row_and_bad_ownership():
    command = _leave_resolution_apply_mutation_command("substitute")
    row = dict(command["assignment_transition_plan"]["after_assignments"][-1])
    for index in range(4):
        extra = dict(row)
        extra["id"] = f"extra-{index}"
        extra["assigned_start_date"] = date(2026, 8, 2 + index)
        extra["assigned_end_date"] = date(2026, 8, 2 + index)
        command["assignment_transition_plan"]["after_assignments"].append(extra)
        command["assignment_transition_plan"]["ownership_by_date"][
            extra["assigned_start_date"].isoformat()
        ] = extra["id"]
        command["assignment_service_impacts"].append(
            {
                "assignment_id": extra["id"],
                "staff_id": extra["staff_id"],
                "service_days": 1,
                "actual_hours": "8",
            }
        )
    with pytest.raises(ValueError, match="one to four"):
        service.execute_assignment_leave_resolution_mutations(
            _ApplyMutationCursor(), command
        )

    command = _leave_resolution_apply_mutation_command("substitute")
    command["assignment_transition_plan"]["ownership_by_date"]["2026-08-01"] = 11
    with pytest.raises(ValueError, match="ownership"):
        service.execute_assignment_leave_resolution_mutations(
            _ApplyMutationCursor(), command
        )


class _ApplyOrchestrationCursor:
    def __init__(
        self,
        *,
        lastrowid: int = 1,
        raise_on_execute: Exception | None = None,
        rowcount: int = 1,
    ):
        self.execute_calls = []
        self.lastrowid = lastrowid
        self.close_calls = 0
        self.raise_on_execute = raise_on_execute
        self.rowcount = rowcount

    def execute(self, sql, params=()):
        self.execute_calls.append((sql, tuple(params)))
        if self.raise_on_execute is not None:
            raise self.raise_on_execute

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        self.close_calls += 1


class _ApplyOrchestrationConnection:
    def __init__(self, cursor, *, raise_on_cursor: Exception | None = None):
        self.cursor_obj = cursor
        self.raise_on_cursor = raise_on_cursor
        self.cursor_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self):
        self.cursor_calls += 1
        if self.raise_on_cursor is not None:
            raise self.raise_on_cursor
        return self.cursor_obj

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1

    def close(self):
        self.close_calls += 1


def test_leave_resolution_apply_orchestration_applied_happy_path_inserts_event_once(
    monkeypatch,
):
    cursor = _ApplyOrchestrationCursor(lastrowid=123)
    connection = _ApplyOrchestrationConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    event_payload = {
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "original_schedule_id": 22,
        "work_date": "2026-08-01",
        "resolution_type": "substitute",
        "substitute_assignment_id": 33,
        "event_key": "leave-11-22",
        "actor": "admin",
        "reason": "single-day leave",
        "schedule_snapshot": {
            "request_identity": {"case_no": "CASE-1"},
            "assignments": [{"id": 11}],
        },
        "payroll_snapshot": {"target_hours": "8.00"},
    }
    mutation_calls = []

    monkeypatch.setattr(
        service,
        "prepare_assignment_leave_resolution_apply",
        lambda cursor_arg, request_arg: {
            "status": "apply",
            "mutation_command": {"canonical": True},
        },
    )
    monkeypatch.setattr(
        service,
        "execute_assignment_leave_resolution_mutations",
        lambda cursor_arg, mutation_command: mutation_calls.append((cursor_arg, mutation_command))
        or {
            "assignments": [{"id": 11, "staff_id": 101}],
            "schedules": {"assignments": [{"id": 11}]},
            "actual_hours": {"11": "8.00"},
            "pending_event_payload": event_payload,
            "payroll_snapshot": event_payload["payroll_snapshot"],
        },
    )

    result = apply_assignment_leave_resolution(_leave_resolution_apply_request())

    assert mutation_calls == [(cursor, {"canonical": True})]
    assert cursor.execute_calls, "expected event insert"
    assert "INSERT INTO assignment_schedule_leave_substitution_events" in " ".join(
        cursor.execute_calls[0][0].split()
    )
    assert cursor.execute_calls[0][0].lower().count("insert into assignment_schedule_leave_substitution_events") == 1
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0
    assert connection.cursor_calls == 1
    assert cursor.close_calls == 1
    assert connection.close_calls == 1
    assert cursor.execute_calls[0][1][6] == "leave-11-22"
    assert cursor.execute_calls[0][1][7] == "admin"
    assert cursor.execute_calls[0][1][8] == "single-day leave"
    assert result["status"] == "applied"
    assert result["result"] == "applied"
    assert result["event_id"] == 123
    assert result["event_payload"] == event_payload
    assert result["schedule_snapshot"] == event_payload["schedule_snapshot"]
    assert result["payroll_snapshot"] == event_payload["payroll_snapshot"]


def test_leave_resolution_apply_orchestration_rejected_rolls_back_without_event(
    monkeypatch,
):
    cursor = _ApplyOrchestrationCursor()
    connection = _ApplyOrchestrationConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    monkeypatch.setattr(
        service,
        "prepare_assignment_leave_resolution_apply",
        lambda cursor_arg, request_arg: {
            "status": "rejected",
            "reason": "preview_not_ready",
        },
    )
    mutation_called = False
    monkeypatch.setattr(
        service,
        "execute_assignment_leave_resolution_mutations",
        lambda *args: pytest.fail("mutation should not run on rejected preflight"),
    )
    result = apply_assignment_leave_resolution(_leave_resolution_apply_request())

    assert result["status"] == "rejected"
    assert result["reason"] == "preview_not_ready"
    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert cursor.execute_calls == []
    assert cursor.close_calls == 1
    assert connection.close_calls == 1


def test_leave_resolution_apply_orchestration_idempotent_replay_rolls_back_without_mutation(
    monkeypatch,
):
    cursor = _ApplyOrchestrationCursor()
    connection = _ApplyOrchestrationConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    replay = {"status": "idempotent_replay", "existing_event_identity": {"id": 7}}
    monkeypatch.setattr(
        service,
        "prepare_assignment_leave_resolution_apply",
        lambda cursor_arg, request_arg: replay,
    )

    result = apply_assignment_leave_resolution(_leave_resolution_apply_request())

    assert result == replay
    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert cursor.execute_calls == []
    assert cursor.close_calls == 1
    assert connection.close_calls == 1


def test_leave_resolution_apply_orchestration_fails_rollback_without_partial_commit(
    monkeypatch,
):
    cursor = _ApplyOrchestrationCursor()
    connection = _ApplyOrchestrationConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "prepare_assignment_leave_resolution_apply",
        lambda cursor_arg, request_arg: {"status": "apply", "mutation_command": {}},
    )
    monkeypatch.setattr(
        service,
        "execute_assignment_leave_resolution_mutations",
        lambda *args: (_ for _ in ()).throw(RuntimeError("mutation failed")),
    )
    with pytest.raises(RuntimeError, match="mutation failed"):
        apply_assignment_leave_resolution(_leave_resolution_apply_request())

    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert cursor.close_calls == 1
    assert connection.close_calls == 1


def test_leave_resolution_apply_orchestration_fails_at_event_insert_and_rolls_back(
    monkeypatch,
):
    cursor = _ApplyOrchestrationCursor(raise_on_execute=RuntimeError("event insert failed"))
    connection = _ApplyOrchestrationConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    monkeypatch.setattr(
        service,
        "prepare_assignment_leave_resolution_apply",
        lambda cursor_arg, request_arg: {"status": "apply", "mutation_command": {}},
    )
    mutation_payload = {
        "case_no": "CASE-1",
        "original_assignment_id": 11,
        "original_schedule_id": 22,
        "work_date": "2026-08-01",
        "resolution_type": "defer_following_assignments",
        "substitute_assignment_id": None,
        "event_key": "leave-11-22",
        "actor": "admin",
        "reason": "single-day leave",
        "schedule_snapshot": {"request_identity": {"case_no": "CASE-1"}},
        "payroll_snapshot": {"target_hours": "8.00"},
    }
    monkeypatch.setattr(
        service,
        "execute_assignment_leave_resolution_mutations",
        lambda *args: {"pending_event_payload": mutation_payload},
    )

    with pytest.raises(RuntimeError, match="event insert failed"):
        apply_assignment_leave_resolution(_leave_resolution_apply_request())

    assert cursor.execute_calls
    assert cursor.execute_calls[-1][0].lower().count(
        "insert into assignment_schedule_leave_substitution_events"
    ) == 1
    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert cursor.close_calls == 1
    assert connection.close_calls == 1


def test_leave_resolution_apply_orchestration_fails_when_preflight_throws_and_rolls_back(
    monkeypatch,
):
    cursor = _ApplyOrchestrationCursor()
    connection = _ApplyOrchestrationConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "prepare_assignment_leave_resolution_apply",
        lambda *args: (_ for _ in ()).throw(RuntimeError("preflight failed")),
    )
    with pytest.raises(RuntimeError, match="preflight failed"):
        apply_assignment_leave_resolution(_leave_resolution_apply_request())

    assert connection.rollback_calls == 1
    assert cursor.execute_calls == []
    assert cursor.close_calls == 1
    assert connection.close_calls == 1


def test_leave_resolution_apply_orchestration_raises_on_unsupported_preflight_status(
    monkeypatch,
):
    cursor = _ApplyOrchestrationCursor()
    connection = _ApplyOrchestrationConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        service,
        "prepare_assignment_leave_resolution_apply",
        lambda cursor_arg, request_arg: {"status": "need_retry"},
    )
    monkeypatch.setattr(
        service,
        "execute_assignment_leave_resolution_mutations",
        lambda *args: pytest.fail("mutation should not run on unsupported preflight"),
    )

    with pytest.raises(ValueError, match="unsupported status"):
        apply_assignment_leave_resolution(_leave_resolution_apply_request())

    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert cursor.execute_calls == []
    assert cursor.close_calls == 1
    assert connection.close_calls == 1


@pytest.mark.parametrize("rowcount", [0, 2])
def test_leave_resolution_apply_orchestration_rejects_unexpected_event_rowcount(
    monkeypatch, rowcount
):
    cursor = _ApplyOrchestrationCursor(lastrowid=5, rowcount=rowcount)
    connection = _ApplyOrchestrationConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    monkeypatch.setattr(
        service,
        "prepare_assignment_leave_resolution_apply",
        lambda cursor_arg, request_arg: {"status": "apply", "mutation_command": {}},
    )
    monkeypatch.setattr(
        service,
        "execute_assignment_leave_resolution_mutations",
        lambda *args: {
            "pending_event_payload": {
                "case_no": "CASE-1",
                "original_assignment_id": 11,
                "original_schedule_id": 22,
                "work_date": "2026-08-01",
                "resolution_type": "defer_following_assignments",
                "substitute_assignment_id": None,
                "event_key": "leave-11-22",
                "actor": "admin",
                "reason": "single-day leave",
                "schedule_snapshot": {"request_identity": {"case_no": "CASE-1"}},
                "payroll_snapshot": {"target_hours": "8.00"},
            }
        },
    )

    with pytest.raises(ValueError, match="did not affect exactly one row"):
        apply_assignment_leave_resolution(_leave_resolution_apply_request())

    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert cursor.execute_calls
    assert cursor.close_calls == 1
    assert connection.close_calls == 1


@pytest.mark.parametrize("invalid_lastrowid", [True, 0, -1, None])
def test_leave_resolution_apply_orchestration_rejects_invalid_event_id(
    monkeypatch, invalid_lastrowid
):
    cursor = _ApplyOrchestrationCursor(lastrowid=invalid_lastrowid)
    connection = _ApplyOrchestrationConnection(cursor)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    monkeypatch.setattr(
        service,
        "prepare_assignment_leave_resolution_apply",
        lambda cursor_arg, request_arg: {"status": "apply", "mutation_command": {}},
    )
    monkeypatch.setattr(
        service,
        "execute_assignment_leave_resolution_mutations",
        lambda *args: {
            "pending_event_payload": {
                "case_no": "CASE-1",
                "original_assignment_id": 11,
                "original_schedule_id": 22,
                "work_date": "2026-08-01",
                "resolution_type": "defer_following_assignments",
                "substitute_assignment_id": None,
                "event_key": "leave-11-22",
                "actor": "admin",
                "reason": "single-day leave",
                "schedule_snapshot": {"request_identity": {"case_no": "CASE-1"}},
                "payroll_snapshot": {"target_hours": "8.00"},
            }
        },
    )

    with pytest.raises(ValueError, match="event_id must be a positive integer"):
        apply_assignment_leave_resolution(_leave_resolution_apply_request())

    assert connection.rollback_calls == 1
    assert connection.commit_calls == 0
    assert cursor.execute_calls
    assert cursor.close_calls == 1
    assert connection.close_calls == 1


def test_normalise_rest_dates_rejects_invalid_elements():
    """rest_dates 應為嚴格 YYYY-MM-DD 字串陣列，任一非法元素整筆失敗。"""
    assert _normalise_rest_dates([]) == []
    assert _normalise_rest_dates(["2026-08-01"]) == ["2026-08-01"]

    with pytest.raises(ValueError, match="must be an array"):
        _normalise_rest_dates("2026-08-01")
    with pytest.raises(ValueError, match="must be an array"):
        _normalise_rest_dates(None)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates(["2026-8-1"])
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates([" 2026-08-01 "])
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates([None])
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates([date(2026, 8, 1)])
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates([datetime(2026, 8, 1, 0, 0, 0)])


def test_as_positive_int_validation_error_has_stable_code_and_message():
    with pytest.raises(AssignmentScheduleRestDateValidationError) as error:
        _as_positive_int(True, "assignment_id")

    assert error.value.code == "invalid_assignment_id"
    assert error.value.message == "assignment_id must be a positive integer"
    assert dict(error.value.details or {})["field"] == "assignment_id"


def test_as_rest_date_string_rejects_invalid_format():
    with pytest.raises(AssignmentScheduleRestDateValidationError) as error:
        _as_rest_date_string("2026-8-1", "rest_date")

    assert error.value.code == "invalid_rest_date"
    assert error.value.message == "rest_date must be a YYYY-MM-DD string"


def _principal_admin():
    from services.admin_auth_service import AdminPrincipal

    return AdminPrincipal(id=1, username="admin", display_name="Admin", role="system_admin")


def _preview_router_request(**overrides):
    payload = {
        "case_no": " CASE-1 ",
        "original_assignment_id": 11,
        "original_schedule_id": 22,
        "work_date": "2026-08-01",
        "resolution_type": "substitute",
        "substitute_staff_id": 202,
    }
    payload.update(overrides)
    from api.routes import assignment_schedule_rest_dates as route

    return route.AssignmentLeaveResolutionPreviewRequest(**payload)


def _apply_router_request(**overrides):
    payload = {
        "case_no": " CASE-1 ",
        "original_assignment_id": 11,
        "original_schedule_id": 22,
        "work_date": "2026-08-01",
        "resolution_type": "substitute",
        "substitute_staff_id": 202,
        "preview_fingerprint": "a" * 64,
        "event_key": " leave-11-22 ",
        "actor": "admin",
        "reason": "single-day leave",
    }
    payload.update(overrides)
    from api.routes import assignment_schedule_rest_dates as route

    return route.AssignmentLeaveResolutionApplyRequest(**payload)


def test_leave_resolution_router_preview_delegates_and_maps_ready(monkeypatch):
    from api.routes import assignment_schedule_rest_dates as route

    calls = []

    def _preview(*, case_no, original_assignment_id, original_schedule_id, work_date, resolution_type, substitute_staff_id):
        calls.append(
            {
                "case_no": case_no,
                "original_assignment_id": original_assignment_id,
                "original_schedule_id": original_schedule_id,
                "work_date": work_date,
                "resolution_type": resolution_type,
                "substitute_staff_id": substitute_staff_id,
            }
        )
        return {
            "status": "ready",
            "preview_fingerprint": "f" * 64,
            "requires_confirmation": True,
            "assignment impacts": {},
        }

    monkeypatch.setattr(route, "preview_assignment_leave_resolution", _preview)
    request = _preview_router_request()
    response = route.preview_assignment_leave_resolution_route(
        req=request,
        assignment_id=11,
    )

    assert calls == [
        {
            "case_no": "CASE-1",
            "original_assignment_id": 11,
            "original_schedule_id": 22,
            "work_date": "2026-08-01",
            "resolution_type": "substitute",
            "substitute_staff_id": 202,
        }
    ]
    assert response.data["status"] == "ready"


def test_leave_resolution_router_preview_rejects_assignment_path_mismatch(monkeypatch):
    from fastapi import HTTPException
    from api.routes import assignment_schedule_rest_dates as route

    def _preview(*_args, **_kwargs):
        raise AssertionError("preview should not be called")

    monkeypatch.setattr(route, "preview_assignment_leave_resolution", _preview)
    request = _preview_router_request()

    with pytest.raises(HTTPException) as error:
        route.preview_assignment_leave_resolution_route(req=request, assignment_id=12)

    assert error.value.status_code == 422
    assert error.value.detail["status"] == "validation_error"
    assert error.value.detail["reason"] == "assignment_id does not match original_assignment_id"


def test_leave_resolution_router_preview_maps_blocked_to_conflict(monkeypatch):
    from fastapi import HTTPException
    from api.routes import assignment_schedule_rest_dates as route

    monkeypatch.setattr(
        route,
        "preview_assignment_leave_resolution",
        lambda **_kwargs: {
            "status": "blocked",
            "blocking_reasons": ["schedule_conflict"],
            "message": "blocked",
        },
    )

    with pytest.raises(HTTPException) as error:
        route.preview_assignment_leave_resolution_route(
            req=_preview_router_request(),
            assignment_id=11,
        )

    assert error.value.status_code == 409
    assert error.value.detail["status"] == "blocked"


def test_leave_resolution_router_apply_requires_actor_match(monkeypatch):
    from fastapi import HTTPException
    from api.routes import assignment_schedule_rest_dates as route

    monkeypatch.setattr(
        route,
        "apply_assignment_leave_resolution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("service should not run")),
    )

    request = _apply_router_request(actor="other-admin")

    with pytest.raises(HTTPException) as error:
        route.apply_assignment_leave_resolution_route(
            req=request,
            assignment_id=11,
            principal=_principal_admin(),
        )

    assert error.value.status_code == 403
    assert error.value.detail["status"] == "authorization"


def test_leave_resolution_router_apply_delegates_and_allows_idempotent_replay(monkeypatch):
    from api.routes import assignment_schedule_rest_dates as route

    calls = []

    monkeypatch.setattr(
        route,
        "apply_assignment_leave_resolution",
        lambda request: calls.append(request) or {
            "status": "idempotent_replay",
            "result": "idempotent_replay",
            "existing_event_identity": {"event_key": "leave-11-22"},
        },
    )

    request = _apply_router_request()
    response = route.apply_assignment_leave_resolution_route(
        req=request,
        assignment_id=11,
        principal=_principal_admin(),
    )

    assert calls == [request.model_dump()]
    assert response.data["status"] == "idempotent_replay"


def test_leave_resolution_router_apply_maps_rejected_to_http_error(monkeypatch):
    from fastapi import HTTPException
    from api.routes import assignment_schedule_rest_dates as route

    monkeypatch.setattr(
        route,
        "apply_assignment_leave_resolution",
        lambda request: {
            "status": "rejected",
            "reason": "preview_not_ready",
            "recomputed_preview": {
                "status": "requires_review",
            },
        },
    )

    with pytest.raises(HTTPException) as error:
        route.apply_assignment_leave_resolution_route(
            req=_apply_router_request(),
            assignment_id=11,
            principal=_principal_admin(),
        )

    assert error.value.status_code == 409
    assert error.value.detail["status"] == "rejected"
    assert error.value.detail["reason"] == "preview_not_ready"


def test_leave_resolution_router_apply_maps_event_key_identity_conflict_to_conflict_http(monkeypatch):
    from fastapi import HTTPException
    from api.routes import assignment_schedule_rest_dates as route

    def _apply(_request):
        raise ValueError("event_key already exists with a different request identity")

    monkeypatch.setattr(route, "apply_assignment_leave_resolution", _apply)

    with pytest.raises(HTTPException) as error:
        route.apply_assignment_leave_resolution_route(
            req=_apply_router_request(),
            assignment_id=11,
            principal=_principal_admin(),
        )

    assert error.value.status_code == 409
    assert error.value.detail["status"] == "validation_error"
    assert "different request identity" in error.value.detail["reason"]


def test_assignment_leave_resolution_domain_error_enforces_allowed_categories():
    for category in [
        "not_found",
        "validation_error",
        "conflict",
        "locked",
        "stale_preview",
        "event_key_identity_conflict",
    ]:
        err = AssignmentLeaveResolutionDomainError(
            category=category,
            code="stable_code",
            reason="stable reason",
            details={"source": "snapshot"},
        )
        assert err.category == category
        assert err.code == "stable_code"
        assert err.reason == "stable reason"
        assert err.as_dict()["category"] == category
        assert err.as_dict()["code"] == "stable_code"
        assert err.as_dict()["reason"] == "stable reason"


def test_assignment_leave_resolution_domain_error_rejects_invalid_inputs():
    with pytest.raises(ValueError, match="invalid category"):
        AssignmentLeaveResolutionDomainError(
            category="unexpected",
            code="stable_code",
            reason="stable reason",
        )
    with pytest.raises(ValueError, match="invalid code"):
        AssignmentLeaveResolutionDomainError(
            category="conflict",
            code="bad code",
            reason="stable reason",
        )
    with pytest.raises(ValueError, match="invalid reason"):
        AssignmentLeaveResolutionDomainError(
            category="conflict",
            code="stable_code",
            reason=" ",
        )
    with pytest.raises(ValueError, match="invalid details"):
        AssignmentLeaveResolutionDomainError(
            category="conflict",
            code="stable_code",
            reason="stable reason",
            details=[],  # type: ignore[type-var]
        )


def test_assignment_leave_resolution_domain_error_json_safe_values_pass_and_round_trip():
    error = AssignmentLeaveResolutionDomainError(
        category="conflict",
        code="schedule_conflict",
        reason="conflicting schedules",
        details={
            "meta": {
                "bool_true": True,
                "bool_false": False,
                "nullable": None,
                "count": 3,
                "ratio": 1.25,
                "notes": ("a", ["b", {"nested": (1, 2.5, None)}]),
            }
        },
    )

    snapshot = error.as_dict()
    assert snapshot == {
        "category": "conflict",
        "code": "schedule_conflict",
        "reason": "conflicting schedules",
        "details": {
            "meta": {
                "bool_true": True,
                "bool_false": False,
                "nullable": None,
                "count": 3,
                "ratio": 1.25,
                "notes": ["a", ["b", {"nested": [1, 2.5, None]}]],
            }
        },
    }
    serialized = json.dumps(snapshot)
    assert isinstance(serialized, str)


def test_assignment_leave_resolution_domain_error_rejects_non_json_scalars_and_keys():
    class CustomPayload:
        pass

    with pytest.raises(ValueError, match="invalid details"):
        AssignmentLeaveResolutionDomainError(
            category="conflict",
            code="schedule_conflict",
            reason="invalid key",
            details={1: "not-string-key"},
        )
    with pytest.raises(ValueError, match="invalid details"):
        AssignmentLeaveResolutionDomainError(
            category="conflict",
            code="schedule_conflict",
            reason="invalid type",
            details={"notes": {1, 2}},
        )
    with pytest.raises(ValueError, match="invalid details"):
        AssignmentLeaveResolutionDomainError(
            category="conflict",
            code="schedule_conflict",
            reason="invalid type",
            details={"ratio": float("nan")},
        )
    with pytest.raises(ValueError, match="invalid details"):
        AssignmentLeaveResolutionDomainError(
            category="conflict",
            code="schedule_conflict",
            reason="invalid type",
            details={"ratio": float("inf")},
        )
    with pytest.raises(ValueError, match="invalid details"):
        AssignmentLeaveResolutionDomainError(
            category="conflict",
            code="schedule_conflict",
            reason="invalid type",
            details={"d": datetime(2026, 8, 1)},
        )
    with pytest.raises(ValueError, match="invalid details"):
        AssignmentLeaveResolutionDomainError(
            category="conflict",
            code="schedule_conflict",
            reason="invalid type",
            details={"obj": CustomPayload()},
        )


def test_assignment_leave_resolution_domain_error_details_are_copy_safe():
    payload = {"note": "original"}
    error = AssignmentLeaveResolutionDomainError(
        category="conflict",
        code="schedule_conflict",
        reason="conflicting schedules",
        details=payload,
    )
    payload["note"] = "mutated"

    serialized = error.as_dict()
    assert serialized["details"] == {"note": "original"}

    with pytest.raises(TypeError):
        error.details["note"] = "blocked"

    second_snapshot = error.as_dict()
    second_snapshot["details"]["note"] = "overwritten"
    assert error.as_dict()["details"]["note"] == "original"


def test_assignment_leave_resolution_domain_error_nested_details_are_immutable_and_isolated():
    payload = {
        "nested": {
            "items": [
                {"name": "rest-day", "values": [1, 2]},
            ],
            "notes": ["a", "b"],
        }
    }
    error = AssignmentLeaveResolutionDomainError(
        category="conflict",
        code="stale_preview",
        reason="nested details are protected",
        details=payload,
    )

    with pytest.raises(TypeError):
        error.details["nested"]["items"].append({"name": "blocked"})

    with pytest.raises(TypeError):
        error.details["nested"]["items"][0]["values"].append(3)

    snapshot = error.as_dict()
    assert snapshot["details"] == {
        "nested": {"items": [{"name": "rest-day", "values": [1, 2]}], "notes": ["a", "b"]},
    }
    snapshot["details"]["nested"]["items"][0]["values"][0] = 3
    assert error.as_dict()["details"]["nested"]["items"][0]["values"][0] == 1


def test_assignment_leave_resolution_domain_error_exception_args_and_string_are_stable():
    error = AssignmentLeaveResolutionDomainError(
        category="validation_error",
        code="validation_error_input",
        reason="invalid preview fingerprint",
        details={"reason": {"code": "invalid", "value": "bad"}},
    )

    assert len(error.args) == 1
    assert isinstance(error.args[0], str)
    assert str(error) == error.args[0]
    assert error.args[0].startswith("assignment leave resolution domain error")
    assert "category=validation_error" in error.args[0]
    assert "code=validation_error_input" in error.args[0]
    assert "invalid preview fingerprint" in error.args[0]


def test_preview_assignment_leave_resolution_uses_stable_code_for_invalid_resolution_type():
    with pytest.raises(AssignmentLeaveResolutionDomainError) as error:
        preview_assignment_leave_resolution(
            case_no="CASE-1",
            original_assignment_id=11,
            original_schedule_id=22,
            work_date="2026-08-01",
            resolution_type="invalid",
            substitute_staff_id=None,
        )

    assert error.value.code == "invalid_resolution_type"
