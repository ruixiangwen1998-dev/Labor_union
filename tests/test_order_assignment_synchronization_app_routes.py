"""Regression coverage for the synchronization endpoints registered on the FastAPI app."""

from fastapi.testclient import TestClient

from api.main import app


def test_assignment_synchronization_preview_route_is_registered_on_main_app(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "route-test-key")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENABLE_ADMIN_AUTH", "false")
    response = TestClient(app).post(
        "/api/v1/orders/C-1/assignment-synchronization/preview",
        json={},
        headers={"X-Internal-API-Key": "route-test-key"},
    )

    assert response.status_code == 422
    assert response.status_code != 404


def test_order_lock_cancellation_route_is_registered_on_main_app():
    response = TestClient(app).post("/api/v1/orders/C-1/cancel", json={})

    assert response.status_code == 422
    assert response.status_code != 404
