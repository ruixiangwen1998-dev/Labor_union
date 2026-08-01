import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.routes import orders
from services.admin_auth_service import AdminPrincipal


def principal(username: str = "admin") -> AdminPrincipal:
    return AdminPrincipal(1, username, "Admin", "system_admin")


def order_change():
    return {
        "client_name": "王小明",
        "service_days": 2,
        "service_hours_per_day": 8,
        "floor_fee": 1200,
        "deposit_date": "2026-07-20",
        "start_date": "2026-08-03",
        "end_date": "2026-08-04",
        "actual_start_date": "2026-08-03",
        "actual_end_date": "2026-08-04",
    }


def preview_request(**overrides):
    payload = {
        "order_change": order_change(),
        "assignment_plan": [{"staff_id": 11, "assignment_sequence": 1}],
    }
    payload.update(overrides)
    return orders.OrderAssignmentSynchronizationPreviewRequest(**payload)


def apply_request(**overrides):
    payload = {
        "order_change": order_change(),
        "assignment_plan": [{"staff_id": 11, "assignment_sequence": 1}],
        "schedule_change_plan": {"remove_schedule_ids": [99]},
        "applied_by": "admin",
    }
    payload.update(overrides)
    return orders.OrderAssignmentSynchronizationApplyRequest(**payload)


def cancel_request(**overrides):
    payload = {
        "event_key": "evt-001",
        "actor": "operator-a",
        "cancel_reason": "Client requested cancellation",
    }
    payload.update(overrides)
    return orders.OrderLockCancellationRequest(**payload)


def test_cancel_route_delegates_lock_cancellation_service(monkeypatch):
    received = {}
    def _fake_cancel(**kwargs):
        received.update(kwargs)
        return {
            "result": "cancelled",
            "case_no": kwargs["case_no"],
            "plan_id": 77,
            "lock_id": 55,
            "order_status": "訂單取消",
            "cancel_reason": kwargs["cancel_reason"],
            "lock_rows": [],
        }

    monkeypatch.setattr(orders, "cancel_caregiver_availability_lock_for_order", _fake_cancel)

    response = orders.cancel_order_lock_for_case_no(cancel_request(), case_no="C-1")

    assert received["case_no"] == "C-1"
    assert received["event_key"] == "evt-001"
    assert received["actor"] == "operator-a"
    assert received["cancel_reason"] == "Client requested cancellation"
    assert response.success is True
    assert response.data["result"] == "cancelled"
    assert response.data["case_no"] == "C-1"


def test_cancel_route_maps_service_validation_to_422(monkeypatch):
    monkeypatch.setattr(
        orders,
        "cancel_caregiver_availability_lock_for_order",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("case is not in negotiation")),
    )

    with pytest.raises(HTTPException) as error:
        orders.cancel_order_lock_for_case_no(cancel_request(), case_no="C-1")

    assert error.value.status_code == 422
    assert error.value.detail == "case is not in negotiation"


def test_cancel_route_maps_service_errors_to_500(monkeypatch):
    monkeypatch.setattr(
        orders,
        "cancel_caregiver_availability_lock_for_order",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    with pytest.raises(HTTPException) as error:
        orders.cancel_order_lock_for_case_no(cancel_request(event_key="evt-2"), case_no="C-1")

    assert error.value.status_code == 500
    assert error.value.detail == "Failed to cancel order lock"


def test_preview_route_delegates_the_complete_explicit_plan(monkeypatch):
    received = {}
    monkeypatch.setattr(
        orders,
        "preview_order_assignment_sync",
        lambda **kwargs: received.update(kwargs) or {"sync_status": "in_sync"},
    )

    response = orders.preview_order_assignment_synchronization(preview_request(), case_no="C-1")

    assert received["case_no"] == "C-1"
    assert received["order_change"] == preview_request().order_change.model_dump()
    assert received["assignment_plan"] == [{"staff_id": 11, "assignment_sequence": 1}]
    assert response.success is True
    assert response.data == {"sync_status": "in_sync"}


def test_preview_route_maps_service_validation_to_422(monkeypatch):
    monkeypatch.setattr(
        orders,
        "preview_order_assignment_sync",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("assignment_plan must be a list")),
    )

    with pytest.raises(HTTPException) as error:
        orders.preview_order_assignment_synchronization(preview_request(), case_no="C-1")

    assert error.value.status_code == 422
    assert error.value.detail == "assignment_plan must be a list"


@pytest.mark.parametrize("field", ["client_name", "floor_fee", "start_date", "actual_start_date"])
def test_synchronization_request_requires_complete_non_cancel_order_target(field):
    payload = order_change()
    payload.pop(field)

    with pytest.raises(ValidationError, match=field):
        preview_request(order_change=payload)


@pytest.mark.parametrize("field", ["clients.identity_status", "identity_status"])
def test_synchronization_request_rejects_writable_identity_fields(field):
    payload = order_change()
    payload[field] = "一般身分"

    with pytest.raises(ValidationError, match=field):
        preview_request(order_change=payload)


def test_synchronization_request_allows_blank_deposit_date():
    payload = order_change()
    payload["deposit_date"] = None

    assert preview_request(order_change=payload).order_change.deposit_date is None


def test_apply_route_does_not_call_service_without_explicit_removal_ids(monkeypatch):
    monkeypatch.setattr(orders, "apply_order_assignment_sync", lambda **_kwargs: pytest.fail("must not apply"))

    with pytest.raises(HTTPException) as error:
        orders.apply_order_assignment_synchronization(
            apply_request(schedule_change_plan={}),
            case_no="C-1",
            principal=principal(),
        )

    assert error.value.status_code == 422
    assert error.value.detail == "remove_schedule_ids is required"


def test_apply_route_maps_unapplied_locked_result_to_409(monkeypatch):
    received = {}
    monkeypatch.setattr(
        orders,
        "apply_order_assignment_sync",
        lambda **kwargs: received.update(kwargs)
        or {"sync_status": "locked", "blocking_reasons": [{"code": "active_staff_payment"}]},
    )

    with pytest.raises(HTTPException) as error:
        orders.apply_order_assignment_synchronization(
            apply_request(),
            case_no="C-1",
            principal=principal(),
        )

    assert error.value.status_code == 409
    assert error.value.detail["sync_status"] == "locked"
    assert received["schedule_change_plan"] == {"remove_schedule_ids": [99]}
    assert received["applied_by"] == "admin"


def test_apply_route_maps_service_validation_to_422(monkeypatch):
    monkeypatch.setattr(
        orders,
        "apply_order_assignment_sync",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("stale schedule plan")),
    )

    with pytest.raises(HTTPException) as error:
        orders.apply_order_assignment_synchronization(
            apply_request(),
            case_no="C-1",
            principal=principal(),
        )

    assert error.value.status_code == 422
    assert error.value.detail == "stale schedule plan"


def test_apply_route_rejects_spoofed_actor_before_service(monkeypatch):
    monkeypatch.setattr(
        orders,
        "apply_order_assignment_sync",
        lambda **_kwargs: pytest.fail("must not apply"),
    )

    with pytest.raises(HTTPException) as error:
        orders.apply_order_assignment_synchronization(
            apply_request(applied_by="other-admin"),
            case_no="C-1",
            principal=principal(),
        )

    assert error.value.status_code == 403
