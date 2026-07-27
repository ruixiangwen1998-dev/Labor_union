from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import staff_monthly_schedule as router_module


def test_staff_monthly_schedule_maps_not_found_to_404(monkeypatch):
    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    def _raise_not_found(*_args, **_kwargs):
        raise ValueError("服務人員不存在：9")

    monkeypatch.setattr(router_module.staff_monthly_calendar_schedule_service, "get_staff_monthly_calendar_schedule", _raise_not_found)

    response = client.get("/api/v1/staff/9/monthly-schedule?year=2026&month=7")
    assert response.status_code == 404


def test_staff_monthly_schedule_maps_validation_error_to_422(monkeypatch):
    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    # 年份超出 Query 範圍會直接回傳 422
    response = client.get("/api/v1/staff/9/monthly-schedule?year=2101&month=7")
    assert response.status_code == 422

    def _raise_validation(*_args, **_kwargs):
        raise ValueError("invalid")

    monkeypatch.setattr(router_module.staff_monthly_calendar_schedule_service, "get_staff_monthly_calendar_schedule", _raise_validation)
    response = client.get("/api/v1/staff/9/monthly-schedule?year=2026&month=7")
    assert response.status_code == 422


def test_staff_monthly_schedule_maps_unexpected_error_to_500(monkeypatch):
    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    def _broken(*_args, **_kwargs):
        raise RuntimeError("db connection failed")

    monkeypatch.setattr(router_module.staff_monthly_calendar_schedule_service, "get_staff_monthly_calendar_schedule", _broken)
    response = client.get("/api/v1/staff/9/monthly-schedule?year=2026&month=7")
    assert response.status_code == 500
