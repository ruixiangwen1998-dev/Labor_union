"""工會人員後台帳號與 LINE 一次性綁定流程測試。"""

from __future__ import annotations

import uuid

import pytest

from services.admin_auth_service import create_admin_user
from services.db_service import get_connection
from services.line_admin_binding_service import (
    LineAdminBindingAuthenticationError,
    build_line_admin_binding_url,
    complete_line_admin_binding,
    issue_line_admin_binding_token,
)


def test_admin_binding_url_reuses_gateway_liff(monkeypatch):
    monkeypatch.delenv("LINE_ADMIN_BINDING_LIFF_ID", raising=False)
    monkeypatch.setenv("LINE_LIFF_ID", "123456-sharedLiff")

    url = build_line_admin_binding_url("secret token")

    assert url.startswith("https://liff.line.me/123456-sharedLiff?")
    assert "target=union-staff-binding" in url
    assert "token=secret%20token" in url


def test_admin_binding_verifies_password_and_updates_both_identities(monkeypatch):
    suffix = uuid.uuid4().hex
    line_user_id = f"U-admin-binding-{suffix}"
    username = f"binding-{suffix}"
    password = "Strong-development-password-123"
    admin_id = create_admin_user(
        username=username,
        password=password,
        display_name="綁定測試管理員",
        role="line_agent",
    )
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            request_id, token = issue_line_admin_binding_token(
                cursor, line_user_id=line_user_id
            )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LIFF_REQUIRE_ID_TOKEN", "false")
    monkeypatch.setattr(
        "services.line_admin_binding_service._load_union_staff_rich_menu_id",
        lambda: "richmenu-test-union-staff",
    )
    try:
        with pytest.raises(LineAdminBindingAuthenticationError):
            complete_line_admin_binding(
                token=token,
                username=username,
                password="wrong-password",
                line_id_token="",
                development_line_user_id=line_user_id,
            )

        result = complete_line_admin_binding(
            token=token,
            username=username,
            password=password,
            line_id_token="",
            development_line_user_id=line_user_id,
        )
        assert result["status"] == "completed"
        assert result["role"] == "line_agent"

        with pytest.raises(LineAdminBindingAuthenticationError):
            complete_line_admin_binding(
                token=token,
                username=username,
                password=password,
                line_id_token="",
                development_line_user_id=f"U-other-{suffix}",
            )

        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT linked_line_user_id,role FROM admin_users WHERE id=%s",
                    (admin_id,),
                )
                admin = cursor.fetchone()
                assert admin["linked_line_user_id"] == line_user_id
                assert admin["role"] == "line_agent"
                cursor.execute(
                    "SELECT role FROM line_users WHERE line_user_id=%s",
                    (line_user_id,),
                )
                assert cursor.fetchone()["role"] == "union_staff"
                cursor.execute(
                    "SELECT status,attempt_count FROM line_admin_binding_requests WHERE id=%s",
                    (request_id,),
                )
                binding = cursor.fetchone()
                assert binding["status"] == "completed"
                assert int(binding["attempt_count"]) == 2
                cursor.execute(
                    "SELECT task_type,payload_json FROM line_tasks WHERE idempotency_key=%s",
                    (f"admin-line-binding:{request_id}",),
                )
                assert cursor.fetchone()["task_type"] == "rich_menu_link"
        finally:
            conn.close()
    finally:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM line_tasks WHERE idempotency_key=%s",
                    (f"admin-line-binding:{request_id}",),
                )
                cursor.execute(
                    "DELETE FROM admin_audit_logs WHERE admin_user_id=%s",
                    (admin_id,),
                )
                cursor.execute(
                    "DELETE FROM line_admin_binding_requests WHERE id=%s",
                    (request_id,),
                )
                cursor.execute("DELETE FROM admin_users WHERE id=%s", (admin_id,))
                cursor.execute(
                    "DELETE FROM line_users WHERE line_user_id=%s",
                    (line_user_id,),
                )
            conn.commit()
        finally:
            conn.close()
