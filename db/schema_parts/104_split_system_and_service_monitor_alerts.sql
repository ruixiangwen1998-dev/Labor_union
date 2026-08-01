-- 檔案名稱: db/schema_parts/104_split_system_and_service_monitor_alerts.sql
-- 功能說明: 將舊版 system_alerts 服務監控資料安全改名，並建立獨立的業務流程警示表。
-- 本遷移可重複執行；不刪除既有監控事件。

SET @system_alerts_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts'
);
SET @system_has_event_type = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='event_type'
);
SET @system_has_alert_code = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='alert_code'
);
SET @service_monitor_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='service_monitor_alerts'
);

SET @migration_sql = IF(
    @system_alerts_exists=1
    AND @system_has_event_type=1
    AND @system_has_alert_code=0
    AND @service_monitor_exists=0,
    'RENAME TABLE system_alerts TO service_monitor_alerts',
    'SELECT 1'
);
PREPARE migration_stmt FROM @migration_sql;
EXECUTE migration_stmt;
DEALLOCATE PREPARE migration_stmt;

CREATE TABLE IF NOT EXISTS service_monitor_alerts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(50) NOT NULL COMMENT '異常事件類型',
    description TEXT NOT NULL COMMENT '詳細異常描述',
    status ENUM('pending', 'resolved') DEFAULT 'pending' COMMENT '處理狀態',
    component VARCHAR(100) NULL COMMENT '異常所屬服務元件',
    severity ENUM('warning','critical') NOT NULL DEFAULT 'warning',
    fingerprint VARCHAR(191) NULL COMMENT '相同異常的穩定識別碼',
    first_detected_at DATETIME NULL,
    last_detected_at DATETIME NULL,
    occurrence_count INT NOT NULL DEFAULT 1,
    details_json JSON NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP NULL,
    resolved_by VARCHAR(50) NULL,
    INDEX idx_alert_status (status),
    INDEX idx_alert_component_status (component, status, last_detected_at),
    INDEX idx_alert_fingerprint_status (fingerprint, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS system_alerts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    alert_code VARCHAR(50) NOT NULL COMMENT '異常代碼，例如 IMPORT-001, ORDER-001',
    source_domain VARCHAR(50) NOT NULL COMMENT '來源領域',
    case_key VARCHAR(100) NOT NULL COMMENT '案件識別鍵',
    reason VARCHAR(500) NOT NULL COMMENT '人類可讀的簡述',
    details JSON NOT NULL COMMENT '目前偵測到的異常內容，每次掃描直接覆蓋更新',
    status ENUM('open', 'claimed', 'resolved') NOT NULL DEFAULT 'open' COMMENT '處理狀態',
    claimed_by VARCHAR(100) NULL,
    claimed_at DATETIME NULL,
    resolved_by VARCHAR(100) NULL,
    resolved_at DATETIME NULL,
    resolution_reason VARCHAR(500) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_alert_case (alert_code, case_key),
    INDEX idx_system_alert_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @system_has_alert_code = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='system_alerts' AND COLUMN_NAME='alert_code'
);
SET @migration_sql = IF(
    @system_has_alert_code=1,
    'SELECT 1',
    'SIGNAL SQLSTATE ''45000'' SET MESSAGE_TEXT=''system_alerts migration requires manual review: both legacy and target tables already exist'''
);
PREPARE migration_stmt FROM @migration_sql;
EXECUTE migration_stmt;
DEALLOCATE PREPARE migration_stmt;
