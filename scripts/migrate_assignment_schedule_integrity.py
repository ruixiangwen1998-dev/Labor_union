# -*- coding: utf-8 -*-
"""
scripts/migrate_assignment_schedule_integrity.py

可稽核的 live-DB migration 腳本 (ADAD bf3dbb 完備防護強化版)：
1. 預設為 check 模式，僅執行檢核與產出 Manifest JSON，零寫入破壞。
2. 傳入 --apply 時，在上游前置檢查與緊接在 ALTER 前的第二輪連線檢查均無錯誤的前提下，執行：
   ALTER TABLE staff_schedule ADD UNIQUE KEY uq_staff_schedule_assignment_date (assignment_id, work_date)
3. 嚴禁在輸出、日誌或 Manifest 中洩漏資料庫密碼或完整 DSN。
4. 索引解析相容 DictCursor 與 Tuple Cursor、大小寫正規化並依 SEQ_IN_INDEX 顯式排序。
5. 欄位或 FK 缺失時自動阻斷依賴 assignment_id 的後續查詢。
6. canonical index 已存在且 post-check 失敗時，於早退分支明確填入 error。
7. ALTER 失敗時保護 post-check 不覆蓋主錯誤；post_check 失敗時明確設置 post_check_failed=true 且 exit code 1。
"""
import argparse
import json
import os
import sys
from pathlib import Path
import pymysql
from dotenv import load_dotenv

# 確保 UTF-8 輸出
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# 載入 .env
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def mask_secrets(obj, secrets):
    """遞迴遮蔽物件中包含的秘密資訊（如資料庫密碼與完整 DSN）。"""
    if not secrets:
        return obj

    clean_secrets = [str(s) for s in secrets if s and len(str(s).strip()) > 0]
    if not clean_secrets:
        return obj

    if isinstance(obj, str):
        masked = obj
        for sec in clean_secrets:
            if sec in masked:
                masked = masked.replace(sec, '***MASKED***')
        return masked
    elif isinstance(obj, dict):
        return {k: mask_secrets(v, clean_secrets) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [mask_secrets(v, clean_secrets) for v in obj]
    elif isinstance(obj, tuple):
        return tuple(mask_secrets(v, clean_secrets) for v in obj)
    return obj


def get_db_config():
    """取得資料庫設定，密碼僅供連線，嚴禁寫入輸出或 Manifest。"""
    return {
        'host': os.getenv('DB_HOST', '127.0.0.1'),
        'port': int(os.getenv('DB_PORT', 3306)),
        'user': os.getenv('DB_USER', 'root'),
        'password': os.getenv('DB_PASSWORD', '1234'),
        'database': os.getenv('DB_NAME', 'labor_union_db'),
        'charset': 'utf8mb4'
    }


def get_indexes_info(cursor, db_name, table_name='staff_schedule'):
    """
    讀取 INFORMATION_SCHEMA.STATISTICS 取得資料表的所有索引詳細資訊。
    通用解析：相容 DictCursor 與 Tuple Cursor、大小寫正規化並依 SEQ_IN_INDEX 顯式排序。
    """
    sql = """
        SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY INDEX_NAME, SEQ_IN_INDEX
    """
    cursor.execute(sql, (db_name, table_name))
    rows = cursor.fetchall()

    raw_indexes = {}

    for row in rows:
        if isinstance(row, dict):
            # DictCursor 相容，不分大小寫取得 key
            normalized_row = {str(k).upper(): v for k, v in row.items()}
            idx_name = str(normalized_row.get('INDEX_NAME', ''))
            non_unique = int(normalized_row.get('NON_UNIQUE', 1))
            col_name = str(normalized_row.get('COLUMN_NAME', ''))
            seq_in_index = int(normalized_row.get('SEQ_IN_INDEX', 1))
        else:
            # Tuple Cursor
            idx_name = str(row[0])
            non_unique = int(row[1])
            col_name = str(row[2])
            seq_in_index = int(row[3])

        idx_key = idx_name.lower()
        if idx_key not in raw_indexes:
            raw_indexes[idx_key] = {
                'index_name': idx_name,
                'is_unique': (non_unique == 0),
                'columns_raw': []
            }
        raw_indexes[idx_key]['columns_raw'].append((seq_in_index, col_name.lower()))

    parsed_indexes = {}
    for idx_key, data in raw_indexes.items():
        sorted_cols = [col for _, col in sorted(data['columns_raw'], key=lambda x: x[0])]
        parsed_indexes[idx_key] = {
            'index_name': data['index_name'],
            'is_unique': data['is_unique'],
            'columns': sorted_cols
        }

    return parsed_indexes


def check_schema_preconditions(cursor, db_name):
    """檢查 staff_schedule.assignment_id 欄位及外鍵是否存在。"""
    cursor.execute("""
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'staff_schedule' AND COLUMN_NAME = 'assignment_id'
    """, (db_name,))
    has_column = cursor.fetchone()[0] > 0

    cursor.execute("""
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'staff_schedule'
          AND CONSTRAINT_NAME = 'fk_staff_schedule_assignment' AND CONSTRAINT_TYPE = 'FOREIGN KEY'
    """, (db_name,))
    has_fk = cursor.fetchone()[0] > 0

    return {
        'assignment_id_column_exists': has_column,
        'fk_staff_schedule_assignment_exists': has_fk
    }


def inspect_ownership_conflicts(cursor):
    """檢查 non-null assignment_id 的 orphan 與 ownership mismatch 衝突。"""
    sql = """
        SELECT
            s.id AS schedule_id,
            s.case_no AS schedule_case_no,
            s.staff_id AS schedule_staff_id,
            s.assignment_id,
            a.case_no AS assignment_case_no,
            a.staff_id AS assignment_staff_id
        FROM staff_schedule s
        LEFT JOIN case_staff_assignments a ON s.assignment_id = a.id
        WHERE s.assignment_id IS NOT NULL
        ORDER BY s.id
    """
    cursor.execute(sql)
    rows = cursor.fetchall()

    conflicts = []
    for row in rows:
        if isinstance(row, dict):
            norm = {str(k).lower(): v for k, v in row.items()}
            sched_id = norm.get('schedule_id')
            s_case = norm.get('schedule_case_no')
            s_staff = norm.get('schedule_staff_id')
            assign_id = norm.get('assignment_id')
            a_case = norm.get('assignment_case_no')
            a_staff = norm.get('assignment_staff_id')
        else:
            sched_id, s_case, s_staff, assign_id, a_case, a_staff = row

        if a_case is None:
            conflicts.append({
                'schedule_id': sched_id,
                'assignment_id': assign_id,
                'type': 'orphan_assignment',
                'detail': f'assignment_id {assign_id} does not exist in case_staff_assignments'
            })
        elif s_case != a_case or s_staff != a_staff:
            conflicts.append({
                'schedule_id': sched_id,
                'assignment_id': assign_id,
                'type': 'ownership_mismatch',
                'schedule_case_no': s_case,
                'assignment_case_no': a_case,
                'schedule_staff_id': s_staff,
                'assignment_staff_id': a_staff,
                'detail': f'schedule ({s_case}, {s_staff}) != assignment ({a_case}, {a_staff})'
            })

    conflicts.sort(key=lambda x: x['schedule_id'])
    return conflicts


def inspect_duplicate_dates(cursor):
    """檢查同指派同日重複排班 (assignment_id, work_date HAVING count > 1)。"""
    sql = """
        SELECT
            assignment_id,
            work_date,
            COUNT(*) AS cnt,
            GROUP_CONCAT(id ORDER BY id) AS schedule_ids,
            GROUP_CONCAT(DISTINCT case_no ORDER BY case_no) AS case_nos,
            GROUP_CONCAT(DISTINCT staff_id ORDER BY staff_id) AS staff_ids
        FROM staff_schedule
        WHERE assignment_id IS NOT NULL
        GROUP BY assignment_id, work_date
        HAVING cnt > 1
        ORDER BY assignment_id, work_date
    """
    cursor.execute(sql)
    rows = cursor.fetchall()

    duplicates = []
    for row in rows:
        if isinstance(row, dict):
            norm = {str(k).lower(): v for k, v in row.items()}
            assign_id = norm.get('assignment_id')
            w_date = norm.get('work_date')
            cnt = norm.get('cnt')
            s_ids = norm.get('schedule_ids')
            c_nos = norm.get('case_nos')
            st_ids = norm.get('staff_ids')
        else:
            assign_id, w_date, cnt, s_ids, c_nos, st_ids = row

        parsed_s_ids = sorted([int(x) for x in str(s_ids).split(',') if str(x).strip().isdigit()])
        parsed_c_nos = sorted([str(x).strip() for x in str(c_nos).split(',') if str(x).strip()])
        parsed_st_ids = sorted([int(x) for x in str(st_ids).split(',') if str(x).strip().isdigit()])

        duplicates.append({
            'assignment_id': assign_id,
            'work_date': str(w_date),
            'count': int(cnt),
            'schedule_ids': parsed_s_ids,
            'case_nos': parsed_c_nos,
            'staff_ids': parsed_st_ids
        })

    duplicates.sort(key=lambda x: (x['assignment_id'], x['work_date']))
    return duplicates


def run_checks(cursor, db_name):
    """執行完整 pre-check，組合為決定性 Manifest。"""
    schema_prechecks = check_schema_preconditions(cursor, db_name)
    indexes_info = get_indexes_info(cursor, db_name)

    errors = []
    if not schema_prechecks['assignment_id_column_exists']:
        errors.append('Column staff_schedule.assignment_id does not exist')
    if not schema_prechecks['fk_staff_schedule_assignment_exists']:
        errors.append('Foreign key fk_staff_schedule_assignment does not exist')

    # Preconditions 門禁：只有在欄位與外鍵同時存在時，才執行依賴 assignment_id 的 ownership/duplicate 查詢
    if schema_prechecks['assignment_id_column_exists'] and schema_prechecks['fk_staff_schedule_assignment_exists']:
        ownership_conflicts = inspect_ownership_conflicts(cursor)
        duplicate_dates = inspect_duplicate_dates(cursor)
    else:
        ownership_conflicts = []
        duplicate_dates = []

    ukey_staff_date = indexes_info.get('ukey_staff_date')
    has_ukey_staff_date = (
        ukey_staff_date is not None
        and ukey_staff_date['is_unique']
        and ukey_staff_date['columns'] == ['staff_id', 'work_date']
    )
    if not has_ukey_staff_date:
        errors.append('Required unique index ukey_staff_date(staff_id, work_date) is missing or invalid')

    canonical_index_name = 'uq_staff_schedule_assignment_date'
    canonical_columns = ['assignment_id', 'work_date']
    canonical_index = indexes_info.get(canonical_index_name)

    canonical_exists = False
    if canonical_index:
        if canonical_index['is_unique'] and canonical_index['columns'] == canonical_columns:
            canonical_exists = True
        else:
            errors.append(
                f'Index {canonical_index_name} exists but has invalid spec: '
                f'is_unique={canonical_index["is_unique"]}, columns={canonical_index["columns"]}'
            )

    equivalent_index_review_required = False
    for name, idx in indexes_info.items():
        if name != canonical_index_name and idx['is_unique'] and idx['columns'] == canonical_columns:
            equivalent_index_review_required = True
            errors.append(f'Equivalent unique index {name} exists with columns (assignment_id, work_date)')

    if ownership_conflicts:
        errors.append(f'Found {len(ownership_conflicts)} ownership conflicts in staff_schedule')
    if duplicate_dates:
        errors.append(f'Found {len(duplicate_dates)} duplicate (assignment_id, work_date) schedule rows')

    manifest = {
        'mode': 'check',
        'success': len(errors) == 0,
        'schema_prechecks': schema_prechecks,
        'index_status': {
            'ukey_staff_date_valid': has_ukey_staff_date,
            'canonical_index_exists': canonical_exists,
            'canonical_index_name': canonical_index_name,
            'canonical_columns': canonical_columns,
            'equivalent_index_review_required': equivalent_index_review_required
        },
        'ownership_conflicts': ownership_conflicts,
        'duplicate_dates': duplicate_dates,
        'errors': sorted(errors),
        'equivalent_index_review_required': equivalent_index_review_required,
        'apply_result': None,
        'post_check': None,
        'post_check_failed': False
    }
    return manifest


def run_post_check(cursor, db_name):
    """Post-check：驗證 canonical 索引為唯一且 ukey_staff_date 仍然存在。"""
    indexes_info = get_indexes_info(cursor, db_name)
    canonical = indexes_info.get('uq_staff_schedule_assignment_date')
    ukey_staff_date = indexes_info.get('ukey_staff_date')

    canonical_valid = (
        canonical is not None
        and canonical['is_unique']
        and canonical['columns'] == ['assignment_id', 'work_date']
    )
    ukey_staff_date_valid = (
        ukey_staff_date is not None
        and ukey_staff_date['is_unique']
        and ukey_staff_date['columns'] == ['staff_id', 'work_date']
    )
    all_passed = (canonical_valid and ukey_staff_date_valid)
    return {
        'canonical_index_valid': canonical_valid,
        'ukey_staff_date_valid': ukey_staff_date_valid,
        'all_post_checks_passed': all_passed
    }


def apply_migration(connection, cursor, db_name, secrets=None):
    """
    執行 --apply：
    1. 執行 Pass 1 的 pre-check。
    2. 緊接在 ALTER 之前利用同一 connection/cursor 執行 Pass 2 的 pre-check。
    3. 只有兩次檢查完全零錯誤且 Canonical 索引不存在時，才執行 ALTER。
    4. ALTER 失敗時原樣遮罩回報且不 commit、不 retry、不刪除資料。受保護的 post_check 不覆蓋主錯誤。
    5. 若 canonical index 已存在且 post-check 失敗，在早退分支中明確記錄 error 並標記 post_check_failed=true。
    """
    secrets = secrets or []

    # Pass 1 Check
    manifest = run_checks(cursor, db_name)
    manifest['mode'] = 'apply'

    if manifest['errors']:
        manifest['apply_result'] = {
            'applied': False,
            'reason': 'Pass 1 pre-checks failed with errors',
            'errors': manifest['errors']
        }
        manifest['success'] = False
        manifest['post_check_failed'] = False
        return manifest

    if manifest['index_status']['canonical_index_exists']:
        manifest['apply_result'] = {
            'applied': False,
            'reason': 'Canonical index already exists and is valid'
        }
        try:
            post_check = run_post_check(cursor, db_name)
            manifest['post_check'] = post_check
            manifest['post_check_failed'] = not post_check['all_post_checks_passed']
            if not post_check['all_post_checks_passed']:
                manifest['errors'].append('Post-check failed: canonical or ukey_staff_date index missing or invalid')
        except Exception as exc:
            masked_exc = mask_secrets(str(exc), secrets)
            manifest['post_check'] = None
            manifest['post_check_failed'] = True
            manifest['errors'].append(f'Post-check execution failed: {masked_exc}')
        manifest['errors'] = sorted(manifest['errors'])
        manifest['success'] = (len(manifest['errors']) == 0 and not manifest['post_check_failed'])
        return manifest

    # Pass 2 Check (同一連線緊接在 ALTER 之前)
    pass2_manifest = run_checks(cursor, db_name)
    if pass2_manifest['errors']:
        manifest['errors'] = pass2_manifest['errors']
        manifest['apply_result'] = {
            'applied': False,
            'reason': 'Pass 2 pre-checks immediately before DDL failed with errors',
            'errors': pass2_manifest['errors']
        }
        manifest['success'] = False
        manifest['post_check_failed'] = False
        return manifest

    if pass2_manifest['index_status']['canonical_index_exists']:
        manifest['apply_result'] = {
            'applied': False,
            'reason': 'Canonical index was created concurrently prior to DDL'
        }
        try:
            post_check = run_post_check(cursor, db_name)
            manifest['post_check'] = post_check
            manifest['post_check_failed'] = not post_check['all_post_checks_passed']
            if not post_check['all_post_checks_passed']:
                manifest['errors'].append('Post-check failed: canonical or ukey_staff_date index missing or invalid')
        except Exception as exc:
            masked_exc = mask_secrets(str(exc), secrets)
            manifest['post_check'] = None
            manifest['post_check_failed'] = True
            manifest['errors'].append(f'Post-check execution failed: {masked_exc}')
        manifest['errors'] = sorted(manifest['errors'])
        manifest['success'] = (len(manifest['errors']) == 0 and not manifest['post_check_failed'])
        return manifest

    alter_sql = "ALTER TABLE staff_schedule ADD UNIQUE KEY uq_staff_schedule_assignment_date (assignment_id, work_date)"
    try:
        cursor.execute(alter_sql)
        connection.commit()
        manifest['apply_result'] = {
            'applied': True,
            'executed_sql': alter_sql
        }
    except Exception as exc:
        masked_exc = mask_secrets(str(exc), secrets)
        manifest['apply_result'] = {
            'applied': False,
            'reason': f'ALTER TABLE failed: {masked_exc}'
        }
        manifest['errors'].append(f'ALTER TABLE failed: {masked_exc}')
        manifest['errors'] = sorted(manifest['errors'])
        manifest['success'] = False
        # 受保護的 post-check，防止例外覆蓋主錯誤
        try:
            post_check = run_post_check(cursor, db_name)
            manifest['post_check'] = post_check
            manifest['post_check_failed'] = not post_check['all_post_checks_passed']
        except Exception:
            manifest['post_check'] = None
            manifest['post_check_failed'] = True
        return manifest

    # Post Check
    try:
        post_check = run_post_check(cursor, db_name)
        manifest['post_check'] = post_check
        manifest['post_check_failed'] = not post_check['all_post_checks_passed']
        if not post_check['all_post_checks_passed']:
            manifest['errors'].append('Post-check failed: canonical or ukey_staff_date index missing or invalid')
            manifest['success'] = False
        else:
            manifest['success'] = (len(manifest['errors']) == 0)
    except Exception as exc:
        masked_exc = mask_secrets(str(exc), secrets)
        manifest['errors'].append(f'Post-check execution failed: {masked_exc}')
        manifest['post_check_failed'] = True
        manifest['success'] = False

    manifest['errors'] = sorted(manifest['errors'])
    return manifest


def main():
    parser = argparse.ArgumentParser(description="AssignmentScheduleIntegrityMigration")
    parser.add_argument('--apply', action='store_true', help='Execute ALTER TABLE to add unique key')
    parser.add_argument('--report-path', type=str, help='Optional path to write manifest JSON report')
    args = parser.parse_args()

    db_config = get_db_config()
    dsn_str = f"mysql://{db_config.get('user')}:{db_config.get('password')}@{db_config.get('host')}:{db_config.get('port')}/{db_config.get('database')}"
    secrets = [db_config.get('password'), dsn_str]

    try:
        connection = pymysql.connect(**db_config)
    except Exception as e:
        masked_err = mask_secrets(str(e), secrets)
        err_manifest = {
            'mode': 'apply' if args.apply else 'check',
            'success': False,
            'post_check_failed': False,
            'errors': [f'Failed to connect to MySQL: {masked_err}'],
            'apply_result': {'applied': False, 'reason': f'DB Connection Error: {masked_err}'}
        }
        print(json.dumps(err_manifest, ensure_ascii=False, indent=2, sort_keys=True))
        sys.exit(1)

    try:
        with connection.cursor() as cursor:
            cursor.execute("SET NAMES utf8mb4;")

            if args.apply:
                manifest = apply_migration(connection, cursor, db_config['database'], secrets)
            else:
                manifest = run_checks(cursor, db_config['database'])
                manifest['mode'] = 'check'

            # 全面秘密遮罩
            manifest = mask_secrets(manifest, secrets)
            manifest['success'] = (
                len(manifest['errors']) == 0
                and not manifest.get('post_check_failed', False)
                and (manifest.get('post_check') is None or manifest['post_check'].get('all_post_checks_passed', True))
            )

            manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            print(manifest_json)

            if args.report_path:
                report_file = Path(args.report_path)
                report_file.parent.mkdir(parents=True, exist_ok=True)
                report_file.write_text(manifest_json, encoding='utf-8')

            # 若 success 為 False，或有 errors，或者 post_check_failed，一律 exit 1
            if not manifest['success'] or manifest['errors'] or manifest.get('post_check_failed', False):
                sys.exit(1)

    finally:
        connection.close()


if __name__ == '__main__':
    main()
