import io
import os
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from api.main import app
from api.schemas.line_config import LineMenusConfig, RichMenuDefinition
from services.json_config_service import read_config
from services.line_rich_menu_service import build_line_action
from services.media_storage_service import (
    MediaValidationError,
    normalize_uploaded_rich_menu_image,
    render_rich_menu_image,
)


ROOT = Path(__file__).resolve().parents[1]


def test_three_role_menu_config_and_union_staff_portal_preview():
    config = read_config("line_menus", LineMenusConfig)
    assert {menu.audience_role for menu in config.menus} == {
        "customer",
        "staff",
        "union_staff",
    }
    union_menus = [
        menu for menu in config.menus if menu.audience_role == "union_staff"
    ]
    assert len(union_menus) == 1
    assert union_menus[0].id == "union_staff_portal_menu"
    assert union_menus[0].menu_group_id is None
    assert union_menus[0].rich_menu_alias_id is None
    assert not union_menus[0].is_group_entry
    assert {button.id for button in union_menus[0].buttons} == {
        "system_status",
        "orders",
        "staff_schedule",
        "reviews",
        "message_center",
    }
    assert all(
        button.action.type != "richmenuswitch"
        for button in union_menus[0].buttons
    )

    content = render_rich_menu_image(union_menus[0].model_dump(mode="json"))
    with Image.open(io.BytesIO(content)) as image:
        assert image.size == (2500, 1686)
        assert image.format == "JPEG"
    assert len(content) <= 1024 * 1024


def test_rich_menu_switch_action_uses_line_alias():
    action = build_line_action(
        {
            "type": "richmenuswitch",
            "rich_menu_alias_id": "union-staff-portal",
            "data": "tab=portal",
        }
    )

    assert action == {
        "type": "richmenuswitch",
        "richMenuAliasId": "union-staff-portal",
        "data": "tab=portal",
    }


def test_menu_validation_rejects_overlap_and_unsafe_uri():
    config = read_config("line_menus", LineMenusConfig)
    payload = config.menus[0].model_dump(mode="json")
    payload["buttons"][1]["bounds"]["x"] = 1000
    try:
        RichMenuDefinition.model_validate(payload)
    except ValueError as exc:
        assert "overlap" in str(exc)
    else:
        raise AssertionError("overlapping buttons must be rejected")

    payload = config.menus[0].model_dump(mode="json")
    payload["buttons"][0]["action"] = {
        "type": "uri",
        "uri_source": "literal",
        "uri": "javascript:alert(1)",
        "text": None,
        "data": None,
    }
    try:
        RichMenuDefinition.model_validate(payload)
    except ValueError as exc:
        assert "http or https" in str(exc)
    else:
        raise AssertionError("unsafe URI scheme must be rejected")


def test_uploaded_image_validation_checks_real_dimensions():
    output = io.BytesIO()
    Image.new("RGB", (100, 100), "red").save(output, format="PNG")
    try:
        normalize_uploaded_rich_menu_image(
            output.getvalue(), expected_width=2500, expected_height=843
        )
    except MediaValidationError as exc:
        assert "2500x843" in str(exc)
    else:
        raise AssertionError("wrong image dimensions must be rejected")


def test_line_menu_state_rejects_stale_revision():
    old = {name: os.environ.get(name) for name in ("APP_ENV", "ENABLE_ADMIN_AUTH", "INTERNAL_API_KEY")}
    os.environ["APP_ENV"] = "development"
    os.environ["ENABLE_ADMIN_AUTH"] = "false"
    os.environ["INTERNAL_API_KEY"] = "stage-5-4-test-key"
    client = TestClient(app)
    headers = {"X-Internal-API-Key": "stage-5-4-test-key"}
    try:
        state = client.get("/api/config/line-menus/state", headers=headers)
        assert state.status_code == 200
        response = client.put(
            "/api/config/line-menus",
            headers={**headers, "If-Match": "0" * 64},
            json=state.json()["config"],
        )
        assert response.status_code == 409
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_rich_menu_ui_has_no_fixed_polling():
    source = (ROOT / "ui/components/line_rich_menu_manager.py").read_text(encoding="utf-8")
    page = (ROOT / "ui/pages/07_line_management.py").read_text(encoding="utf-8")
    assert "time.sleep" not in source
    assert "autorefresh" not in source.lower()
    assert "render_rich_menu_manager(client, token, profile)" in page


def test_rich_menu_manager_hides_line_engineering_fields():
    source = (ROOT / "ui/components/line_rich_menu_manager.py").read_text(encoding="utf-8")

    assert "LINE Menu ID" not in source
    assert "Postback Data" not in source
    assert "儲存草稿" not in source
