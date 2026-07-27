"""
================================================================================
檔案名稱: ui/pages/05_form_management.py
功能說明: 表單與履歷問卷管理專頁頁面殼層 (FormManagementUI)
================================================================================
"""

import os
import requests
import streamlit as st

from ui.pages.form_management.shared import (
    DB_TABLE_FIELDS,
    safe_int,
    load_json_templates,
)
from ui.pages.form_management.tab1_form_builder import _render_tab1_form_builder
from ui.pages.form_management.tab2_template_library import _render_tab2_template_library
from ui.pages.form_management.tab3_contract_management import _render_tab3_contract_management

title = "📋 表單與履歷問卷管理"


def _render_form_management_page_shell(form_db_table_fields, field_types, field_widths, global_stats, target_order, form_table_for_key):
    """Render FormManagementUI's 3 fixed tabs."""
    tab1, tab2, tab3 = st.tabs([
        "➕ 1. 手動創建與設計新表單 (UX 實驗室)",
        "🗄️ 2. 自訂表單模板庫與 5:5 雙視窗線上編輯預覽",
        "📜 3. 制式定型化契約管理 (EPPP 變數代理引擎)"
    ])

    with tab1:
        _render_tab1_form_builder(form_db_table_fields, field_types, field_widths, global_stats, target_order)

    with tab2:
        _render_tab2_template_library(form_db_table_fields, field_types, field_widths, global_stats, target_order)

    with tab3:
        _render_tab3_contract_management(form_db_table_fields, form_table_for_key, global_stats, target_order)


def show():
    """FormManagementUI 進入點 (5:5 雙視窗 Side-by-Side + 拖拉排序 + 二次確認刪除)"""
    st.title("📋 表單與履歷問卷管理專區")

    form_db_table_fields = {table_name: dict(fields) for table_name, fields in DB_TABLE_FIELDS.items()}
    order_table_name = "orders (訂單主表 - 36 大業務與金額 calculations)"
    form_db_table_fields[order_table_name] = {
        key: label
        for key, label in form_db_table_fields[order_table_name].items()
        if not (key.startswith("subsidy") and key.endswith("eligibility"))
    }
    form_db_table_fields["clients (客戶主表 - 個人基本資料與市府申請表)"]["identity_status"] = "身分資格（唯讀） (identity_status)"

    def form_table_for_key(db_key: str) -> str:
        for table_name, fields in form_db_table_fields.items():
            if db_key in fields:
                return table_name
        return next(iter(form_db_table_fields))

    base_url = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")

    try:
        resp_orders = requests.get(f"{base_url}/api/v1/orders", timeout=10)
        resp_orders.raise_for_status()
        orders_payload = resp_orders.json()
        orders_data = orders_payload.get("data") if isinstance(orders_payload, dict) and orders_payload.get("success") else []
        if not isinstance(orders_data, list):
            orders_data = []
    except Exception as e:
        st.error(f"讀取訂單 API 失敗: {e}")
        orders_data = []

    global_stats = {
        "global_active_orders_count": len([o for o in orders_data if o.get('order_status') in ['服務中', '訂單成立']]),
        "global_active_staff_count": len(set([o['staff_name'] for o in orders_data if o.get('staff_name')])),
        "global_subsidy_orders_count": len([
            o for o in orders_data
            if o.get('identity_status') not in {None, '', '一般身分', '一般市民'}
        ]),
        "global_total_receivable_sum": sum([safe_int(o.get('total_employer_self_pay_payable')) for o in orders_data]),
        "global_govt_claim_count": len([o for o in orders_data if o.get('govt_claim_date')])
    }

    col_scope, col_order = st.columns([1.5, 2.5])
    with col_scope:
        scope_mode = st.radio("⚙️ 選擇表單連動作用域", ["🎯 特定單筆案件 (契約/個人單據)", "📊 全域/多案件統計模式 (週報/統計表)"], horizontal=True, key="sbs_scope_mode")
    
    target_order = None
    with col_order:
        if "特定單筆案件" in scope_mode and orders_data:
            order_opts = {
                f"案件 #{o['case_no']} - 客戶: {o['client_name']} [{o['order_status']}] (月嫂: {o.get('staff_name') or '尚未指派'})": o['case_no']
                for o in orders_data
            }
            sel_label = st.selectbox("🎯 選擇連動測試的訂單案件", list(order_opts.keys()), key="sbs_order_picker")
            target_case_no = order_opts[sel_label]
            target_order = next((o for o in orders_data if o['case_no'] == target_case_no), None)
            if target_order:
                try:
                    resp_clients = requests.get(f"{base_url}/api/v1/clients", timeout=10)
                    resp_clients.raise_for_status()
                    clients_payload = resp_clients.json()
                    client_rows = clients_payload.get("data") if isinstance(clients_payload, dict) and clients_payload.get("success") else []
                    if isinstance(client_rows, list):
                        client_row = next((row for row in client_rows if row.get('case_no') == target_case_no), None)
                        if client_row:
                            target_order = {
                                **target_order,
                                **{
                                    key: client_row.get(key, '')
                                    for key in ('service_time', 'service_type', 'delivery_type', 'residence_type', 'city', 'identity_status')
                                },
                            }
                except Exception:
                    pass

        else:
            st.info("💡 目前切換為「全域/多案件統計模式」，無須鎖定單一訂單。")

    st.session_state['custom_form_templates'] = load_json_templates()

    st.markdown("---")

    field_widths = {
        "half": "半寬 (50% 雙欄並排)",
        "full": "全寬 (100% 單獨一列)"
    }
    field_types = {
        "text": "單行文字輸入",
        "textarea": "多行備註區域",
        "number": "數字/金額數值",
        "date": "日期選擇器",
        "db_link": "⚡ 連動 DB 欄位 (支援單筆與全域統計)"
    }

    _render_form_management_page_shell(
        form_db_table_fields, field_types, field_widths, global_stats, target_order, form_table_for_key
    )
