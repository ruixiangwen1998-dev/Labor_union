import ast
from pathlib import Path


def _render_editor_source() -> str:
    source_path = Path("ui/pages/order/editor.py")
    source = source_path.read_text(encoding="utf-8")
    module = ast.parse(source)
    render_editor = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "render_editor"
    )
    return ast.get_source_segment(source, render_editor) or ""


def test_edit_order_removes_assignment_sync_and_only_saves_basic_order_details():
    render_source = _render_editor_source()

    assert "/assignment-synchronization/preview" not in render_source
    assert "/assignment-synchronization/apply" not in render_source
    assert "/assignment-schedules" not in render_source
    assert '"remove_schedule_ids"' not in render_source
    assert "/full-details" in render_source
    assert '"儲存訂單基本資料"' in render_source
    assert "db_service.update_order_full_details" not in render_source
    assert "db_service.update_order_status" not in render_source


def test_edit_order_does_not_keep_operator_or_schedule_removal_controls():
    render_source = _render_editor_source()

    assert "applied_by" not in render_source
    assert "selected_removal_ids" not in render_source
    assert "required_schedule_removals" not in render_source


def test_edit_order_keeps_staffing_sensitive_fields_read_only_and_out_of_basic_update():
    render_source = _render_editor_source()

    assert "clients.identity_status" not in render_source
    assert "client_identity_status = target_order.get('identity_status')" in render_source
    assert '"身分資格（唯讀）"' in render_source
    save_payload = render_source.split(
        'f"/api/v1/orders/{target_case_no}/full-details"',
        1,
    )[1]
    assert '"client_name": w_client_name.strip() or None' in save_payload
    for field in (
        '"service_days"',
        '"service_hours_per_day"',
        '"floor_fee"',
        '"start_date"',
        '"actual_start_date"',
        '"end_date"',
        '"deposit_date"',
    ):
        assert field not in save_payload
    assert render_source.count("disabled=True") >= 10
    assert '"儲存訂單基本資料"' in render_source
