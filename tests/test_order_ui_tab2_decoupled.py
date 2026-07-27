"""
================================================================================
檔案名稱: tests/test_order_ui_tab2_decoupled.py
功能說明: 驗證 OrderUI_Tab2_Assign REST API 全面解耦與無 db_service 引用
================================================================================
"""

from pathlib import Path
import ast


def _assign_source() -> str:
    text = Path("ui/pages/order/tab2_assign.py").read_text(encoding="utf-8")
    module = ast.parse(text)
    render_fn = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_render_tab2_assign"
    )
    return ast.get_source_segment(text, render_fn) or ""


def test_order_ui_tab2_decoupled_from_db_service():
    """驗證 ui/pages/order/tab2_assign.py 完全解耦，不再匯入 db_service"""
    file_content = Path("ui/pages/order/tab2_assign.py").read_text(encoding="utf-8")
    assert "from services import db_service" not in file_content
    assert "import db_service" not in file_content


def test_order_ui_tab2_uses_rest_api():
    """驗證 ui/pages/order/tab2_assign.py 使用 REST API 端點"""
    file_content = Path("ui/pages/order/tab2_assign.py").read_text(encoding="utf-8")
    assert "/api/v1/orders/" in file_content
    assert "/matches/recommend-staff" in file_content


def test_tab2_uses_synchronization_send_resume_endpoints():
    file_content = _assign_source()
    assert "/api/v1/matches/" in file_content and "send-resume" in file_content
    assert "assignment-synchronization/preview" in file_content
    assert "assignment-synchronization/apply" in file_content
    assert "/api/v1/orders/{case_no}/assign-staff" not in file_content


def test_tab2_clears_sync_state_after_apply():
    file_content = _assign_source()
    assert 'f"remove_schedule_{target_case_no}"' in file_content
    assert 'f"assignment_sync_applied_by_{target_case_no}"' in file_content
    assert 'f"assignment_sync_confirm_{target_case_no}"' in file_content
    assert "st.session_state.pop(f\"remove_schedule_{target_case_no}\", None)" in file_content
    assert "st.session_state.pop(f\"assignment_sync_applied_by_{target_case_no}\", None)" in file_content
    assert "st.session_state.pop(f\"assignment_sync_confirm_{target_case_no}\", None)" in file_content
    assert "st.session_state[\"tab2_assignment_sync_success\"]" in file_content
