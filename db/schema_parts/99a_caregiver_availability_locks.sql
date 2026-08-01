-- 99a_caregiver_availability_locks.sql
-- 建立等待訂金階段的配對方案鎖定批次 Header 表與逐月嫂逐日占用 Detail 表。
-- 包含狀態生命週期 CHECK 約束 (要求 released_by trim 後非空)、TIMESTAMP 顯式 NOT NULL、
-- UNIQUE 鍵防同方案/同月嫂同日重複 active 鎖定，外鍵刪除策略一律為 RESTRICT，
-- 並含 4 個與 DatabaseSchemaLoader 相容的單一 Statement 機械阻斷 Triggers。

CREATE TABLE IF NOT EXISTS caregiver_availability_locks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    plan_id BIGINT NOT NULL COMMENT '對應 caregiver_matching_plans.id',
    status ENUM('active', 'released', 'converted', 'cancelled') NOT NULL DEFAULT 'active' COMMENT '鎖定批次狀態',
    is_active TINYINT(1) NULL COMMENT '1表示該方案目前有效鎖定批次；歷史/無效為 NULL 以支援 UNIQUE(plan_id, is_active)',
    created_by VARCHAR(100) NOT NULL COMMENT '建立鎖定批次的非空管理員識別',
    released_by VARCHAR(100) NULL COMMENT '解除/轉換/取消鎖定批次的管理員識別',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '建立時間',
    released_at TIMESTAMP NULL COMMENT '解除/轉換/取消時間',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新時間',
    UNIQUE KEY uq_availability_lock_plan_active (plan_id, is_active),
    INDEX idx_availability_locks_status (status, created_at),
    CONSTRAINT fk_availability_locks_plan
        FOREIGN KEY (plan_id) REFERENCES caregiver_matching_plans (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT chk_availability_locks_status_state
        CHECK (
            (status = 'active' AND is_active = 1 AND released_by IS NULL AND released_at IS NULL)
            OR (status IN ('released', 'converted', 'cancelled') AND is_active IS NULL AND CHAR_LENGTH(TRIM(released_by)) > 0 AND released_at IS NOT NULL)
        ),
    CONSTRAINT chk_availability_locks_created_by
        CHECK (CHAR_LENGTH(TRIM(created_by)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS caregiver_availability_lock_days (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    lock_id BIGINT NOT NULL COMMENT '對應 caregiver_availability_locks.id',
    segment_id BIGINT NOT NULL COMMENT '對應 caregiver_matching_plan_segments.id',
    staff_id INT NOT NULL COMMENT '月嫂識別；對應 staff.id',
    lock_date DATE NOT NULL COMMENT '等待訂金占用日期',
    active_marker TINYINT(1) NULL COMMENT '1表示該月嫂該日有效等待訂金鎖；已解除為 NULL 以支援 UNIQUE(staff_id, lock_date, active_marker)',
    released_by VARCHAR(100) NULL COMMENT '解除鎖定的管理員識別',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '建立時間',
    released_at TIMESTAMP NULL COMMENT '解除時間',
    UNIQUE KEY uq_availability_lock_staff_date_active (staff_id, lock_date, active_marker),
    UNIQUE KEY uq_availability_lock_segment_date (lock_id, segment_id, lock_date),
    INDEX idx_availability_lock_days_segment (segment_id, lock_date),
    CONSTRAINT fk_availability_lock_days_lock
        FOREIGN KEY (lock_id) REFERENCES caregiver_availability_locks (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT fk_availability_lock_days_segment
        FOREIGN KEY (segment_id) REFERENCES caregiver_matching_plan_segments (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT fk_availability_lock_days_staff
        FOREIGN KEY (staff_id) REFERENCES staff (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT chk_availability_lock_days_active_state
        CHECK (
            (active_marker = 1 AND released_by IS NULL AND released_at IS NULL)
            OR (active_marker IS NULL AND CHAR_LENGTH(TRIM(released_by)) > 0 AND released_at IS NOT NULL)
        )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DROP TRIGGER IF EXISTS trg_caregiver_availability_locks_before_update;
CREATE TRIGGER trg_caregiver_availability_locks_before_update BEFORE UPDATE ON caregiver_availability_locks FOR EACH ROW SET NEW.created_by = IF(OLD.id <=> NEW.id AND OLD.plan_id <=> NEW.plan_id AND OLD.created_by <=> NEW.created_by AND OLD.created_at <=> NEW.created_at, NEW.created_by, NULL);

DROP TRIGGER IF EXISTS trg_caregiver_availability_locks_before_delete;
CREATE TRIGGER trg_caregiver_availability_locks_before_delete BEFORE DELETE ON caregiver_availability_locks FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'caregiver_availability_locks records cannot be deleted';

DROP TRIGGER IF EXISTS trg_caregiver_availability_lock_days_before_update;
CREATE TRIGGER trg_caregiver_availability_lock_days_before_update BEFORE UPDATE ON caregiver_availability_lock_days FOR EACH ROW SET NEW.lock_id = IF(OLD.id <=> NEW.id AND OLD.lock_id <=> NEW.lock_id AND OLD.segment_id <=> NEW.segment_id AND OLD.staff_id <=> NEW.staff_id AND OLD.lock_date <=> NEW.lock_date AND OLD.created_at <=> NEW.created_at, NEW.lock_id, NULL);

DROP TRIGGER IF EXISTS trg_caregiver_availability_lock_days_before_delete;
CREATE TRIGGER trg_caregiver_availability_lock_days_before_delete BEFORE DELETE ON caregiver_availability_lock_days FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'caregiver_availability_lock_days records cannot be deleted';
