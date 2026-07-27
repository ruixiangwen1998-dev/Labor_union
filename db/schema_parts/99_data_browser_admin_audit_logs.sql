-- 建立與調整 Data Browser admin audit logs，採「先新增可空欄位→回填舊資料→再改為 NOT NULL」以確保部署可回滾且不中斷。
CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    action VARCHAR(64) NULL,
    table_name VARCHAR(64) NULL,
    pk_value VARCHAR(255) NULL,
    changed_fields JSON NULL,
    actor VARCHAR(128) NULL,
    role VARCHAR(64) NULL,
    request_id VARCHAR(128) NULL,
    before_hash CHAR(64) NULL,
    after_hash CHAR(64) NULL,
    changed_fields_hash CHAR(64) NULL,
    occurred_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_audit_logs_table_pk_time (table_name, pk_value, occurred_at),
    INDEX idx_audit_logs_request (request_id),
    INDEX idx_audit_logs_actor (actor)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @staff_audit_table_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'audit_logs'
);

SET @staff_audit_actor_sql = IF(
    @staff_audit_table_exists = 0 OR NOT EXISTS(
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'audit_logs'
          AND COLUMN_NAME = 'actor'
    ),
    'ALTER TABLE `audit_logs` ADD COLUMN `actor` VARCHAR(128) NULL AFTER `changed_fields`',
    'SELECT 1'
);
SET @staff_audit_role_sql = IF(
    @staff_audit_table_exists = 0 OR NOT EXISTS(
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'audit_logs'
          AND COLUMN_NAME = 'role'
    ),
    'ALTER TABLE `audit_logs` ADD COLUMN `role` VARCHAR(64) NULL AFTER `actor`',
    'SELECT 1'
);
SET @staff_audit_request_id_sql = IF(
    @staff_audit_table_exists = 0 OR NOT EXISTS(
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'audit_logs'
          AND COLUMN_NAME = 'request_id'
    ),
    'ALTER TABLE `audit_logs` ADD COLUMN `request_id` VARCHAR(128) NULL AFTER `role`',
    'SELECT 1'
);
SET @staff_audit_before_hash_sql = IF(
    @staff_audit_table_exists = 0 OR NOT EXISTS(
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'audit_logs'
          AND COLUMN_NAME = 'before_hash'
    ),
    'ALTER TABLE `audit_logs` ADD COLUMN `before_hash` CHAR(64) NULL AFTER `request_id`',
    'SELECT 1'
);
SET @staff_audit_after_hash_sql = IF(
    @staff_audit_table_exists = 0 OR NOT EXISTS(
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'audit_logs'
          AND COLUMN_NAME = 'after_hash'
    ),
    'ALTER TABLE `audit_logs` ADD COLUMN `after_hash` CHAR(64) NULL AFTER `before_hash`',
    'SELECT 1'
);
SET @staff_audit_occurred_at_sql = IF(
    @staff_audit_table_exists = 0 OR NOT EXISTS(
        SELECT 1
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'audit_logs'
          AND COLUMN_NAME = 'occurred_at'
    ),
    'ALTER TABLE `audit_logs` ADD COLUMN `occurred_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP',
    'SELECT 1'
);

PREPARE staff_audit_actor_stmt FROM @staff_audit_actor_sql;
EXECUTE staff_audit_actor_stmt;
DEALLOCATE PREPARE staff_audit_actor_stmt;
PREPARE staff_audit_role_stmt FROM @staff_audit_role_sql;
EXECUTE staff_audit_role_stmt;
DEALLOCATE PREPARE staff_audit_role_stmt;
PREPARE staff_audit_request_id_stmt FROM @staff_audit_request_id_sql;
EXECUTE staff_audit_request_id_stmt;
DEALLOCATE PREPARE staff_audit_request_id_stmt;
PREPARE staff_audit_before_hash_stmt FROM @staff_audit_before_hash_sql;
EXECUTE staff_audit_before_hash_stmt;
DEALLOCATE PREPARE staff_audit_before_hash_stmt;
PREPARE staff_audit_after_hash_stmt FROM @staff_audit_after_hash_sql;
EXECUTE staff_audit_after_hash_stmt;
DEALLOCATE PREPARE staff_audit_after_hash_stmt;
PREPARE staff_audit_occurred_at_stmt FROM @staff_audit_occurred_at_sql;
EXECUTE staff_audit_occurred_at_stmt;
DEALLOCATE PREPARE staff_audit_occurred_at_stmt;

UPDATE `audit_logs`
SET actor = COALESCE(
        NULLIF(TRIM(COALESCE(actor, '')), ''),
        NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(changed_fields, '$._audit_actor'))), ''),
        'system'
    ),
    role = COALESCE(
        NULLIF(TRIM(COALESCE(role, '')), ''),
        NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(changed_fields, '$._audit_role'))), ''),
        'admin'
    ),
    request_id = COALESCE(
        NULLIF(TRIM(COALESCE(request_id, '')), ''),
        NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(changed_fields, '$._audit_request_id'))), ''),
        UUID()
    ),
    before_hash = COALESCE(
        NULLIF(TRIM(COALESCE(before_hash, '')), ''),
        NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(changed_fields, '$._audit_before_hash'))), ''),
        REPEAT('0', 64)
    ),
    after_hash = COALESCE(
        NULLIF(TRIM(COALESCE(after_hash, '')), ''),
        NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(changed_fields, '$._audit_after_hash'))), ''),
        REPEAT('0', 64)
    ),
    occurred_at = COALESCE(occurred_at, NOW())
WHERE 1 = 1;

ALTER TABLE `audit_logs`
    MODIFY `action` VARCHAR(64) NOT NULL,
    MODIFY `table_name` VARCHAR(64) NOT NULL,
    MODIFY `pk_value` VARCHAR(255) NOT NULL,
    MODIFY `changed_fields` JSON NOT NULL,
    MODIFY `actor` VARCHAR(128) NOT NULL,
    MODIFY `role` VARCHAR(64) NOT NULL,
    MODIFY `request_id` VARCHAR(128) NOT NULL,
    MODIFY `before_hash` CHAR(64) NOT NULL,
    MODIFY `after_hash` CHAR(64) NOT NULL,
    MODIFY `occurred_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

-- 保留既有索引，避免重複添加
SET @staff_audit_idx_table_pk_time_sql = IF(
    NOT EXISTS(
        SELECT 1
        FROM information_schema.statistics
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'audit_logs'
          AND INDEX_NAME = 'idx_audit_logs_table_pk_time'
    ),
    'CREATE INDEX idx_audit_logs_table_pk_time ON `audit_logs` (table_name, pk_value, occurred_at)',
    'SELECT 1'
);
SET @staff_audit_idx_request_sql = IF(
    NOT EXISTS(
        SELECT 1
        FROM information_schema.statistics
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'audit_logs'
          AND INDEX_NAME = 'idx_audit_logs_request'
    ),
    'CREATE INDEX idx_audit_logs_request ON `audit_logs` (request_id)',
    'SELECT 1'
);
SET @staff_audit_idx_actor_sql = IF(
    NOT EXISTS(
        SELECT 1
        FROM information_schema.statistics
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'audit_logs'
          AND INDEX_NAME = 'idx_audit_logs_actor'
    ),
    'CREATE INDEX idx_audit_logs_actor ON `audit_logs` (actor)',
    'SELECT 1'
);

PREPARE staff_audit_idx_table_pk_time_stmt FROM @staff_audit_idx_table_pk_time_sql;
EXECUTE staff_audit_idx_table_pk_time_stmt;
DEALLOCATE PREPARE staff_audit_idx_table_pk_time_stmt;
PREPARE staff_audit_idx_request_stmt FROM @staff_audit_idx_request_sql;
EXECUTE staff_audit_idx_request_stmt;
DEALLOCATE PREPARE staff_audit_idx_request_stmt;
PREPARE staff_audit_idx_actor_stmt FROM @staff_audit_idx_actor_sql;
EXECUTE staff_audit_idx_actor_stmt;
DEALLOCATE PREPARE staff_audit_idx_actor_stmt;
