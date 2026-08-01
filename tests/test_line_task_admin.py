import os
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from api.main import app
from ui.components.line_schedule_manager import _build_schedule_payload, _preview_rows


ROOT = Path(__file__).resolve().parents[1]


def test_schedule_builder_sorts_steps_and_previews_dates():
    config = {
        "version": 1,
        "timezone": "Asia/Taipei",
        "schedules": [
            {
                "id": "new_user_onboarding",
                "name": "三日引導",
                "enabled": True,
                "trigger": "follow",
                "restart_on_refollow": False,
                "steps": [{"day": 1, "send_time": "10:00", "template_id": "d1"}],
            }
        ],
    }
    rows = pd.DataFrame(
        [
            {"day": 3, "send_time": "10:00", "template_id": "d3"},
            {"day": 1, "send_time": "09:30", "template_id": "d1"},
        ]
    )
    payload = _build_schedule_payload(
        config=config,
        schedule_id="new_user_onboarding",
        timezone_name="Asia/Taipei",
        name="三日引導",
        enabled=True,
        restart_on_refollow=True,
        rows=rows,
    )

    assert [step["day"] for step in payload["schedules"][0]["steps"]] == [1, 3]
    assert payload["schedules"][0]["restart_on_refollow"] is True
    assert len(_preview_rows(payload, "new_user_onboarding")) == 2


def test_task_ui_does_not_add_fixed_polling():
    source = (ROOT / "ui/components/line_task_manager.py").read_text(encoding="utf-8")

    assert "time.sleep" not in source
    assert "autorefresh" not in source.lower()
    assert "st.rerun" in source


def test_schema_contains_task_attempt_history():
    schema = (ROOT / "db/schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS line_task_attempts" in schema
    assert "UNIQUE KEY uk_line_task_attempt_no" in schema


def test_schedule_api_rejects_stale_revision_without_writing():
    old_values = {
        name: os.environ.get(name)
        for name in ("APP_ENV", "ENABLE_ADMIN_AUTH", "INTERNAL_API_KEY")
    }
    os.environ["APP_ENV"] = "development"
    os.environ["ENABLE_ADMIN_AUTH"] = "false"
    os.environ["INTERNAL_API_KEY"] = "stage-5-3-schedule-test-key"
    headers = {"X-Internal-API-Key": "stage-5-3-schedule-test-key"}
    client = TestClient(app)
    try:
        state_response = client.get(
            "/api/config/message-schedules/state", headers=headers
        )
        assert state_response.status_code == 200
        config = state_response.json()["config"]
        stale_response = client.put(
            "/api/config/message-schedules",
            headers={**headers, "If-Match": "stale-revision"},
            json=config,
        )
        assert stale_response.status_code == 409
    finally:
        for name, value in old_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_task_manager_hides_internal_identifiers_from_service_staff():
    source = (ROOT / "ui/components/line_task_manager.py").read_text(encoding="utf-8")

    assert "LINE User ID 包含" not in source
    assert "st.json(task)" not in source
    assert "重新整理任務" not in source
