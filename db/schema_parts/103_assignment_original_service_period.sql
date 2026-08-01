-- 保存 assignment 初次建立的服務區段，讓調整前／調整後可被穩定查詢。
SET @assignment_original_start_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'case_staff_assignments'
      AND COLUMN_NAME = 'original_assigned_start_date'
);
SET @assignment_original_period_sql = IF(
    @assignment_original_start_exists = 0,
    'ALTER TABLE `case_staff_assignments` ADD COLUMN `original_assigned_start_date` DATE NULL AFTER `assigned_end_date`',
    'SELECT 1'
);
PREPARE assignment_original_period_stmt FROM @assignment_original_period_sql;
EXECUTE assignment_original_period_stmt;
DEALLOCATE PREPARE assignment_original_period_stmt;

SET @assignment_original_end_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'case_staff_assignments'
      AND COLUMN_NAME = 'original_assigned_end_date'
);
SET @assignment_original_period_sql = IF(
    @assignment_original_end_exists = 0,
    'ALTER TABLE `case_staff_assignments` ADD COLUMN `original_assigned_end_date` DATE NULL AFTER `original_assigned_start_date`',
    'SELECT 1'
);
PREPARE assignment_original_period_stmt FROM @assignment_original_period_sql;
EXECUTE assignment_original_period_stmt;
DEALLOCATE PREPARE assignment_original_period_stmt;

UPDATE case_staff_assignments
SET original_assigned_start_date = COALESCE(original_assigned_start_date, assigned_start_date),
    original_assigned_end_date = COALESCE(original_assigned_end_date, assigned_end_date)
WHERE original_assigned_start_date IS NULL OR original_assigned_end_date IS NULL;

DROP TRIGGER IF EXISTS trg_case_staff_assignments_original_period_insert;
CREATE TRIGGER trg_case_staff_assignments_original_period_insert
BEFORE INSERT ON case_staff_assignments
FOR EACH ROW
SET NEW.original_assigned_start_date = COALESCE(NEW.original_assigned_start_date, NEW.assigned_start_date),
    NEW.original_assigned_end_date = COALESCE(NEW.original_assigned_end_date, NEW.assigned_end_date);

DROP TRIGGER IF EXISTS trg_case_staff_assignments_original_period_update;
CREATE TRIGGER trg_case_staff_assignments_original_period_update
BEFORE UPDATE ON case_staff_assignments
FOR EACH ROW
SET NEW.original_assigned_start_date = OLD.original_assigned_start_date,
    NEW.original_assigned_end_date = OLD.original_assigned_end_date;
