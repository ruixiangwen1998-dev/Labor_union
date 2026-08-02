-- 檔案名稱: db/schema_parts/106_line_alert_notifications.sql
-- 功能說明: 建立 LINE 服務異常通知對象與可靠派送紀錄，支援工會人員及通知群組。

CREATE TABLE IF NOT EXISTS line_alert_notification_targets (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    target_type ENUM('user','group') NOT NULL,
    admin_user_id BIGINT NULL COMMENT '個人通知對應的後台帳號；發送時取得最新 linked_line_user_id',
    line_target_id VARCHAR(100) NULL COMMENT '群組通知的 LINE groupId；個人通知不重複保存 userId',
    display_name VARCHAR(100) NOT NULL,
    minimum_severity ENUM('warning','critical') NOT NULL DEFAULT 'critical',
    notify_recovery BOOLEAN NOT NULL DEFAULT TRUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    verified_at DATETIME NULL,
    last_tested_at DATETIME NULL,
    created_by_admin_user_id BIGINT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_line_alert_target_group (target_type, line_target_id),
    UNIQUE KEY uk_line_alert_target_admin (admin_user_id),
    INDEX idx_line_alert_target_enabled (enabled, minimum_severity),
    CONSTRAINT fk_line_alert_target_admin FOREIGN KEY (admin_user_id)
        REFERENCES admin_users(id) ON DELETE CASCADE,
    CONSTRAINT fk_line_alert_target_creator FOREIGN KEY (created_by_admin_user_id)
        REFERENCES admin_users(id) ON DELETE SET NULL,
    CONSTRAINT chk_line_alert_target_identity CHECK (
        (target_type='user' AND admin_user_id IS NOT NULL AND line_target_id IS NULL)
        OR (target_type='group' AND admin_user_id IS NULL AND line_target_id IS NOT NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS line_alert_deliveries (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    monitor_alert_id INT NULL,
    target_id BIGINT NOT NULL,
    transition ENUM('opened','escalated','recovered','reminder','test') NOT NULL,
    severity ENUM('warning','critical') NOT NULL,
    status ENUM('pending','processing','retry_scheduled','sent','failed','cancelled')
        NOT NULL DEFAULT 'pending',
    idempotency_key VARCHAR(191) NOT NULL,
    line_retry_key CHAR(36) NOT NULL,
    payload_json JSON NOT NULL,
    attempt_count INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    next_retry_at DATETIME NULL,
    processing_started_at DATETIME NULL,
    sent_at DATETIME NULL,
    failed_at DATETIME NULL,
    error_code VARCHAR(100) NULL,
    error_message TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_line_alert_delivery_idempotency (idempotency_key),
    INDEX idx_line_alert_delivery_due (status, next_retry_at, id),
    INDEX idx_line_alert_delivery_alert (monitor_alert_id, target_id, transition),
    CONSTRAINT fk_line_alert_delivery_monitor FOREIGN KEY (monitor_alert_id)
        REFERENCES service_monitor_alerts(id) ON DELETE CASCADE,
    CONSTRAINT fk_line_alert_delivery_target FOREIGN KEY (target_id)
        REFERENCES line_alert_notification_targets(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

