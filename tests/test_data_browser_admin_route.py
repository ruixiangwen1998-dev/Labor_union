"""Data Browser routes must reuse the formal administrator dependency."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.dependencies import admin_auth
from api.routes import data_browser_admin
from api.schemas.data_browser import DataBrowserPatchRequest
from services import data_browser_admin_schema_service
from services.admin_auth_service import AdminPrincipal


def _principal(role: str = "system_admin") -> AdminPrincipal:
    return AdminPrincipal(7, "verified-admin", "Verified Admin", role)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(data_browser_admin.router)
    return TestClient(app)


def _headers() -> dict[str, str]:
    return {
        "X-Internal-API-Key": "internal-test-key",
        "Authorization": "Bearer session-token",
    }


def test_admin_router_get_without_internal_key_returns_401(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENABLE_ADMIN_AUTH", "true")
    monkeypatch.setenv("INTERNAL_API_KEY", "internal-test-key")

    response = _client().get("/api/v1/admin/data-browser/orders")

    assert response.status_code == 401
    assert response.json()["detail"] == "內部服務金鑰錯誤"


def test_admin_router_rejects_insufficient_formal_role(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENABLE_ADMIN_AUTH", "true")
    monkeypatch.setenv("INTERNAL_API_KEY", "internal-test-key")
    monkeypatch.setattr(admin_auth, "get_admin_session", lambda _token: _principal("line_manager"))

    response = _client().get(
        "/api/v1/admin/data-browser/orders",
        headers=_headers(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "需要 system_admin 或更高權限"


def test_patch_passes_verified_principal_username_to_service(monkeypatch):
    captured = {}

    def _fake_patch(
        table_name,
        row_id,
        updates,
        operator_id="admin_ui",
        operator_role="admin",
    ):
        captured.update(
            table=table_name,
            row_id=row_id,
            operator=operator_id,
            role=operator_role,
            updates=updates,
        )
        return True

    monkeypatch.setattr(
        data_browser_admin_schema_service,
        "patch_data_browser_table_row",
        _fake_patch,
    )

    response = data_browser_admin.patch_data_browser_row(
        DataBrowserPatchRequest(updates={"service_days": 10}),
        table="orders",
        row_id_str="TEST_ROUTE_001",
        principal=_principal(),
    )

    assert response.data is True
    assert captured["operator"] == "verified-admin"
    assert captured["role"] == "system_admin"


def test_patch_data_browser_row_validation_error_maps_to_422(monkeypatch):
    monkeypatch.setattr(
        data_browser_admin_schema_service,
        "patch_data_browser_table_row",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("欄位 [bad] 不在可編輯白名單中，更新已取消。")
        ),
    )

    with pytest.raises(HTTPException) as error:
        data_browser_admin.patch_data_browser_row(
            DataBrowserPatchRequest(updates={"bad": "x"}),
            table="orders",
            row_id_str="TEST_ROUTE_001",
            principal=_principal(),
        )

    assert error.value.status_code == 422


def test_patch_data_browser_row_not_found_maps_to_404(monkeypatch):
    monkeypatch.setattr(
        data_browser_admin_schema_service,
        "patch_data_browser_table_row",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("指定資料列不存在或欄位變更未生效，更新已取消。")
        ),
    )

    with pytest.raises(HTTPException) as error:
        data_browser_admin.patch_data_browser_row(
            DataBrowserPatchRequest(updates={"service_days": 99}),
            table="orders",
            row_id_str="TEST_ROUTE_MISS",
            principal=_principal(),
        )

    assert error.value.status_code == 404
