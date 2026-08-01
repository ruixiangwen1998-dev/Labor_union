# -*- coding: utf-8 -*-
"""
tests/test_assignment_schedule_integrity_migration.py

AssignmentScheduleIntegrityMigration@v1@bf3dbb 完備驗收測試。
涵蓋：
 - 腳本存在與編譯
 - mask_secrets 遞迴遮蔽（含 DSN）
 - canonical index 早退分支 post-check 失敗（回傳 False）
 - canonical index 早退分支 post-check 拋出例外（遮蔽、post_check_failed）
 - pass-2 canonical 早退分支 post-check 拋出例外
 - CLI main() 連線失敗 → SystemExit(1)
 - CLI main() missing canonical → SystemExit(1)
 - CLI main() missing ukey_staff_date → SystemExit(1)
 - pass-2 ownership dirty → 阻斷 ALTER
 - pass-2 duplicate dirty → 阻斷 ALTER
 - ALTER failure：僅一次、無 commit/repair/retry、DSN 遮蔽、遞迴 JSON 不含密碼
 - orphan、兩條 NULL filter、決定性排序 Group Concat
 - metadata 矩陣：大寫 dict、反序 SEQ_IN_INDEX dict、tuple 反序 SEQ_IN_INDEX、NON_UNIQUE=1、反向欄位順序
 - case mismatch
"""
from pathlib import Path
import json
import sys
import re
import pytest

from scripts.migrate_assignment_schedule_integrity import (
    mask_secrets,
    get_db_config,
    get_indexes_info,
    check_schema_preconditions,
    inspect_ownership_conflicts,
    inspect_duplicate_dates,
    run_checks,
    apply_migration,
    run_post_check,
    main
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "migrate_assignment_schedule_integrity.py"

# 用於遞迴 manifest JSON 檢查的禁止字串
FORBIDDEN_SECRETS = ["super_secret_db_pass_888", "secret_pass_999",
                     "mysql://admin:secret_pass_999@127.0.0.1:3306/labor_union_db",
                     "mysql://user:super_secret_db_pass_888@127.0.0.1:3306/labor_union_db"]


def _assert_manifest_json_clean(manifest, forbidden):
    """遞迴序列化 manifest 為 JSON，驗證完整字串不含任何禁止密碼/DSN。"""
    full_json = json.dumps(manifest, ensure_ascii=False, default=str)
    for secret in forbidden:
        assert secret not in full_json, f"Manifest JSON leaks secret: {secret}"


# ── Mock 基礎建設 ───────────────────────────────────────────────────

class MockCursor:
    def __init__(self, responses=None, pass2_conflict=False, pass2_duplicate=False, pass2_canonical=False):
        self.responses = responses or {}
        self.pass2_conflict = pass2_conflict
        self.pass2_duplicate = pass2_duplicate
        self.pass2_canonical = pass2_canonical
        self.executed = []
        self.current_fetch = []
        self.stats_call_count = 0
        self.ownership_call_count = 0
        self.duplicate_call_count = 0

    def execute(self, query, params=None):
        self.executed.append((query.strip(), params))
        compact = " ".join(query.split())

        if "INFORMATION_SCHEMA.COLUMNS" in compact:
            self.current_fetch = [(1,)] if self.responses.get('has_column', True) else [(0,)]
        elif "INFORMATION_SCHEMA.TABLE_CONSTRAINTS" in compact:
            self.current_fetch = [(1,)] if self.responses.get('has_fk', True) else [(0,)]
        elif "INFORMATION_SCHEMA.STATISTICS" in compact:
            self.stats_call_count += 1
            if self.pass2_canonical and self.stats_call_count >= 2:
                self.current_fetch = [
                    ('ukey_staff_date', 0, 'staff_id', 1),
                    ('ukey_staff_date', 0, 'work_date', 2),
                    ('uq_staff_schedule_assignment_date', 0, 'assignment_id', 1),
                    ('uq_staff_schedule_assignment_date', 0, 'work_date', 2)
                ]
            else:
                self.current_fetch = self.responses.get('statistics', [
                    ('ukey_staff_date', 0, 'staff_id', 1),
                    ('ukey_staff_date', 0, 'work_date', 2)
                ])
        elif "LEFT JOIN case_staff_assignments" in compact:
            self.ownership_call_count += 1
            if self.pass2_conflict and self.ownership_call_count >= 2:
                self.current_fetch = [(10, 'C01', 1, 100, 'C01', 99)]
            else:
                self.current_fetch = self.responses.get('ownership', [])
        elif "HAVING cnt > 1" in compact:
            self.duplicate_call_count += 1
            if self.pass2_duplicate and self.duplicate_call_count >= 2:
                self.current_fetch = [(100, '2026-07-05', 2, '10,11', 'C01', '1')]
            else:
                self.current_fetch = self.responses.get('duplicates', [])
        else:
            self.current_fetch = []

    def fetchall(self):
        return self.current_fetch

    def fetchone(self):
        return self.current_fetch[0] if self.current_fetch else (0,)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class MockConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        pass


# ── 1. 腳本存在與編譯 ───────────────────────────────────────────────

def test_script_exists_and_compiles():
    assert SCRIPT_PATH.exists()
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "def main():" in content
    assert "uq_staff_schedule_assignment_date" in content


# ── 2. mask_secrets 遞迴遮蔽 ─────────────────────────────────────────

def test_mask_secrets_utility_with_full_dsn():
    dsn = "mysql://user:super_secret_db_pass_888@127.0.0.1:3306/labor_union_db"
    secrets = ["super_secret_db_pass_888", dsn]
    text = f"Connection failed to {dsn} using password super_secret_db_pass_888"

    masked = mask_secrets(text, secrets)
    assert "super_secret_db_pass_888" not in masked
    assert dsn not in masked
    assert "***MASKED***" in masked


# ── 3. 早退分支 post-check 失敗（回傳 False） ─────────────────────────

def test_canonical_index_exists_early_return_adds_error_when_post_check_fails():
    """Pass 1 canonical exists → early return，但 post-check 回報 ukey_staff_date 缺失。"""
    class PostCheckDegradedCursor(MockCursor):
        def execute(self, query, params=None):
            compact = " ".join(query.split())
            if "INFORMATION_SCHEMA.STATISTICS" in compact:
                self.stats_call_count += 1
                if self.stats_call_count >= 2:
                    # post-check：ukey_staff_date 缺失
                    self.current_fetch = [
                        ('uq_staff_schedule_assignment_date', 0, 'assignment_id', 1),
                        ('uq_staff_schedule_assignment_date', 0, 'work_date', 2)
                    ]
                    self.executed.append((query.strip(), params))
                    return
                else:
                    # Pass 1：兩者均存在
                    self.current_fetch = [
                        ('ukey_staff_date', 0, 'staff_id', 1),
                        ('ukey_staff_date', 0, 'work_date', 2),
                        ('uq_staff_schedule_assignment_date', 0, 'assignment_id', 1),
                        ('uq_staff_schedule_assignment_date', 0, 'work_date', 2)
                    ]
                    self.executed.append((query.strip(), params))
                    return
            super().execute(query, params)

    cursor = PostCheckDegradedCursor()
    conn = MockConnection(cursor)
    manifest = apply_migration(conn, cursor, 'test_db')

    assert manifest['apply_result']['applied'] is False
    assert manifest['post_check_failed'] is True
    assert any("Post-check failed" in err for err in manifest['errors'])
    assert manifest['success'] is False


# ── 4. 早退分支 post-check 拋出例外（Pass 1） ────────────────────────

def test_pass1_early_return_post_check_exception_masked_and_captured():
    """Pass 1 canonical exists → early return，但 run_post_check 拋出例外，必須被遮蔽並標記 post_check_failed=True。"""
    secret_pw = "super_secret_db_pass_888"
    dsn = f"mysql://user:{secret_pw}@127.0.0.1:3306/labor_union_db"

    class PostCheckExplodeCursor(MockCursor):
        def execute(self, query, params=None):
            compact = " ".join(query.split())
            if "INFORMATION_SCHEMA.STATISTICS" in compact:
                self.stats_call_count += 1
                if self.stats_call_count >= 2:
                    # post-check 查詢直接拋出例外
                    raise Exception(f"Lost connection to {dsn} password={secret_pw}")
                else:
                    # Pass 1：canonical 已存在
                    self.current_fetch = [
                        ('ukey_staff_date', 0, 'staff_id', 1),
                        ('ukey_staff_date', 0, 'work_date', 2),
                        ('uq_staff_schedule_assignment_date', 0, 'assignment_id', 1),
                        ('uq_staff_schedule_assignment_date', 0, 'work_date', 2)
                    ]
                    self.executed.append((query.strip(), params))
                    return
            super().execute(query, params)

    cursor = PostCheckExplodeCursor()
    conn = MockConnection(cursor)
    manifest = apply_migration(conn, cursor, 'test_db', secrets=[secret_pw, dsn])

    assert manifest['apply_result']['applied'] is False
    assert manifest['post_check'] is None
    assert manifest['post_check_failed'] is True
    assert manifest['success'] is False
    assert any("Post-check execution failed" in err for err in manifest['errors'])
    # 遮蔽驗證
    _assert_manifest_json_clean(manifest, [secret_pw, dsn])


# ── 5. 早退分支 post-check 拋出例外（Pass 2） ────────────────────────

def test_pass2_early_return_post_check_exception_masked_and_captured():
    """Pass 2 canonical exists → early return，但 run_post_check 拋出例外，必須被遮蔽並標記 post_check_failed=True。"""
    secret_pw = "secret_pass_999"
    dsn = f"mysql://admin:{secret_pw}@127.0.0.1:3306/labor_union_db"

    class Pass2PostCheckExplodeCursor(MockCursor):
        def execute(self, query, params=None):
            compact = " ".join(query.split())
            if "INFORMATION_SCHEMA.STATISTICS" in compact:
                self.stats_call_count += 1
                if self.stats_call_count == 3:
                    # Pass 2 post-check 拋出例外
                    raise Exception(f"Connection reset by {dsn}")
                elif self.stats_call_count == 2:
                    # Pass 2：canonical 已由併發程序建立
                    self.current_fetch = [
                        ('ukey_staff_date', 0, 'staff_id', 1),
                        ('ukey_staff_date', 0, 'work_date', 2),
                        ('uq_staff_schedule_assignment_date', 0, 'assignment_id', 1),
                        ('uq_staff_schedule_assignment_date', 0, 'work_date', 2)
                    ]
                    self.executed.append((query.strip(), params))
                    return
                else:
                    # Pass 1：canonical 不存在
                    self.current_fetch = [
                        ('ukey_staff_date', 0, 'staff_id', 1),
                        ('ukey_staff_date', 0, 'work_date', 2)
                    ]
                    self.executed.append((query.strip(), params))
                    return
            super().execute(query, params)

    cursor = Pass2PostCheckExplodeCursor()
    conn = MockConnection(cursor)
    manifest = apply_migration(conn, cursor, 'test_db', secrets=[secret_pw, dsn])

    assert manifest['apply_result']['applied'] is False
    assert manifest['apply_result']['reason'] == 'Canonical index was created concurrently prior to DDL'
    assert manifest['post_check'] is None
    assert manifest['post_check_failed'] is True
    assert manifest['success'] is False
    _assert_manifest_json_clean(manifest, [secret_pw, dsn])


# ── 6. CLI main() 連線失敗 → SystemExit(1) ───────────────────────────

def test_cli_main_connection_failure_exits_1(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['script', '--apply'])

    def mock_connect_fail(**kwargs):
        raise Exception("Connection failed secret_db_pw")

    monkeypatch.setattr("pymysql.connect", mock_connect_fail)

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


# ── 7. CLI main() --apply post-check missing canonical index → SystemExit(1) ──

def test_cli_apply_post_check_missing_canonical_exits_1(monkeypatch, capsys):
    """
    sys.argv=['script', '--apply'] 真正呼叫 main()。
    模擬：ALTER 執行成功，但 post-check 依然缺少 canonical index。
    驗證：SystemExit(1)、post_check_failed=True、success=False、manifest 含明確 post-check error。
    """
    monkeypatch.setattr(sys, 'argv', ['script', '--apply'])

    class MissingCanonicalOnPostCheckCursor(MockCursor):
        def execute(self, query, params=None):
            compact = " ".join(query.split())
            if "INFORMATION_SCHEMA.STATISTICS" in compact:
                self.stats_call_count += 1
                # 任何時候 (Pass 1, Pass 2, Post-check) 均只有 ukey_staff_date，沒有 canonical
                self.current_fetch = [
                    ('ukey_staff_date', 0, 'staff_id', 1),
                    ('ukey_staff_date', 0, 'work_date', 2)
                ]
                self.executed.append((query.strip(), params))
                return
            super().execute(query, params)

    cursor = MissingCanonicalOnPostCheckCursor()

    class FakeConn:
        def cursor(self_inner):
            return cursor
        def commit(self_inner):
            pass
        def close(self_inner):
            pass

    monkeypatch.setattr("pymysql.connect", lambda **kw: FakeConn())

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1

    captured = capsys.readouterr().out
    manifest = json.loads(captured)

    assert manifest['post_check_failed'] is True
    assert manifest['success'] is False
    assert any("Post-check failed" in err for err in manifest['errors'])
    assert manifest['apply_result']['applied'] is True


# ── 8. CLI main() --apply post-check missing ukey_staff_date → SystemExit(1) ──

def test_cli_apply_post_check_missing_ukey_staff_date_exits_1(monkeypatch, capsys):
    """
    sys.argv=['script', '--apply'] 真正呼叫 main()。
    模擬：ALTER 執行成功，但 post-check 發現既有 ukey_staff_date 缺失。
    驗證：SystemExit(1)、post_check_failed=True、success=False、manifest 含明確 post-check error。
    """
    monkeypatch.setattr(sys, 'argv', ['script', '--apply'])

    class MissingUkeyOnPostCheckCursor(MockCursor):
        def execute(self, query, params=None):
            compact = " ".join(query.split())
            if "INFORMATION_SCHEMA.STATISTICS" in compact:
                self.stats_call_count += 1
                if self.stats_call_count >= 3:
                    # Post-check (Call 3)：ukey_staff_date 缺失，只有 canonical
                    self.current_fetch = [
                        ('uq_staff_schedule_assignment_date', 0, 'assignment_id', 1),
                        ('uq_staff_schedule_assignment_date', 0, 'work_date', 2)
                    ]
                else:
                    # Pass 1 (Call 1) & Pass 2 (Call 2)：只有 ukey_staff_date
                    self.current_fetch = [
                        ('ukey_staff_date', 0, 'staff_id', 1),
                        ('ukey_staff_date', 0, 'work_date', 2)
                    ]
                self.executed.append((query.strip(), params))
                return
            super().execute(query, params)

    cursor = MissingUkeyOnPostCheckCursor()

    class FakeConn:
        def cursor(self_inner):
            return cursor
        def commit(self_inner):
            pass
        def close(self_inner):
            pass

    monkeypatch.setattr("pymysql.connect", lambda **kw: FakeConn())

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1

    captured = capsys.readouterr().out
    manifest = json.loads(captured)

    assert manifest['post_check_failed'] is True
    assert manifest['success'] is False
    assert any("Post-check failed" in err for err in manifest['errors'])
    assert manifest['apply_result']['applied'] is True


# ── 9. Pass-2 ownership dirty → 阻斷 ALTER ───────────────────────────

def test_pass2_race_ownership_dirty_blocks_alter():
    """第一輪乾淨，第二輪併發新增 ownership mismatch → 阻斷 ALTER。"""
    cursor = MockCursor(pass2_conflict=True)
    conn = MockConnection(cursor)

    manifest = apply_migration(conn, cursor, 'test_db')

    assert manifest['apply_result']['applied'] is False
    assert "Pass 2 pre-checks immediately before DDL failed" in manifest['apply_result']['reason']
    assert conn.committed is False
    assert not any(q.startswith("ALTER TABLE") for q, _ in cursor.executed)


# ── 10. Pass-2 duplicate dirty → 阻斷 ALTER ──────────────────────────

def test_pass2_race_duplicate_dirty_blocks_alter():
    cursor = MockCursor(pass2_duplicate=True)
    conn = MockConnection(cursor)

    manifest = apply_migration(conn, cursor, 'test_db')

    assert manifest['apply_result']['applied'] is False
    assert "Pass 2 pre-checks immediately before DDL failed" in manifest['apply_result']['reason']
    assert conn.committed is False
    assert not any(q.startswith("ALTER TABLE") for q, _ in cursor.executed)


# ── 11. ALTER failure：僅一次、無 repair、DSN 遮蔽、遞迴 JSON 不含密碼 ─

def test_alter_duplicate_key_failure_no_commit_or_repair_and_dsn_masked():
    dsn = "mysql://admin:secret_pass_999@127.0.0.1:3306/labor_union_db"
    secret_pw = "secret_pass_999"

    class AlterFailCursor(MockCursor):
        def execute(self, query, params=None):
            super().execute(query, params)
            if query.strip().startswith("ALTER TABLE"):
                raise Exception(f"Duplicate entry for key in {dsn} with password {secret_pw}")

    cursor = AlterFailCursor()
    conn = MockConnection(cursor)

    manifest = apply_migration(conn, cursor, 'test_db', secrets=[secret_pw, dsn])

    # 基本斷言
    assert manifest['apply_result']['applied'] is False
    assert "***MASKED***" in manifest['apply_result']['reason']
    assert conn.committed is False
    assert manifest['success'] is False

    # ALTER 僅執行恰好一次
    alter_count = sum(1 for q, _ in cursor.executed if q.startswith("ALTER TABLE"))
    assert alter_count == 1, f"ALTER should execute exactly once, got {alter_count}"

    # 完整排除所有 repair / mutation SQL
    repair_prefixes = ("DELETE", "UPDATE", "DROP TABLE", "DROP INDEX", "TRUNCATE", "REPAIR", "INSERT", "REPLACE")
    for sql, _ in cursor.executed:
        for prefix in repair_prefixes:
            assert not sql.upper().startswith(prefix), f"Forbidden repair SQL found: {sql[:60]}"

    # 遞迴檢查整份 manifest JSON 不含密碼/DSN
    _assert_manifest_json_clean(manifest, [secret_pw, dsn])


# ── 12. Orphan、兩條 NULL filter、決定性排序 ──────────────────────────

def test_orphan_two_null_filters_and_deterministic_aggregation():
    responses = {
        'ownership': [
            (10, 'C01', 1, 100, None, None),  # orphan (a.id IS NULL)
            (11, 'C02', 2, 101, 'C02', 3)     # mismatch
        ],
        'duplicates': [
            (100, '2026-07-05', 2, '12,10,11', 'C02,C01', '2,1')
        ]
    }
    cursor = MockCursor(responses)
    conflicts = inspect_ownership_conflicts(cursor)
    duplicates = inspect_duplicate_dates(cursor)

    # 1. Orphan
    assert conflicts[0]['type'] == 'orphan_assignment'

    # 2. 兩條 SQL 均包含 WHERE ... IS NOT NULL
    executed_sqls = [sql for sql, _ in cursor.executed]
    assert any("WHERE s.assignment_id IS NOT NULL" in sql for sql in executed_sqls)
    assert any("WHERE assignment_id IS NOT NULL" in sql for sql in executed_sqls)

    # 3. 決定性排序 Group Concat
    assert duplicates[0]['schedule_ids'] == [10, 11, 12]
    assert duplicates[0]['case_nos'] == ['C01', 'C02']
    assert duplicates[0]['staff_ids'] == [1, 2]


# ── 13. Case mismatch ────────────────────────────────────────────────

def test_case_mismatch_ownership_conflict():
    """schedule case_no = 'C01' vs assignment case_no = 'c01' → mismatch。"""
    responses = {
        'ownership': [
            (10, 'C01', 1, 100, 'c01', 1)  # case mismatch
        ]
    }
    cursor = MockCursor(responses)
    conflicts = inspect_ownership_conflicts(cursor)

    assert len(conflicts) == 1
    assert conflicts[0]['type'] == 'ownership_mismatch'
    assert conflicts[0]['schedule_case_no'] == 'C01'
    assert conflicts[0]['assignment_case_no'] == 'c01'


# ── 14. Metadata 矩陣 ────────────────────────────────────────────────

def test_full_metadata_matrix():
    # 矩陣 1：大寫 DictCursor
    mixed_stats = [
        {'INDEX_NAME': 'UQ_STAFF_SCHEDULE_ASSIGNMENT_DATE', 'NON_UNIQUE': 0, 'COLUMN_NAME': 'ASSIGNMENT_ID', 'SEQ_IN_INDEX': 1},
        {'INDEX_NAME': 'UQ_STAFF_SCHEDULE_ASSIGNMENT_DATE', 'NON_UNIQUE': 0, 'COLUMN_NAME': 'WORK_DATE', 'SEQ_IN_INDEX': 2},
        {'INDEX_NAME': 'ukey_staff_date', 'NON_UNIQUE': 0, 'COLUMN_NAME': 'staff_id', 'SEQ_IN_INDEX': 1},
        {'INDEX_NAME': 'ukey_staff_date', 'NON_UNIQUE': 0, 'COLUMN_NAME': 'work_date', 'SEQ_IN_INDEX': 2}
    ]
    cursor1 = MockCursor(responses={'statistics': mixed_stats})
    manifest1 = run_checks(cursor1, 'test_db')
    assert manifest1['index_status']['canonical_index_exists'] is True

    # 矩陣 2：反序 SEQ_IN_INDEX (DictCursor)
    seq_reverse_stats = [
        {'INDEX_NAME': 'uq_staff_schedule_assignment_date', 'NON_UNIQUE': 0, 'COLUMN_NAME': 'work_date', 'SEQ_IN_INDEX': 2},
        {'INDEX_NAME': 'uq_staff_schedule_assignment_date', 'NON_UNIQUE': 0, 'COLUMN_NAME': 'assignment_id', 'SEQ_IN_INDEX': 1},
        {'INDEX_NAME': 'ukey_staff_date', 'NON_UNIQUE': 0, 'COLUMN_NAME': 'staff_id', 'SEQ_IN_INDEX': 1},
        {'INDEX_NAME': 'ukey_staff_date', 'NON_UNIQUE': 0, 'COLUMN_NAME': 'work_date', 'SEQ_IN_INDEX': 2}
    ]
    cursor2 = MockCursor(responses={'statistics': seq_reverse_stats})
    manifest2 = run_checks(cursor2, 'test_db')
    assert manifest2['index_status']['canonical_index_exists'] is True

    # 矩陣 3：反序 SEQ_IN_INDEX (Tuple Cursor) — 新增
    tuple_seq_reverse_stats = [
        ('uq_staff_schedule_assignment_date', 0, 'work_date', 2),
        ('uq_staff_schedule_assignment_date', 0, 'assignment_id', 1),
        ('ukey_staff_date', 0, 'staff_id', 1),
        ('ukey_staff_date', 0, 'work_date', 2)
    ]
    cursor3t = MockCursor(responses={'statistics': tuple_seq_reverse_stats})
    manifest3t = run_checks(cursor3t, 'test_db')
    assert manifest3t['index_status']['canonical_index_exists'] is True

    # 矩陣 4：同名錯誤規格 (NON_UNIQUE=1)
    non_unique_stats = [
        ('uq_staff_schedule_assignment_date', 1, 'assignment_id', 1),
        ('uq_staff_schedule_assignment_date', 1, 'work_date', 2),
        ('ukey_staff_date', 0, 'staff_id', 1),
        ('ukey_staff_date', 0, 'work_date', 2)
    ]
    cursor4 = MockCursor(responses={'statistics': non_unique_stats})
    manifest4 = run_checks(cursor4, 'test_db')
    assert any("has invalid spec" in err for err in manifest4['errors'])

    # 矩陣 5：反向欄位順序 (work_date, assignment_id)
    rev_col_stats = [
        ('uq_staff_schedule_assignment_date', 0, 'work_date', 1),
        ('uq_staff_schedule_assignment_date', 0, 'assignment_id', 2),
        ('ukey_staff_date', 0, 'staff_id', 1),
        ('ukey_staff_date', 0, 'work_date', 2)
    ]
    cursor5 = MockCursor(responses={'statistics': rev_col_stats})
    manifest5 = run_checks(cursor5, 'test_db')
    assert any("has invalid spec" in err for err in manifest5['errors'])
