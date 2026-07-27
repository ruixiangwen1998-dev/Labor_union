from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import finance_reports
from services import accounts_payable_export


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(finance_reports.router)
    return TestClient(app)


def test_export_openapi_declares_xlsx_content_type():
    openapi = _client().app.openapi()
    export_paths = (
        "/api/v1/finance-reports/accounts-payable/export",
        "/api/v1/finance-reports/subsidy-reconciliation/quarterly/export",
        "/api/v1/finance-reports/subsidy-reconciliation/annual/export",
    )

    for path in export_paths:
        content = openapi["paths"][path]["get"]["responses"]["200"]["content"]
        assert set(content) == {finance_reports.XLSX_MEDIA_TYPE}


def _payable_row(serial: str, amount: Decimal) -> dict:
    row = {header: "" for header in accounts_payable_export.EXPORT_HEADERS}
    row[accounts_payable_export.EXPORT_HEADERS[0]] = serial
    row[accounts_payable_export.EXPORT_HEADERS[5]] = amount
    return row


def test_accounts_payable_preview_is_read_only_and_includes_subsidy_returns(monkeypatch):
    serial_key = accounts_payable_export.EXPORT_HEADERS[0]
    amount_key = accounts_payable_export.EXPORT_HEADERS[5]
    monkeypatch.setattr(
        finance_reports.accounts_payable_export,
        "build_accounts_payable_export",
        lambda target_month: {
            "payable_rows": [
                _payable_row("7-31-1", Decimal("18000")),
                _payable_row("7-633-1", Decimal("12000")),
            ],
            "xlsx_bytes": b"legacy workbook",
            "bank_totals": {"31": Decimal("18000"), "633": Decimal("12000")},
        },
    )

    response = _client().get(
        "/api/v1/finance-reports/accounts-payable?target_month=2026-07&view=export"
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert len(payload["payable_rows"]) == 2
    assert payload["payable_rows"][0][serial_key] == "7-31-1"
    assert payload["payable_rows"][0][amount_key] == 18000
    assert payload["payable_rows"][1][serial_key] == "7-633-1"
    assert payload["payable_rows"][1][amount_key] == 12000
    assert payload["bank_totals"] == {"31": 18000, "633": 12000}
    assert "xlsx_bytes" not in payload


def test_accounts_payable_export_is_attachment_including_subsidy_returns(monkeypatch):
    monkeypatch.setattr(
        finance_reports.accounts_payable_export,
        "build_accounts_payable_export",
        lambda target_month: {
            "payable_rows": [
                _payable_row("7-31-1", Decimal("18000")),
                _payable_row("7-633-1", Decimal("12000")),
            ],
            "xlsx_bytes": b"legacy workbook",
            "bank_totals": {"31": Decimal("18000"), "633": Decimal("12000")},
        },
    )

    response = _client().get("/api/v1/finance-reports/accounts-payable/export?target_month=2026-07")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert "attachment;" in response.headers["content-disposition"]
    assert response.content == b"legacy workbook"


def test_reconciliation_previews_exclude_workbook_bytes_and_exports_attach(monkeypatch):
    quarterly_report = {
        "general_citizen_rows": [{"case_no": "115000001"}],
        "subsidized_citizen_rows": [],
        "xlsx_bytes": b"quarterly xlsx",
    }
    annual_report = {
        "general_citizen_rows": [{"case_no": "115000001"}],
        "subsidized_citizen_rows": [],
        "xlsx_bytes": b"annual xlsx",
    }
    monkeypatch.setattr(
        finance_reports.subsidy_reconciliation_register,
        "build_quarterly_subsidy_register",
        lambda year, quarter: quarterly_report,
    )
    monkeypatch.setattr(
        finance_reports.subsidy_reconciliation_register,
        "build_annual_subsidy_summary",
        lambda year: annual_report,
    )

    client = _client()
    quarterly_preview = client.get(
        "/api/v1/finance-reports/subsidy-reconciliation/quarterly?application_year=2026&quarter=2",
    )
    annual_preview = client.get(
        "/api/v1/finance-reports/subsidy-reconciliation/annual?application_year=2026",
    )
    quarterly_export = client.get(
        "/api/v1/finance-reports/subsidy-reconciliation/quarterly/export?application_year=2026&quarter=2",
    )
    annual_export = client.get(
        "/api/v1/finance-reports/subsidy-reconciliation/annual/export?application_year=2026",
    )

    assert quarterly_preview.status_code == annual_preview.status_code == 200
    assert "xlsx_bytes" not in quarterly_preview.json()["data"]
    assert "xlsx_bytes" not in annual_preview.json()["data"]
    assert quarterly_export.content == b"quarterly xlsx"
    assert annual_export.content == b"annual xlsx"
    assert "attachment;" in quarterly_export.headers["content-disposition"]
    assert "attachment;" in annual_export.headers["content-disposition"]
