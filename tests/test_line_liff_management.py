"""Stage 5.5 LIFF configuration and identity safety tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from api.routes import line_system_config
from api.schemas.line_config import LiffField, LiffSettingsConfig
from services.json_config_service import read_config
from services.line_liff_identity_service import (
    LiffIdentityError,
    resolve_line_user_id,
)
from services.line_liff_config_service import upgrade_liff_snapshot


ROOT = Path(__file__).resolve().parent.parent


def test_stored_liff_config_has_all_runtime_pages_and_system_contracts():
    config = read_config("liff", LiffSettingsConfig)
    assert config.version == 2
    assert set(config.pages) == {
        "gateway", "bind", "registration", "union_staff_binding"
    }
    assert config.pages["gateway"].page_type == "navigation"
    assert {field.id for field in config.pages["bind"].fields} >= {"name", "phone"}
    assert {field.id for field in config.pages["registration"].fields} >= {
        "name",
        "phone",
        "expected_date",
        "service_days",
        "address",
    }


def test_required_system_field_cannot_be_disabled():
    config = read_config("liff", LiffSettingsConfig).model_dump(mode="json")
    target = next(
        field for field in config["pages"]["registration"]["fields"]
        if field["id"] == "name"
    )
    target["enabled"] = False
    with pytest.raises(ValidationError, match="must remain enabled and required"):
        LiffSettingsConfig.model_validate(config)


def test_legacy_liff_snapshot_is_upgraded_with_admin_binding_page():
    legacy = read_config("liff", LiffSettingsConfig).model_dump(mode="json")
    legacy["pages"].pop("union_staff_binding")

    upgraded = LiffSettingsConfig.model_validate(upgrade_liff_snapshot(legacy))

    assert upgraded.pages["union_staff_binding"].page_type == "admin_binding"


def test_runtime_filters_disabled_custom_fields(monkeypatch):
    config = read_config("liff", LiffSettingsConfig)
    config.pages["registration"].fields.append(
        LiffField(
            id="disabled_question",
            label="停用問題",
            type="text",
            enabled=False,
            order=999,
        )
    )
    monkeypatch.setattr(line_system_config, "_read", lambda *_: config)
    monkeypatch.setattr(line_system_config, "config_revision", lambda *_: "revision-1")
    response = Response()
    result = line_system_config.get_liff_runtime(response, "registration")
    assert result["revision"] == "revision-1"
    assert "disabled_question" not in {field.id for field in result["page"].fields}
    assert response.headers["cache-control"] == "no-cache"


def test_stale_liff_revision_is_rejected(monkeypatch):
    monkeypatch.setattr(line_system_config, "config_revision", lambda *_: "current")
    with pytest.raises(HTTPException) as raised:
        line_system_config._require_liff_revision('"stale"')
    assert raised.value.status_code == 409


def test_liff_identity_allows_development_fallback(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("LIFF_REQUIRE_ID_TOKEN", raising=False)
    monkeypatch.delenv("LINE_LOGIN_CHANNEL_ID", raising=False)
    assert resolve_line_user_id(id_token="", development_user_id="U-dev") == "U-dev"


def test_liff_identity_requires_token_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "123456")
    monkeypatch.delenv("LIFF_REQUIRE_ID_TOKEN", raising=False)
    with pytest.raises(LiffIdentityError, match="缺少 LIFF ID Token"):
        resolve_line_user_id(id_token="", development_user_id="U-forged")


def test_liff_identity_uses_verified_subject(monkeypatch):
    class FakeResponse:
        ok = True

        @staticmethod
        def json():
            return {"sub": "U-trusted"}

    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "123456")
    monkeypatch.setattr(
        "services.line_liff_identity_service.requests.post",
        lambda *args, **kwargs: FakeResponse(),
    )
    assert resolve_line_user_id(
        id_token="signed-token",
        development_user_id="U-forged",
    ) == "U-trusted"


@pytest.mark.parametrize(
    ("filename", "page_id"),
    [
        ("gateway.html", "gateway"),
        ("bind.html", "bind"),
        ("register.html", "registration"),
        ("union_staff_binding.html", "union_staff_binding"),
    ],
)
def test_liff_pages_consume_public_runtime_config(filename: str, page_id: str):
    source = (ROOT / "line" / "static" / filename).read_text(encoding="utf-8")
    assert f"/api/config/liff/runtime?page={page_id}" in source
    assert "textContent" in source


def test_bind_and_registration_send_id_token_to_backend():
    bind = (ROOT / "line" / "static" / "bind.html").read_text(encoding="utf-8")
    register = (ROOT / "line" / "static" / "register.html").read_text(encoding="utf-8")
    assert "liff.getIDToken()" in bind
    assert "line_id_token: currentIdToken" in bind
    assert "idToken=" not in bind
    assert "fetch('/api/line/client-info'," in bind
    assert "liff.getIDToken()" in register
    assert "line_id_token: currentIdToken" in register


def test_gateway_routes_staff_verification_after_shared_liff_login():
    gateway = (ROOT / "line" / "static" / "gateway.html").read_text(encoding="utf-8")
    staff_page = (ROOT / "line" / "static" / "staff_verification.html").read_text(encoding="utf-8")

    assert "liff.state" in gateway
    assert "'union-staff-binding'" in gateway
    assert "'/staff-verification-page'" in gateway
    assert "/union-staff-binding-page" in gateway
    assert "viaLiff" in gateway
    assert "LINE 登入狀態已失效" in staff_page


def test_union_staff_binding_liff_keeps_password_out_of_urls_and_uses_id_token():
    source = (ROOT / "line" / "static" / "union_staff_binding.html").read_text(
        encoding="utf-8"
    )

    assert "liff.getIDToken()" in source
    assert "line_id_token: idToken" in source
    assert "development_line_user_id: developmentUserId" in source
    assert "password: document.getElementById('password').value" in source
    assert "?password=" not in source


def test_liff_manager_uses_service_staff_friendly_labels():
    source = (ROOT / "ui/components/line_liff_manager.py").read_text(encoding="utf-8")

    assert "選項 JSON" not in source
    assert "版本紀錄與還原" not in source
    assert "LIFF 頁面" not in source
