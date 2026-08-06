-- 檔案名稱: db/schema_parts/107_line_order_groups.sql
-- 功能說明: 建立訂單 LINE 群組生命週期、預期成員與邀請派送追蹤資料表

SET @index_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='orders'
      AND INDEX_NAME='uk_orders_line_group_id'
);
SET @migration_sql = IF(
    @index_exists=0,
    'ALTER TABLE orders ADD UNIQUE INDEX uk_orders_line_group_id (line_group_id)',
    'SELECT 1'
);
PREPARE stmt FROM @migration_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS line_order_group_bindings (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    case_no VARCHAR(50) NOT NULL,
    line_group_id VARCHAR(100) NOT NULL,
    status ENUM('awaiting_invite','inviting','active','left','replaced','cancelled')
        NOT NULL DEFAULT 'awaiting_invite',
    bound_by_admin_user_id BIGINT NOT NULL,
    bound_by_line_user_id VARCHAR(100) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deactivated_at DATETIME NULL,
    UNIQUE KEY uk_line_order_group_id (line_group_id),
    INDEX idx_line_order_group_case_status (case_no, status, created_at),
    INDEX idx_line_order_group_status_time (status, updated_at),
    CONSTRAINT fk_line_order_group_case FOREIGN KEY (case_no)
        REFERENCES orders(case_no) ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_line_order_group_admin FOREIGN KEY (bound_by_admin_user_id)
        REFERENCES admin_users(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS line_order_group_members (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    binding_id BIGINT NOT NULL,
    participant_type ENUM('client','staff') NOT NULL,
    participant_record_id INT NOT NULL COMMENT '依 participant_type 對應 clients.id 或 staff.id',
    line_user_id VARCHAR(100) NULL,
    invitation_status ENUM('not_ready','pending','sent','joined','failed','left')
        NOT NULL DEFAULT 'not_ready',
    invite_task_id BIGINT NULL,
    sent_at DATETIME NULL,
    joined_at DATETIME NULL,
    left_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_line_order_group_member (binding_id, participant_type, participant_record_id),
    INDEX idx_line_order_group_member_line (line_user_id, invitation_status),
    INDEX idx_line_order_group_member_task (invite_task_id),
    CONSTRAINT fk_line_order_group_member_binding FOREIGN KEY (binding_id)
        REFERENCES line_order_group_bindings(id) ON DELETE CASCADE,
    CONSTRAINT fk_line_order_group_member_task FOREIGN KEY (invite_task_id)
        REFERENCES line_tasks(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
