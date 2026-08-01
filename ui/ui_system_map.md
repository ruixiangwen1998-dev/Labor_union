# UI System Map

### Domain: UI
- Description: Streamlit user interface, page composition, and HTTP client contracts.
- Allowed Dependencies: [API]

##### Module: AppShellUI
- Sub Map: ui_layer
- Type: ui_shell
- Source: ui/app.py
- Description: Streamlit 側邊欄導覽殼層，動態載入 ui/pages/ 頁面。
- Dependencies: [DataBrowserUI, OrderUI, MultiCaregiverSchedulingPageUI, FormManagementUI, FinanceAlertCenterUI, LineManagementUI]
- Observability: not_required

##### Module: DataBrowserUI
- Sub Map: ui_layer
- Type: ui_page
- State: `validated`
- Source: ui/pages/01_data_browser.py
- Description: 原始資料庫表格瀏覽頁面。只提供 DbService 仍支援的資料表檢視；legacy payments 選項與相關唯讀／編輯設定必須完全移除。
- Dependencies: [DataBrowserAdminRouter, HolidayRouter, UIAdminApiContext]
- Invariants:
  - INV-UI-BROWSER-01: 原始資料表格欄位必須支援透過對照表轉換為中文名稱 (含英文原鍵名或純中文)，未記錄欄位自動安全回退原鍵名。
  - orders 的資料瀏覽不得顯示或編輯 clients.identity_status；資格資訊只顯示 clients.identity_status，且該欄位在 DataBrowserUI 必須唯讀。
  - table_options、EDITABLE_COLUMNS 與 READ_ONLY_TABLES 均不得包含精確 legacy 表名 payments；必須保留 client_payments、client_payment_transactions、staff_payments 與 staff_payment_transactions。
  - 所有 Data Browser metadata、PATCH 與 holidays 請求必須使用 UIAdminApiContext 產生的正式 headers；不得送出 X-Auth-Context。
  - 正式模式缺少 internal service key 或管理員 session 時必須停止渲染資料操作且不得發出 HTTP 請求。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "-q", "tests\\test_data_browser_identity_status_ui.py", "tests\\test_data_browser_runtime_acceptance_app_test.py"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: UIAdminApiContext
- Sub Map: ui_layer
- Type: ui_helper
- State: `planned`
- Source: ui/pages/shared.py
- Description: Streamlit 管理員頁共用 runtime API base URL、internal service key 與 Bearer session header 組裝。
- Dependencies: [AdminAuthorizationDependency]
- Input:
  - environment: API_BASE_URL、INTERNAL_API_KEY、APP_ENV、ENABLE_ADMIN_AUTH。
  - session_state: line_admin_access_token。
- Output:
  - api_base_url: runtime 解析且去除尾端斜線的 API base URL。
  - admin_headers: X-Internal-API-Key 與正式模式 Authorization Bearer header。
- Invariants:
  - INTERNAL_API_KEY 永遠必須存在；不得提供 admin_role、operator、user_role 或 ADMIN_AUTH_CONTEXT fallback。
  - 正式模式必須從 line_admin_access_token 取得非空 token；不得接受 UI 文字欄位指定 principal 或 role。
  - 只有與 backend 相同的明確 development bypass 條件下可省略 Authorization，且仍必須送 internal service key。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_data_browser_runtime_acceptance_app_test.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: OrderUI
- Sub Map: ui_layer
- Type: ui_page
- Source: ui/pages/02_orders.py::_render_order_page_shell
- Description: Page 2 訂單與帳務管理頁的殼層；建立固定順序的四個 Tab，配對與正式人力配置已移至 MultiCaregiverSchedulingPageUI。
- Dependencies: [OrderUI_Tab1_Overview, OrderUI_Tab3_Finance, AccountsPayableExportUI, SubsidyReconciliationRegisterUI]
- Input:
  - orders_data: 已載入的訂單資料。
  - clients: 已載入的客戶資料。
  - staff_list: 已載入的服務人員資料。
- Output:
  - page2_tabs: 依序渲染訂單總覽、帳務總覽、應付帳款與核銷補助四個 Tab。
- Invariants:
  - INV-UI-01: 所有費用與金額數字統一無條件四捨五入整數化呈現 (帶千分位)，無小數點。
  - INV-UI-02: 必須透過 safe_int() 轉換數值，防範 NaN, None, Inf 及空字串導致的 ValueError 崩潰。
  - 必須固定建立四個 Tab，且依序分派 OrderUI_Tab1_Overview、OrderUI_Tab3_Finance、AccountsPayableExportUI 與 SubsidyReconciliationRegisterUI。
  - 不得渲染 LegacySingleCaregiverMatchingRenderer、正式 assignment 配置或 assignment-synchronization Preview／Apply；這些入口只屬 MultiCaregiverSchedulingPageUI。
  - 不得直接讀取資料庫或帳務 API；資料載入只屬於 Page2TabNavigation，帳務寫入只屬於 PaymentManagementUI。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "-q", "tests\\test_order_ui_shell_ownership.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\02_orders.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Non Goals:
  - 不改動 Tab3、Tab4、Tab5 各自 renderer 的帳務或報表行為。
  - 不新增客戶收款或月嫂應付／轉帳操作。
- Observability: not_required

##### Module: OrderUI_Tab1_Overview
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/order/tab1_overview.py::_render_tab1_overview
- Dependencies: [EditOrderUI]
- Description: Tab 1 訂單資訊總覽。預設不限定訂單狀態，將所有篩選結果以單一下拉式選單呈現，並以 clients.identity_status 顯示身分資格。
- Algorithm:
  - 狀態篩選的預設值為不限定；使用者未選任何狀態時顯示所有訂單狀態。
  - 依篩選與搜尋結果建立包含全部符合訂單的單一下拉式選單；不得分頁或限制每頁筆數。
  - 使用者選擇單筆訂單後委派 EditOrderUI；不得自行寫入訂單、客戶或帳務資料。
- Invariants:
  - 顯示與篩選身分資格時只能讀取 clients.identity_status；不得讀取、顯示或重建 clients.identity_status。
  - 下拉式選單必須包含全部篩選結果，且訂單總數必須清楚標示。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_order_overview_ui.py", "-q", "-p", "no:cacheprovider", "--basetemp", "C:\\tmp\\pytest-order-overview-ui"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: OrderUIDerivedDateHelpers
- Sub Map: ui_layer
- Type: function
- State: `planned`
- Source: ui/pages/order/shared.py::safe_int,safe_float,safe_date,_parse_date,_month_index,_derive_service_end_date,_derive_staff_payment_date,_derive_subsidy_refund_date,_payment_api_request,_finance_report_request
- Description: Page 2 訂單與帳務總覽共用的安全數值／日期正規化、服務結束日、服務人員付款日與補助退款日推導 helper；不寫入訂單或帳務資料。
- Input:
  - order: 含服務起訖、服務天數與唯讀身分資格的訂單資料。
- Output:
  - derived_dates: 服務結束日、服務人員付款日與補助退款日的顯示值。
- Invariants:
  - helper 只能讀取傳入訂單與帳務資料；不得寫入訂單、客戶、付款或交易資料。
  - 服務人員付款日與補助退款日必須由服務結束日及身分資格推導；缺少必要資料時回傳空值，不得使用今天或其他預設日期補值。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\order\\shared.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: LegacySingleCaregiverMatchingRenderer
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/order/tab2_assign.py::_render_tab2_assign,_api_request,_build_sync_request,_single_caregiver_covers_service_period,_iso_date_text,_parse_iso_date
- Description: 洽談中案件的原四步單月嫂配對相容 renderer；live module path 保留 tab2_assign 以維持匯入相容，但唯一產品 owner 是 MultiCaregiverMatchingCenterUI。先由專用單月嫂 eligibility endpoint 判斷是否存在完整期間候選，只有明確無完整單月嫂組合時才委派多段 renderer。
- Dependencies: [CaregiverSegmentAvailabilityRouter, MatchRouter, OrderRouter]
- Invariants:
  - INV-UI-ASSIGN-01: 媒合紀錄清單僅能顯示至少有一項發送紀錄 (sent_info_1_at/sent_info_2_at) 或意願已變更的有效紀錄。
  - INV-UI-ASSIGN-02: 選取月嫂檢視時嚴禁 speculative 預先建立 DB 紀錄，必須在點擊發送/變更動作時按需 (On-Demand) 建立。
  - 單月嫂 gate 必須呼叫 caregiver-single-eligibility/check 專用 endpoint，並以案件完整起訖與 complete_combinations 判斷；segment_count=1 只存在於 API/Service 內部相容層，不得由產品 UI 傳入。
  - 多段 renderer 只在 single_caregiver_available 明確為 false 時顯示；2／3／4 段 UI 不得與原四步單月嫂流程同時出現。
  - `_build_sync_request` 將空白 deposit_date 正規化為 null；空白 actual_start_date／actual_end_date 分別回退 start_date／end_date，避免把空字串送入 date schema。
  - 案件選單與摘要只顯示 API 投影的 identity_status；不得直接查詢或修改 clients.identity_status，亦不得把 identity_status 送入 assignment-synchronization request。
  - 本 renderer 不再由 OrderUI 或 Page2TabNavigation 呼叫；唯一產品入口是 MultiCaregiverMatchingCenterUI。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests/test_order_assign_identity_status_ui.py", "tests/test_multi_caregiver_scheduling_ui_shell.py", "-q", "-p", "no:cacheprovider", "--basetemp", "C:\\tmp\\pytest-order-assign-identity"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required


##### Module: OrderUI_Tab3_Finance
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/order/tab3_finance.py::_render_tab3_finance,_to_arrow_scalar,_normalize_arrow_compatible_df
- Description: Tab 3 渲染函數 (訂單帳務總覽)，以及將日期型別正規化為 Streamlit Arrow 相容標量的私有 helper。獨立顯示客戶收款與月嫂應付總覽兩張表格，按案件懶加載交易明細，所有帳務寫入僅經由 FastAPI。
- Invariants:
  - Arrow 正規化 helper 僅轉換 DataFrame 顯示用的 null 與日期值；不得修改 API 回應、訂單或帳務資料。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_payment_management_ui.py", "-q", "-p", "no:cacheprovider", "--basetemp", "C:\\tmp\\pytest-payment-management-ui"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: CalendarUI
- Sub Map: ui_layer
- Type: ui_page
- State: `planned`
- Source: ui/pages/03_calendar.py::_render_staff_calendar,_render_assignment_leave_resolution,safe_float,safe_int,safe_date,_normalise_calendar_schedule_map,_multi_caregiver_request,_current_admin_actor,_calendar_has_unsaved_leave_changes,_discard_calendar_leave_drafts,_multi_caregiver_error,_coerce_iso_date_strict,_coerce_staff_id,_extract_case_assignments_for_staff
- Description: 服務人員月曆分頁。由 StaffMonthlyCalendarScheduleRouter 載入單月 assignment／等待訂金鎖占用；出勤精算時沿用上方已選案件與該月嫂的正式 assignment，提供多日期休假逐筆順延／代班 Preview、調整前後與阻擋原因、確認後 atomic Apply。原「多月嫂指派排班」案件→指派功能模塊已從 live code 移除。
- Dependencies: [StaffMonthlyCalendarScheduleRouter, HolidayRouter, StaffRouter, OrderRouter, OrderScheduleCalculationRouter, UIAdminApiContext, MultiCaregiverCaseAssignmentListRouter, MultiCaregiverScheduleReadRouter, MultiCaregiverScheduleRouter, AssignmentScheduleRestDateRouter]
- Invariants:
  - INV-CAL-01: 必須在 HTML 月曆表格繪製前優先執行精算引擎，確保休假天數即時 100% 連動呈現。
  - INV-CAL-02 (兩階段選單隔離): 「訂單匹配」模式僅於行事曆展示黃底預排與 7 天預留備用期，不顯示單日排假與出勤精算面板；「出勤天數精算」模式僅適用於確定實際開工日 (actual_start_date) 案件，解鎖紅底工作日與綠底休假排假控制。
  - INV-CAL-03 (四色月曆視覺公理): ⚪白底=無排班或超出完工日解鎖區間; 🟡黃底=預排案件與完工日後 7 天預留備用期; 🔴紅底=確定服務工作日; 🟢綠底=自訂請假與國定假日放假。
  - INV-CAL-04 (綠底休假與動態順延): 每增加 1 天綠底 🟢 休假，後續紅底 🔴 工作日與服務結束日 (actual_end_date) 自動向後動態順延 1 天，確保實際服務天數 100% 足額達 N 天。
  - INV-CAL-05 (國定假日單日獨立決策): 支援連假期間針對每一個獨立國定假日進行單日個體勾選；選擇放假者在月曆標示為綠底 🟢 且完工日順延 1 天，選擇上班者計為紅底 🔴 正常工作日，`is_double_pay` 一律預設 false。國定假日不得自動加倍；只有個別案件另有明確約定時，才能人工指定 assignment-owned 排班日加倍並留下備註。
  - 月嫂月度檔期視圖必須經由 REST API (`StaffMonthlyCalendarScheduleRouter`: GET /api/v1/staff/{staff_id}/monthly-schedule) 讀取，嚴禁在 UI 層直接執行 Python SQL 語法進行查詢。
  - 上個月／下個月／回到本月與年月直選必須共用單一 view state；畫面「正在查看」標題及兩個 selectbox 必須永遠顯示同一年月。
  - API JSON 的 schedule_map 日期鍵必須先正規化為整數日再查詢；同一日已有正式 assignment 工作列時不得同時顯示「可接案」。
  - 與當月正式 assignment 日列有關的已完成訂單必須保留在出勤精算訂單選單，並以唯讀歷史模式呈現，不得允許修改已完成資料。
  - 國定假日必須經由 `HolidayRouter` 的 GET `/api/v1/holidays` 讀取，並使用 `UIAdminApiContext` 產生的正式 headers；缺少 internal service key 或正式模式管理員 session 時必須停止該請求，不得裸送或偽造權限。
  - 月曆只依已選月嫂與案件 API 回傳的未取消正式 assignments 篩選 assignment_id；不得由 orders.staff_id、日期或姓名推測正式歸屬。
  - 休假草稿每個日期都必須明確選擇順延或代班，代班必填 substitute_staff_id；同次多日期只送一份 batch-preview、一個 preview_fingerprint 與一個 batch-apply。
  - Preview 必須顯示 service_plan_transition.before／after 與 canonical blocking diagnostics；只有管理員填寫原因並確認後才能 Apply，取消或切換前的草稿不得寫入正式排班。
  - 不得直接呼叫 legacy `PUT /rest-dates` 保存休假；正式休假／順延／代班只能透過 AssignmentScheduleRestDateRouter 的 batch Preview／Apply。
  - 不提供同日分時段、planned_hours 或 actual_hours 手動輸入；不得在未經 Preview／Apply 時直接改寫下一位月嫂區段。
  - Preview 必須顯示同案所有有效 assignment 的 actual_hours 加總、訂單計畫時數與差額；不足或超額時 Apply 不可操作。成功 Apply 後薪資依正式排班自動計算，UI 不另提供薪資確認時間或按鈕。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\03_calendar.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_calendar_ui_explicit_errors.py", "tests\\test_multi_caregiver_scheduling_ui_shell.py", "-q", "-p", "no:cacheprovider"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: MultiCaregiverSchedulingPageUI
- Sub Map: ui_layer
- Type: ui_page
- State: `planned`
- Source: ui/pages/03_calendar.py::show
- Description: 多月嫂排班產品入口；主 app 動態導覽與 Streamlit `/calendar` 直接入口共用同一個 `show()`，只建立「服務人員月曆／月嫂配對中心／案件人力配置」三個固定分頁並委派各自 renderer；不在分頁間搬移或重建正式資料。
- Dependencies: [CalendarUI, MultiCaregiverMatchingCenterUI, MultiCaregiverCaseStaffingUI, UIAdminApiContext]
- Invariants:
  - 固定且僅能建立三個產品分頁，順序為服務人員月曆、月嫂配對中心、案件人力配置。
  - 月嫂配對中心的 orders／staff 初始資料只經管理端 API 載入；任一載入失敗必須顯示錯誤，不得 fallback 直連資料庫。
  - 服務人員月曆不得渲染已移除的「多月嫂指派排班」案件→指派功能模塊。
  - 主 app radio 與 Streamlit `/calendar` route 必須顯示相同三分頁；直接入口由 module 的 `__main__` entrypoint 呼叫 `show()`，不得建立第二份頁面流程。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_multi_caregiver_scheduling_ui_shell.py", "-q", "-p", "no:cacheprovider"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: MultiCaregiverMatchingCenterUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/scheduling/matching_center.py::_request,_as_date,_actor,_render_multi_segment_matching,render_matching_center
- Description: 月嫂配對中心。完整重用 LegacySingleCaregiverMatchingRenderer 的原四步單月嫂配對；只有專用單月嫂 eligibility 查詢明確回傳無完整期間組合時才顯示 2／3／4 段多人 fallback，並接續方案版本、逐位資訊／意願、共用履歷、等待訂金鎖定／回復與訂金轉正式 assignment。development／dev／local／test 環境另提供無寫入的多人介面測試預覽，便於驗證 fallback UI，但不改變正式自動 gate。
- Dependencies: [LegacySingleCaregiverMatchingRenderer, CaregiverSegmentAvailabilityRouter, MatchRouter, CaregiverAvailabilityLockRouter]
- Invariants:
  - 單月嫂可完整承接時只顯示原四步機制；查詢失敗不得視為無人符合，2／3／4 段入口不得出現。
  - 非 production 環境可明確開啟「測試顯示多月嫂配對」；此預覽只能查詢最新檔期及編輯 2／3／4 段草稿，聯繫按鈕必須 disabled，且不得建立方案、鎖定檔期或發送聯繫／履歷。
  - 多段預設 2 段、最多 4 段；草稿可暫時不完整，重新查詢後必須保留 partial candidates、未覆蓋日期、衝突 staff/date/reason。
  - 每位月嫂只能被選入一段；候選依目前段日期與已選月嫂在已載入結果本機重篩，並保留「重新查詢最新檔期」操作。
  - 聯繫、資訊-1、資訊-2、意願與履歷為獨立動作；方案建立及每次發送都由 server 重新驗證最新檔期。多段履歷按鈕保持可操作，但未全員願意時 server 必須拒絕實際發送並回報未同意區段。
  - 等待訂金 lock、回復未綁定及轉正式只能呼叫固定 lifecycle endpoints；UI 不得自行建立 assignment、排班或推導 segment 費率。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_multi_caregiver_scheduling_ui_shell.py", "tests\\test_caregiver_matching_plan_router.py", "tests\\test_caregiver_availability_lock_router.py", "-q", "-p", "no:cacheprovider"], "cwd": "project", "timeout": 120, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: MultiCaregiverCaseStaffingUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/scheduling/case_staffing.py::_request,_date_value,render_case_staffing
- Description: 案件人力配置。只列訂單成立／服務中案件，以 1–4 行編輯完整正式 assignment 目標計畫；減少行數只顯示取消候選，必須先顯示調整前／後、排班移除與阻擋原因，再由管理員確認後呼叫同步 Apply。未建立正式 assignment 的未來 legacy 案件可用 orders.staff_id 作為首次 UI 建議值，但只有 Apply 成功後建立的 case_staff_assignments 才是正式 ownership。
- Dependencies: [OrderRouter, StaffRouter, MultiCaregiverCaseAssignmentListRouter, LegacySingleCaregiverMatchingRenderer]
- Invariants:
  - 預設行數為目前未取消 assignment 數量（最少 1、最多 4），一行只代表一個 assignment；同一月嫂不連續期間不得在 UI 自動合併。
  - 空白 deposit_date 必須送 null，空白 actual dates 必須由共用 `_build_sync_request` 回退 planned dates，不得把空字串送入日期 schema。
  - 現況無正式 assignment 時，legacy orders.staff_id 只能一次性預填第一段候選，不得顯示為既有正式配置、不得略過 Preview／Apply，也不得作為排班或薪資 ownership。
  - `sync_status=requires_allocation` 時 UI 必須顯示目標時數、提議 actual_hours、差額與調整指引，並停止渲染 Apply 操作；只有 `in_sync` 且無 blocking_reasons 時才能確認套用。
  - Preview request 變更後舊結果立即失效；調整前、調整後、required_schedule_removals 與 blocking_reasons 必須在 Apply 前顯示。
  - 阻擋原因非空時不得 Apply；成功 Apply 必須包含完整 removal ids、非空 actor 並清除舊 preview state。
  - 取消調整只清除 UI 草稿，不得修改正式 assignment 或 staff_schedule。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_multi_caregiver_scheduling_ui_shell.py", "tests\\test_order_assignment_synchronization_router.py", "-q", "-p", "no:cacheprovider"], "cwd": "project", "timeout": 120, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required


##### Module: EditOrderUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/order/editor.py::render_editor
- Dependencies: [OrderRouter, OrderScheduleCalculationRouter, ClientPaymentRouter]
- Description: 單筆訂單基本資料與動態試算維護頁。正式月嫂、assignment、服務區段、換人、排班移除與同步 Preview／Apply 已移至 MultiCaregiverSchedulingPageUI；本頁只保存基本訂單資料與既有訂單狀態／取消動作。
- Complexity: medium
- Input:
  - editable_order_change: 訂單基本欄位與 UI 即時計算所需值，不含 assignment_plan、schedule_change_plan 或 applied_by。
- Output:
  - basic_order_update: 經 `/full-details` 保存的訂單基本資料結果。
- Algorithm:
  - 讀取 API 投影的 identity_status 並以唯讀欄位顯示，透過 OrderScheduleCalculationRouter 提供表單內動態日期／金額試算。
  - 「儲存訂單基本資料」只呼叫 OrderRouter `/full-details`；正式月嫂與排班提示使用者前往 MultiCaregiverSchedulingPageUI。
- Invariants:
  - INV-EDIT-01: 修改輸入欄位時，費用與完工日必須即時連動試算，且金額統一無小數點 safe_int 呈現。
  - INV-EDIT-03: 所有由公式自動衍生之金額與時數欄位，預設必須為唯讀鎖定狀態。
  - INV-EDIT-04: 強制解鎖自動試算欄位時，必須顯性跳出警告告知公式連動失效風險。
  - 服務人員付款日與補助退款日必須依服務結束日及身分資格推導，並在「五、實收對帳、狀態與備註登錄區」以唯讀鎖定欄位顯示；不得寫入訂單或帳務資料。
  - 本頁的 `/full-details` 只保存 client_name；服務日期、服務天數、每日時數、樓層費與訂金日期皆為唯讀，正式人力與排班敏感變更只由案件人力配置的 assignment-synchronization Preview／Apply 處理。
  - 必須呼叫既有 `OrderScheduleCalculationRouter` 的 calculate-schedule endpoint 進行表單內試算，不得在 UI 直接執行 SQL。
  - 補助資格只能使用訂單 API 投影的 identity_status 唯讀呈現；UI 不得提供修改控制項或傳送 identity_status。
  - 訂金應收日期可為空值，空值不得以今天或第一期應收日自動補值。
  - 本頁不得呈現 assignment_plan、required_schedule_removals、applied_by、換月嫂或正式同步控制項。
  - 帳務仍由新帳務介面獨立處理；本頁不得建立、調整或取消 client／staff payments、月結或轉帳。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests/test_edit_order_synchronization_ui.py", "-q", "-p", "no:cacheprovider", "--basetemp", "C:\\tmp\\pytest-edit-order-synchronization-ui"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required


##### Module: EditOrderDerivedDateHelpers
- Sub Map: ui_layer
- Type: function
- State: `planned`
- Source: ui/pages/order/editor.py::_parse_date,_month_index,_derive_service_end_date,_derive_staff_payment_date,_derive_subsidy_refund_date,_generate_virtual_account
- Description: 訂單編輯頁顯示用的服務結束日、服務人員付款日與補助退款日推導 helper；結果僅供唯讀欄位呈現。
- Input:
  - order: 含實際服務日期、服務天數與唯讀身分資格的訂單資料。
- Output:
  - derived_dates: 顯示於實收對帳區的衍生日期。
- Invariants:
  - helper 不得寫入訂單、客戶、付款、月結或交易資料。
  - 服務人員付款日與補助退款日必須由服務結束日及身分資格推導，且僅供「五、實收對帳、狀態與備註登錄區」的唯讀欄位使用。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\order\\editor.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: FormManagementUI
- Sub Map: ui_layer
- Type: ui_page
- State: `validated`
- Source: ui/pages/05_form_management.py::show,_render_form_management_page_shell
- Dependencies: [FormManagementUI_Shared, FormManagementUI_Tab1_FormBuilder, FormManagementUI_Tab2_TemplateLibrary, FormManagementUI_Tab3_ContractManagement]
- Description: 表單與履歷問卷管理專頁。支援動態新建自訂表單沙盒、線上編輯修改既有模板欄位名稱、拖拉平移排序、二次確認刪除防呆、5:5側邊雙視窗實時預覽/PDF導出、SQL原生資料表歸屬分類選擇器、獨立JSON模板目錄、Excel長文字溢出不撐高列高、顯式邊框與PDF乾淨去雜線、全量資料庫欄位100%開載、100%全寬滿版預覽切換器，以及 Tab 3: Excel 變數代理制式定型化契約引擎 (EPPP Engine)。
- Invariants:
  - INV-UI-FORM-01: 支援手動新增自訂欄位，並提供單行文字、多行文字、數字、日期與綁定 DB 欄位 5 大資料型態。
  - INV-UI-FORM-06: 實施 Draft Buffer 編輯草稿隔離機制，點擊取消時 100% 丟棄記憶體草稿，嚴禁修改硬碟。
  - INV-UI-FORM-09: 刪除表單模板必須具備二次顯性確認視窗 (Delete Confirmation Modal Guardrail)，防範誤觸刪除。
  - INV-UI-FORM-16: 支援 Excel 原生範本 (.xlsx) 之 {P1}, {P2} 變數標籤自動掃描解析器 (EPPP Protocol)。
  - INV-UI-FORM-25: 全量資料庫欄位 100% 完整開載公理 (Full Schema Enrollment Protocol): 掃描 orders, clients, staff, beclass_records 100+ 個全量欄位填入 UI 二階選單。
  - INV-UI-FORM-28: 100% 全寬滿版契約預覽切換公理 (Full-Width Contract Canvas Switcher Protocol): 提供 5:5 左右對照維護模式與 100% 全寬滿版 A4 沉浸預覽模式無縫切換。
  - 訂單欄位字典、範本選項與統計只能使用 clients.identity_status；不得提供或統計 clients.identity_status，且 identity_status 為唯讀資料來源。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_form_management_identity_status_ui.py", "-q", "-p", "no:cacheprovider", "--basetemp", "C:\\tmp\\pytest-form-management-identity"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: FormManagementUI_Shared
- Sub Map: ui_layer
- Type: function
- State: `planned`
- Source: ui/pages/form_management/shared.py::fetch_staff_contract_context,flatten_staff_contract_context,generate_field_id,get_html_hex_color,get_border_style,load_contract_templates,save_contract_template,load_json_templates,save_single_template,delete_single_template,save_json_templates,safe_int,format_db_value,get_table_for_key,render_html_document
- Description: 表單與履歷問卷管理共用的模板 JSON/Excel 讀寫、樣式渲染與格式化 helper。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\form_management\\shared.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: FormManagementUI_Tab1_FormBuilder
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/form_management/tab1_form_builder.py::_render_tab1_form_builder
- Description: Tab 1 手動創建與設計新表單 (UX 實驗室) 渲染組件。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\form_management\\tab1_form_builder.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: FormManagementUI_Tab2_TemplateLibrary
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/form_management/tab2_template_library.py::_render_tab2_template_library
- Description: Tab 2 自訂表單模板庫與 5:5 雙視窗線上編輯預覽渲染組件。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\form_management\\tab2_template_library.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: FormManagementUI_Tab3_ContractManagement
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/form_management/tab3_contract_management.py::_render_tab3_contract_management
- Description: Tab 3 制式定型化契約管理 (EPPP 變數代理引擎) 渲染組件。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\form_management\\tab3_contract_management.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: LegacyPaymentUIFreeze
- Sub Map: ui_layer
- Type: ui_page
- State: `planned`
- Source: ui/pages/order/tab3_finance.py::_render_legacy_mixed_payment_overview
- Description: Page 2 第三分頁的帳務明細總覽；先呈現全部案件的可篩選帳務摘要，僅在展開案件時才讀取客戶與月嫂交易明細，取代已移除的 legacy payments 編輯器。
- Input:
  - orders_data: current order list
- Output:
  - payment_overview: filterable all-case payment summaries with on-demand ledger details
- Invariants:
  - 不得查詢或寫入 legacy payments。
  - 預設必須以客戶收款總覽與月嫂應付總覽兩張獨立表格顯示全部已有帳務的案件，並提供案件編號、訂單狀態與各自付款狀態篩選；兩張表不得交錯欄位。
  - 客戶收款總覽必須唯讀顯示依案件服務結束日及身分資格推導的服務人員付款日與補助退款日；不得將其寫入付款或訂單資料。
  - 客戶表必須顯示訂金、第一期、第二期各自的應收、實收、應收日與實收日，以及合計；有退還補助款時一併顯示。
  - 月嫂表必須逐筆顯示服務時數、單價、服務薪資、樓層費、調整額、應付／實付／餘額與付款日期，並使用 staff_payments 的 amount_paid 與 due_date 欄位。
  - 使用者選擇特定案件後，自動取得並在展開區顯示客戶／月嫂交易明細；不得預先讀取其他案件明細。
  - 實收／實付與日期只能來自交易明細；人工補登交易時必填原因，不得直接覆寫摘要欄位。
  - 不得在此分頁重複實作待匯清單或匯出功能。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\order\\tab3_finance.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: LegacyPaymentEditFreeze
- Sub Map: ui_layer
- Type: function
- State: `planned`
- Source: ui/pages/order/editor.py::safe_float,safe_int,safe_date,safe_optional_date
- Description: 停止訂單編輯頁的輔助資料處理讀寫舊 payments；實際編輯與同步提交由 EditOrderUI 擁有，新帳務改由新帳務介面處理。
- Invariants:
  - 不得呼叫 get_table_data('payments') 或 update_payment_details。
  - 訂單主資料與狀態更新不得因停用舊帳務同步而中斷。
- Observability: not_required

##### Module: Page2TabNavigation
- Sub Map: ui_layer
- Type: ui_page
- State: `planned`
- Source: ui/pages/02_orders.py::show
- Dependencies: [OrderUI, OrderRouter, StaffRouter]
- Description: Page 2 的入口；只經既有 FastAPI 載入 orders 與 staff，使用空 clients 清單作為既有函式簽名相容橋接，並處理初始化錯誤後將資料交給不含配對 Tab 的 OrderUI 四分頁殼層。
- Output:
  - page2_entry: 完成初始化後交由 OrderUI 顯示的訂單頁。
- Invariants:
  - 不得出現 get_table_data('payments')、update_payment_details 或 legacy payments SQL。
  - show() 不得匯入或呼叫 services.db_service；訂單與月嫂初始資料只能分別來自 GET /api/v1/orders 與 GET /api/v1/staff。
  - 兩支 API 都必須設定有限 timeout、驗證 BaseResponse 成功狀態、取出 data 並確認為 list；HTTP、JSON 或資料形狀錯誤時必須顯示初始化失敗並停止，不得 fallback 直連 DB。
  - 本節點不得查詢全量 clients；在 OrderUI 舊介面移除 clients 參數前，必須以 clients=[] 呼叫殼層，不得新增第三支初始化 API。
  - show() 不得直接建立 Tab 或直接呼叫任何 Tab renderer；必須只呼叫 _render_order_page_shell(orders_data, clients, staff_list)。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "-q", "tests\\test_order_ui_shell_ownership.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\02_orders.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Non Goals:
  - 不渲染或呼叫 LegacySingleCaregiverMatchingRenderer；配對中心只由 MultiCaregiverSchedulingPageUI 擁有。
  - 不新增或修改 API Router、Service、帳務總覽、應付帳款或補助核銷 renderer。
- Observability: not_required

##### Module: PaymentManagementUI
- Sub Map: ui_layer
- Type: ui_page
- State: `planned`
- Source: ui/pages/order/tab3_finance.py::_render_client_payment_ledger,_render_staff_payment_ledger
- Description: 提供客戶收款與月嫂應付／轉帳兩個獨立操作區；所有帳務讀寫只經 FastAPI，退還補助金額暫不提供 UI 操作。
- Input:
  - api_request: path、method 與 JSON payload。
  - client_ledger: case_no 與單一客戶帳務／交易明細。
  - staff_ledger: case_no 與同案月嫂應付／交易明細清單。
- Output:
  - client_receipt_zone: 客戶應收／實收、交易明細與收款／沖回提交表單。
  - staff_payable_transfer_zone: 月嫂應付／實付、交易明細與轉帳／沖回提交表單。
- Invariants:
  - 客戶收款與月嫂應付／轉帳必須是獨立操作區與表單；任一區塊儲存時不得覆蓋、改寫或重建另一張帳務表。
  - 客戶區只可讀寫 /client-payments/*；提交時只傳送 case_no、stage、transaction_type、amount、occurred_at、external_reference 與 notes。
  - 月嫂區只可讀寫 /staff-payments/*；提交時只傳送 staff_payment_id、transaction_type、amount、occurred_at、external_reference 與 notes。
  - external_reference 與 notes 為兩區提交交易的必填追溯資料；不得直接覆寫帳務摘要欄位。
  - 不得改動 LegacyPaymentUIFreeze 的 _render_tab3_finance；不得影響既有 Tab4 AccountsPayableExportUI 與 Tab5 SubsidyReconciliationRegisterUI。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "-q", "tests\\test_payment_management_ui.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\order\\tab3_finance.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Non Goals:
  - 不新增退還補助款操作 UI。
  - 不清理或改寫 _render_legacy_mixed_payment_overview。
  - 不處理 ADAD v2→v3 遷移、approved hash 回填、helper Source binding 或 FinanceImportRawStagingSchema 跨分片依賴。
- Observability: not_required

##### Module: AccountsPayableExportUI
- Sub Map: ui_layer
- Type: ui_page
- State: `planned`
- Source: ui/pages/order/tab4_accounts_payable.py::_render_tab4_accounts_payable
- Description: Reconnect Page 2's fourth accounts-payable tab and fifth subsidy-reconciliation tab to their read-only FastAPI endpoints.
- Complexity: low
- Invariants:
  - The tab is read-only preparation and download; it must not mark staff or client payments as transferred, paid, refunded, or submitted.
  - The tab must be the fourth Page 2 tab, while the existing frozen third finance tab remains unchanged.
  - 顯示永豐銀行月嫂款與台新銀行退還補助款總額；不得顯示解約退款。
- Algorithm:
  - Read monthly preview and XLSX only through FinanceReportRouter; do not import AccountsPayableExport or db_service directly.
  - Read quarterly and annual reconciliation previews and downloads only through FinanceReportRouter; do not import the reconciliation service directly.
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\order\\tab4_accounts_payable.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: SubsidyReconciliationRegisterUI
- Sub Map: ui_layer
- Type: ui_page
- State: `planned`
- Source: ui/pages/order/tab5_subsidy_reconciliation.py::_render_tab5_subsidy_reconciliation
- Description: Add Page 2's fifth tab, 核銷補助清冊, with quarterly and annual read-only previews and XLSX downloads.
- Complexity: low
- Invariants:
  - The tab must be the fifth Page 2 tab and must not alter the previous four tabs.
  - Provide separate quarterly and annual views, with downloads only and no data writes.
  - Do not render the subsidized-citizen lower section when it has no rows.
- Algorithm:
  - Read quarterly and annual previews and downloads only through FinanceReportRouter; do not import the reconciliation service directly.
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\order\\tab5_subsidy_reconciliation.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: FinanceAlertCenterUI
- Sub Map: ui_layer
- Type: ui_page
- State: `planned`
- Source: ui/pages/06_finance_alerts.py::show
- Dependencies: [FinanceAlertRouter]
- Description: 提供跨 CLIENT、RETURN、SUBSIDY、STAFF、COMMON 的財務警示中心，供人工檢視、認領與解除，不直接操作正式帳務。
- Complexity: medium
- Input:
  - filters: status、alert_code、source domain 與分頁
  - operator_action: operator reference、claim 或 resolve reason
- Output:
  - alert_center: 警示清單、row/batch 或正式來源、候選、expected/actual/difference 與事件歷程
- Invariants:
  - UI 只可透過 FinanceAlertRouter 讀取、claim、resolve；不得 import FinanceAlertWorkflowService、FinanceAlertDetectionService、FinanceAlertEventService、db_service 或直接執行 SQL。
  - 不提供建立 transaction、allocation、retransfer、reversal、修改應收／應付或強制對平的操作。
  - resolve 畫面必須明示「解除警示不等於完成核銷」，並要求非空原因。
  - candidate snapshot 只供人工判讀；不得用預設選項、列表第一筆或同額候選自動提交正式對象。
- Algorithm:
  - 以獨立 Streamlit 頁面載入警示清單及詳細事件歷程，依狀態與警示編號篩選。
  - 對選定警示呼叫 claim 或 resolve API，顯示 conflict 與 invalid transition，不在 UI 本地假設成功。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\06_finance_alerts.py"], "cwd": "project", "expect_exit": 0}
- Observability: not_required

##### Module: LineManagementUI
- Sub Map: ui_layer
- Type: ui_page
- State: `planned`
- Source: ui/pages/07_line_management.py::show
- Dependencies: [LineAdminApiClient, LineMessageManagementUI, LineScheduleManagementUI, LineTaskManagementUI, LineRichMenuManagementUI, LineLiffManagementUI, LineReviewManagementUI]
- Description: LINE 管理中心入口，組合登入狀態、管理分頁與各專責 LINE 管理元件。
- Complexity: low
- Observability: not_required

##### Module: LineAdminApiClient
- Sub Map: ui_layer
- Type: ui_client
- State: `planned`
- Source: ui/api_clients/line_api_client.py
- Description: LINE 管理頁的 HTTP client，封裝管理員登入、管理 API 呼叫與錯誤回應處理。
- Complexity: low
- Observability: not_required

##### Module: LineMessageManagementUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/components/line_message_manager.py::render_message_manager
- Description: LINE 訊息內容管理分頁的渲染元件。
- Complexity: low
- Observability: not_required

##### Module: LineScheduleManagementUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/components/line_schedule_manager.py::render_schedule_manager
- Description: LINE 自動通知排程管理分頁的渲染元件。
- Complexity: low
- Observability: not_required

##### Module: LineTaskManagementUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/components/line_task_manager.py::render_task_manager
- Description: LINE 發送任務與執行紀錄管理分頁的渲染元件。
- Complexity: low
- Observability: not_required

##### Module: LineRichMenuManagementUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/components/line_rich_menu_manager.py::render_rich_menu_manager
- Description: LINE Rich Menu 管理分頁的渲染元件。
- Complexity: low
- Observability: not_required

##### Module: LineLiffManagementUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/components/line_liff_manager.py::render_liff_manager
- Description: LINE LIFF 表單管理分頁的渲染元件。
- Complexity: low
- Observability: not_required

##### Module: LineReviewManagementUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/components/line_review_manager.py::render_review_manager
- Description: LINE 待確認申請管理分頁的渲染元件。
- Complexity: low
- Observability: not_required

##### Module: StaffContractExcelMirror
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/form_management/shared.py::render_excel_contract_mirror
- Description: Register and render the copied staff-service contract workbook through the existing read-only Excel mirror.
- Complexity: low
- Invariants:
  - The staff contract must use db/templates/contracts/服務人員契約.xlsx and must not modify that workbook.
  - Contract template selection must render any registered .xlsx contract through the same mirror path.
  - Only fields available in the selected order may be filled; unmapped template cells remain unchanged.
- Algorithm:
  - Fetch selected staff-contract context from ContractContextRouter by case_no and assignment_id; do not assemble contract facts from db_service directly.
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\form_management\\shared.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required
