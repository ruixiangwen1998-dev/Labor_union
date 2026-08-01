# 新竹市月子照顧服務人員職業工會－LINE 應用與行政流程自動化系統

> 目前版本：**v0.2.1**（2026-07-25）｜最新功能更新：**2026-07-31**｜ADAD Master System Map：**56.0**

## 2026-07-31 更新（多月嫂排班 UX）

本次更新完成管理端多月嫂排班、配對與案件人力調整流程，正式 ownership 統一以 `case_staff_assignments` 與 assignment-owned `staff_schedule` 為準。

- **三分頁操作入口**：集中為「服務人員月曆」、「月嫂配對中心」與「案件人力配置」；服務人員月曆不再提供案件指派功能。
- **月曆資訊修正**：顯示目前瀏覽月份，支援上／下月與回到本月；同日多筆案件逐筆呈現，不再以「可接案」覆蓋正式訂單資訊。
- **原配對流程與多人 fallback**：保留原本單月嫂四步配對；只有找不到可完整承接服務期間的單一月嫂時，才顯示 2～4 段多人配對。測試環境暫時保留無寫入的多人介面預覽。
- **案件人力 Preview／Apply**：以 1～4 段編輯完整正式 assignment 計畫，先顯示調整前後、排班移除、時數差額與阻擋原因，再由管理員確認套用。
- **多日期休假、順延與代班**：一次操作共用單一 Preview、fingerprint 與 atomic Apply；任一日期失敗即整批 rollback，不留下部分寫入。
- **服務時數守恆**：每次 Preview 與 Apply 都以最新正式資料重算；所有未取消 assignment 的 `actual_hours` 總和必須精確等於訂單 `service_days × service_hours_per_day`，否則拒絕寫入。
- **薪資與國定假日**：薪資依成功寫入的最新正式排班自動計算，不另設人工薪資確認時間；國定假日預設不產生雙倍薪，個案例外必須由工會人員針對明確排班日人工指定並留下備註。
- **配對與檔期鎖定**：新增逐段檔期查詢、媒合方案與事件、逐位聯繫／意願、共用履歷，以及等待訂金鎖的取得、釋放、取消與轉正式流程。

驗證結果：

- 嚴格 flake8（`E9`、`F63`、`F7`、`F82`）：`0`
- 核心資料安全測試：`618 passed, 1 warning`
- 完整 pytest：`1540 passed, 6 warnings`

既有資料庫升級提醒：

- `online.bat` 不會自動套用資料庫 schema。
- 正式啟動新版前必須先備份資料庫，在維護窗口依序套用 `db/schema_parts/95`、`98`～`103` 的相關更新。
- 執行 `scripts/migrate_assignment_schedule_integrity.py` 時應先使用預設 check 模式，確認既有 assignment ownership、同日重複排班與索引狀態，再視結果使用 `--apply`。

## 2026-07-25 更新（v0.2.1）

本次版本完成管理介面 API 化的安全收尾，並統一沿用 LINE 管理中心既有的正式管理員身分與授權系統。

- **正式管理員認證共用**：Data Browser 與國定假日 GET／POST／DELETE 全部改用 `AdminPrincipal` 與 `require_system_admin`，不再自行維護 `X-Auth-Context`、`ADMIN_AUTH_CONTEXT` 或 `admin_role` 字串判斷。
- **雙層管理 API 防護**：Streamlit 管理頁統一送出伺服器端 `X-Internal-API-Key` 與登入後取得的 `Authorization: Bearer <session>`；缺少設定、Session 失效或角色不足時採 fail-closed。
- **可信稽核身分**：Data Browser PATCH 的 audit actor 與 role 直接取自已驗證的 `AdminPrincipal.username`／`AdminPrincipal.role`，不接受 UI payload 指定，也不再由 username 推測角色。
- **UI → API 串接**：Data Browser、訂單／媒合、訂單編輯與月嫂月曆持續改走 FastAPI；排休 ownership 僅使用 `assignment_id`，正式指派維持 preview → confirm → apply 流程。
- **安全交易邊界**：Data Browser 更新、更新前後快照與 audit insert 共用同一 transaction；非法欄位整批拒絕，audit schema 不在 request runtime 動態建表。
- **ADAD 規格同步**：新增 `AdminAuthService`、`AdminAuthorizationDependency`、`UIAdminApiContext` 節點，更新 Data Browser／Holiday 的 dependency、invariant、verification 與編譯後 YAML IR。

部署或啟動管理介面前至少需要：

```env
INTERNAL_API_KEY=replace-with-a-long-random-secret
APP_ENV=production
ENABLE_ADMIN_AUTH=true
```

正式環境必須先由管理員登入取得 Session。只有 `APP_ENV` 為 `development`、`dev`、`local` 或 `test`，且 `ENABLE_ADMIN_AUTH=false` 時可以略過 Bearer Session；`X-Internal-API-Key` 永遠不能略過。

本次安全整合的針對性驗證為 `22 passed`，涵蓋正式認證核心、Data Browser Router／Service、Holiday Router 與 Streamlit runtime AppTest。對應 commits：`8fa3910`、`bd09413`。

## 2026-07-24 更新

本次整合範圍為 `8067706` 至 `35d48be`，主要包含以下變更：

- **管理介面模組化**：`02_orders.py` 與 `05_form_management.py` 保留為輕量頁面殼層，實際 Tab 功能分別移至 `ui/pages/order/` 與 `ui/pages/form_management/`，降低單檔規模並保留原有頁面操作流程。
- **訂單編輯入口統一**：原獨立 Page 4 已移至 `ui/pages/order/editor.py`，並由 Page 2 Tab 1 委派進入；側邊欄不再提供重複的 Page 4。
- **訂單總覽與日期處理**：簡化案件選項建立方式，集中共用格式化與安全日期／數值 helper；HCM 新增案件會依服務起日、服務天數、服務類型及假日初始化訂單起訖日。
- **固定資料庫測試快照**：新增 `fixtures/db_snapshot_v2/v3/` 的 27 表固定資料集，以及序列化、驗證、匯出、匯入、日期校正和安全重設工具；開發者可用 `reset_DB.bat` 重建本機 `union_db`。
- **資料瀏覽與行事曆防呆**：資料瀏覽器支援目前使用中的案件、排班與財務資料表，複合鍵及財務表維持唯讀；行事曆可安全處理沒有服務人員選項的情況。
- **退役 legacy payments**：FastAPI 不再掛載舊 `payments` Router，舊 Payment schema 與 `payments` 建表定義已移除；帳務功能改由 `client_payments`、`staff_payments` 及其交易／結算資料流負責。
- **架構與驗證同步**：同步 API、Service、UI 與 Master System Map 的 Source binding、節點契約及 YAML IR，並補齊 UI runtime、shell ownership、fixture reset 與 importer 測試。

既有應付帳款匯出契約維持不變：依預定付款／退款日期月份取數，月嫂款按 `staff_id` 彙總，補助退款保留原始應退金額，輸出仍採固定九欄與分銀行流水號。

## v0.2.0 版本重點

- 正式導入 assignment-owned 多月嫂排班：支援兩位／三位月嫂連續交接、個別排班、雙薪日與實際時數隔離。
- 訂單修改改採 preview／apply 同步流程，明確處理指派配置、排班移除、薪資鎖定與 append-only 稽核快照。
- 客戶資格唯一來源統一為 `clients.identity_status`，移除訂單層重複資格來源並補上安全遷移與 UI／API 驗證。
- 強化帳務匯入、客戶收款對帳、應付帳款摘要／固定九欄匯出，以及補助核銷資料流。
- 擴充 50 筆既有生命週期假資料：加入多月嫂交接、雙薪、超收、退款與跨批次重複匯入，同時保留原有狀態與排班多樣性。
- 完成案件日期防呆：服務中涵蓋基準日、已完成案件不得出現未來實際服務日期、取消案件維持零實際時數。
- 財務警示判斷器與警示生命週期仍列為後續 post-seed 工作；本版假資料不建立 `finance_alerts`／`finance_alert_events`。
- `file_watcher.py` 明確使用 UTF-8 開啟監控檔案，避免 Windows 預設編碼造成非 ASCII 路徑或內容處理差異。

驗證基準：本版 30 個變更測試檔共 `177 passed`；整合 commits 為 `aecca9b` 至 `3cabb4c`。

---

## 2026-07-20 最近更新

- 完成財務導入與核帳流程的第二階段：新增 Legacy / Sinopac / Taishin 匯入格式支援，並補齊帳務正規化驗證測試（`tests/imports/*`）。
- 新增/修訂服務層與資料庫 schema：支援月嫂逐月薪酬、行政補助歸還、補助對帳流程、財務警報管道，並同步調整 `system_map`/`services_system_map`/`api_system_map`。
- 新增「財務警報」後台頁面（`ui/pages/06_finance_alerts.py`）與對應 API/Service；並擴充測試覆蓋（帳務、補助、交易分類、交易指紋、匯入與移轉）。
- 新增 ADAD 遷移腳本與資料清理腳本：`migrate_remove_other_addition.py`、`migrate_adad_task_snapshots.py`，確保欄位清理與快照遷移可受控執行。
- 同步更新 `CHANGES_UI_CHANG.md`，並補齊新 schema 分拆 SQL（`db/schema_parts/*`）以便版本升級。

---

## 2026-07 帳務與管理介面更新

- 全系統訂單關聯鍵統一為 `case_no`，不再使用 `orders.id`／`order_id`。
- 帳務拆分為 `client_payments` 與 `staff_payments`：客戶三期收款與月嫂逐指派應付分開管理。
- 管理端「帳務明細總覽」分開顯示客戶收款、月嫂應付，可依案件編號、訂單狀態與付款狀態篩選；選擇案件後才載入交易明細。
- 新增應付帳款 Excel：月嫂款使用永豐銀行代碼 31，退還補助款使用台新銀行代碼 633。
- 新增分季核銷補助清冊與年度總表，補助天數固定顯示至小數點後 2 位。
- 新增服務人員契約 Excel 鏡像輸出，以及對應的契約、帳務與財務報表 FastAPI。
- FastAPI 的正式 ASGI 入口為 `api.main:app`；LINE、LIFF 與 Webhook 以子路由掛載。

上一個帳務整合版本：`0f9c11f`。

---

本專案旨在為「新竹市月子照顧服務人員職業工會」開發地端運作的 **LINE 客服與行政流程自動化系統**。透過將行政人員手動下載的 Excel 名冊自動化匯入資料庫，並提供 Streamlit 管理後台，未來將延伸串接 LINE Messaging API 實現半自動化客戶配對、合約發送與 RAG 客服問答。

---

## 📂 專案檔案結構與設計緣由

本專案的目錄與檔案結構設計如下：

```text
Lobar_union/
├── .venv/                      # Python 虛擬環境 (Git 已忽略)
├── .agents/                    # ADAD 工作流 / 代理自定義配置目錄
├── db/                         # 資料庫 Schema
│   └── schema.sql              # MySQL 資料庫建表語句（帳務使用 client/staff payments 正規化資料表）
├── document/                   # 專案設計與規格說明文件
│   ├── API/                    # API 整合設計文件
│   ├── line/                   # LINE 平台整合相關說明
│   ├── 地端部屬/               # 地端部署指南與安全架構
│   ├── 管理端UI/               # Streamlit 管理介面原型與規格
│   │   └── 表格需求模板/       # 管理端所需的 Excel 報表設計模板 (帳務.xlsx、所需表格.xlsx、週報.xlsx、服務人員契約.xlsx)
│   └── 資料庫、資料處理/        # 資料庫欄位對應、SSOT 業務規則與 Data Pipeline 設計
├── downloads/                  # 檔案監控下載根目錄 (由 File Watcher 監聽)
│   ├── bank/                   # 存放銀行對帳單 Excel 來源檔
│   ├── client_beclass/         # 存放客戶 BeClass Excel 來源檔
│   ├── hcm/                    # 存放 HCM 月子平台 - 市府 Excel 來源檔
│   └── staff_beclass/          # 存放月嫂 BeClass Excel 來源檔
├── api/                        # 後端 FastAPI RESTful API 服務
│   ├── main.py                 # FastAPI 入口程式
│   ├── routes/                 # API 路由模組（orders、matches、schedule、clients、staff、holidays、finance 等）
│   └── schemas/                # Pydantic 資料驗證 Schema 模型
├── services/                   # 業務邏輯與資料庫存取服務層
│   └── db_service.py           # 核心 DB 服務 (含訂單 CRUD、出勤天數動態精算引擎與 36 欄位 safe_int 防護)
├── ui/                         # Streamlit Web 管理前端專區
│   ├── app.py                  # 側邊欄動態導覽殼層 (AppShellUI)
│   └── pages/                  # 獨立頁面模組專區
│       ├── 01_data_browser.py  # 🗄️ 原始資料庫瀏覽與國定假日管理 (DataBrowserUI)
│       ├── 02_orders.py        # 📊 訂單與帳務管理頁面殼層（五個 Tab 委派至 order/）
│       ├── order/              # 訂單總覽、配對、財務、應付帳款、補助核銷與 editor 子模組
│       ├── 03_calendar.py      # 📅 服務人員行事曆與檔期調控 (CalendarUI - 四色 HTML 月曆與天數精算)
│       ├── 05_form_management.py # 📝 表單管理頁面殼層
│       └── form_management/    # 表單建置、範本庫、契約管理與共用 helper 子模組
├── scripts/                    # 核心 Python 運作與 Pipeline 腳本
│   ├── imports/                # 微匯入 Pipeline 專屬目錄 (Micro-Pipelines)
│   │   ├── import_client_beclass.py # 處理 BeClass 客戶匯入
│   │   ├── import_client_hcm.py     # 處理 HCM 客戶匯入 (初始化訂單為「洽談中」)
│   │   ├── import_finance_excel.py  # 處理銀行對帳流水單
│   │   └── import_staff_beclass.py  # 處理 BeClass 月嫂匯入
│   ├── file_watcher.py         # 地端檔案自動監控服務
│   ├── generate_fake_data.py   # 已凍結的歷史假資料腳本（僅供人工參考，不可執行或匯入）
│   ├── reset_fake_database.py  # 以固定 v3 fixture 安全重建本機 union_db
│   ├── export_db_snapshot_fixture_v2.py # 匯出固定格式資料庫快照
│   ├── import_db_snapshot_fixture_v2.py # 驗證後匯入資料庫快照
│   ├── fix_schedule_conflicts.py # 月嫂檔期衝突檢測與自動修復工具
│   ├── init_db.py              # 資料庫初始化與 Schema 導入
│   └── wait_for_db.py          # 輪詢檢測 MySQL 連線就緒腳本
├── docker-compose.yml          # Docker Compose 配置文件，一鍵啟動 MySQL 8.0 持久化容器
├── main.py                     # 專案主程式入口 (FastAPI 與 Streamlit 同時啟動或導向)
├── online.bat                  # 一鍵啟動生產上線服務 (啟動 Docker, wait_for_db, 啟動 services / watcher)
├── reset_DB.bat                # 僅供開發環境：確認資料庫名稱後套用固定 v3 fixture
├── pyproject.toml              # uv 專案管理配置文件
├── requirements.txt            # 從 pyproject.toml 自動編譯導出的相容性依賴清單
├── system_map.yaml             # ADAD 系統架構 SSOT 記憶與狀態事實來源 (Version 56)
├── system_map.md               # ADAD 系統架構 SSOT 說明文件 (Version 56)
└── uv.lock                     # uv 依賴鎖定檔
```

---

## 📄 本次更新說明 (開發實作收尾)

在本次更新中，我們主要進行了以下優化與擴展：
* **API 服務層與 UI 前端整合**：全面導入 FastAPI RESTful API 後端與 Streamlit 前端分離架構，並擴展 UI 表單與履歷問卷管理頁面（Tab 3 變數代理 EPPP 契約引擎）。
* **Data Pipeline 優化**：重構並優化微服務 Pipeline 導入流程，支援客戶、月嫂 BeClass 名冊及 HCM 系統的自動化去重與安全防護。
* **ADAD 架構更新**：系統架構已升級至 Version 54.0，補齊跨子地圖帳務 staging 合約、多月嫂內部 helper 所有權及 Task v3 timeout，維持 SSOT 與 pre-commit 一致。

---

## 🛠️ 開發環境與部署架設指南

本專案保留 `online.bat` 作為正式服務啟動腳本。會重設資料庫並產生假資料的 `start.bat` 已移除；開發與測試環境請改用手動啟動流程。

### 1. 批次檔說明

#### 🌐 `online.bat` (生產上線環境一鍵啟動)
此腳本適合生產環境正式上線使用。執行流程如下：
* 啟動 Docker 中的 MySQL 8.0 容器。
* 等待 MySQL 資料庫連線就緒。
* **⚠️ 安全防護**：**不會**執行資料庫初始化與假資料生成，以確保歷史生產資料的安全。
* 並行啟動 FastAPI 後端、Streamlit 網頁前端，以及 `file_watcher.py` 地端 Excel 檔案自動監控匯入服務。

---

### 2. 啟動方式

#### 批次啟動方式
直接在 Windows 終端機（PowerShell）中執行：
```powershell
# 開發/測試環境啟動
.\start.bat

# 只啟動並監控FastAPI與ngrok（不初始化DB、不啟動UI）
.\.venv\Scripts\python.exe .\start_fastapi_ngrok.py

# 生產/上線環境啟動
.\online.bat
```

`online.bat`不啟動開發用ngrok。正式環境的公開入口已移至第七階段，預定改用 Tailscale Funnel。

### LINE 管理中心（第五階段 5.1）

Streamlit 現在提供「LINE 管理中心」入口。FastAPI 使用兩層驗證：由後端服務持有的
`X-Internal-API-Key`，以及登入後取得的短時效管理員 Session。瀏覽器不會直接取得內部金鑰。

第一次使用前先初始化開發資料庫，再建立一個管理員：

```powershell
.\.venv\Scripts\python.exe scripts\init_db.py
.\.venv\Scripts\python.exe scripts\create_admin.py --role system_admin
```

`scripts/create_admin.py` 是可重複使用的管理工具，不會建立預設密碼。管理員密碼以 scrypt
雜湊保存，Session 原始值只回傳一次，資料庫僅保存 SHA-256 雜湊。正式啟動前必須在 `.env`
設定固定且足夠長的 `INTERNAL_API_KEY`；`online.bat` 缺少此值會拒絕啟動。

開發期間若不想重複登入，可設定：

```env
APP_ENV=development
ENABLE_ADMIN_AUTH=false
```

此模式只略過帳號 Session，`X-Internal-API-Key` 仍會驗證。`APP_ENV=production` 永遠強制
啟用登入，不受此開關影響。

#### 5.1.1 一鍵本機開發初始化（含金鑰）

不同開發者可各自維護本機 `.env`。若要快速補齊最少三個參數，直接執行：

```powershell
.\bootstrap_admin_dev_env.bat
```

腳本會自動寫入（或更新）：

```env
APP_ENV=development
ENABLE_ADMIN_AUTH=false
INTERNAL_API_KEY=<隨機且本機專用金鑰>
```

完成後再啟動本機服務（例如 `.\start.bat` 或其他本機啟動流程）即可進行不需登入的管理端開發測試。

若要一鍵完成「補齊環境變數 + 啟動 API/UI + watcher」，可直接執行：

```powershell
.\dev_API.bat
```

#### 5.2 訊息管理中心

LINE 管理中心的「訊息管理」已接上 `config/message_templates.json`，支援搜尋、分類／狀態
篩選、新增、修改、複製、文字與 Flex JSON 預覽、啟停及二次確認刪除。管理介面會帶入
設定檔內容 revision，若其他管理員已先修改，後端回傳 409 並要求重新載入，避免覆蓋新版。

啟用中的 D+1～D+3 排程所引用的範本不能停用或刪除；必須先在後續排程管理頁解除引用。
已經建立於 `line_tasks` 的待發送任務保存建立當時的訊息快照，不會因範本文字更新而被改寫。

#### 5.3 排程與 Worker 任務管理

LINE 管理中心的「排程任務」已接上 D+N 排程編輯器及 Worker 任務佇列。排程可設定時區、
D+天數、發送時間、訊息範本、啟停及重新加入好友是否重跑；儲存時使用 revision／`If-Match`
避免多人同時修改互相覆蓋。排程變更只影響之後建立的新任務，既有 `line_tasks` 不回溯更新。

任務管理提供狀態統計、條件篩選、分頁、詳細內容與每次執行歷史。依角色可取消待執行任務、
將待執行任務改成立即執行，或把失敗任務重新排入。所有人工操作均經資料庫狀態鎖與管理稽核；
Worker 仍採 Webhook／管理操作喚醒加低頻容錯掃描，前端不會固定每數秒輪詢。

```text
GET  /api/config/message-schedules/state
PUT  /api/config/message-schedules
GET  /api/v1/line/tasks/summary
GET  /api/v1/line/tasks
GET  /api/v1/line/tasks/{task_id}
POST /api/v1/line/tasks/{task_id}/cancel
POST /api/v1/line/tasks/{task_id}/run-now
POST /api/v1/line/tasks/{task_id}/retry
```

#### 5.4 Rich Menu 管理中心

Rich Menu 分頁已接上三種角色選單，可修改名稱、角色、尺寸、顏色、按鈕範圍及
Message／URI／LIFF／Postback Action，並可產生預覽、上傳圖片、保存草稿及建立發布工作。
草稿使用 revision／`If-Match` 防止多人互相覆蓋；發布與儲存分離，不會因修改設定就直接
更動 LINE 官方帳號。

發布工作保存在 `line_rich_menu_publications`，由既有 Worker 喚醒後執行單一 Menu 建立、
圖片上傳及預設選單設定。成功後，`staff`／`union_staff` 角色會分批建立 `rich_menu_link`
任務切換至新版；失敗保留舊版並提供錯誤與人工重試。圖片本體放在 `MEDIA_STORAGE_ROOT`，
MySQL `media_assets` 只保存中繼資料與 SHA-256，不保存 BLOB。

```text
GET  /api/config/line-menus/state
POST /api/v1/line/rich-menus/preview
POST /api/v1/line/rich-menus/{menu_id}/images
POST /api/v1/line/rich-menus/{menu_id}/publish
GET  /api/v1/line/rich-menus/publications
POST /api/v1/line/rich-menus/publications/{publication_id}/retry
```

#### 5.5 LIFF 設定中心

LINE 管理中心的「LIFF 設定」已接上入口選擇、舊客戶綁定及新客戶登記三個頁面。工會人員
可修改共用主題、頁面文字、入口卡片、欄位順序及自訂問題，並先做手機版預覽。儲存後，
使用者下次載入頁面即套用，不需要像 Rich Menu 一樣另外發布。

後端以 revision／`If-Match` 防止多人覆蓋，並保存最多 20 個修改前快照供人工還原。姓名、
電話、預產期、服務天數及地址等系統欄位不能刪除、停用或改變必要類型；新增問題答案會
寫入既有 `beclass_records.survey_details`。

正式環境必須設定 LIFF 所屬的 LINE Login Channel ID。頁面會送出 `liff.getIDToken()`，
FastAPI 向 LINE 驗證後從 token 取得使用者 ID，不採信瀏覽器自行填入的 ID。開發環境可保留
明確的模擬 ID 降級模式。

```env
LINE_LOGIN_CHANNEL_ID=your_line_login_channel_id_here
LIFF_REQUIRE_ID_TOKEN=true
```

```text
GET  /api/config/liff/runtime?page=registration
GET  /api/config/liff/state
POST /api/config/liff/validate
PUT  /api/config/liff
GET  /api/config/liff/history
POST /api/config/liff/rollback/{revision}
```

#### 5.6 人工審查中心

LINE 管理中心的「人工審查」已接上月嫂身分申請與客戶重新綁定。清單支援類型、狀態、
日期及關鍵字篩選，LINE User ID 在清單中會遮蔽，進入具權限的詳細資料後才顯示完整值。

查看審查資料需要 `line_agent` 以上權限；核准或拒絕需要 `line_manager` 以上權限。拒絕必須
填寫原因。所有決定均保存處理管理員、原因與時間，並寫入 `admin_audit_logs`。核准前會以
資料列鎖重新確認狀態；重新綁定還會檢查舊綁定是否已變更及新 LINE 是否與其他客戶衝突。

```text
GET  /api/v1/line/review-requests/summary
GET  /api/v1/line/review-requests
GET  /api/v1/line/review-requests/{request_id}
POST /api/v1/line/review-requests/{request_id}/approve
POST /api/v1/line/review-requests/{request_id}/reject
```

開發終端的一次性 `y/n` 審查仍保留；舊內部接口改為呼叫同一個交易服務，不會固定輪詢。
管理中心也只在頁面操作或人工重新整理時讀取資料。

#### 手動啟動個別服務
若需單獨除錯，可在啟動 Docker 後手動執行以下指令：
```powershell
# 1. 啟動 Docker 容器
docker-compose up -d

# 2. 啟動 FastAPI 後端
uvicorn api.main:app --reload

# 3. 啟動 Streamlit 管理介面
streamlit run ui/app.py

# 4. 啟動檔案監控
python scripts/file_watcher.py
```

`scripts/init_db.py` 會初始化資料庫，僅能在明確確認目標資料庫後個別執行。請勿執行或匯入 `scripts/generate_fake_data.py`；需要新增測試資料時，優先更新有版本且可驗證的 fixture，或建立用途明確的獨立播種腳本及對應測試。一般開發者不需安裝或操作 ADAD，依標準 Git、Python 與 pytest 流程開發即可。

### 3. 重設本機測試資料庫

固定 v3 fixture 只供本機開發／測試使用，會重建 `union_db`，不可對正式資料庫執行。

```powershell
# 顯示檢查結果，不寫入資料庫
.\.venv\Scripts\python.exe -m scripts.reset_fake_database

# 重建本機 union_db；批次檔會傳入明確的資料庫名稱確認
.\reset_DB.bat
```

重設流程會先驗證 manifest、27 表 allowlist、檔案雜湊與資料內容，再套用 schema 及匯入固定快照；任一步失敗都會停止，不會改用歷史 `generate_fake_data.py`。

---

## 🤝 開發與協作規範

本專案由固定開發人員維護。請團隊成員在進行開發與提交修改前，詳閱 **[🤝 開發與協作規範指南](CONTRIBUTING.md)** 以瞭解分支開發流程與 Pull Request (PR) 規範。
