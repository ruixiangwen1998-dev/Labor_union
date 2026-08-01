from __future__ import annotations

import importlib
from datetime import date

import pandas as pd


hcm = importlib.import_module("scripts.imports.import_client_hcm")


class Cursor:
    def __init__(self, existing_case_nos=(), fail_on=None, holiday_rows=()):
        self.existing_case_nos = set(existing_case_nos)
        self.fail_on = fail_on
        self.holiday_rows = list(holiday_rows)
        self.calls = []
        self.lastrowid = 100
        self.current_case_no = None

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.calls.append((compact, params))
        if self.fail_on and self.fail_on in compact:
            raise RuntimeError("injected insert failure")
        if compact.startswith("SELECT id FROM clients"):
            self.current_case_no = params[0]
        if compact.startswith("INSERT INTO clients"):
            self.lastrowid += 1

    def fetchone(self):
        return (99,) if self.current_case_no in self.existing_case_nos else None

    def fetchall(self):
        return self.holiday_rows


class Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class Workbook:
    sheet_names = ["HCM 客戶"]

    def __init__(self, frame):
        self.frame = frame

    def parse(self, sheet_name):
        assert sheet_name == "HCM 客戶"
        return self.frame


def _patch_import(monkeypatch, frame, connection):
    monkeypatch.setattr(hcm.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(hcm.pd, "ExcelFile", lambda _path: Workbook(frame))
    monkeypatch.setattr(hcm.pymysql, "connect", lambda **_kwargs: connection)
    # Keep this legacy import test focused on insert-only order/client writes.
    # System-alert persistence is covered by its own service/integration tests.
    monkeypatch.setattr(hcm, "upsert_system_alert", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(hcm, "resolve_if_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(hcm, "delete_system_alert", lambda *_args, **_kwargs: False)


def test_mixed_rows_only_insert_new_case_no(monkeypatch):
    frame = pd.DataFrame([
        {"查詢序號(案件編號)": "new-001", "姓名": "新客戶"},
        {"查詢序號(案件編號)": "old-001", "姓名": "既有客戶"},
        {"查詢序號(案件編號)": None, "姓名": "缺案件號"},
    ])
    cursor = Cursor(existing_case_nos={"old-001"})
    connection = Connection(cursor)
    _patch_import(monkeypatch, frame, connection)

    result = hcm.process_import("hcm.xlsx")
    statements = [sql for sql, _ in cursor.calls]

    # The minimal new row is inserted and separately marked for field review;
    # the row without a case number is also review-required.
    assert result == {"inserted": 1, "skipped_existing": 1, "review_required": 2, "failed": 0}
    assert not any(sql.startswith("UPDATE") for sql in statements)
    assert sum(sql.startswith("INSERT INTO clients") for sql in statements) == 1
    assert sum(sql.startswith("INSERT INTO orders") for sql in statements) == 1
    order_call = next(call for call in cursor.calls if call[0].startswith("INSERT INTO orders"))
    assert "subsidy_eligibility" not in order_call[0]
    assert order_call[1] == ("new-001", 101, 20, 9, None, None)
    assert connection.rollbacks == 0
    assert connection.closed is True


def test_new_order_dates_follow_client_service_date_and_service_rules(monkeypatch):
    frame = pd.DataFrame([{
        "查詢序號(案件編號)": "new-001",
        "姓名": "新客戶",
        "預計服務日期": "2026/08/01",
        "希望服務天數": 3,
        "服務方式": "週休2日",
    }])
    cursor = Cursor(holiday_rows=[(date(2026, 8, 3),)])
    connection = Connection(cursor)
    _patch_import(monkeypatch, frame, connection)

    result = hcm.process_import("hcm.xlsx")

    order_call = next(call for call in cursor.calls if call[0].startswith("INSERT INTO orders"))
    assert result["inserted"] == 1
    assert "start_date" in order_call[0]
    assert "end_date" in order_call[0]
    assert order_call[1] == (
        "new-001",
        101,
        3,
        9,
        date(2026, 8, 1),
        date(2026, 8, 6),
    )


def test_invalid_service_start_date_keeps_order_dates_null(monkeypatch):
    frame = pd.DataFrame([{
        "查詢序號(案件編號)": "new-001",
        "姓名": "新客戶",
        "預計服務日期": "not-a-date",
    }])
    cursor = Cursor()
    connection = Connection(cursor)
    _patch_import(monkeypatch, frame, connection)

    result = hcm.process_import("hcm.xlsx")

    order_call = next(call for call in cursor.calls if call[0].startswith("INSERT INTO orders"))
    assert result["inserted"] == 1
    assert order_call[1][-2:] == (None, None)
    assert not any(sql.startswith("SELECT holiday_date") for sql, _ in cursor.calls)


def test_existing_case_is_skipped_before_date_lookup_or_order_write(monkeypatch):
    frame = pd.DataFrame([{
        "查詢序號(案件編號)": "old-001",
        "姓名": "既有客戶",
        "預計服務日期": "2026-08-01",
    }])
    cursor = Cursor(existing_case_nos={"old-001"})
    connection = Connection(cursor)
    _patch_import(monkeypatch, frame, connection)

    result = hcm.process_import("hcm.xlsx")

    assert result["skipped_existing"] == 1
    assert not any(sql.startswith("SELECT holiday_date") for sql, _ in cursor.calls)
    assert not any(sql.startswith("INSERT INTO orders") for sql, _ in cursor.calls)
    assert not any(sql.startswith("UPDATE") for sql, _ in cursor.calls)


def test_new_order_failure_rolls_back(monkeypatch):
    frame = pd.DataFrame([{"查詢序號(案件編號)": "new-001", "姓名": "新客戶"}])
    cursor = Cursor(fail_on="INSERT INTO orders")
    connection = Connection(cursor)
    _patch_import(monkeypatch, frame, connection)

    result = hcm.process_import("hcm.xlsx")

    assert result["inserted"] == 0
    assert result["failed"] == 1
    assert connection.rollbacks == 1
    assert connection.closed is True
