# 月嫂 LIFF 驗證、LINE 主動監控、雙向恢復與 DB 結構防呆

## 更新摘要

本次只記錄目前 `origin/LINE-Bot-Wen` 尚未包含的 LINE 功能：

1. 月嫂透過 LIFF 輸入基本資料，和既有月嫂資料比對後交由工會人員人工核准並綁定 LINE。
2. LINE 系統改為背景主動監控，不需要打開管理頁才開始檢查。
3. 開發環境的 Monitor 與服務監督器改成同層獨立程序，可互相偵測、受控重啟，取消外層批次 watchdog。

對應尚未推送的主要提交：

- `3d69704 feat: add LIFF staff verification and approval binding`
- `c12b036 feat: add active monitoring and mutual service recovery`

## 新增檔案

### 月嫂 LIFF 驗證

- `line/static/staff_verification.html`：月嫂輸入姓名、身分證字號與生日的 LIFF 頁面。
- `api/routes/line_staff_verification.py`：驗證頁狀態查詢與資料送出 API。
- `api/schemas/line_staff_verification.py`：月嫂驗證 API 請求格式。
- `services/line_staff_verification_service.py`：一次性連結、LINE 身分確認、既有月嫂資料比對與結果保存。
- `db/schema_parts/98_line_staff_verification.sql`：月嫂驗證欄位及索引 migration。
- `tests/test_line_staff_verification.py`：資料比對、送出、人工核准及 LINE 綁定測試。

### LINE 主動監控與程序恢復

- `line/monitor.py`：獨立主動監控程序，並在服務監督器失聯時執行受控重啟。
- `services/line_health_checks.py`：FastAPI、DB、Worker、任務、LINE API、公開入口、LIFF、設定、磁碟及監督器心跳檢查。
- `services/line_monitor_service.py`：監控排程、防抖、心跳、DB／本機快照及異常／恢復事件。
- `services/runtime_supervision_service.py`：同層程序單例鎖、PID 解析、命令列驗證、安全終止與獨立重啟工具。
- `api/routes/line_monitoring.py`、`api/schemas/line_monitoring.py`：受保護的監控狀態與事件 API。
- `ui/components/line_health_monitor.py`：LINE 管理中心健康狀態與異常紀錄介面。
- `config/line_monitoring.json`：檢查間隔、防抖及警戒門檻。
- `db/schema_parts/97_line_active_monitoring.sql`：程序心跳、健康狀態與監控事件 migration。
- `tests/test_line_monitoring.py`：監控、心跳、防抖、API、單例鎖及雙向恢復測試。

## 主要修改

### 月嫂驗證與人工核准

- `line/line_bot.py`：收到「我是月嫂」時建立申請並傳送驗證頁連結。
- `line/static/gateway.html`：由既有 LIFF Gateway 安全導向月嫂驗證頁，避免 redirect URI HTTP 400。
- `services/line_review_service.py`：核准時綁定 `staff_id`、LINE 帳號、`staff` 角色與對應 Rich Menu。
- `ui/components/line_review_manager.py`：顯示送出資料、比對結果及遮蔽後的既有月嫂資料。
- `db/schema.sql`：擴充 `line_confirmation_requests` 驗證、比對、期限與嘗試次數欄位。

### 主動監控與雙向恢復

- `line/worker.py`：每 15 秒更新 Worker 心跳及工作迴圈進度。
- `start_fastapi_ngrok.py`：管理 FastAPI、ngrok、Streamlit，檢查 Monitor 快照並在失聯時重啟 Monitor。
- `start.bat`：直接啟動 Monitor 與服務監督器兩個同層程序；不再重複啟動 Streamlit。
- `online.bat`：正式／連線測試模式獨立啟動 LINE Monitor，不啟動 ngrok。
- `api/main.py`：掛載監控 API；Worker 仍由 FastAPI lifespan 管理。
- `api/routes/line_admin.py`：管理中心總覽改讀 Monitor 已保存的狀態。
- `ui/api_clients/line_api_client.py`、`ui/pages/07_line_management.py`：串接並顯示主動監控狀態及事件。
- `db/schema.sql`：新增 `service_heartbeats`、`system_health_status`，擴充 `system_alerts`。
- `.gitignore`：忽略 DB 故障時的 `.monitor_state` 本機快照及程序標記。

## 執行方式

開發環境使用：

```bat
start.bat
```

啟動後：

```text
LINE Monitor
  ↕ 心跳、PID、快照與受控重啟
服務監督器
  ├─ FastAPI
  ├─ ngrok
  └─ Streamlit
```

- 兩個同層程序都有單例鎖，防止重複啟動。
- 重啟前會核對 PID 的實際命令列，避免誤終止其他程序。
- 正常 Ctrl+C 會留下停機標記，不會被另一方誤判為故障。
- 異常、重啟及恢復會寫入 `system_alerts`，並顯示在 LINE 管理中心。

## 驗證結果

- LINE／監控／LIFF／任務／審查／啟動回歸：60 項通過。
- 完整測試：812 項通過。
- 另有 4 項既有訂單／表單 Streamlit AppTest 因固定 3 秒時限逾時，與本次 LINE 修改無關且和前次結果相同。
- migration 已套用開發 DB，未清空既有資料。
- 未建立或遺留一次性 Python 檔案。

## 2026-08-01 合併後追加修復

### 功能修正

- 新增監控用 DB Schema 主動檢查，會確認 `system_alerts`、`service_monitor_alerts`、`service_heartbeats` 與 `system_health_status` 的關鍵欄位。
- 監控資料寫入或讀取 DB 失敗時，不再靜默忽略或誤顯示正常；API 與 LINE 管理中心會顯示監控資料保存異常。
- 異常事件無法讀取時，管理中心不再顯示成「目前沒有異常紀錄」。
- 修復 `95_multi_caregiver_schedule.sql` 對 MySQL `CHECK_CONSTRAINTS.TABLE_NAME` 不存在欄位的引用，避免 `reset_DB.bat` 在 DROP 後中斷。

### 本次修改檔案

- `services/line_health_checks.py`：新增資料庫結構檢查。
- `services/line_monitor_service.py`：揭露持久化及讀取錯誤，並在本機快照標記 degraded／critical。
- `api/schemas/line_monitoring.py`：新增監控資料保存狀態欄位。
- `config/line_monitoring.json`：啟用 `database_schema` 定期檢查。
- `ui/components/line_health_monitor.py`：顯示 DB 結構及監控紀錄保存異常。
- `db/schema_parts/95_multi_caregiver_schedule.sql`：修復 CHECK constraint metadata 查詢。
- `tests/test_line_monitoring.py`、`tests/test_init_db_schema_parts.py`：新增回歸測試。

### 驗證結果

- 本機 `union_db` 已使用固定 v3 fixture 成功完整重建。
- 監控／migration／DB 重建目標測試：20 項通過。
- 完整測試：1569 項通過，0 項失敗，6 個既有棄用警告。
- 未建立或遺留一次性 Python 檔案。
