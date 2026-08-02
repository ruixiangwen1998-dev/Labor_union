import json
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from api.main import app
from api.routes.line_system_config import _template_schedule_references
from api.schemas.line_config import MessageTemplatesConfig
from services.json_config_service import read_config
from ui.components.line_message_manager import _payload_from_form


ROOT = Path(__file__).resolve().parents[1]


def test_onboarding_template_reports_schedule_reference():
    references = _template_schedule_references("new_user_d1")

    assert references
    assert references[0]["schedule_id"] == "new_user_onboarding"
    assert references[0]["day"] == 1


def test_ui_payload_builder_parses_text_variables():
    payload = _payload_from_form(
        template_id="hello_member",
        name="會員問候",
        category="webhook_reply",
        message_type="text",
        enabled=True,
        content_source="您好 {name}",
        usage=["webhook"],
        variable_rows=pd.DataFrame(
            [{"name": "name", "required": True, "description": "會員姓名"}]
        ),
    )

    assert payload["content"] == "您好 {name}"
    assert payload["variables"][0]["name"] == "name"


def test_ui_payload_builder_parses_flex_json():
    payload = _payload_from_form(
        template_id="flex_card",
        name="Flex 卡片",
        category="push",
        message_type="flex",
        enabled=True,
        content_source=json.dumps({"type": "bubble", "body": {"type": "box"}}),
        usage=["push"],
        variable_rows=pd.DataFrame(columns=["name", "required", "description"]),
    )

    assert payload["content"]["type"] == "bubble"


def test_message_template_state_and_stale_update_protection(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENABLE_ADMIN_AUTH", "false")
    monkeypatch.setenv("INTERNAL_API_KEY", "message-management-test-key")
    client = TestClient(app)
    headers = {"X-Internal-API-Key": "message-management-test-key"}

    state_response = client.get("/api/config/message-templates/state", headers=headers)
    assert state_response.status_code == 200
    state = state_response.json()
    assert len(state["revision"]) == 64

    template = state["config"]["templates"][0]
    stale_response = client.put(
        f"/api/config/message-templates/{template['id']}",
        headers={**headers, "If-Match": "0" * 64},
        json=template,
    )
    assert stale_response.status_code == 409


def test_line_management_page_uses_real_message_component():
    source = (ROOT / "ui/pages/07_line_management.py").read_text(encoding="utf-8")

    assert "render_message_manager(client, token, profile)" in source


def test_message_manager_hides_engineering_fields_from_service_staff():
    source = (ROOT / "ui/components/line_message_manager.py").read_text(encoding="utf-8")

    assert "範本 ID" not in source
    assert "預覽變數（JSON）" not in source
    assert "範本變數" not in source


def test_union_staff_quick_menu_templates_cover_three_audiences():
    config = read_config("message_templates", MessageTemplatesConfig)
    quick_templates = [
        template for template in config.templates if template.quick_menu_enabled
    ]

    assert {template.quick_menu_audience for template in quick_templates} >= {
        "customer",
        "staff",
        "group_help",
    }
    assert all(template.enabled for template in quick_templates)
