"""Runtime acceptance tests for the 5-tab Order UI shell."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest


def _run_order_page_with_mock_data(orders_data, clients=None, staff=None):
    clients = clients or []
    staff = staff or []

    def _app():
        import importlib
        import os as _os
        import pathlib
        import streamlit as st_local
        import sys as _sys

        _sys.path.insert(0, str(pathlib.Path(_os.getcwd()).resolve()))
        page = importlib.import_module("ui.pages.02_orders")
        db_service = importlib.import_module("services.db_service")

        db_service.get_order_details = lambda: orders_data
        db_service.get_table_data = lambda table_name: clients if table_name == "clients" else staff if table_name == "staff" else []
        page.show()

    app = AppTest.from_function(_app)
    app.run(timeout=15)
    return app


def test_order_ui_runtime_no_data_shows_shell_without_exception():
    app = _run_order_page_with_mock_data([])

    assert not app.exception
    rendered = [m.value for m in app.markdown]
    assert (
        "本系統串接了 `v_order_details` 整合計算檢視表，提供訂單生命週期、指派配對以及帳務實收狀態的管理。"
        in rendered
    )


def test_order_ui_runtime_single_case_shows_shell():
    minimal_order = [
        {
            "case_no": "A1",
            "client_name": "Alice",
            "order_status": "洽談中",
            "staff_name": "",
            "start_date": "2026-07-01",
            "actual_start_date": "2026-07-01",
            "service_days": 1,
            "identity_status": "",
            "service_mode": "週休1日",
            "client_id": 101,
            "total_employer_self_pay_payable": 1000,
        }
    ]

    app = _run_order_page_with_mock_data(minimal_order)

    assert not app.exception
    rendered = [m.value for m in app.markdown]
    assert (
        "本系統串接了 `v_order_details` 整合計算檢視表，提供訂單生命週期、指派配對以及帳務實收狀態的管理。"
        in rendered
    )


def test_order_tab_renderers_handle_empty_and_single_record():
    def run_tab1_empty():
        import importlib
        import streamlit as st_local

        tab1 = importlib.import_module("ui.pages.order.tab1_overview")
        tab1._render_tab1_overview([])

    app = AppTest.from_function(run_tab1_empty)
    app.run(timeout=15)
    assert not app.exception

    def run_tab2_empty():
        import importlib
        import streamlit as st_local

        tab2 = importlib.import_module("ui.pages.order.tab2_assign")
        tab2._render_tab2_assign([], [], [])

    app = AppTest.from_function(run_tab2_empty)
    app.run(timeout=15)
    assert not app.exception

    def run_tab3():
        import importlib
        import streamlit as st_local

        tab3 = importlib.import_module("ui.pages.order.tab3_finance")

        def _mock_payment_api_request(path, method="GET", payload=None):
            if path in ("/client-payments", "/staff-payments"):
                return []
            return {}

        tab3._payment_api_request = _mock_payment_api_request
        tab3._render_tab3_finance([])

    app = AppTest.from_function(run_tab3)
    app.run(timeout=15)
    assert not app.exception

    def run_tab4():
        import importlib
        import streamlit as st_local

        tab4 = importlib.import_module("ui.pages.order.tab4_accounts_payable")
        def _mock_finance_report_request(path, params=None, download=False):
            if download:
                return b""
            if path == "/accounts-payable":
                return {"payable_rows": []}
            if path == "/accounts-payable-summary":
                return {"summary_rows": [], "headers": [], "totals": {}}
            if path.endswith("/accounts-payable"):
                return {"summary_rows": [], "headers": [], "totals": {}}
            return {"summary_rows": [], "headers": []}

        tab4._finance_report_request = _mock_finance_report_request
        tab4._render_tab4_accounts_payable()

    app = AppTest.from_function(run_tab4)
    app.run(timeout=15)
    assert not app.exception

    def run_tab5():
        import importlib
        import streamlit as st_local

        tab5 = importlib.import_module("ui.pages.order.tab5_subsidy_reconciliation")
        def _mock_finance_report_request(path, params=None, download=False):
            if download:
                return b""
            return {"general_citizen_rows": [], "subsidized_citizen_rows": [], "summary_rows": []}

        tab5._finance_report_request = _mock_finance_report_request
        tab5._render_tab5_subsidy_reconciliation()

    app = AppTest.from_function(run_tab5)
    app.run(timeout=15)
    assert not app.exception
