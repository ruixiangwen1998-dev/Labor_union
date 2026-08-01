-- 退休舊版放寬同一月嫂同日多個排班的 schema part。
-- 保留檔名 100_staff_schedule_allow_same_day_multiple_assignments.sql 以維護 lexical loader 相容性，
-- 但轉改為 fail-closed、可重跑 (idempotent) 的 canonical staff-date (staff_id, work_date) 唯一鍵守衛。
-- 嚴禁 DROP/RENAME/放寬唯一鍵；存在衝突、同名錯誤索引或等價異名索引時一律 fail-closed 並要求人工覆核。

SET @staff_schedule_table_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
);

SET @canonical_any_cols = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
      AND INDEX_NAME = 'ukey_staff_date'
);

SET @canonical_exact_match = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
      AND INDEX_NAME = 'ukey_staff_date'
      AND NON_UNIQUE = 0
      AND (
          (COLUMN_NAME = 'staff_id' AND SEQ_IN_INDEX = 1)
       OR (COLUMN_NAME = 'work_date' AND SEQ_IN_INDEX = 2)
      )
);

SET @canonical_valid = IF(@canonical_any_cols = 2 AND @canonical_exact_match = 2, 1, 0);
SET @canonical_has_invalid_spec = IF(@canonical_any_cols > 0 AND @canonical_valid = 0, 1, 0);

SET @equivalent_index_count = (
    SELECT COUNT(DISTINCT INDEX_NAME)
    FROM (
        SELECT INDEX_NAME,
               COUNT(*) AS total_cols,
               SUM(IF((COLUMN_NAME = 'staff_id' AND SEQ_IN_INDEX = 1) OR (COLUMN_NAME = 'work_date' AND SEQ_IN_INDEX = 2), 1, 0)) AS match_cols,
               MIN(NON_UNIQUE) AS min_non_unique
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'staff_schedule'
          AND INDEX_NAME != 'ukey_staff_date'
        GROUP BY INDEX_NAME
    ) t
    WHERE min_non_unique = 0 AND total_cols = 2 AND match_cols = 2
);

SET @duplicate_rows_exist = IF(
    @staff_schedule_table_exists = 1 AND @canonical_any_cols = 0 AND @canonical_has_invalid_spec = 0 AND @equivalent_index_count = 0,
    (
        SELECT IF(COUNT(*) > 0, 1, 0)
        FROM (
            SELECT staff_id, work_date
            FROM staff_schedule
            GROUP BY staff_id, work_date
            HAVING COUNT(*) > 1
        ) dup_t
    ),
    0
);

SET @action_sql = IF(
    @staff_schedule_table_exists = 0,
    'SELECT * FROM `FAIL_CLOSED_STAFF_SCHEDULE_TABLE_NOT_FOUND`',
    IF(
        @canonical_has_invalid_spec = 1,
        'SELECT * FROM `FAIL_CLOSED_UKEY_STAFF_DATE_INVALID_SPEC_REVIEW_REQUIRED`',
        IF(
            @canonical_valid = 1,
            'SELECT 1',
            IF(
                @equivalent_index_count > 0,
                'SELECT * FROM `FAIL_CLOSED_EQUIVALENT_INDEX_REVIEW_REQUIRED`',
                IF(
                    @duplicate_rows_exist = 1,
                    'SELECT * FROM `FAIL_CLOSED_DUPLICATE_STAFF_DATE_ROWS_FOUND_REVIEW_REQUIRED`',
                    'ALTER TABLE `staff_schedule` ADD UNIQUE KEY `ukey_staff_date` (`staff_id`, `work_date`)'
                )
            )
        )
    )
);

PREPARE staff_schedule_guard_stmt FROM @action_sql;
EXECUTE staff_schedule_guard_stmt;
DEALLOCATE PREPARE staff_schedule_guard_stmt;
