-- 98_caregiver_matching_plans.sql
-- 建立洽談中訂單案件的配對方案 Header 表與連續服務區段 Detail 表。
-- 支援版本控管、同一案件唯一有效版本、最多四個連續區段及同一月嫂在單一版本內唯一。
-- 外鍵刪除策略使用 RESTRICT，維護歷史配對紀錄不可連帶刪除。
-- 包含與 DatabaseSchemaLoader 相容的單一 Statement 4 個 BEFORE UPDATE/DELETE 機械阻斷 Triggers。

CREATE TABLE IF NOT EXISTS caregiver_matching_plans (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    case_no VARCHAR(50) NOT NULL COMMENT '洽談中訂單案件編號；對應 orders.case_no',
    version INT NOT NULL DEFAULT 1 COMMENT '配對方案版本號 (1, 2, ...)',
    status ENUM('draft', 'proposed', 'accepted', 'rejected', 'superseded', 'cancelled') NOT NULL DEFAULT 'draft' COMMENT '配對方案狀態',
    is_active TINYINT(1) NULL COMMENT '1表示該案件目前有效版本；歷史版本或無效版本為 NULL 以支援 UNIQUE(case_no, is_active)',
    start_date DATE NOT NULL COMMENT '本方案完整服務開始日',
    end_date DATE NOT NULL COMMENT '本方案完整服務結束日',
    created_by VARCHAR(100) NOT NULL COMMENT '建立方案版本的非空管理員識別',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_caregiver_matching_plan_case_version (case_no, version),
    UNIQUE KEY uq_caregiver_matching_plan_active (case_no, is_active),
    INDEX idx_caregiver_matching_plan_status (status, created_at),
    CONSTRAINT fk_caregiver_matching_plans_case_no
        FOREIGN KEY (case_no) REFERENCES orders (case_no)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT chk_caregiver_matching_plans_created_by
        CHECK (created_by IS NOT NULL AND CHAR_LENGTH(TRIM(created_by)) > 0),
    CONSTRAINT chk_caregiver_matching_plans_version
        CHECK (version >= 1),
    CONSTRAINT chk_caregiver_matching_plans_dates
        CHECK (start_date <= end_date),
    CONSTRAINT chk_caregiver_matching_plans_is_active
        CHECK (is_active IS NULL OR is_active = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS caregiver_matching_plan_segments (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    plan_id BIGINT NOT NULL COMMENT '對應 caregiver_matching_plans.id',
    segment_order TINYINT NOT NULL COMMENT '服務區段順序 (1 至 4)',
    staff_id INT NOT NULL COMMENT '月嫂識別；對應 staff.id',
    assigned_start_date DATE NOT NULL COMMENT '該區段預計服務開始日',
    assigned_end_date DATE NOT NULL COMMENT '該區段預計服務結束日',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_matching_plan_segment_order (plan_id, segment_order),
    UNIQUE KEY uq_matching_plan_staff (plan_id, staff_id),
    INDEX idx_matching_plan_segment_staff (staff_id, assigned_start_date, assigned_end_date),
    CONSTRAINT fk_matching_plan_segments_plan
        FOREIGN KEY (plan_id) REFERENCES caregiver_matching_plans (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT fk_matching_plan_segments_staff
        FOREIGN KEY (staff_id) REFERENCES staff (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT chk_matching_plan_segments_order
        CHECK (segment_order BETWEEN 1 AND 4),
    CONSTRAINT chk_matching_plan_segments_dates
        CHECK (assigned_start_date <= assigned_end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 舊環境的表可能早於起訖欄位；先補欄位、依既有區段回填，再收斂為 NOT NULL。
SET @matching_plan_start_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'caregiver_matching_plans'
      AND COLUMN_NAME = 'start_date'
);
SET @matching_plan_boundary_sql = IF(
    @matching_plan_start_exists = 0,
    'ALTER TABLE `caregiver_matching_plans` ADD COLUMN `start_date` DATE NULL AFTER `is_active`',
    'SELECT 1'
);
PREPARE matching_plan_boundary_stmt FROM @matching_plan_boundary_sql;
EXECUTE matching_plan_boundary_stmt;
DEALLOCATE PREPARE matching_plan_boundary_stmt;

SET @matching_plan_end_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'caregiver_matching_plans'
      AND COLUMN_NAME = 'end_date'
);
SET @matching_plan_boundary_sql = IF(
    @matching_plan_end_exists = 0,
    'ALTER TABLE `caregiver_matching_plans` ADD COLUMN `end_date` DATE NULL AFTER `start_date`',
    'SELECT 1'
);
PREPARE matching_plan_boundary_stmt FROM @matching_plan_boundary_sql;
EXECUTE matching_plan_boundary_stmt;
DEALLOCATE PREPARE matching_plan_boundary_stmt;

UPDATE caregiver_matching_plans p
JOIN (
    SELECT plan_id,
           MIN(assigned_start_date) AS start_date,
           MAX(assigned_end_date) AS end_date
    FROM caregiver_matching_plan_segments
    GROUP BY plan_id
) bounds ON bounds.plan_id = p.id
SET p.start_date = COALESCE(p.start_date, bounds.start_date),
    p.end_date = COALESCE(p.end_date, bounds.end_date)
WHERE p.start_date IS NULL OR p.end_date IS NULL;

ALTER TABLE caregiver_matching_plans
    MODIFY COLUMN start_date DATE NOT NULL COMMENT '本方案完整服務開始日',
    MODIFY COLUMN end_date DATE NOT NULL COMMENT '本方案完整服務結束日';

DROP TRIGGER IF EXISTS trg_caregiver_matching_plans_before_update;
CREATE TRIGGER trg_caregiver_matching_plans_before_update BEFORE UPDATE ON caregiver_matching_plans FOR EACH ROW SET NEW.created_by = IF(OLD.id <=> NEW.id AND OLD.case_no <=> NEW.case_no AND OLD.version <=> NEW.version AND OLD.start_date <=> NEW.start_date AND OLD.end_date <=> NEW.end_date AND OLD.created_by <=> NEW.created_by AND OLD.created_at <=> NEW.created_at, NEW.created_by, NULL);

DROP TRIGGER IF EXISTS trg_caregiver_matching_plans_before_delete;
CREATE TRIGGER trg_caregiver_matching_plans_before_delete BEFORE DELETE ON caregiver_matching_plans FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'caregiver_matching_plans records cannot be deleted';

DROP TRIGGER IF EXISTS trg_caregiver_matching_plan_segments_before_update;
CREATE TRIGGER trg_caregiver_matching_plan_segments_before_update BEFORE UPDATE ON caregiver_matching_plan_segments FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'caregiver_matching_plan_segments records cannot be updated';

DROP TRIGGER IF EXISTS trg_caregiver_matching_plan_segments_before_delete;
CREATE TRIGGER trg_caregiver_matching_plan_segments_before_delete BEFORE DELETE ON caregiver_matching_plan_segments FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'caregiver_matching_plan_segments records cannot be deleted';
