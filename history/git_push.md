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
