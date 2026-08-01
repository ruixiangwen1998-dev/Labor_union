-- 102_assignment_schedule_leave_substitution_batches.sql
-- 為同一案件多日休假／順延／代班 Apply 建立 batch 聚合根，並保留既有事件的
-- 可回填式欄位（預設 NULL，不做歷史回填或更新）。

CREATE TABLE IF NOT EXISTS assignment_schedule_leave_substitution_batches (
    batch_key VARCHAR(100) NOT NULL COMMENT '整批冪等鍵',
    case_no VARCHAR(50) NOT NULL COMMENT '事件所屬案件（對應 orders.case_no）',
    preview_fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT 'canonical preview sha256 lowercase hex',
    item_count INT UNSIGNED NOT NULL COMMENT 'canonical items 數量',
    actor VARCHAR(100) NOT NULL COMMENT '執行者管理員識別',
    reason VARCHAR(255) NOT NULL COMMENT '統一 non-empty 原因',
    request_snapshot JSON NOT NULL COMMENT 'canonical request snapshot',
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '批次建立時間',
    PRIMARY KEY (batch_key),
    INDEX idx_assignment_schedule_leave_substitution_batches_case_time (case_no, occurred_at),
    CONSTRAINT fk_assignment_schedule_leave_substitution_batches_case_no
        FOREIGN KEY (case_no) REFERENCES orders(case_no)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT chk_assignment_schedule_leave_substitution_batches_identity
        CHECK (
            CHAR_LENGTH(TRIM(batch_key)) > 0
            AND CHAR_LENGTH(TRIM(case_no)) > 0
            AND CHAR_LENGTH(TRIM(actor)) > 0
            AND CHAR_LENGTH(TRIM(reason)) > 0
        ),
    CONSTRAINT chk_leave_batch_fingerprint
        CHECK (preview_fingerprint REGEXP '^[0-9a-f]{64}$'),
    CONSTRAINT chk_assignment_schedule_leave_substitution_batches_item_count
        CHECK (item_count >= 1),
    CONSTRAINT chk_leave_batch_request_snapshot
        CHECK (JSON_TYPE(request_snapshot) = 'OBJECT')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @batch_header_exact = (
    SELECT IF(
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 'assignment_schedule_leave_substitution_batches'
           AND ENGINE = 'InnoDB'
           AND TABLE_COLLATION = 'utf8mb4_unicode_ci') = 1
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 'assignment_schedule_leave_substitution_batches') = 8
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 'assignment_schedule_leave_substitution_batches'
           AND (
             (COLUMN_NAME = 'batch_key' AND DATA_TYPE = 'varchar' AND CHARACTER_MAXIMUM_LENGTH = 100 AND IS_NULLABLE = 'NO')
             OR (COLUMN_NAME = 'case_no' AND DATA_TYPE = 'varchar' AND CHARACTER_MAXIMUM_LENGTH = 50 AND IS_NULLABLE = 'NO')
             OR (COLUMN_NAME = 'preview_fingerprint' AND DATA_TYPE = 'char' AND CHARACTER_MAXIMUM_LENGTH = 64 AND CHARACTER_SET_NAME = 'ascii' AND COLLATION_NAME = 'ascii_bin' AND IS_NULLABLE = 'NO')
             OR (COLUMN_NAME = 'item_count' AND DATA_TYPE = 'int' AND COLUMN_TYPE = 'int unsigned' AND IS_NULLABLE = 'NO')
             OR (COLUMN_NAME = 'actor' AND DATA_TYPE = 'varchar' AND CHARACTER_MAXIMUM_LENGTH = 100 AND IS_NULLABLE = 'NO')
             OR (COLUMN_NAME = 'reason' AND DATA_TYPE = 'varchar' AND CHARACTER_MAXIMUM_LENGTH = 255 AND IS_NULLABLE = 'NO')
             OR (COLUMN_NAME = 'request_snapshot' AND DATA_TYPE = 'json' AND IS_NULLABLE = 'NO')
             OR (COLUMN_NAME = 'occurred_at' AND DATA_TYPE = 'timestamp' AND IS_NULLABLE = 'NO' AND UPPER(COLUMN_DEFAULT) = 'CURRENT_TIMESTAMP')
           )) = 8
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 'assignment_schedule_leave_substitution_batches'
           AND INDEX_NAME = 'PRIMARY') = 1
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 'assignment_schedule_leave_substitution_batches'
           AND INDEX_NAME = 'PRIMARY'
           AND NON_UNIQUE = 0
           AND SEQ_IN_INDEX = 1
           AND COLUMN_NAME = 'batch_key') = 1
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 'assignment_schedule_leave_substitution_batches'
           AND INDEX_NAME = 'idx_assignment_schedule_leave_substitution_batches_case_time') = 2
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 'assignment_schedule_leave_substitution_batches'
           AND INDEX_NAME = 'idx_assignment_schedule_leave_substitution_batches_case_time'
           AND NON_UNIQUE = 1
           AND ((SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'case_no')
             OR (SEQ_IN_INDEX = 2 AND COLUMN_NAME = 'occurred_at'))) = 2
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
         JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS r
           ON k.CONSTRAINT_SCHEMA = r.CONSTRAINT_SCHEMA
          AND k.TABLE_NAME = r.TABLE_NAME
          AND k.CONSTRAINT_NAME = r.CONSTRAINT_NAME
         WHERE k.CONSTRAINT_SCHEMA = DATABASE()
           AND k.TABLE_NAME = 'assignment_schedule_leave_substitution_batches'
           AND k.CONSTRAINT_NAME = 'fk_assignment_schedule_leave_substitution_batches_case_no'
           AND k.COLUMN_NAME = 'case_no'
           AND k.REFERENCED_TABLE_NAME = 'orders'
           AND k.REFERENCED_COLUMN_NAME = 'case_no'
           AND r.UPDATE_RULE = 'RESTRICT'
           AND r.DELETE_RULE = 'RESTRICT') = 1
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS
         WHERE CONSTRAINT_SCHEMA = DATABASE()
           AND CONSTRAINT_NAME = 'chk_assignment_schedule_leave_substitution_batches_identity'
           AND UPPER(REPLACE(REPLACE(REPLACE(REPLACE(CHECK_CLAUSE, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', ''))
             = '((CHAR_LENGTH(TRIM(BATCH_KEY))>0)AND(CHAR_LENGTH(TRIM(CASE_NO))>0)AND(CHAR_LENGTH(TRIM(ACTOR))>0)AND(CHAR_LENGTH(TRIM(REASON))>0))') = 1
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS
         WHERE CONSTRAINT_SCHEMA = DATABASE()
           AND CONSTRAINT_NAME = 'chk_leave_batch_fingerprint'
           AND (
             BINARY REPLACE(REPLACE(REPLACE(REPLACE(CHECK_CLAUSE, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', '')
               = BINARY '(preview_fingerprintREGEXP''^[0-9a-f]{64}$'')'
             OR (
               LOWER(REPLACE(REPLACE(REPLACE(REPLACE(CHECK_CLAUSE, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', ''))
                 LIKE 'regexp_like(preview_fingerprint,%'
               AND BINARY REPLACE(REPLACE(REPLACE(REPLACE(CHECK_CLAUSE, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', '')
                 LIKE BINARY '%^[0-9a-f]{64}$%'
               AND BINARY REPLACE(REPLACE(REPLACE(REPLACE(CHECK_CLAUSE, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', '')
                 NOT LIKE BINARY '%[0-9A-F]{64}%'
             )
           )) = 1
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS
         WHERE CONSTRAINT_SCHEMA = DATABASE()
           AND CONSTRAINT_NAME = 'chk_assignment_schedule_leave_substitution_batches_item_count'
           AND UPPER(REPLACE(REPLACE(REPLACE(REPLACE(CHECK_CLAUSE, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', ''))
             = '(ITEM_COUNT>=1)') = 1
        AND
        (SELECT COUNT(*) FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS
         WHERE CONSTRAINT_SCHEMA = DATABASE()
           AND CONSTRAINT_NAME = 'chk_leave_batch_request_snapshot'
           AND (
             UPPER(REPLACE(REPLACE(REPLACE(REPLACE(CHECK_CLAUSE, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', ''))
               = '(JSON_TYPE(REQUEST_SNAPSHOT)=''OBJECT'')'
             OR UPPER(REPLACE(REPLACE(REPLACE(REPLACE(CHECK_CLAUSE, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', ''))
               LIKE '(JSON_TYPE(REQUEST_SNAPSHOT)=%OBJECT%)'
           )) = 1,
        1,
        0
    )
);

SET @batch_header_guard_action_sql = IF(
    @batch_header_exact = 1,
    'SELECT 1',
    'SELECT * FROM `FAIL_CLOSED_BATCH_HEADER_INVALID_SPEC_REVIEW_REQUIRED`'
);
PREPARE stmt_batch_header_guard FROM @batch_header_guard_action_sql;
EXECUTE stmt_batch_header_guard;
DEALLOCATE PREPARE stmt_batch_header_guard;

SET @batch_before_update_trigger_any = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TRIGGERS
    WHERE TRIGGER_SCHEMA = DATABASE()
      AND EVENT_OBJECT_TABLE = 'assignment_schedule_leave_substitution_batches'
      AND TRIGGER_NAME = 'trg_assignment_schedule_leave_substitution_batches_before_update'
);

SET @batch_before_update_trigger_valid = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TRIGGERS
    WHERE TRIGGER_SCHEMA = DATABASE()
      AND EVENT_OBJECT_TABLE = 'assignment_schedule_leave_substitution_batches'
      AND TRIGGER_NAME = 'trg_assignment_schedule_leave_substitution_batches_before_update'
      AND ACTION_TIMING = 'BEFORE'
      AND EVENT_MANIPULATION = 'UPDATE'
      AND UPPER(REPLACE(REPLACE(REPLACE(REPLACE(ACTION_STATEMENT, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', ''))
        = 'SIGNALSQLSTATE''45000''SETMESSAGE_TEXT=''ASSIGNMENT_SCHEDULE_LEAVE_SUBSTITUTION_BATCHESRECORDSCANNOTBEUPDATED'''
);

SET @batch_before_update_trigger_action_sql = IF(
    @batch_before_update_trigger_any > 0 AND @batch_before_update_trigger_valid = 0,
    'SELECT * FROM `FAIL_CLOSED_BATCH_BEFORE_UPDATE_TRIGGER_INVALID_SPEC_REVIEW_REQUIRED`',
    'SELECT 1'
);

PREPARE stmt_batch_before_update_trigger FROM @batch_before_update_trigger_action_sql;
EXECUTE stmt_batch_before_update_trigger;
DEALLOCATE PREPARE stmt_batch_before_update_trigger;

DROP TRIGGER IF EXISTS trg_assignment_schedule_leave_substitution_batches_before_update;
CREATE TRIGGER trg_assignment_schedule_leave_substitution_batches_before_update
BEFORE UPDATE ON assignment_schedule_leave_substitution_batches
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'assignment_schedule_leave_substitution_batches records cannot be updated';

SET @batch_before_delete_trigger_any = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TRIGGERS
    WHERE TRIGGER_SCHEMA = DATABASE()
      AND EVENT_OBJECT_TABLE = 'assignment_schedule_leave_substitution_batches'
      AND TRIGGER_NAME = 'trg_assignment_schedule_leave_substitution_batches_before_delete'
);

SET @batch_before_delete_trigger_valid = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TRIGGERS
    WHERE TRIGGER_SCHEMA = DATABASE()
      AND EVENT_OBJECT_TABLE = 'assignment_schedule_leave_substitution_batches'
      AND TRIGGER_NAME = 'trg_assignment_schedule_leave_substitution_batches_before_delete'
      AND ACTION_TIMING = 'BEFORE'
      AND EVENT_MANIPULATION = 'DELETE'
      AND UPPER(REPLACE(REPLACE(REPLACE(REPLACE(ACTION_STATEMENT, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', ''))
        = 'SIGNALSQLSTATE''45000''SETMESSAGE_TEXT=''ASSIGNMENT_SCHEDULE_LEAVE_SUBSTITUTION_BATCHESRECORDSCANNOTBEDELETED'''
);

SET @batch_before_delete_trigger_action_sql = IF(
    @batch_before_delete_trigger_any > 0 AND @batch_before_delete_trigger_valid = 0,
    'SELECT * FROM `FAIL_CLOSED_BATCH_BEFORE_DELETE_TRIGGER_INVALID_SPEC_REVIEW_REQUIRED`',
    'SELECT 1'
);

PREPARE stmt_batch_before_delete_trigger FROM @batch_before_delete_trigger_action_sql;
EXECUTE stmt_batch_before_delete_trigger;
DEALLOCATE PREPARE stmt_batch_before_delete_trigger;

DROP TRIGGER IF EXISTS trg_assignment_schedule_leave_substitution_batches_before_delete;
CREATE TRIGGER trg_assignment_schedule_leave_substitution_batches_before_delete
BEFORE DELETE ON assignment_schedule_leave_substitution_batches
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'assignment_schedule_leave_substitution_batches records cannot be deleted';

SET @events_table_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
);

SET @event_batch_key_col_any = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND COLUMN_NAME = 'batch_key'
);

SET @event_batch_key_col_exact = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND COLUMN_NAME = 'batch_key'
      AND (DATA_TYPE = 'varchar' OR COLUMN_TYPE LIKE '%varchar%')
      AND CHARACTER_MAXIMUM_LENGTH = 100
      AND IS_NULLABLE = 'YES'
);

SET @event_batch_key_col_action_sql = IF(
    @events_table_exists = 0,
    'SELECT * FROM `FAIL_CLOSED_EVENTS_TABLE_NOT_FOUND`',
    IF(
        @event_batch_key_col_any > 0 AND @event_batch_key_col_exact = 0,
        'SELECT * FROM `FAIL_CLOSED_BATCH_KEY_COLUMN_INVALID_SPEC_REVIEW_REQUIRED`',
        IF(
            @event_batch_key_col_any = 0,
            'ALTER TABLE `assignment_schedule_leave_substitution_events` ADD COLUMN `batch_key` VARCHAR(100) NULL',
            'SELECT 1'
        )
    )
);

PREPARE stmt_event_batch_key_col FROM @event_batch_key_col_action_sql;
EXECUTE stmt_event_batch_key_col;
DEALLOCATE PREPARE stmt_event_batch_key_col;

SET @event_batch_item_index_col_any = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND COLUMN_NAME = 'batch_item_index'
);

SET @event_batch_item_index_col_exact = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND COLUMN_NAME = 'batch_item_index'
      AND DATA_TYPE = 'int'
      AND COLUMN_TYPE LIKE '%unsigned%'
      AND IS_NULLABLE = 'YES'
);

SET @event_batch_item_index_col_action_sql = IF(
    @events_table_exists = 0,
    'SELECT * FROM `FAIL_CLOSED_EVENTS_TABLE_NOT_FOUND`',
    IF(
        @event_batch_item_index_col_any > 0 AND @event_batch_item_index_col_exact = 0,
        'SELECT * FROM `FAIL_CLOSED_BATCH_ITEM_INDEX_COLUMN_INVALID_SPEC_REVIEW_REQUIRED`',
        IF(
            @event_batch_item_index_col_any = 0,
            'ALTER TABLE `assignment_schedule_leave_substitution_events` ADD COLUMN `batch_item_index` INT UNSIGNED NULL',
            'SELECT 1'
        )
    )
);

PREPARE stmt_event_batch_item_index_col FROM @event_batch_item_index_col_action_sql;
EXECUTE stmt_event_batch_item_index_col;
DEALLOCATE PREPARE stmt_event_batch_item_index_col;

SET @event_batch_linkage_index_any = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND INDEX_NAME = 'uq_assignment_schedule_leave_substitution_events_batch_linkage'
);

SET @event_batch_linkage_index_seq1 = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND INDEX_NAME = 'uq_assignment_schedule_leave_substitution_events_batch_linkage'
      AND NON_UNIQUE = 0
      AND COLUMN_NAME = 'batch_key'
      AND SEQ_IN_INDEX = 1
);

SET @event_batch_linkage_index_seq2 = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND INDEX_NAME = 'uq_assignment_schedule_leave_substitution_events_batch_linkage'
      AND NON_UNIQUE = 0
      AND COLUMN_NAME = 'batch_item_index'
      AND SEQ_IN_INDEX = 2
);

SET @event_batch_linkage_index_exact = IF(
    @event_batch_linkage_index_any = 2
       AND @event_batch_linkage_index_seq1 = 1
       AND @event_batch_linkage_index_seq2 = 1,
    1,
    0
);

SET @event_batch_linkage_index_action_sql = IF(
    @events_table_exists = 0,
    'SELECT * FROM `FAIL_CLOSED_EVENTS_TABLE_NOT_FOUND`',
    IF(
        @event_batch_linkage_index_any > 0 AND @event_batch_linkage_index_exact = 0,
        'SELECT * FROM `FAIL_CLOSED_EVENT_BATCH_LINKAGE_INDEX_INVALID_SPEC_REVIEW_REQUIRED`',
        IF(
            @event_batch_linkage_index_any = 0,
            'ALTER TABLE `assignment_schedule_leave_substitution_events` ADD UNIQUE KEY '
            '`uq_assignment_schedule_leave_substitution_events_batch_linkage` (`batch_key`, `batch_item_index`)',
            'SELECT 1'
        )
    )
);

PREPARE stmt_event_batch_linkage_index FROM @event_batch_linkage_index_action_sql;
EXECUTE stmt_event_batch_linkage_index;
DEALLOCATE PREPARE stmt_event_batch_linkage_index;

SET @event_batch_work_date_index_any = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND INDEX_NAME = 'idx_assignment_schedule_leave_substitution_events_batch_key'
);

SET @event_batch_work_date_index_seq1 = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND INDEX_NAME = 'idx_assignment_schedule_leave_substitution_events_batch_key'
      AND NON_UNIQUE = 1
      AND COLUMN_NAME = 'batch_key'
      AND SEQ_IN_INDEX = 1
);

SET @event_batch_work_date_index_seq2 = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND INDEX_NAME = 'idx_assignment_schedule_leave_substitution_events_batch_key'
      AND NON_UNIQUE = 1
      AND COLUMN_NAME = 'work_date'
      AND SEQ_IN_INDEX = 2
);

SET @event_batch_work_date_index_exact = IF(
    @event_batch_work_date_index_any = 2
       AND @event_batch_work_date_index_seq1 = 1
       AND @event_batch_work_date_index_seq2 = 1,
    1,
    0
);

SET @event_batch_work_date_index_action_sql = IF(
    @events_table_exists = 0,
    'SELECT * FROM `FAIL_CLOSED_EVENTS_TABLE_NOT_FOUND`',
    IF(
        @event_batch_work_date_index_any > 0 AND @event_batch_work_date_index_exact = 0,
        'SELECT * FROM `FAIL_CLOSED_EVENT_BATCH_KEY_WORK_DATE_INDEX_INVALID_SPEC_REVIEW_REQUIRED`',
        IF(
            @event_batch_work_date_index_any = 0,
            'ALTER TABLE `assignment_schedule_leave_substitution_events` ADD INDEX '
            '`idx_assignment_schedule_leave_substitution_events_batch_key` (`batch_key`, `work_date`)',
            'SELECT 1'
        )
    )
);

PREPARE stmt_event_batch_work_date_index FROM @event_batch_work_date_index_action_sql;
EXECUTE stmt_event_batch_work_date_index;
DEALLOCATE PREPARE stmt_event_batch_work_date_index;

SET @event_batch_fk_any = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND CONSTRAINT_NAME = 'fk_assignment_schedule_leave_substitution_events_batch'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);

SET @event_batch_fk_exact = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
    JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS r
      ON k.CONSTRAINT_SCHEMA = r.CONSTRAINT_SCHEMA
     AND k.TABLE_NAME = r.TABLE_NAME
     AND k.CONSTRAINT_NAME = r.CONSTRAINT_NAME
    WHERE k.CONSTRAINT_SCHEMA = DATABASE()
      AND k.TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND k.CONSTRAINT_NAME = 'fk_assignment_schedule_leave_substitution_events_batch'
      AND k.COLUMN_NAME = 'batch_key'
      AND k.REFERENCED_TABLE_NAME = 'assignment_schedule_leave_substitution_batches'
      AND k.REFERENCED_COLUMN_NAME = 'batch_key'
      AND r.UPDATE_RULE = 'RESTRICT'
      AND r.DELETE_RULE = 'RESTRICT'
);

SET @event_batch_fk_action_sql = IF(
    @events_table_exists = 0,
    'SELECT * FROM `FAIL_CLOSED_EVENTS_TABLE_NOT_FOUND`',
    IF(
        @event_batch_fk_any > 0 AND @event_batch_fk_exact = 0,
        'SELECT * FROM `FAIL_CLOSED_EVENT_BATCH_FK_INVALID_SPEC_REVIEW_REQUIRED`',
        IF(
            @event_batch_fk_any = 0,
            'ALTER TABLE `assignment_schedule_leave_substitution_events` ADD CONSTRAINT '
            '`fk_assignment_schedule_leave_substitution_events_batch` FOREIGN KEY (batch_key) '
            'REFERENCES assignment_schedule_leave_substitution_batches(batch_key) '
            'ON UPDATE RESTRICT ON DELETE RESTRICT',
            'SELECT 1'
        )
    )
);

PREPARE stmt_event_batch_fk FROM @event_batch_fk_action_sql;
EXECUTE stmt_event_batch_fk;
DEALLOCATE PREPARE stmt_event_batch_fk;

SET @event_batch_linkage_check_any = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS c
    JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS t
      ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA
     AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME
    WHERE c.CONSTRAINT_SCHEMA = DATABASE()
      AND t.TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND c.CONSTRAINT_NAME = 'chk_assignment_schedule_leave_substitution_events_batch_linkage'
);

SET @event_batch_linkage_check_exact = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS c
    JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS t
      ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA
     AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME
    WHERE c.CONSTRAINT_SCHEMA = DATABASE()
      AND t.TABLE_NAME = 'assignment_schedule_leave_substitution_events'
      AND c.CONSTRAINT_NAME = 'chk_assignment_schedule_leave_substitution_events_batch_linkage'
      AND UPPER(REPLACE(REPLACE(REPLACE(REPLACE(c.CHECK_CLAUSE, ' ', ''), CHAR(9), ''), CHAR(10), ''), '`', ''))
        = '(((BATCH_KEYISNULL)AND(BATCH_ITEM_INDEXISNULL))OR((BATCH_KEYISNOTNULL)AND(BATCH_ITEM_INDEXISNOTNULL)AND(BATCH_ITEM_INDEX>=0)))'
);

SET @event_batch_linkage_check_action_sql = IF(
    @events_table_exists = 0,
    'SELECT * FROM `FAIL_CLOSED_EVENTS_TABLE_NOT_FOUND`',
    IF(
        @event_batch_linkage_check_any > 0 AND @event_batch_linkage_check_exact = 0,
        'SELECT * FROM `FAIL_CLOSED_EVENT_BATCH_LINKAGE_CHECK_INVALID_SPEC_REVIEW_REQUIRED`',
        IF(
            @event_batch_linkage_check_any = 0,
            'ALTER TABLE `assignment_schedule_leave_substitution_events` ADD CONSTRAINT '
            '`chk_assignment_schedule_leave_substitution_events_batch_linkage` CHECK ('
            '(batch_key IS NULL AND batch_item_index IS NULL)'
            ' OR (batch_key IS NOT NULL AND batch_item_index IS NOT NULL AND batch_item_index >= 0)'
            ')',
            'SELECT 1'
        )
    )
);

PREPARE stmt_event_batch_linkage_check FROM @event_batch_linkage_check_action_sql;
EXECUTE stmt_event_batch_linkage_check;
DEALLOCATE PREPARE stmt_event_batch_linkage_check;
