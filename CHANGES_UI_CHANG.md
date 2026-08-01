# UI_CHANG 變更說明

## 2026-07-31 待推送變更

### 多月嫂排班 UX

- 將多月嫂排班集中為「服務人員月曆」、「月嫂配對中心」與「案件人力配置」三個固定分頁；服務人員月曆不再顯示案件指派功能。
- 服務人員月曆補上目前瀏覽月份、上／下月與回到本月操作；同日多筆案件逐筆顯示，不再以「可接案」覆蓋正式訂單資訊。
- 月嫂配對中心保留原本單月嫂四步配對流程；只有單月嫂無法完整覆蓋服務檔期時才顯示 2～4 段多人配對。測試環境暫時保留無寫入的多人介面預覽入口。
- 案件人力配置以 1～4 段編輯完整正式 assignment 計畫，所有異動均先顯示調整前後、排班移除項目、時數差額與阻擋原因，再由管理員確認 Apply。

### 排班、休假與薪資規則

- 正式 ownership 統一使用 `case_staff_assignments` 與 assignment-owned `staff_schedule`；`orders.staff_id` 只可在首次正式配置時作 UI 建議值，不得作為正式排班或薪資歸屬。
- 多日期休假、順延與代班採單次 Preview、單一 fingerprint 與 atomic Apply；任一日期驗證失敗即整批拒絕，不留下部分寫入。
- Preview 與 Apply 都會重新讀取最新正式資料，重算跨案件占用、服務區段、後續順延、代班 lineage、付款／月結鎖定及排班衝突。
- 所有未取消 assignment 的 `actual_hours` 總和必須精確等於訂單 `service_days × service_hours_per_day`；不相等時拒絕 Apply。成功寫入後薪資依最新正式排班自動計算，不另設人工薪資確認時間點。
- 國定假日預設不產生雙倍薪；只有工會人員針對明確 assignment 排班日人工勾選並留下備註時，才計入雙倍薪。

### 媒合、檔期鎖定與資料模型

- 新增逐段檔期查詢、媒合方案與版本事件、逐位聯繫／意願、共用履歷，以及等待訂金檔期鎖的取得、釋放、取消與轉正式流程。
- 新增休假／順延／代班 batch 與逐日 append-only 事件，保留原 assignment、代班月嫂、日期、操作者與批次冪等識別。
- 新增原始服務區段欄位、同日多 assignment 相容規則、assignment schedule 唯一性與 live DB 完整性檢查／遷移工具。
- 補上 legacy 單月嫂配對相容層；正式 assignment、排班與薪資不得由姓名、日期或舊訂單月嫂欄位推測。

### API、文件與架構同步

- 新增／調整月嫂逐段可用性、媒合方案、檔期鎖、休假 batch Preview／Apply、案件人力同步與 payroll reconciliation API／Service。
- 修正空字串日期在案件人力 Preview 被解析為無效日期，以及首次正式配置零 assignment 的合法 bootstrap 情境。
- 新增多月嫂 UX 討論紀錄、目標指南、驗收矩陣與 API／Server 共用整頓計畫；同步排班、行事曆、配對、假日雙倍薪與資料字典規格。
- 同步 Master、API、Services、UI System Map 與 YAML IR，並記錄目前程式規格和後續待查核差異。

### 驗證與測試清理

- 清理後核心資料安全測試為 `618 passed, 1 warning`；完整 pytest 為 `1540 passed, 6 warnings`。
- 與 GitHub workflow 相同的嚴格 flake8（`E9`、`F63`、`F7`、`F82`）結果為 `0`。
- 移除依賴固定資料庫內容、外部服務、純 AST／原始碼文字、重複 schema／router 契約或一次性驗收狀態的測試，只保留可在隔離環境重跑且保護正式資料規則的 pytest。
- 待補可重複測試：DB snapshot exporter／importer、固定案件日期 reconciliation，以及 match-record service／router 的 fake-backed 測試；缺口已記錄於 System Map TODO。

### 部署與推送前提醒

- 本節記錄待推送內容，不代表已部署；目前專案版本維持 `0.2.1`，若要建立正式 release，需另行決定下一版號並同步 `README.md`、`pyproject.toml` 與 `uv.lock`。
- `online.bat` 不會自動更新既有資料庫。正式啟動新版前，必須先備份資料庫，在維護窗口套用新增 schema parts，並先以 `scripts/migrate_assignment_schedule_integrity.py` 的預設 check 模式檢查，再視結果使用 `--apply`。
- 本批比較範圍為本機 `origin/main` 的 `36bedc5` 至本機 `main` 的 `1e86553`；推送前仍需 fetch 遠端並重新確認 ahead／behind、衝突與工作樹狀態。
- 本次未包含 push 或正式部署。

---

## 2026-07-22 待推送變更

### 已核准並部署

- `AccountsPayableExport`：依月嫂預定付款日與補助預定退款日篩選月份；同月月嫂款按 `staff_id` 彙總，案件編號去重穩定排序，補助退款輸出原始應退金額。固定九欄、永豐代碼 31、台新代碼 633、分銀行流水號與銀行總額契約維持不變。
- `GenerateFakeData`：歷史產生器已凍結，直接執行與 import 都會立即非零停止；新假資料條件必須另建獨立腳本、測試及 ADAD 節點／Task。
- `ClientBeClassInsertOnlyImporter`：無命令列路徑時改讀 `document/資料庫、資料處理/假資料_模板.xlsx`，明確指定路徑與 insert-only 行為不變。
- 移除會初始化資料庫並呼叫已凍結產生器的 `start.bat`，README 改列安全的手動開發啟動流程。

### 推送前狀態提醒

- 補助退還人工覆核欄位、收款寫入觸發鏈及相關服務變更雖已有核准 Task，目前部分 ADAD 節點仍停在 `validated`，不得在狀態推進與整體驗證完成前宣告為已部署功能。
- 本節不調整 `pyproject.toml` 版本；正式升版應在本批所有預定納入節點均為 `deployed`、完整測試完成後進行。

---

## v0.2.0 — 2026-07-22

### 多月嫂排班與訂單同步

- Page 2 與 Page 3 改以 `case_staff_assignments`／`assignment_id` 呈現正式月嫂服務區段與每日排班，不再以 `orders.staff_id` 推測多月嫂服務歸屬。
- 支援兩位與三位月嫂連續交接、個別休假／雙薪設定，以及薪資建立前的全案實際時數確認。
- 訂單修改新增 preview／apply 流程；有薪資、月結或人工時數調整的指派會被鎖定，不得靜默重排。

### 身分資格、帳務與假資料

- 客戶身分資格統一讀取 `clients.identity_status`，同步更新訂單總覽、編輯頁、表單管理、匯入與計價流程。
- 應付帳款 API 分離摘要與固定九欄匯出；補強客戶收款日期、超收、退款及財務匯入重複 occurrence 對帳。
- 假資料維持原有 50 筆生命週期案件，新增多月嫂交接、雙薪、部分實收後超收、單筆溢收及訂金退款邊界。
- 已完成案件的實際服務日期保證不晚於 reference date；服務中案件必須涵蓋 reference date。
- 財務警示判斷器與警示事件生命週期延後處理，本版本不由 seed 建立警示資料。

### 版本與驗證

- 專案版本提升為 `0.2.0`，ADAD Master System Map 提升為 `54.0`。
- 本次變更測試基準：30 個測試檔、`177 passed`。
- `file_watcher.py` 使用明確 UTF-8 編碼開啟監控檔案，改善 Windows 編碼一致性。

---

## 2026-07-20

### 近期版本更新
- 財務匯入流程新增 Legacy / Sinopac / Taishin 匯入支援，並補齊 `tests/imports/*` 驗證案例。
- `ui/pages/06_finance_alerts.py` 新增「財務警示」頁面，並補上 `api`、`services` 與 `ui` 對應測試。
- 新增 ADAD 套件相關遷移腳本：`scripts/migrate_remove_other_addition.py`、`scripts/migrate_adad_task_snapshots.py`。
- 更新 `CHANGES_UI_CHANG.md`、`db/schema_parts/*` 與 `system_map` 對齊本次變更。

---

## 2026-07-15－帳務拆分、財務報表與契約介面

### 帳務明細總覽（Page 2）

- 客戶帳務與月嫂帳務改成兩張獨立表格，欄位不再交錯。
- 客戶收款總覽顯示訂金、第一期、第二期各自的應收金額、實收金額、應收日期、實收日期，以及應收／實收總額與未收餘額。
- 月嫂應付總覽逐筆顯示案件、服務人員、指派、服務時數、單價、服務薪資、樓層費、調整額、應付／實付／未付餘額、應付日期、實付日期與付款狀態。
- 支援案件編號、訂單狀態及客戶／月嫂付款狀態篩選。
- 選擇案件後，自動透過 `GET /api/v1/client-payments/{case_no}` 與 `GET /api/v1/staff-payments/{case_no}` 取得該案件交易明細；不預先載入其他案件。
- 人工補登或沖正交易必須填寫外部識別與原因，摘要金額仍由交易明細計算。

### 應付帳款查詢／輸出（Page 2）

- 可預覽並下載每月應付帳款 Excel。
- 月嫂薪資由永豐銀行代碼 31 出款；客戶退還補助款由台新銀行代碼 633 出款。
- 「退還補助款」與尚未啟用的「解約退款」已明確分開。

### 核銷補助清冊（Page 2）

- 新增分季核銷與年度總表預覽及 Excel 下載。
- 一般市民與補助市民分區顯示；當季沒有補助市民時不顯示下半部。
- 補助天數依補助時數除以每日服務時數計算，固定顯示至小數點後 2 位。

### 表單管理與 FastAPI

- 新增服務人員契約 Excel 鏡像輸出，不修改原始模板。
- 新增客戶收款、月嫂應付、契約內容、應付帳款及補助核銷報表 API。
- FastAPI 正式啟動入口為 `api.main:app`；舊的 `line.main` 相容入口已移除。

對應整合 commit：`0f9c11f`。

---

本文件彙整 `UI_CHANG` 分支相較於 `main` 的主要功能異動，供組長 review 時快速掌握改動重點與影響範圍。

---

## a. 訂單與帳務管理系統 - 訂單總覽與計算對帳（`ui/pages/02_orders.py` 分頁一）

**異動內容**：原本分頁一是一張唯讀的完整資料表（`st.dataframe`），列出所有訂單的全部欄位供瀏覽。現在改為「清單 + 點入展開」模式：每筆訂單顯示為一條可點擊的摘要列（案件編號、客戶姓名、訂單狀態、月嫂、預期開始日、服務天數、雇主自費合計），點擊該列後會直接在同一列下方展開完整的 36 欄位編輯面板（重用 `04_edit_order.py` 的編輯邏輯），可直接調整數值並儲存，不需跳轉頁面。同一時間僅會展開一筆，點選其他筆會自動收合前一筆（手風琴效果）。

**影響範圍**：因改為列表 + 展開模式，**預覽列表所呈現的欄位數量會比原本的完整表格少**（僅保留摘要用欄位），完整欄位需點開該筆訂單才能看到與編輯。

---

## b. 訂單與帳務管理系統 - 案件與配對中心（`ui/pages/02_orders.py` 分頁二，4步智慧配對）

**異動內容**：
- **步驟 1「發送 訂單資訊-1（粗篩）」**：原本僅能單選一位月嫂發送，現改為可**複選多位月嫂**，一次批次發送。
- **步驟 2「發送 訂單資訊-2（精篩）」**：候選名單為步驟 1 已發送過的所有月嫂，清單同時呈現每位月嫂目前的意願回覆狀態（待回覆／願意接案／拒絕接案，可直接於清單中更新），並可自由勾選要發送訂單資訊-2 的對象（不限制僅能選已接案者，保留人工判斷彈性）。
- **步驟 3、4（傳送履歷、成立訂單並定案指派）**：維持僅能選擇單一位月嫂執行，但下拉選單僅列出目前意願為「願意接案」的月嫂，避免誤選尚未回覆或已拒絕的人選。

---

## c. 資料庫原始資料瀏覽（`ui/pages/01_data_browser.py`）

**異動內容**：原本為唯讀表格，現改為可直接於網頁表格上點選儲存格進行即時編輯（`st.data_editor`），編輯後需另外點擊「儲存變更」按鈕才會正式寫入資料庫。系統自動管理欄位（如 `id`、建立/更新時間等）已設為鎖定唯讀，無法從表格上誤改。

**待確認事項**：**目前尚未排除「哪些訂單/資料狀態下不可修改」的情境**（例如訂單已進入「服務中」或「訂單完成」後，某些欄位理論上不應再被隨意覆寫）。此限制邏輯尚待與組長/組員確認規則後再補上，目前是全欄位（除系統唯讀欄位外）皆可編輯。

---

## 其他一併包含於本分支的異動

- 資料庫連線設定（host/port/user/password/database）改為讀取專案根目錄 `.env` 檔案，涵蓋 `services/db_service.py`、4 支 `scripts/imports/*.py`、`scripts/init_db.py`、`scripts/wait_for_db.py`，並保留原寫死數值作為 fallback 預設值。
- `docker-compose.yml` 的 `ports`／`MYSQL_DATABASE`／`MYSQL_ROOT_PASSWORD` 改用 Docker Compose 的 `${VAR:-default}` 語法讀取 `.env`。
- `document/資料庫、資料處理/假資料_範例.xlsx` 新增測試用假資料（HCM 市府、beclass 各 10 筆），供測試匯入流程使用。
- `.gitignore` 新增 `graceAdd/`，排除個人協作用的異動紀錄資料夾。

---

如需查看更詳細的逐次修改歷程（含每次修改的問題背景、實作細節與測試注意事項），可參考 `graceAdd/alterContent.md`（此檔案已被 `.gitignore` 排除，不會出現在本分支的 GitHub 內容中，僅存在於本機）。


---

## 2026-07-13 - Case number normalization

- All customer-facing order and case identifiers now use clients.case_no exclusively.
- Removed the legacy order_no specification and obsolete order-number labels from the UI, forms, API examples, and LINE documents.
- LINE binding, LIFF screens, database relations, and APIs now use `case_no`; the former internal numeric order key has been removed.
- When a LINE-native registration has not yet received a case number, the user is informed that administrative issuance is pending instead of receiving an internal ID.
