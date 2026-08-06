"""Tests for order LINE group commands, transient invite payloads, and schema."""

from pathlib import Path

import pytest

from services.line_order_group_service import (
    INVITE_REDACTED_VALUE,
    LineOrderGroupError,
    _validate_invite_url,
    build_invite_flex_message,
    redact_invite_event,
    redact_invite_text,
    sanitize_task_for_output,
)


ROOT = Path(__file__).resolve().parents[1]


def test_invite_command_is_redacted_from_logs_and_webhook_payload() -> None:
    command = "發送邀請連結 https://line.me/ti/g/secret-token"
    assert redact_invite_text(command) == f"發送邀請連結 {INVITE_REDACTED_VALUE}"
    event = {"message": {"type": "text", "text": command}}
    sanitized = redact_invite_event(event)
    assert "secret-token" not in str(sanitized)
    assert event["message"]["text"] == command


def test_unprefixed_url_is_not_treated_as_an_invite_command() -> None:
    text = "https://line.me/ti/g/secret-token"
    assert redact_invite_text(text) == text


@pytest.mark.parametrize(
    "url",
    [
        "http://line.me/ti/g/token",
        "https://example.com/ti/g/token",
        "https://line.me/not-a-group/token",
        "https://line.me/ti/g/token?redirect=evil",
    ],
)
def test_only_line_group_invite_urls_are_allowed(url: str) -> None:
    with pytest.raises(LineOrderGroupError):
        _validate_invite_url(url)


def test_worker_builds_flex_card_but_admin_output_hides_url() -> None:
    task = {
        "id": 1,
        "task_type": "order_group_invite",
        "payload_json": (
            '{"case_no":"115000001","participant_type":"client",'
            '"invite_url":"https://line.me/ti/g/secret-token"}'
        ),
    }
    messages = build_invite_flex_message(task)
    assert messages[0]["type"] == "flex"
    assert messages[0]["contents"]["footer"]["contents"][0]["action"]["uri"].endswith(
        "secret-token"
    )
    safe_task = sanitize_task_for_output(task)
    assert "secret-token" not in safe_task["payload_json"]
    assert INVITE_REDACTED_VALUE in safe_task["payload_json"]


def test_schema_contains_order_group_lifecycle_tables() -> None:
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    migration = (ROOT / "db" / "schema_parts" / "107_line_order_groups.sql").read_text(
        encoding="utf-8"
    )
    for content in (schema, migration):
        assert "line_order_group_bindings" in content
        assert "line_order_group_members" in content
        assert "uk_orders_line_group_id" in content
