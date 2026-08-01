from decimal import Decimal

import pytest
from fastapi import HTTPException

from api.routes import caregiver_availability_locks as route
from services.admin_auth_service import AdminPrincipal


def _principal(username="admin"):
    return AdminPrincipal(
        id=1,
        username=username,
        display_name="Admin",
        role="system_admin",
    )


@pytest.mark.parametrize(
    ("handler", "request_model", "path_args", "service_name", "expected"),
    [
        (
            route.acquire_availability_lock,
            route.AcquireAvailabilityLockRequest(event_key="acquire-1", actor="admin"),
            {"case_no": "CASE-1", "plan_id": 8},
            "acquire_caregiver_availability_lock",
            {
                "case_no": "CASE-1",
                "plan_id": 8,
                "event_key": "acquire-1",
                "actor": "admin",
            },
        ),
        (
            route.release_availability_lock,
            route.ReleaseAvailabilityLockRequest(
                event_key="release-1", actor="admin", reason="customer changed plan"
            ),
            {"case_no": "CASE-1", "plan_id": 8, "lock_id": 9},
            "release_caregiver_availability_lock",
            {
                "case_no": "CASE-1",
                "plan_id": 8,
                "lock_id": 9,
                "event_key": "release-1",
                "actor": "admin",
                "reason": "customer changed plan",
            },
        ),
    ],
)
def test_lifecycle_routes_delegate_exactly_once(
    monkeypatch, handler, request_model, path_args, service_name, expected
):
    calls = []
    result = {"result": "existing", "snapshot": [{"segment_id": 2}, {"segment_id": 1}]}

    def service(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(route, service_name, service)
    response = handler(request=request_model, principal=_principal(), **path_args)

    assert calls == [expected]
    assert response.data is result
    assert response.data["snapshot"] == [{"segment_id": 2}, {"segment_id": 1}]


def test_conversion_preserves_explicit_decimal_terms(monkeypatch):
    calls = []
    result = {"result": "created", "assignments": [{"assignment_id": 11}]}

    def service(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(route, "convert_availability_lock_to_assignments", service)
    request = route.ConvertAvailabilityLockRequest(
        event_key="convert-1",
        actor="admin",
        reason="deposit confirmed",
        assignment_terms=[
            {
                "segment_id": 3,
                "hourly_rate": "350.50",
                "floor_fee_allocated": "100.00",
            }
        ],
    )
    response = route.convert_availability_lock(
        request=request,
        case_no="CASE-1",
        lock_id=9,
        principal=_principal(),
    )

    assert calls == [
        {
            "case_no": "CASE-1",
            "lock_id": 9,
            "event_key": "convert-1",
            "actor": "admin",
            "reason": "deposit confirmed",
            "assignment_terms": [
                {
                    "segment_id": 3,
                    "hourly_rate": Decimal("350.50"),
                    "floor_fee_allocated": Decimal("100.00"),
                }
            ],
        }
    ]
    assert response.data is result


def test_conversion_rejects_spoofed_actor_before_service(monkeypatch):
    monkeypatch.setattr(
        route,
        "convert_availability_lock_to_assignments",
        lambda **_kwargs: pytest.fail("service must not be called"),
    )
    request = route.ConvertAvailabilityLockRequest(
        event_key="convert-1",
        actor="other-admin",
        reason="deposit confirmed",
        assignment_terms=[
            {
                "segment_id": 3,
                "hourly_rate": "350.50",
                "floor_fee_allocated": "100.00",
            }
        ],
    )

    with pytest.raises(HTTPException) as caught:
        route.convert_availability_lock(
            request=request,
            case_no="CASE-1",
            lock_id=9,
            principal=_principal(),
        )

    assert caught.value.status_code == 403


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            route.AcquireAvailabilityLockRequest,
            {"event_key": "event", "actor": "admin", "status": "active"},
        ),
        (
            route.ConvertAvailabilityLockRequest,
            {
                "event_key": "event",
                "actor": "admin",
                "reason": "deposit",
                "assignment_terms": [
                    {
                        "segment_id": 1,
                        "hourly_rate": 1.5,
                        "floor_fee_allocated": "0",
                    }
                ],
            },
        ),
    ],
)
def test_requests_fail_closed_for_forbidden_or_float_fields(model, payload):
    with pytest.raises(ValueError):
        model(**payload)


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (ValueError("invalid lifecycle"), 422, "invalid lifecycle"),
        (RuntimeError("sql password"), 500, "Unexpected error during availability lock acquisition"),
    ],
)
def test_errors_are_conservative_and_unexpected_details_are_hidden(
    monkeypatch, error, status_code, detail
):
    def service(**kwargs):
        raise error

    monkeypatch.setattr(route, "acquire_caregiver_availability_lock", service)
    with pytest.raises(HTTPException) as caught:
        route.acquire_availability_lock(
            request=route.AcquireAvailabilityLockRequest(
                event_key="event", actor="admin"
            ),
            case_no="CASE-1",
            plan_id=1,
            principal=_principal(),
        )

    assert caught.value.status_code == status_code
    assert caught.value.detail == detail
    assert "sql password" not in caught.value.detail


def test_router_exposes_three_fixed_actions_without_cancellation():
    paths = {item.path for item in route.router.routes}
    assert paths == {
        "/api/v1/orders/{case_no}/matching-plans/{plan_id}/availability-lock/acquire",
        "/api/v1/orders/{case_no}/matching-plans/{plan_id}/availability-locks/{lock_id}/release",
        "/api/v1/orders/{case_no}/availability-locks/{lock_id}/convert",
    }
    assert all("{action}" not in path and "/cancel" not in path for path in paths)
