-- 將日層級排班連回正式服務指派。既有排班一律保留 NULL，不能由 migration 推測歸屬。
-- 本 Schema Part 為可重跑 (idempotent)、純擴充 (additive-only) 的 DDL 守衛，嚴禁破壞性異動與寫入。
-- 遇到表缺失、同名錯誤規格或異名等價 metadata 時，一律以 MySQL PREPARE 相容之固定 sentinel 語句執行 fail-closed。

-- 1. 前置資料表存在性守衛 (staff_schedule 與 case_staff_assignments 均必須存在)
SET @ss_table_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
);

SET @csa_table_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'case_staff_assignments'
);

SET @prereq_action_sql = IF(
    @ss_table_exists = 0,
    'SELECT * FROM `FAIL_CLOSED_STAFF_SCHEDULE_TABLE_NOT_FOUND`',
    IF(
        @csa_table_exists = 0,
        'SELECT * FROM `FAIL_CLOSED_CASE_STAFF_ASSIGNMENTS_TABLE_NOT_FOUND`',
        'SELECT 1'
    )
);

PREPARE stmt_prereq FROM @prereq_action_sql;
EXECUTE stmt_prereq;
DEALLOCATE PREPARE stmt_prereq;

-- 2. assignment_id 欄位守衛 (BIGINT NULL)
SET @col_any_count = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
      AND COLUMN_NAME = 'assignment_id'
);

SET @col_exact_match = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
      AND COLUMN_NAME = 'assignment_id'
      AND (DATA_TYPE = 'bigint' OR COLUMN_TYPE LIKE '%bigint%')
      AND IS_NULLABLE = 'YES'
);

SET @col_action_sql = IF(
    @col_any_count > 0 AND @col_exact_match = 0,
    'SELECT * FROM `FAIL_CLOSED_ASSIGNMENT_ID_COLUMN_INVALID_SPEC_REVIEW_REQUIRED`',
    IF(
        @col_any_count = 0,
        'ALTER TABLE `staff_schedule` ADD COLUMN `assignment_id` BIGINT NULL COMMENT \'正式服務指派；既有未覆核排班保留 NULL\' AFTER `staff_id`',
        'SELECT 1'
    )
);

PREPARE stmt_col FROM @col_action_sql;
EXECUTE stmt_col;
DEALLOCATE PREPARE stmt_col;

-- 3. idx_staff_schedule_assignment 索引守衛 (NON_UNIQUE = 1, assignment_id)
SET @idx_any_cols = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
      AND INDEX_NAME = 'idx_staff_schedule_assignment'
);

SET @idx_exact_match = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
      AND INDEX_NAME = 'idx_staff_schedule_assignment'
      AND NON_UNIQUE = 1
      AND COLUMN_NAME = 'assignment_id'
      AND SEQ_IN_INDEX = 1
);

SET @idx_has_invalid_spec = IF(@idx_any_cols > 0 AND NOT (@idx_any_cols = 1 AND @idx_exact_match = 1), 1, 0);

SET @eq_idx_count = (
    SELECT COUNT(DISTINCT INDEX_NAME)
    FROM (
        SELECT INDEX_NAME,
               COUNT(*) AS total_cols,
               SUM(IF(COLUMN_NAME = 'assignment_id' AND SEQ_IN_INDEX = 1, 1, 0)) AS match_cols,
               MIN(NON_UNIQUE) AS min_non_unique
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'staff_schedule'
          AND INDEX_NAME != 'idx_staff_schedule_assignment'
        GROUP BY INDEX_NAME
    ) t
    WHERE min_non_unique = 1 AND total_cols = 1 AND match_cols = 1
);

SET @idx_action_sql = IF(
    @idx_has_invalid_spec = 1,
    'SELECT * FROM `FAIL_CLOSED_IDX_ASSIGNMENT_INVALID_SPEC_REVIEW_REQUIRED`',
    IF(
        @eq_idx_count > 0,
        'SELECT * FROM `FAIL_CLOSED_EQUIVALENT_ASSIGNMENT_INDEX_REVIEW_REQUIRED`',
        IF(
            @idx_any_cols = 0,
            'ALTER TABLE `staff_schedule` ADD INDEX `idx_staff_schedule_assignment` (`assignment_id`)',
            'SELECT 1'
        )
    )
);

PREPARE stmt_idx FROM @idx_action_sql;
EXECUTE stmt_idx;
DEALLOCATE PREPARE stmt_idx;

-- 4. fk_staff_schedule_assignment 外鍵守衛 (ON UPDATE RESTRICT ON DELETE RESTRICT)
SET @fk_any_count = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
      AND CONSTRAINT_NAME = 'fk_staff_schedule_assignment'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);

SET @fk_exact_match = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
    JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS r
      ON k.CONSTRAINT_SCHEMA = r.CONSTRAINT_SCHEMA
     AND k.TABLE_NAME = r.TABLE_NAME
     AND k.CONSTRAINT_NAME = r.CONSTRAINT_NAME
    WHERE k.CONSTRAINT_SCHEMA = DATABASE()
      AND k.TABLE_NAME = 'staff_schedule'
      AND k.CONSTRAINT_NAME = 'fk_staff_schedule_assignment'
      AND k.COLUMN_NAME = 'assignment_id'
      AND k.REFERENCED_TABLE_NAME = 'case_staff_assignments'
      AND k.REFERENCED_COLUMN_NAME = 'id'
      AND r.UPDATE_RULE = 'RESTRICT'
      AND r.DELETE_RULE = 'RESTRICT'
);

SET @fk_has_invalid_spec = IF(@fk_any_count > 0 AND @fk_exact_match = 0, 1, 0);

SET @eq_fk_count = (
    SELECT COUNT(DISTINCT k.CONSTRAINT_NAME)
    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
    JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS r
      ON k.CONSTRAINT_SCHEMA = r.CONSTRAINT_SCHEMA
     AND k.TABLE_NAME = r.TABLE_NAME
     AND k.CONSTRAINT_NAME = r.CONSTRAINT_NAME
    WHERE k.CONSTRAINT_SCHEMA = DATABASE()
      AND k.TABLE_NAME = 'staff_schedule'
      AND k.CONSTRAINT_NAME != 'fk_staff_schedule_assignment'
      AND k.COLUMN_NAME = 'assignment_id'
      AND k.REFERENCED_TABLE_NAME = 'case_staff_assignments'
      AND k.REFERENCED_COLUMN_NAME = 'id'
      AND r.UPDATE_RULE = 'RESTRICT'
      AND r.DELETE_RULE = 'RESTRICT'
);

SET @fk_action_sql = IF(
    @fk_has_invalid_spec = 1,
    'SELECT * FROM `FAIL_CLOSED_FK_ASSIGNMENT_INVALID_SPEC_REVIEW_REQUIRED`',
    IF(
        @eq_fk_count > 0,
        'SELECT * FROM `FAIL_CLOSED_EQUIVALENT_ASSIGNMENT_FK_REVIEW_REQUIRED`',
        IF(
            @fk_any_count = 0,
            'ALTER TABLE `staff_schedule` ADD CONSTRAINT `fk_staff_schedule_assignment` FOREIGN KEY (assignment_id) REFERENCES case_staff_assignments (id) ON UPDATE RESTRICT ON DELETE RESTRICT',
            'SELECT 1'
        )
    )
);

PREPARE stmt_fk FROM @fk_action_sql;
EXECUTE stmt_fk;
DEALLOCATE PREPARE stmt_fk;

-- 5. staff_schedule_assignment_reviews 覆核表動態守衛與建立 (含完整 9 欄位規格、UQ、FK 與 RESTRICT 契約核對)
SET @reviews_table_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule_assignment_reviews'
);

SET @reviews_col_exact_count = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule_assignment_reviews'
      AND (
          (COLUMN_NAME = 'id' AND (DATA_TYPE = 'bigint' OR COLUMN_TYPE LIKE '%bigint%') AND EXTRA LIKE '%auto_increment%' AND IS_NULLABLE = 'NO')
       OR (COLUMN_NAME = 'schedule_id' AND (DATA_TYPE = 'int' OR COLUMN_TYPE LIKE '%int%') AND IS_NULLABLE = 'NO')
       OR (COLUMN_NAME = 'review_reason' AND (DATA_TYPE = 'varchar' OR COLUMN_TYPE LIKE '%varchar%') AND CHARACTER_MAXIMUM_LENGTH = 100 AND IS_NULLABLE = 'NO')
       OR (COLUMN_NAME = 'review_status' AND (DATA_TYPE = 'enum' OR COLUMN_TYPE LIKE '%enum%') AND COLUMN_TYPE LIKE '%review_required%' AND COLUMN_TYPE LIKE '%resolved%' AND COLUMN_DEFAULT = 'review_required' AND IS_NULLABLE = 'NO')
       OR (COLUMN_NAME = 'resolved_assignment_id' AND (DATA_TYPE = 'bigint' OR COLUMN_TYPE LIKE '%bigint%') AND IS_NULLABLE = 'YES')
       OR (COLUMN_NAME = 'resolved_by' AND (DATA_TYPE = 'varchar' OR COLUMN_TYPE LIKE '%varchar%') AND CHARACTER_MAXIMUM_LENGTH = 100 AND IS_NULLABLE = 'YES')
       OR (COLUMN_NAME = 'resolved_at' AND (DATA_TYPE = 'timestamp' OR COLUMN_TYPE LIKE '%timestamp%') AND IS_NULLABLE = 'YES')
       OR (COLUMN_NAME = 'created_at' AND (DATA_TYPE = 'timestamp' OR COLUMN_TYPE LIKE '%timestamp%') AND UPPER(COALESCE(COLUMN_DEFAULT, '')) LIKE 'CURRENT_TIMESTAMP%' AND IS_NULLABLE = 'NO')
       OR (COLUMN_NAME = 'updated_at' AND (DATA_TYPE = 'timestamp' OR COLUMN_TYPE LIKE '%timestamp%') AND UPPER(COALESCE(COLUMN_DEFAULT, '')) LIKE 'CURRENT_TIMESTAMP%' AND IS_NULLABLE = 'NO')
      )
);

SET @reviews_updated_at_on_update_count = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule_assignment_reviews'
      AND COLUMN_NAME = 'updated_at'
      AND UPPER(COALESCE(EXTRA, '')) LIKE '%ON UPDATE CURRENT_TIMESTAMP%'
);

SET @reviews_uq_count = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule_assignment_reviews'
      AND INDEX_NAME = 'uq_schedule_review'
      AND NON_UNIQUE = 0
      AND COLUMN_NAME = 'schedule_id'
);

SET @reviews_fk_count = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule_assignment_reviews'
      AND CONSTRAINT_NAME IN ('fk_schedule_assignment_review_schedule', 'fk_schedule_assignment_review_assignment')
      AND UPDATE_RULE = 'RESTRICT'
      AND DELETE_RULE = 'RESTRICT'
);

SET @reviews_check_count = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS c
    JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS t
      ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA
     AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME
    WHERE c.CONSTRAINT_SCHEMA = DATABASE()
      AND t.TABLE_NAME = 'staff_schedule_assignment_reviews'
      AND c.CONSTRAINT_NAME = 'chk_schedule_assignment_review_resolution'
      AND UPPER(c.CHECK_CLAUSE) LIKE '%REVIEW_STATUS%'
      AND UPPER(c.CHECK_CLAUSE) LIKE '%RESOLVED_ASSIGNMENT_ID%'
      AND UPPER(c.CHECK_CLAUSE) LIKE '%RESOLVED_BY%'
      AND UPPER(c.CHECK_CLAUSE) LIKE '%RESOLVED_AT%'
);

SET @reviews_valid = IF(
    @reviews_table_exists = 1
    AND @reviews_col_exact_count = 9
    AND @reviews_updated_at_on_update_count = 1
    AND @reviews_uq_count = 1
    AND @reviews_fk_count = 2
    AND @reviews_check_count = 1,
    1,
    0
);

SET @reviews_invalid_spec = IF(@reviews_table_exists = 1 AND @reviews_valid = 0, 1, 0);

SET @reviews_action_sql = IF(
    @reviews_invalid_spec = 1,
    'SELECT * FROM `FAIL_CLOSED_REVIEWS_TABLE_INVALID_SPEC_REVIEW_REQUIRED`',
    IF(
        @reviews_table_exists = 0,
        'CREATE TABLE staff_schedule_assignment_reviews (id BIGINT AUTO_INCREMENT PRIMARY KEY, schedule_id INT NOT NULL, review_reason VARCHAR(100) NOT NULL, review_status ENUM(\'review_required\', \'resolved\') NOT NULL DEFAULT \'review_required\', resolved_assignment_id BIGINT NULL, resolved_by VARCHAR(100) NULL, resolved_at TIMESTAMP NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, UNIQUE KEY uq_schedule_review (schedule_id), INDEX idx_schedule_assignment_review_status (review_status, created_at), CONSTRAINT chk_schedule_assignment_review_resolution CHECK ((review_status = \'review_required\' AND resolved_assignment_id IS NULL AND resolved_by IS NULL AND resolved_at IS NULL) OR (review_status = \'resolved\' AND resolved_assignment_id IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)), CONSTRAINT fk_schedule_assignment_review_schedule FOREIGN KEY (schedule_id) REFERENCES staff_schedule(id) ON UPDATE RESTRICT ON DELETE RESTRICT, CONSTRAINT fk_schedule_assignment_review_assignment FOREIGN KEY (resolved_assignment_id) REFERENCES case_staff_assignments(id) ON UPDATE RESTRICT ON DELETE RESTRICT) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
        'SELECT 1'
    )
);

PREPARE stmt_reviews FROM @reviews_action_sql;
EXECUTE stmt_reviews;
DEALLOCATE PREPARE stmt_reviews;
