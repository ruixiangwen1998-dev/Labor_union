from __future__ import annotations

import importlib

import pandas as pd


staff_importer = importlib.import_module("scripts.imports.import_staff_beclass")


class Cursor:
    def __init__(self, existing_identity_cards=(), fail_on=None):
        self.existing_identity_cards = set(existing_identity_cards)
        self.fail_on = fail_on
        self.calls = []
        self.lastrowid = 100
        self.current_identity_card = None

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.calls.append((compact, params))
        if self.fail_on and self.fail_on in compact:
            raise RuntimeError("injected insert failure")
        if compact.startswith("SELECT name FROM staff WHERE identity_card"):
            self.current_identity_card = params[0]
        if compact.startswith("INSERT INTO staff"):
            self.lastrowid += 1

    def fetchone(self):
        return (1,) if self.current_identity_card in self.existing_identity_cards else (0,)

    def fetchall(self):
        if self.current_identity_card in self.existing_identity_cards:
            return [{"name": "existing"}]
        return []


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
    sheet_names = ["服務人員"]

    def __init__(self, frame):
        self.frame = frame

    def parse(self, sheet_name):
        assert sheet_name == "服務人員"
        return self.frame


def _patch_import(monkeypatch, frame, connection):
    monkeypatch.setattr(staff_importer.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(staff_importer.pd, "ExcelFile", lambda _path: Workbook(frame))
    monkeypatch.setattr(staff_importer.pymysql, "connect", lambda **_kwargs: connection)
    # Alert persistence is intentionally isolated from these insert-only tests.
    monkeypatch.setattr(staff_importer, "upsert_system_alert", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(staff_importer, "resolve_if_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(staff_importer, "delete_system_alert", lambda *_args, **_kwargs: False)


def test_mixed_rows_only_insert_new_identity_card(monkeypatch):
    frame = pd.DataFrame([
        {"姓名": "新服務人員", "身分證字號": "A123456789"},
        {"姓名": "既有服務人員", "身分證字號": "B123456789"},
        {"姓名": "待確認服務人員", "身分證字號": None},
    ])
    cursor = Cursor(existing_identity_cards={"B123456789"})
    connection = Connection(cursor)
    _patch_import(monkeypatch, frame, connection)

    result = staff_importer.process_import("staff.xlsx")
    statements = [sql for sql, _ in cursor.calls]

    # The minimal new row is inserted and separately marked for field review;
    # the row without an identity card is also review-required.
    assert result == {"inserted": 1, "skipped_existing": 1, "review_required": 2, "failed": 0}
    assert not any(sql.startswith("UPDATE") for sql in statements)
    assert sum(sql.startswith("INSERT INTO staff") for sql in statements) == 1
    delete_calls = [params for sql, params in cursor.calls if sql.startswith("DELETE FROM")]
    assert delete_calls
    assert all(params == (101,) for params in delete_calls)
    assert connection.rollbacks == 0
    assert connection.closed is True


def test_child_write_failure_rolls_back_new_staff(monkeypatch):
    frame = pd.DataFrame([{"姓名": "新服務人員", "身分證字號": "A123456789"}])
    cursor = Cursor(fail_on="DELETE FROM staff_regions")
    connection = Connection(cursor)
    _patch_import(monkeypatch, frame, connection)

    result = staff_importer.process_import("staff.xlsx")

    assert result["inserted"] == 0
    assert result["failed"] == 1
    assert connection.rollbacks == 1
    assert connection.closed is True
