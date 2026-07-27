"""Holiday administration is protected by the formal system-admin dependency."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.dependencies import admin_auth
from api.routes import holidays
from services import db_service
from services.admin_auth_service import AdminPrincipal


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(holidays.router)
    return TestClient(app)


def _headers() -> dict[str, str]:
    return {
        "X-Internal-API-Key": "internal-test-key",
        "Authorization": "Bearer session-token",
    }


def _configure_formal_auth(monkeypatch, role: str) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENABLE_ADMIN_AUTH", "true")
    monkeypatch.setenv("INTERNAL_API_KEY", "internal-test-key")
    monkeypatch.setattr(
        admin_auth,
        "get_admin_session",
        lambda _token: AdminPrincipal(9, "holiday-admin", "Holiday Admin", role),
    )


def test_holiday_routes_reject_missing_internal_key(monkeypatch):
    _configure_formal_auth(monkeypatch, "system_admin")

    response = _client().get("/api/v1/holidays")

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/api/v1/holidays", None),
        (
            "POST",
            "/api/v1/holidays",
            {
                "holiday_date": "2026-01-01",
                "holiday_name": "元旦",
                "is_double_pay_default": True,
            },
        ),
        ("DELETE", "/api/v1/holidays/2026-01-01", None),
    ],
)
def test_every_holiday_route_rejects_line_manager(
    monkeypatch,
    method,
    path,
    json_body,
):
    _configure_formal_auth(monkeypatch, "line_manager")

    response = _client().request(
        method,
        path,
        headers=_headers(),
        json=json_body,
    )

    assert response.status_code == 403


def test_holiday_get_delegates_for_system_admin(monkeypatch):
    _configure_formal_auth(monkeypatch, "system_admin")
    monkeypatch.setattr(
        db_service,
        "get_table_data",
        lambda table: [{"holiday_date": "2026-01-01", "holiday_name": "元旦"}]
        if table == "holidays"
        else [],
    )

    response = _client().get("/api/v1/holidays", headers=_headers())

    assert response.status_code == 200
    assert response.json()["data"][0]["holiday_name"] == "元旦"
