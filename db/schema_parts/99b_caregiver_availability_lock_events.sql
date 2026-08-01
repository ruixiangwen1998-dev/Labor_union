-- 99b_caregiver_availability_lock_events.sql
-- 建立 append-only 鎖定生命週期稽核事件表。
-- 包含事件型別與原因契約 CHECK 約束、payload JSON Object CHECK 約束、
-- event_key 冪等全表唯一，外鍵刪除策略為 RESTRICT，
-- 及 2 個與 DatabaseSchemaLoader 相容的單一 Statement 機械阻斷 Triggers。

CREATE TABLE IF NOT EXISTS caregiver_availability_lock_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    lock_id BIGINT NOT NULL COMMENT '對應 caregiver_availability_locks.id',
    event_type ENUM('lock_acquired', 'lock_released', 'lock_converted', 'lock_cancelled') NOT NULL COMMENT '事件類型',
    event_key VARCHAR(100) NOT NULL COMMENT '呼叫端提供的全域唯一非空冪等鍵',
    actor VARCHAR(100) NOT NULL COMMENT '記錄事件的非空管理員識別',
    reason TEXT NULL COMMENT 'release/convert/cancel 的非空原因；acquired 為 NULL',
    payload JSON NOT NULL COMMENT '不可變 JSON Object 事件內容',
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '事件發生時間',
    UNIQUE KEY uq_availability_lock_event_key (event_key),
    INDEX idx_availability_lock_events_lock (lock_id, occurred_at),
    INDEX idx_availability_lock_events_type (event_type, occurred_at),
    CONSTRAINT fk_availability_lock_events_lock
        FOREIGN KEY (lock_id) REFERENCES caregiver_availability_locks (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT chk_availability_lock_events_reason
        CHECK (
            (event_type = 'lock_acquired' AND reason IS NULL)
            OR (event_type IN ('lock_released', 'lock_converted', 'lock_cancelled') AND CHAR_LENGTH(TRIM(reason)) > 0)
        ),
    CONSTRAINT chk_availability_lock_events_payload_object
        CHECK (JSON_TYPE(payload) = 'OBJECT'),
    CONSTRAINT chk_availability_lock_events_nonempty
        CHECK (CHAR_LENGTH(TRIM(event_key)) > 0 AND CHAR_LENGTH(TRIM(actor)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DROP TRIGGER IF EXISTS trg_caregiver_availability_lock_events_before_update;
CREATE TRIGGER trg_caregiver_availability_lock_events_before_update BEFORE UPDATE ON caregiver_availability_lock_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'caregiver_availability_lock_events records cannot be updated';

DROP TRIGGER IF EXISTS trg_caregiver_availability_lock_events_before_delete;
CREATE TRIGGER trg_caregiver_availability_lock_events_before_delete BEFORE DELETE ON caregiver_availability_lock_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'caregiver_availability_lock_events records cannot be deleted';
