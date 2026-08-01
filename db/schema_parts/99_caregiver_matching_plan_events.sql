-- 99_caregiver_matching_plan_events.sql
-- 建立 append-only 配對方案與區段的操作、意願與發送事實事件表。
-- 包含事件型別與標的契約 CHECK 約束、payload JSON Object CHECK 約束、event_key 冪等全表唯一，
-- 及 2 個與 DatabaseSchemaLoader 相容的單一 Statement 機械阻斷 Triggers。

CREATE TABLE IF NOT EXISTS caregiver_matching_plan_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    plan_id BIGINT NOT NULL COMMENT '對應 caregiver_matching_plans.id',
    segment_id BIGINT NULL COMMENT '對應 caregiver_matching_plan_segments.id；方案層級事件為 NULL',
    event_type ENUM('info_1_sent', 'info_2_sent', 'willingness_changed', 'resume_sent', 'plan_cancelled') NOT NULL COMMENT '事件類型',
    event_key VARCHAR(100) NOT NULL COMMENT '呼叫端提供的全表唯一非空冪等鍵',
    actor VARCHAR(100) NOT NULL COMMENT '記錄事件的非空管理員識別',
    payload JSON NOT NULL COMMENT '事件型別限定的不可變 JSON 內容',
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '事件發生時間',
    UNIQUE KEY uq_caregiver_matching_plan_event_key (event_key),
    INDEX idx_caregiver_matching_plan_events_plan (plan_id, occurred_at),
    INDEX idx_caregiver_matching_plan_events_segment (segment_id, occurred_at),
    INDEX idx_caregiver_matching_plan_events_type (event_type, occurred_at),
    CONSTRAINT fk_caregiver_matching_plan_events_plan
        FOREIGN KEY (plan_id) REFERENCES caregiver_matching_plans (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT fk_caregiver_matching_plan_events_segment
        FOREIGN KEY (segment_id) REFERENCES caregiver_matching_plan_segments (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT chk_caregiver_matching_plan_events_target
        CHECK (
            (event_type IN ('info_1_sent', 'info_2_sent', 'willingness_changed', 'resume_sent') AND segment_id IS NOT NULL)
            OR (event_type = 'plan_cancelled' AND segment_id IS NULL)
        ),
    CONSTRAINT chk_caregiver_matching_plan_events_payload_object
        CHECK (JSON_TYPE(payload) = 'OBJECT'),
    CONSTRAINT chk_caregiver_matching_plan_events_nonempty
        CHECK (CHAR_LENGTH(TRIM(event_key)) > 0 AND CHAR_LENGTH(TRIM(actor)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DROP TRIGGER IF EXISTS trg_caregiver_matching_plan_events_before_update;
CREATE TRIGGER trg_caregiver_matching_plan_events_before_update BEFORE UPDATE ON caregiver_matching_plan_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'caregiver_matching_plan_events records cannot be updated';

DROP TRIGGER IF EXISTS trg_caregiver_matching_plan_events_before_delete;
CREATE TRIGGER trg_caregiver_matching_plan_events_before_delete BEFORE DELETE ON caregiver_matching_plan_events FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'caregiver_matching_plan_events records cannot be deleted';
