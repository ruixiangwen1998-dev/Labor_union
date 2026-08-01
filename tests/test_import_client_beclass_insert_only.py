from __future__ import annotations

import importlib
import json
from pathlib import Path

import pandas as pd


beclass = importlib.import_module("scripts.imports.import_client_beclass")


def test_cli_defaults_to_the_frozen_template_and_keeps_explicit_path_support():
    source = Path(beclass.__file__).read_text(encoding="utf-8")

    assert "document/資料庫、資料處理/假資料_模板.xlsx" in source
    assert "假資料_範例.xlsx" not in source
    assert "sys.argv[1] if len(sys.argv) > 1 else" in source


class Cursor:
    def __init__(self, counts=None, fail_on=None):
        self.counts = counts or {}
        self.fail_on = fail_on
        self.calls = []
        self.current_query_no = None

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.calls.append((compact, params))
        if self.fail_on and self.fail_on in compact:
            raise RuntimeError("injected insert failure")
        if compact.startswith("SELECT COUNT(*) AS existing_cnt FROM beclass_records"):
            self.current_query_no = params[0]

    def fetchone(self):
        return {"existing_cnt": self.counts.get(self.current_query_no, 0)}


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
    sheet_names = ["客戶 BeClass"]

    def __init__(self, frame):
        self.frame = frame

    def parse(self, sheet_name):
        assert sheet_name == "客戶 BeClass"
        return self.frame


def _patch_import(monkeypatch, frame, connection):
    monkeypatch.setattr(beclass.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(beclass.pd, "ExcelFile", lambda _path: Workbook(frame))
    monkeypatch.setattr(beclass.pymysql, "connect", lambda **_kwargs: connection)
    # These tests exercise insert-only import semantics. Alert persistence has
    # dedicated service/integration coverage and needs a richer DB cursor.
    monkeypatch.setattr(beclass, "upsert_system_alert", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(beclass, "resolve_if_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(beclass, "delete_system_alert", lambda *_args, **_kwargs: False)


def test_query_no_is_the_only_deduplication_key(monkeypatch):
    frame = pd.DataFrame([
        {"查詢序號": "new-001", "姓名": "同名客戶", "出生年": 80, "月": 1, "日": 2, "問卷答案": "保留內容"},
        {"查詢序號": "old-001", "姓名": "同名客戶", "出生年": 80, "月": 1, "日": 2, "問卷答案": "不得覆寫"},
        {"查詢序號": "dup-001", "姓名": "衝突客戶"},
        {"查詢序號": None, "姓名": "缺查詢序號"},
    ])
    cursor = Cursor(counts={"old-001": 1, "dup-001": 2})
    connection = Connection(cursor)
    _patch_import(monkeypatch, frame, connection)

    result = beclass.process_import("client-beclass.xlsx")
    statements = [sql for sql, _ in cursor.calls]

    # The new record is inserted but also flagged because this deliberately
    # minimal fixture omits required validation fields.
    assert result == {"inserted": 1, "skipped_existing": 1, "review_required": 3, "failed": 0}
    assert not any(sql.startswith("UPDATE") for sql in statements)
    assert sum(sql.startswith("INSERT INTO beclass_records") for sql in statements) == 1

    insert_sql, insert_params = next(
        (sql, params) for sql, params in cursor.calls if sql.startswith("INSERT INTO beclass_records")
    )
    columns = [part.strip().strip("`") for part in insert_sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    inserted_record = dict(zip(columns, insert_params))
    assert inserted_record["query_no"] == "new-001"
    assert json.loads(inserted_record["survey_details"]) == {"問卷答案": "保留內容"}
    assert connection.rollbacks == 0
    assert connection.closed is True


def test_insert_failure_rolls_back(monkeypatch):
    frame = pd.DataFrame([{"查詢序號": "new-001", "姓名": "新客戶", "問卷答案": "完整內容"}])
    cursor = Cursor(fail_on="INSERT INTO beclass_records")
    connection = Connection(cursor)
    _patch_import(monkeypatch, frame, connection)

    result = beclass.process_import("client-beclass.xlsx")

    assert result["inserted"] == 0
    assert result["failed"] == 1
    assert connection.rollbacks == 1
    assert connection.closed is True
