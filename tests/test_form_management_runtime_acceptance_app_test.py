"""Runtime acceptance tests for the three-tab Form Management UI."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest


def _run_form_page_with_mock_data(orders_data, clients=None):
    clients = clients or []

    def _app():
        import importlib
        import os as _os
        import pathlib
        import sys as _sys

        _sys.path.insert(0, str(pathlib.Path(_os.getcwd()).resolve()))
        page = importlib.import_module("ui.pages.05_form_management")
        db_service = importlib.import_module("services.db_service")
        tab3 = importlib.import_module(
            "ui.pages.form_management.tab3_contract_management"
        )

        db_service.get_order_details = lambda: orders_data
        db_service.get_table_data = (
            lambda table_name: clients if table_name == "clients" else []
        )
        page.load_json_templates = lambda: []
        tab3.load_contract_templates = lambda: [
            {
                "id": "runtime-contract",
                "name": "Runtime Contract",
                "param_mappings": {},
                "template_filename": "",
            }
        ]
        page.show()

    app = AppTest.from_function(_app)
    app.run(timeout=15)
    return app


def test_form_management_runtime_empty_data_and_rerun_are_stable():
    app = _run_form_page_with_mock_data([])

    assert not app.exception
    assert [title.value for title in app.title] == ["📋 表單與履歷問卷管理專區"]
    assert len(app.tabs) == 3

    app.run(timeout=15)
    assert not app.exception
    assert len(app.tabs) == 3


def test_form_management_runtime_single_case_renders_all_three_tabs():
    app = _run_form_page_with_mock_data(
        [
            {
                "case_no": "A1",
                "client_name": "Alice",
                "order_status": "訂單成立",
                "staff_name": "Caregiver",
                "identity_status": "補助市民",
                "total_employer_self_pay_payable": 1000,
                "govt_claim_date": "2026-07-01",
            }
        ],
        clients=[{"case_no": "A1", "identity_status": "補助市民"}],
    )

    assert not app.exception
    assert len(app.tabs) == 3
    assert any("手動創建" in tab.label for tab in app.tabs)
    assert any("模板庫" in tab.label for tab in app.tabs)
    assert any("契約管理" in tab.label for tab in app.tabs)
