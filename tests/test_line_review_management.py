"""Stage 5.6 LINE artificial review workflow tests."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from services.admin_auth_service import create_admin_user
from services.db_service import get_connection
from services.line_review_service import (
    LineReviewDataConflictError,
    LineReviewStateConflictError,
    approve_line_review,
    get_line_review,
    list_line_reviews,
    reject_line_review,
)

ROOT = Path(__file__).resolve().parents[1]


def _insert_staff_review(user_id: str) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            staff_name = f"review-{user_id[-32:]}"
            cursor.execute(
                "INSERT INTO staff (name,identity_card,birthday) VALUES (%s,%s,'1990-01-01')",
                (staff_name, f"T1{uuid.uuid4().int % 100000000:08d}"),
            )
            staff_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO line_users (line_user_id,role,status) VALUES (%s,'customer','active')",
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO line_confirmation_requests (
                    request_type,line_user_id,matched_staff_id,match_status,
                    submitted_name,submitted_birthday,submitted_identity_last4,submitted_at
                ) VALUES ('staff_verification',%s,%s,'matched',%s,'1990-01-01','0000',UTC_TIMESTAMP())
                """,
                (user_id, staff_id, staff_name),
            )
            request_id = int(cursor.lastrowid)
        conn.commit()
        return request_id
    finally:
        conn.close()


def _cleanup_review(request_id: int, user_ids: list[str], client_id: int | None = None) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM line_tasks WHERE idempotency_key LIKE %s",
                (f"%-review-%:{request_id}",),
            )
            cursor.execute(
                "DELETE FROM line_tasks WHERE idempotency_key LIKE %s",
                (f"client-rebind-%:{request_id}",),
            )
            cursor.execute("DELETE FROM line_confirmation_requests WHERE id=%s", (request_id,))
            for user_id in user_ids:
                cursor.execute(
                    "DELETE FROM staff WHERE line_user_id=%s OR name=%s",
                    (user_id, f"review-{user_id[-32:]}"),
                )
                cursor.execute("DELETE FROM line_users WHERE line_user_id=%s", (user_id,))
            if client_id:
                cursor.execute("DELETE FROM clients WHERE id=%s", (client_id,))
        conn.commit()
    finally:
        conn.close()


def test_staff_approval_records_reason_switches_role_and_is_idempotent():
    user_id = f"U-review-{uuid.uuid4().hex}"
    request_id = _insert_staff_review(user_id)
    try:
        result = approve_line_review(
            request_id,
            admin_user_id=None,
            reason="資格文件已人工核對",
        )
        assert result["status"] == "approved"
        detail = get_line_review(request_id)
        assert detail["status"] == "approved"
        assert detail["decision_reason"] == "資格文件已人工核對"
        assert detail["current_line_role"] == "staff"
        with pytest.raises(LineReviewStateConflictError):
            approve_line_review(request_id, admin_user_id=None)

        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS total FROM line_tasks WHERE idempotency_key=%s",
                    (f"staff-review-approved:{request_id}",),
                )
                assert int(cursor.fetchone()["total"]) == 1
        finally:
            conn.close()
    finally:
        _cleanup_review(request_id, [user_id])


def test_staff_rejection_uses_rejected_status_and_requires_reason():
    user_id = f"U-review-{uuid.uuid4().hex}"
    request_id = _insert_staff_review(user_id)
    try:
        with pytest.raises(ValueError, match="必須填寫原因"):
            reject_line_review(request_id, admin_user_id=None, reason="")
        result = reject_line_review(
            request_id,
            admin_user_id=None,
            reason="尚未完成資格查核",
        )
        assert result["status"] == "rejected"
        detail = get_line_review(request_id)
        assert detail["status"] == "rejected"
        assert detail["decision_reason"] == "尚未完成資格查核"
    finally:
        _cleanup_review(request_id, [user_id])


def test_rebind_approval_rechecks_current_binding_before_overwrite():
    suffix = uuid.uuid4().hex
    old_user_id = f"U-old-{suffix}"
    changed_user_id = f"U-changed-{suffix}"
    new_user_id = f"U-new-{suffix}"
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO clients (name,line_user_id) VALUES ('審查測試客戶',%s)",
                (old_user_id,),
            )
            client_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO line_confirmation_requests (
                    request_type,line_user_id,client_id,client_name,
                    old_line_user_id,new_line_user_id
                ) VALUES ('client_rebind',%s,%s,'審查測試客戶',%s,%s)
                """,
                (new_user_id, client_id, old_user_id, new_user_id),
            )
            request_id = int(cursor.lastrowid)
            cursor.execute(
                "UPDATE clients SET line_user_id=%s WHERE id=%s",
                (changed_user_id, client_id),
            )
        conn.commit()
    finally:
        conn.close()
    try:
        with pytest.raises(LineReviewDataConflictError, match="申請後變更"):
            approve_line_review(request_id, admin_user_id=None)
        assert get_line_review(request_id)["status"] == "pending"
    finally:
        _cleanup_review(request_id, [], client_id)


def test_review_api_in_development_bypass_and_list_is_masked():
    user_id = f"U-review-{uuid.uuid4().hex}"
    request_id = _insert_staff_review(user_id)
    old_values = {
        name: os.environ.get(name)
        for name in ("APP_ENV", "ENABLE_ADMIN_AUTH", "INTERNAL_API_KEY")
    }
    os.environ["APP_ENV"] = "development"
    os.environ["ENABLE_ADMIN_AUTH"] = "false"
    os.environ["INTERNAL_API_KEY"] = "stage-5-6-review-key"
    headers = {"X-Internal-API-Key": "stage-5-6-review-key"}
    client = TestClient(app)
    try:
        assert client.get(
            "/api/v1/line/review-requests/summary", headers=headers
        ).status_code == 200
        listed = client.get(
            "/api/v1/line/review-requests",
            headers=headers,
            params={"search": request_id},
        )
        assert listed.status_code == 200
        item = listed.json()["data"]["items"][0]
        assert item["line_user_id_masked"] != user_id
        assert client.get(
            f"/api/v1/line/review-requests/{request_id}", headers=headers
        ).status_code == 200
        missing_reason = client.post(
            f"/api/v1/line/review-requests/{request_id}/reject",
            headers=headers,
            json={"reason": ""},
        )
        assert missing_reason.status_code == 422
        rejected = client.post(
            f"/api/v1/line/review-requests/{request_id}/reject",
            headers=headers,
            json={"reason": "API 測試拒絕"},
        )
        assert rejected.status_code == 200
    finally:
        _cleanup_review(request_id, [user_id])
        for name, value in old_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_legacy_console_endpoints_use_the_same_review_service():
    user_id = f"U-review-{uuid.uuid4().hex}"
    request_id = _insert_staff_review(user_id)
    old_key = os.environ.get("INTERNAL_API_KEY")
    os.environ["INTERNAL_API_KEY"] = "stage-5-6-console-key"
    headers = {"X-Internal-API-Key": "stage-5-6-console-key"}
    client = TestClient(app)
    try:
        listed = client.get(
            "/api/line/staff/review-requests",
            headers=headers,
            params={"request_type": "staff_verification"},
        )
        assert listed.status_code == 200
        target = next(
            item
            for item in listed.json()["data"]
            if int(item["request_id"]) == request_id
        )
        assert target["details"]["line_user_id"] == user_id
        wrong_legacy_route = client.post(
            "/api/line/rebind_requests/approve",
            headers=headers,
            json={"request_id": str(request_id)},
        )
        assert wrong_legacy_route.status_code == 409
        rejected = client.post(
            f"/api/line/staff/review-requests/staff_verification/{request_id}/reject",
            headers=headers,
        )
        assert rejected.status_code == 200
        detail = get_line_review(request_id)
        assert detail["status"] == "rejected"
        assert detail["decision_reason"] == "開發終端拒絕"
    finally:
        _cleanup_review(request_id, [user_id])
        if old_key is None:
            os.environ.pop("INTERNAL_API_KEY", None)
        else:
            os.environ["INTERNAL_API_KEY"] = old_key


def test_review_filters_and_ui_have_no_fixed_polling():
    user_id = f"U-review-{uuid.uuid4().hex}"
    request_id = _insert_staff_review(user_id)
    try:
        listed = list_line_reviews(
            request_type="staff_verification",
            status="pending",
            search=str(request_id),
        )
        assert any(int(item["id"]) == request_id for item in listed["items"])
    finally:
        _cleanup_review(request_id, [user_id])

    source = (ROOT / "ui" / "components" / "line_review_manager.py").read_text(
        encoding="utf-8"
    )
    page = (ROOT / "ui" / "pages" / "07_line_management.py").read_text(
        encoding="utf-8"
    )
    assert "time.sleep" not in source
    assert "autorefresh" not in source.lower()
    assert "render_review_manager(client, token, profile)" in page


def test_schema_contains_reviewer_metadata_and_replayable_migration():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    migration = (
        ROOT / "db" / "schema_parts" / "97_line_confirmation_review.sql"
    ).read_text(encoding="utf-8")
    assert "reviewed_by_admin_user_id" in schema
    assert "decision_reason" in schema
    assert "INFORMATION_SCHEMA.COLUMNS" in migration
    assert "fk_confirmation_admin_reviewer" in migration


def test_review_manager_hides_raw_line_identifier_labels():
    source = (ROOT / "ui/components/line_review_manager.py").read_text(encoding="utf-8")

    assert "LINE User ID" not in source


def test_line_review_and_task_pagination_buttons_use_distinct_keys():
    review_source = (ROOT / "ui/components/line_review_manager.py").read_text(
        encoding="utf-8"
    )
    task_source = (ROOT / "ui/components/line_task_manager.py").read_text(
        encoding="utf-8"
    )

    expected_keys = {
        'key="line_review_previous_page"',
        'key="line_review_next_page"',
        'key="line_task_previous_page"',
        'key="line_task_next_page"',
    }
    combined_source = review_source + task_source
    assert all(key in combined_source for key in expected_keys)
