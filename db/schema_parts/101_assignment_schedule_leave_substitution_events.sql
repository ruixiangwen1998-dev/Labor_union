-- 101_assignment_schedule_leave_substitution_events.sql
-- 記錄正式服務指派在單日休假/順延/代班流程中的事件事實。
-- 僅 append-only，無任何歷史修補與回填邏輯；欄位皆附上明確約束，
-- 供後續交易流程在單一交易中寫入核對快照。

CREATE TABLE IF NOT EXISTS assignment_schedule_leave_substitution_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    case_no VARCHAR(50) NOT NULL COMMENT '事件所屬案件（對應 orders.case_no）',
    original_assignment_id BIGINT NOT NULL COMMENT '請假日原始正式服務指派 id',
    original_schedule_id INT NOT NULL COMMENT '被處置之日排班 id',
    work_date DATE NOT NULL COMMENT '被處置之休假日期',
    resolution_type ENUM('leave_only', 'defer_following_assignments', 'substitute') NOT NULL COMMENT '處置類型',
    substitute_assignment_id BIGINT NULL COMMENT '只在 substitute 時為非空',
    event_key VARCHAR(100) NOT NULL COMMENT '呼叫端提供的全域唯一冪等鍵',
    actor VARCHAR(100) NOT NULL COMMENT '執行者管理員識別',
    reason VARCHAR(255) NOT NULL COMMENT '非空原因',
    schedule_snapshot JSON NOT NULL COMMENT '原排班/順延/代班日套用前後快照',
    payroll_snapshot JSON NOT NULL COMMENT '原 assignment 與代班 assignment 的核對快照',
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '事件發生時間',
    UNIQUE KEY uq_assignment_schedule_leave_substitution_event_key (event_key),
    INDEX idx_assignment_schedule_leave_substitution_event_case_time (case_no, occurred_at),
    INDEX idx_assignment_schedule_leave_substitution_event_assignments (original_assignment_id, substitute_assignment_id, work_date),
    CONSTRAINT fk_assignment_schedule_leave_substitution_event_case_no
        FOREIGN KEY (case_no) REFERENCES orders(case_no)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT fk_assignment_schedule_leave_substitution_original_assignment
        FOREIGN KEY (original_assignment_id) REFERENCES case_staff_assignments(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT fk_assignment_schedule_leave_substitution_substitute_assignment
        FOREIGN KEY (substitute_assignment_id) REFERENCES case_staff_assignments(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT fk_assignment_schedule_leave_substitution_original_schedule
        FOREIGN KEY (original_schedule_id) REFERENCES staff_schedule(id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT chk_assignment_schedule_leave_substitution_resolution
        CHECK (
            (resolution_type = 'substitute' AND substitute_assignment_id IS NOT NULL AND substitute_assignment_id <> original_assignment_id)
            OR (resolution_type IN ('leave_only', 'defer_following_assignments') AND substitute_assignment_id IS NULL)
        ),
    CONSTRAINT chk_leave_sub_actor_reason_key
        CHECK (CHAR_LENGTH(TRIM(event_key)) > 0 AND CHAR_LENGTH(TRIM(actor)) > 0 AND CHAR_LENGTH(TRIM(reason)) > 0),
    CONSTRAINT chk_leave_sub_schedule_snapshot
        CHECK (JSON_TYPE(schedule_snapshot) = 'OBJECT'),
    CONSTRAINT chk_leave_sub_payroll_snapshot
        CHECK (JSON_TYPE(payroll_snapshot) = 'OBJECT')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DROP TRIGGER IF EXISTS trg_assignment_schedule_leave_substitution_events_before_update;
CREATE TRIGGER trg_assignment_schedule_leave_substitution_events_before_update
BEFORE UPDATE ON assignment_schedule_leave_substitution_events
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'assignment_schedule_leave_substitution_events records cannot be updated';

DROP TRIGGER IF EXISTS trg_assignment_schedule_leave_substitution_events_before_delete;
CREATE TRIGGER trg_assignment_schedule_leave_substitution_events_before_delete
BEFORE DELETE ON assignment_schedule_leave_substitution_events
FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'assignment_schedule_leave_substitution_events records cannot be deleted';
