-- 月嫂 LIFF 基本資料比對與 staff.id 綁定欄位；可由 init_db.py 重複執行。
SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND COLUMN_NAME='matched_staff_id');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE line_confirmation_requests ADD COLUMN matched_staff_id INT NULL COMMENT ''月嫂資料比對成功後對應 staff.id；核准前不直接綁定'' AFTER new_line_user_id', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND COLUMN_NAME='match_status');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE line_confirmation_requests ADD COLUMN match_status ENUM(''not_submitted'',''matched'',''not_found'',''conflict'',''already_bound'') NOT NULL DEFAULT ''not_submitted'' AFTER matched_staff_id', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND COLUMN_NAME='submitted_name');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE line_confirmation_requests ADD COLUMN submitted_name VARCHAR(100) NULL AFTER match_status', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND COLUMN_NAME='submitted_birthday');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE line_confirmation_requests ADD COLUMN submitted_birthday DATE NULL AFTER submitted_name', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND COLUMN_NAME='submitted_identity_last4');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE line_confirmation_requests ADD COLUMN submitted_identity_last4 VARCHAR(4) NULL AFTER submitted_birthday', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND COLUMN_NAME='verification_token_hash');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE line_confirmation_requests ADD COLUMN verification_token_hash CHAR(64) NULL AFTER submitted_identity_last4', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND COLUMN_NAME='verification_token_expires_at');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE line_confirmation_requests ADD COLUMN verification_token_expires_at DATETIME NULL AFTER verification_token_hash', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND COLUMN_NAME='submission_attempts');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE line_confirmation_requests ADD COLUMN submission_attempts INT NOT NULL DEFAULT 0 AFTER verification_token_expires_at', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @column_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND COLUMN_NAME='submitted_at');
SET @migration_sql = IF(@column_exists=0, 'ALTER TABLE line_confirmation_requests ADD COLUMN submitted_at DATETIME NULL AFTER submission_attempts', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @index_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND INDEX_NAME='uk_confirmation_verification_token');
SET @migration_sql = IF(@index_exists=0, 'ALTER TABLE line_confirmation_requests ADD UNIQUE INDEX uk_confirmation_verification_token (verification_token_hash)', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @index_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND INDEX_NAME='idx_confirmation_matched_staff');
SET @migration_sql = IF(@index_exists=0, 'ALTER TABLE line_confirmation_requests ADD INDEX idx_confirmation_matched_staff (matched_staff_id,status)', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;

SET @fk_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME='line_confirmation_requests' AND CONSTRAINT_NAME='fk_confirmation_matched_staff' AND CONSTRAINT_TYPE='FOREIGN KEY');
SET @migration_sql = IF(@fk_exists=0, 'ALTER TABLE line_confirmation_requests ADD CONSTRAINT fk_confirmation_matched_staff FOREIGN KEY (matched_staff_id) REFERENCES staff(id) ON DELETE RESTRICT', 'SELECT 1');
PREPARE migration_stmt FROM @migration_sql; EXECUTE migration_stmt; DEALLOCATE PREPARE migration_stmt;
