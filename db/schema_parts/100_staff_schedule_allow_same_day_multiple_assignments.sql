-- 允許同一位月嫂同日有多個 assignment 的排班列，移除舊的 staff/date 唯一鍵。
SET @staff_schedule_table_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
);

SET @staff_schedule_staff_date_unique_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'staff_schedule'
      AND INDEX_NAME = 'ukey_staff_date'
);

SET @staff_schedule_drop_unique_sql = IF(
    @staff_schedule_table_exists = 1
        AND @staff_schedule_staff_date_unique_exists = 1,
    'ALTER TABLE `staff_schedule` DROP INDEX `ukey_staff_date`',
    'SELECT 1'
);

PREPARE staff_schedule_drop_unique_stmt FROM @staff_schedule_drop_unique_sql;
EXECUTE staff_schedule_drop_unique_stmt;
DEALLOCATE PREPARE staff_schedule_drop_unique_stmt;
