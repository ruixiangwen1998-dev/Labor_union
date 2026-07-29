## 📌 更新摘要
本次更新 LINE 管理中心與系統後台，涵蓋了管理員驗證、訊息範本管理、排程任務管理、圖文選單管理、LIFF 設定管理以及人工審查中心等功能，並優化了系統啟動與服務架構。

## 🆕 新增功能與檔案

- **系統安全與管理員登入**
  - 新增管理員驗證服務與 API 路由 (`services/admin_auth_service.py`, `api/routes/admin_auth.py`, `api/dependencies/admin_auth.py`)。
  - 擴充資料庫表：新增 `admin_users`, `admin_sessions`, `admin_audit_logs` 以記錄管理員操作。
  - 新增命令列工具 `scripts/create_admin.py` 用於安全建立管理員帳號。
  - 支援開發模式下透過 `ENABLE_ADMIN_AUTH` 變數略過登入流程。

- **LINE 後台管理 UI (Streamlit)**
  - 新增 LINE 管理中心分頁與介面骨架 (`ui/pages/07_line_management.py`)。
  - 新增各項管理元件：
    - `ui/components/line_message_manager.py` (訊息範本建立與管理)
    - `ui/components/line_schedule_manager.py` (排程推播管理)
    - `ui/components/line_task_manager.py` (Worker 任務監控與管理)
    - `ui/components/line_rich_menu_manager.py` (圖文選單上傳與發布管理)
    - `ui/components/line_liff_manager.py` (LIFF 頁面設定管理)
    - `ui/components/line_review_manager.py` (月嫂驗證與綁定之人工審查管理)
  - 新增 Streamlit 專用 API Client (`ui/api_clients/line_api_client.py`) 介接後端功能。

- **多媒體與圖文選單服務**
  - 新增多媒體儲存服務 (`services/media_storage_service.py`) 提供檔案驗證與雜湊管理。
  - 新增圖文選單發布服務 (`services/line_rich_menu_service.py`)。

- **LIFF 與人工審查服務**
  - 新增 LIFF 設定與身分驗證服務 (`services/line_liff_config_service.py`, `services/line_liff_identity_service.py`)。
  - 新增統一審查中心服務 (`services/line_review_service.py`) 用於處理與記錄人工審批。

## 🔄 修改與優化檔案

- **啟動器與設定環境**
  - 將開發啟動器移至專案根目錄，並更名為 `start_fastapi_ngrok.py` (原 `line/start_line_bot.py`)。
  - 更新 `start.bat` 與 `online.bat`，確保環境變數 (`INTERNAL_API_KEY`) 正確讀取與虛擬環境一致性。
- **資料庫與資料結構**
  - 更新 `db/schema.sql`，擴充 `line_task_attempts`、`media_assets`、`line_rich_menu_publications` 等結構，以支援後端系統功能。
- **後端路由與設定 JSON**
  - 擴充 `api/routes/line_system_config.py`，加入寫入鎖定、版本衝突防護 (If-Match) 及操作稽核保護。
  - 更新 `config/line_menu.json` 及 `config/liff_settings.json` 為 v2 版本。
- **程式碼與介面優化**
  - 為 LINE 相關 Python 模組統一補齊中文「檔案名稱／功能說明」檔頭註解。
  - 簡化了服務人員在 LINE 管理中心的介面，隱藏冗長的工程細節與 JSON 資料，提升實際操作體驗。

## 🗑️ 刪除/重新命名檔案
- `ui/services` 資料夾重新命名為 `ui/api_clients`，解決了與後端 `services` 模組撞名的問題。

---

## 月嫂 LIFF 資料驗證與人工核准綁定

### 新增檔案

- `line/static/staff_verification.html`：月嫂輸入姓名、身分證字號與生日的 LIFF 頁面。
- `api/routes/line_staff_verification.py`：驗證頁狀態查詢及資料送出 API。
- `api/schemas/line_staff_verification.py`：月嫂驗證 API 請求格式。
- `services/line_staff_verification_service.py`：一次性連結、LINE 身分確認、既有月嫂資料比對及結果保存。
- `db/schema_parts/98_line_staff_verification.sql`：可重複執行的資料庫欄位與索引 migration。
- `tests/test_line_staff_verification.py`：驗證送出、資料比對、人工核准及 LINE 綁定整合測試。

### 修改檔案

- `line/line_bot.py`：收到「我是月嫂」時建立確認申請並傳送驗證頁連結。
- `api/main.py`：掛載月嫂驗證 API。
- `services/line_review_service.py`：審查畫面資料擴充，核准時安全綁定 `staff_id`、LINE 帳號與 `staff` 角色。
- `ui/components/line_review_manager.py`：顯示送出資料、比對結果及既有月嫂資訊，未成功比對時禁止核准。
- `db/schema.sql`：擴充 `line_confirmation_requests` 驗證與比對欄位。
- `.env.example`：新增 `LINE_STAFF_VERIFICATION_LIFF_ID` 設定說明。
- `tests/test_line_review_management.py`：調整人工審查測試資料以符合先比對再核准流程。

### 驗證結果

- migration 已套用至開發資料庫，未清空既有資料。
- LINE、LIFF、人工審查、任務及 API 安全測試共 35 項通過。
- 未建立或遺留一次性 Python 檔案。

### 修復：沿用既有 LIFF 時開啟月嫂驗證頁跳轉 HTTP 400

- `services/line_staff_verification_service.py`：未設定專用 LIFF ID 時，改由既有 `LINE_LIFF_ID` Gateway 產生驗證入口。
- `line/static/gateway.html`：登入完成後解析 query／`liff.state`，只允許固定的月嫂驗證目標並安全轉送 token。
- `line/static/staff_verification.html`：共用 LIFF 登入失效時顯示錯誤，不再使用未登記的目前網址呼叫 `liff.login()`。
- 新增共用 Gateway、專用 LIFF 相容性及前端導向防退化測試；相關測試 38 項通過。
- `.env.example` 將舊名稱 `LINE_LOGIN_ID` 統一為後端實際驗證 ID Token 使用的 `LINE_LOGIN_CHANNEL_ID`。

---

## 新增 LINE 主動式健康監控

### 新增

- `line/monitor.py`：與 FastAPI 分離的持續監控程序。
- `services/line_health_checks.py`：API、DB、Worker、任務、LINE、公開入口、LIFF、設定與磁碟檢查。
- `services/line_monitor_service.py`：檢查排程、狀態防抖、心跳、DB／快照保存及異常恢復事件。
- `api/routes/line_monitoring.py`、`api/schemas/line_monitoring.py`：受保護的監控狀態及事件 API。
- `ui/components/line_health_monitor.py`：管理中心細分健康狀態、檢查時間與異常紀錄。
- `config/line_monitoring.json`：檢查間隔與門檻。
- `db/schema_parts/97_line_active_monitoring.sql`：可重跑監控 migration。
- `tests/test_line_monitoring.py`：心跳、防抖、恢復、API 權限及獨立程序測試。

### 修改

- `line/worker.py` 每 15 秒更新 Worker 心跳。
- `db/schema.sql` 新增心跳與目前健康狀態，擴充既有異常事件。
- LINE 管理中心改讀持續監控結果，不再因打開頁面才執行健康檢查。
- `start_fastapi_ngrok.py` 與 `online.bat` 加入獨立 Monitor 啟動與程序監視。
- DB 故障時使用被 Git 忽略的 `.monitor_state` 快照保留診斷狀態。

### 驗證

- migration 已套用開發 DB，未清空既有資料。
- 33 項監控與 LINE 核心回歸測試通過；完整套件 808 項通過，另有 4 項未修改頁面的 Streamlit 測試因既有 3 秒時限逾時。
- 本次只建立異常事件，尚未發送 LINE 警報。

---

## 開發服務自動重啟與同層雙向監督

### 新增

- `services/runtime_supervision_service.py`：提供同層程序單例鎖、安全 PID 驗證、終止與獨立重啟能力。
- Monitor 新增開發監督器心跳檢查；監督器每 15 秒更新 `service_heartbeats`。
- `system_alerts` 新增 `service_supervisor` 事件生命週期，記錄服務中斷、重啟與恢復。

### 修改

- `start.bat` 直接啟動 Monitor 與服務監督器兩個同層程序，刪除外層 `run_development_supervisor.bat`。
- `start_fastapi_ngrok.py` 管理 FastAPI、ngrok、Streamlit，並在 Monitor 失聯時重啟它。
- `line/monitor.py` 在服務監督器失聯時，依心跳 PID 安全清理其三項服務並重啟監督器。
- 兩程序加入單例鎖與正常停機標記，避免重複程序、重啟風暴及人工關閉後被強制拉起。
- 服務失敗只重啟故障項目，依 1、3、10 秒最多重試三次；仍失敗時才依開關顯示彈窗或終端提示。
- `start.bat` 改為只啟動單一開發監督器，不再重複開啟 Streamlit。
- `config/line_monitoring.json` 新增開發監督器檢查門檻；`config/README_CONFIG.md` 與 `.env.example` 補上分工及開關說明。
- Windows 批次檔統一使用 UTF-8、CRLF。

### 驗證

- 監控測試 9 項通過；LINE 管理、任務、LIFF、審查與啟動回歸共 60 項通過。
- 完整測試 812 項通過；另 4 項既有 Streamlit AppTest 固定 3 秒逾時，與本次服務監督修改無關且和前次結果相同。
- 未建立或遺留一次性 Python 檔案。
