"""
================================================================================
檔案名稱: ui/pages/02_orders.py
功能說明: 訂單與帳務管理系統頁面殼層 (OrderUI)
================================================================================
"""

import os
import requests
import streamlit as st

from ui.pages.order.tab1_overview import _render_tab1_overview
from ui.pages.order.tab2_assign import _render_tab2_assign
from ui.pages.order.tab3_finance import _render_tab3_finance
from ui.pages.order.tab4_accounts_payable import _render_tab4_accounts_payable
from ui.pages.order.tab5_subsidy_reconciliation import _render_tab5_subsidy_reconciliation

title = "📦 訂單與帳務管理系統"


def _render_order_page_shell(orders_data, clients, staff_list):
    """Render Page 2's fixed tab layout from data loaded by ``show``."""
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 訂單資訊總覽",
        "🤝 月嫂配對中心",
        "💰 訂單帳務總覽",
        "📤 應付帳款查詢/輸出",
        "核銷補助清冊",
    ])

    with tab1:
        _render_tab1_overview(orders_data)

    with tab2:
        _render_tab2_assign(orders_data, clients, staff_list)

    with tab3:
        _render_tab3_finance(orders_data)

    with tab4:
        _render_tab4_accounts_payable()

    with tab5:
        _render_tab5_subsidy_reconciliation()


def show():
    """Load Page 2's initial data from FastAPI endpoints, then delegate all tab rendering to OrderUI."""
    st.title("📦 訂單與帳務管理系統")
    st.write("本系統串接了 `v_order_details` 整合計算檢視表，提供訂單生命週期、指派配對以及帳務實收狀態的管理。")

    base_url = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")

    try:
        resp_orders = requests.get(f"{base_url}/api/v1/orders", timeout=10)
        resp_orders.raise_for_status()
        orders_payload = resp_orders.json()
        if not isinstance(orders_payload, dict) or not orders_payload.get("success"):
            raise ValueError("取得訂單清單回應狀態不符")
        orders_data = orders_payload.get("data")
        if not isinstance(orders_data, list):
            raise ValueError("訂單資料格式非陣列")

        resp_staff = requests.get(f"{base_url}/api/v1/staff", timeout=10)
        resp_staff.raise_for_status()
        staff_payload = resp_staff.json()
        if not isinstance(staff_payload, dict) or not staff_payload.get("success"):
            raise ValueError("取得月嫂清單回應狀態不符")
        staff_list = staff_payload.get("data")
        if not isinstance(staff_list, list):
            raise ValueError("月嫂資料格式非陣列")

        clients = []
    except Exception as e:
        st.error(f"初始化載入資料失敗: {e}")
        return

    _render_order_page_shell(orders_data, clients, staff_list)
