-- 檔案名稱: db/schema_parts/97_line_active_monitoring.sql
-- 功能說明: 建立程序心跳、健康狀態並擴充監控／服務重啟異常事件；可重複執行。
CREATE TABLE IF NOT EXISTS service_heartbeats (
    service_name VARCHAR(100) NOT NULL,
    instance_id VARCHAR(191) NOT NULL,
    status ENUM('healthy','warning','critical','maintenance') NOT NULL DEFAULT 'healthy',
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    details_json JSON NULL,
    PRIMARY KEY (service_name, instance_id),
    INDEX idx_service_heartbeat_seen (service_name, last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS system_health_status (
    check_name VARCHAR(100) PRIMARY KEY,
    component VARCHAR(100) NOT NULL,
    status ENUM('healthy','warning','critical','unknown','maintenance') NOT NULL DEFAULT 'unknown',
    raw_status ENUM('healthy','warning','critical','unknown','maintenance') NOT NULL DEFAULT 'unknown',
    message VARCHAR(500) NOT NULL,
    response_ms INT NULL,
    consecutive_failures INT NOT NULL DEFAULT 0,
    consecutive_successes INT NOT NULL DEFAULT 0,
    last_checked_at DATETIME NOT NULL,
    last_success_at DATETIME NULL,
    status_changed_at DATETIME NOT NULL,
    details_json JSON NULL,
    INDEX idx_health_component_status (component, status),
    INDEX idx_health_checked (last_checked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='component');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE system_alerts ADD COLUMN component VARCHAR(100) NULL AFTER status', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='severity');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE system_alerts ADD COLUMN severity ENUM(''warning'',''critical'') NOT NULL DEFAULT ''warning'' AFTER component', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='fingerprint');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE system_alerts ADD COLUMN fingerprint VARCHAR(191) NULL AFTER severity', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='first_detected_at');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE system_alerts ADD COLUMN first_detected_at DATETIME NULL AFTER fingerprint', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='last_detected_at');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE system_alerts ADD COLUMN last_detected_at DATETIME NULL AFTER first_detected_at', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='occurrence_count');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE system_alerts ADD COLUMN occurrence_count INT NOT NULL DEFAULT 1 AFTER last_detected_at', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='details_json');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE system_alerts ADD COLUMN details_json JSON NULL AFTER occurrence_count', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @index_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND INDEX_NAME='idx_alert_component_status');
SET @migration_sql = IF(@index_exists=0, 'ALTER TABLE system_alerts ADD INDEX idx_alert_component_status (component,status,last_detected_at)', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @index_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND INDEX_NAME='idx_alert_fingerprint_status');
SET @migration_sql = IF(@index_exists=0, 'ALTER TABLE system_alerts ADD INDEX idx_alert_fingerprint_status (fingerprint,status)', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;
