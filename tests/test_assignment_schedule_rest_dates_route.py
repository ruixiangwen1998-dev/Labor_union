import pytest
from fastapi import HTTPException

from api.routes import assignment_schedule_rest_dates as router_module
from api.schemas.orders import AssignmentRestDatesUpdateRequest


def test_rest_dates_route_delegates_request(monkeypatch):
    received = []

    def fake_service(*, assignment_id, rest_dates):
        received.append((assignment_id, list(rest_dates)))
        return {"success": True, "status": "ok"}

    monkeypatch.setattr(router_module.assignment_schedule_rest_date_service, "save_assignment_rest_dates", fake_service)

    response = router_module.save_assignment_rest_dates(
        AssignmentRestDatesUpdateRequest(rest_dates=["2026-08-01", "2026-08-02"]),
        88,
    )
    assert response.success is True
    assert received == [(88, ["2026-08-01", "2026-08-02"])]


def test_rest_dates_route_maps_not_found_to_404(monkeypatch):
    monkeypatch.setattr(
        router_module.assignment_schedule_rest_date_service,
        "save_assignment_rest_dates",
        lambda **kwargs: {"success": False, "status": "not_found", "message": "assignment_id does not exist"},
    )
    with pytest.raises(HTTPException) as error:
        router_module.save_assignment_rest_dates(
            AssignmentRestDatesUpdateRequest(rest_dates=["2026-08-01"]),
            77,
        )
    assert error.value.status_code == 404


def test_rest_dates_route_maps_validation_to_422(monkeypatch):
    monkeypatch.setattr(
        router_module.assignment_schedule_rest_date_service,
        "save_assignment_rest_dates",
        lambda **kwargs: {
            "success": False,
            "status": "validation_error",
            "message": "invalid date format",
        },
    )
    with pytest.raises(HTTPException) as error:
        router_module.save_assignment_rest_dates(
            AssignmentRestDatesUpdateRequest(rest_dates=["2026-13-01"]),
            77,
        )
    assert error.value.status_code == 422


def test_rest_dates_route_maps_conflict_to_409(monkeypatch):
    monkeypatch.setattr(
        router_module.assignment_schedule_rest_date_service,
        "save_assignment_rest_dates",
        lambda **kwargs: {
            "success": False,
            "status": "conflict",
            "message": "schedule conflict",
        },
    )
    with pytest.raises(HTTPException) as error:
        router_module.save_assignment_rest_dates(
            AssignmentRestDatesUpdateRequest(rest_dates=["2026-08-01"]),
            77,
        )
    assert error.value.status_code == 409


def test_rest_dates_route_maps_locked_to_409(monkeypatch):
    monkeypatch.setattr(
        router_module.assignment_schedule_rest_date_service,
        "save_assignment_rest_dates",
        lambda **kwargs: {
            "success": False,
            "status": "locked",
            "message": "assignment is locked",
        },
    )
    with pytest.raises(HTTPException) as error:
        router_module.save_assignment_rest_dates(
            AssignmentRestDatesUpdateRequest(rest_dates=["2026-08-01"]),
            77,
        )
    assert error.value.status_code == 409


def test_rest_dates_route_maps_unexpected_exception_to_500(monkeypatch):
    monkeypatch.setattr(
        router_module.assignment_schedule_rest_date_service,
        "save_assignment_rest_dates",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db offline")),
    )
    with pytest.raises(HTTPException) as error:
        router_module.save_assignment_rest_dates(
            AssignmentRestDatesUpdateRequest(rest_dates=["2026-08-01"]),
            77,
        )
    assert error.value.status_code == 500
