"""Normalize the historical multi-sheet bank statement layout."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.imports.finance_normalized_row import validate_normalized_row


REQUIRED_HEADERS = {
    "帳號",
    "交易日",
    "計息日",
    "入帳日",
    "摘要",
    "幣別",
    "支出",
    "存入",
    "餘額",
    "銷帳編號",
    "交易參考編號",
    "更正註記",
    "存摺備註",
}
COMPARISON_FIELD_INDEX = 11
TRANSACTION_REFERENCE_INDEX = 10
CORRECTION_MARKER_INDEX = 12
PASSBOOK_MEMO_INDEX = 13
COUNTERPARTY_NAME_INDEX = 14
MAX_CONTRACT_COLUMNS = 15


def _clean_header(value: Any) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _identifier(value: Any, warnings: list[str]) -> str | None:
    if pd.isna(value) or str(value).strip() in {"", "--"}:
        return None
    if isinstance(value, float) and value.is_integer():
        warnings.append("identifier_not_text")
        return str(int(value))
    if isinstance(value, (int, float)):
        warnings.append("identifier_not_text")
    return str(value).strip()


def _decimal(value: Any, field: str, warnings: list[str]) -> Decimal | None:
    if pd.isna(value) or str(value).strip() in {"", "--"}:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        warnings.append(f"invalid_{field}")
        return None


def _date_and_time(value: Any, field: str, warnings: list[str]) -> tuple[str | None, str | None]:
    if pd.isna(value) or str(value).strip() in {"", "--"}:
        return None, None
    try:
        parsed = pd.to_datetime(value, errors="raise")
    except (TypeError, ValueError):
        warnings.append(f"invalid_{field}")
        return None, None
    return parsed.date().isoformat(), parsed.time().replace(microsecond=0).isoformat()


def _date(value: Any, field: str, warnings: list[str]) -> str | None:
    parsed, _ = _date_and_time(value, field, warnings)
    return parsed


def _json_scalar(value: Any) -> str | int | float | bool | None:
    if pd.isna(value):
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _direction(debit: Decimal | None, credit: Decimal | None, warnings: list[str]) -> str:
    debit_positive = debit is not None and debit > 0
    credit_positive = credit is not None and credit > 0
    if debit_positive and not credit_positive:
        return "outgoing"
    if credit_positive and not debit_positive:
        return "incoming"
    warnings.append("direction_ambiguous" if debit_positive and credit_positive else "direction_missing")
    return "unknown"


def normalize_legacy_rows(
    excel_path: str | Path,
    sheet_name: str,
    header_row: int,
) -> list[dict[str, Any]]:
    """Read a detected historical sheet and return validated common rows."""

    assert header_row >= 1
    raw = pd.read_excel(excel_path, sheet_name=sheet_name, header=None, dtype=object)
    headers = [
        _clean_header(value)
        for value in raw.iloc[header_row - 1].tolist()[:MAX_CONTRACT_COLUMNS]
    ]
    nonblank_headers = [header for header in headers if header]
    if len(nonblank_headers) != len(set(nonblank_headers)):
        raise ValueError("歷史對帳單包含重複表頭")
    missing = REQUIRED_HEADERS - set(headers)
    if missing:
        raise ValueError(f"歷史對帳單缺少必要欄位: {', '.join(sorted(missing))}")
    if (
        len(headers) <= CORRECTION_MARKER_INDEX
        or headers[TRANSACTION_REFERENCE_INDEX] != "交易參考編號"
        or headers[COMPARISON_FIELD_INDEX] != ""
        or headers[CORRECTION_MARKER_INDEX] != "更正註記"
        or len(headers) <= PASSBOOK_MEMO_INDEX
        or headers[PASSBOOK_MEMO_INDEX] != "存摺備註"
    ):
        raise ValueError(
            "歷史對帳單第 12 欄必須是交易參考編號與更正註記之間的空白比對欄位"
        )
    if (
        len(headers) > COUNTERPARTY_NAME_INDEX
        and headers[COUNTERPARTY_NAME_INDEX] not in {"", "姓名-貼值"}
    ):
        raise ValueError("歷史對帳單第 15 欄只允許空白或「姓名-貼值」")

    normalized: list[dict[str, Any]] = []
    for row_index in range(header_row, len(raw)):
        values = raw.iloc[row_index].tolist()[:MAX_CONTRACT_COLUMNS]
        source = dict(zip(headers, values))
        account_text = "" if pd.isna(source.get("帳號")) else str(source.get("帳號")).strip()
        if not account_text.isdigit() or len(account_text) < 8:
            continue

        warnings: list[str] = []
        transaction_date, transaction_time = _date_and_time(
            source.get("交易日"), "transaction_date", warnings
        )
        debit = _decimal(source.get("支出"), "debit", warnings)
        credit = _decimal(source.get("存入"), "credit", warnings)
        balance = _decimal(source.get("餘額"), "balance", warnings)
        account = _identifier(source.get("帳號"), warnings)
        cancellation_code = _identifier(source.get("銷帳編號"), warnings)
        transaction_reference = _identifier(source.get("交易參考編號"), warnings)
        comparison_field = _identifier(values[COMPARISON_FIELD_INDEX], warnings)
        correction_marker = _identifier(source.get("更正註記"), warnings)
        passbook_memo = _identifier(source.get("存摺備註"), warnings)
        counterparty_name = (
            _identifier(values[COUNTERPARTY_NAME_INDEX], warnings)
            if len(headers) > COUNTERPARTY_NAME_INDEX
            and headers[COUNTERPARTY_NAME_INDEX] == "姓名-貼值"
            else None
        )

        raw_payload = {
            header: _json_scalar(value)
            for header, value in zip(headers, values)
        }
        row = {
            "format_id": "legacy",
            "source_file": str(Path(excel_path)),
            "source_bank_account": account,
            "sheet_name": sheet_name,
            "source_row": row_index + 1,
            "source_reference": None,
            "transaction_date": transaction_date,
            "transaction_time": transaction_time,
            "posting_date": _date(source.get("入帳日"), "posting_date", warnings),
            "value_date": _date(source.get("計息日"), "value_date", warnings),
            "debit": debit,
            "credit": credit,
            "direction": _direction(debit, credit, warnings),
            "balance": balance,
            "currency": None if pd.isna(source.get("幣別")) else str(source.get("幣別")).strip() or None,
            "summary": None if pd.isna(source.get("摘要")) else str(source.get("摘要")),
            "memo": comparison_field,
            "counterparty_name": counterparty_name,
            "counterparty_account": None,
            "cancellation_code": cancellation_code,
            "bank_references": {
                "transaction_reference": transaction_reference,
                "comparison_field": comparison_field,
                "correction_marker": correction_marker,
                "passbook_memo": passbook_memo,
                "存摺備註": passbook_memo,
            },
            "warnings": list(dict.fromkeys(warnings)),
            "raw_payload": raw_payload,
        }
        normalized.append(validate_normalized_row(row))
    return normalized
