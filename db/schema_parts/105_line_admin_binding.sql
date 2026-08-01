-- 工會人員 LINE 與後台帳號一次性綁定請求；可由 scripts/init_db.py 重複執行。
CREATE TABLE IF NOT EXISTS line_admin_binding_requests (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    line_user_id VARCHAR(100) NOT NULL COMMENT '發起綁定的 LINE 使用者；必須與 LIFF 驗證結果一致',
    token_hash CHAR(64) NOT NULL COMMENT '一次性 Token 的 SHA-256；不得保存原始 Token',
    status ENUM('pending','completed','expired','locked','cancelled') NOT NULL DEFAULT 'pending',
    expires_at DATETIME NOT NULL,
    attempt_count INT NOT NULL DEFAULT 0,
    admin_user_id BIGINT NULL COMMENT '成功綁定後對應 admin_users.id',
    last_attempt_at DATETIME NULL,
    completed_at DATETIME NULL,
    cancelled_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_line_admin_binding_token (token_hash),
    INDEX idx_line_admin_binding_pending (line_user_id, status, expires_at),
    INDEX idx_line_admin_binding_admin (admin_user_id, completed_at),
    CONSTRAINT fk_line_admin_binding_line_user FOREIGN KEY (line_user_id)
        REFERENCES line_users(line_user_id) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_line_admin_binding_admin_user FOREIGN KEY (admin_user_id)
        REFERENCES admin_users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
