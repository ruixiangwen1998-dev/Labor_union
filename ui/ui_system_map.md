# UI System Map

### Domain: UI
- Description: Streamlit user interface, page composition, and HTTP client contracts.
- Allowed Dependencies: [API]

##### Module: AppShellUI
- Sub Map: ui_layer
- Type: ui_shell
- Source: ui/app.py
- Description: Streamlit 側邊欄導覽殼層，動態載入 ui/pages/ 頁面。
- Dependencies: [DataBrowserUI, OrderUI, CalendarUI, FormManagementUI, FinanceAlertCenterUI, LineManagementUI]
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
- Description: Page 2 訂單與帳務管理頁的殼層；建立固定順序的五個 Tab，並將已載入資料分派至各自的 renderer。
- Dependencies: [OrderUI_Tab1_Overview, OrderUI_Tab2_Assign, OrderUI_Tab3_Finance, AccountsPayableExportUI, SubsidyReconciliationRegisterUI]
- Input:
  - orders_data: 已載入的訂單資料。
  - clients: 已載入的客戶資料。
  - staff_list: 已載入的服務人員資料。
- Output:
  - page2_tabs: 依序渲染的訂單總覽、配對、帳務總覽、應付帳款與核銷補助 Tab。
- Invariants:
  - INV-UI-01: 所有費用與金額數字統一無條件四捨五入整數化呈現 (帶千分位)，無小數點。
  - INV-UI-02: 必須透過 safe_int() 轉換數值，防範 NaN, None, Inf 及空字串導致的 ValueError 崩潰。
  - 必須固定建立五個 Tab，且依序分派 OrderUI_Tab1_Overview, OrderUI_Tab2_Assign, OrderUI_Tab3_Finance, AccountsPayableExportUI 與 SubsidyReconciliationRegisterUI。
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

##### Module: OrderUI_Tab2_Assign
- Sub Map: ui_layer
- Type: ui_component
- State: `validated`
- Source: ui/pages/order/tab2_assign.py::_render_tab2_assign,_api_request,_build_sync_request,_iso_date_text,_parse_iso_date
- Description: Tab 2 渲染函數 (案件與配對中心)。僅列出「洽談中」待配對案件，提供單筆案件控制面板、4 大智慧粗篩可選條件與 4 步智慧配對流程；排休更新必須綁定 assignment_id 專屬 API。
- Dependencies: [AssignmentScheduleRestDateRouter, MatchRecordRouter]
- Invariants:
  - INV-UI-ASSIGN-01: 媒合紀錄清單僅能顯示至少有一項發送紀錄 (sent_info_1_at/sent_info_2_at) 或意願已變更的有效紀錄。
  - INV-UI-ASSIGN-02: 選取月嫂檢視時嚴禁 speculative 預先建立 DB 紀錄，必須在點擊發送/變更動作時按需 (On-Demand) 建立。
  - 案件選單與摘要的身分資格只能顯示 clients.identity_status；不得讀取或顯示 clients.identity_status。
  - 排休與動態順延保存必須透過 API 呼叫 `PUT /api/v1/assignment-schedules/{assignment_id}/rest-dates`，嚴禁呼叫或衍生已停用/危險之 `PUT /api/v1/orders/{case_no}/rest-dates` 端點。
  - Page2TabNavigation (Tab導覽選單) 必須單獨保留、單獨維護與審查，不得將本 UI 與其他 UI 混合變更。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests/test_order_assign_identity_status_ui.py", "-q", "-p", "no:cacheprovider", "--basetemp", "C:\\tmp\\pytest-order-assign-identity"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
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
- State: `validated`
- Source: ui/pages/03_calendar.py::show,safe_float,safe_int,safe_date,_multi_caregiver_request,_multi_caregiver_error,_render_multi_caregiver_panel,_coerce_iso_date_strict,_coerce_staff_id,_extract_case_assignments_for_staff,_parse_stored_rest_dates
- Description: 服務人員行事曆與檔期調控獨立頁面。由 StaffMonthlyCalendarScheduleRouter 載入月度檔期視圖，提供多月嫂案件→正式服務指派選擇、指派專屬日排班呈現與單日調整。
- Dependencies: [StaffMonthlyCalendarScheduleRouter, HolidayRouter, UIAdminApiContext, MultiCaregiverCaseAssignmentListRouter, MultiCaregiverScheduleReadRouter, MultiCaregiverScheduleRouter]
- Invariants:
  - INV-CAL-01: 必須在 HTML 月曆表格繪製前優先執行精算引擎，確保休假天數即時 100% 連動呈現。
  - INV-CAL-02 (兩階段選單隔離): 「訂單匹配」模式僅於行事曆展示黃底預排與 7 天預留備用期，不顯示單日排假與出勤精算面板；「出勤天數精算」模式僅適用於確定實際開工日 (actual_start_date) 案件，解鎖紅底工作日與綠底休假排假控制。
  - INV-CAL-03 (四色月曆視覺公理): ⚪白底=無排班或超出完工日解鎖區間; 🟡黃底=預排案件與完工日後 7 天預留備用期; 🔴紅底=確定服務工作日; 🟢綠底=自訂請假與國定假日放假。
  - INV-CAL-04 (綠底休假與動態順延): 每增加 1 天綠底 🟢 休假，後續紅底 🔴 工作日與服務結束日 (actual_end_date) 自動向後動態順延 1 天，確保實際服務天數 100% 足額達 N 天。
  - INV-CAL-05 (國定假日單日獨立決策): 支援連假期間針對每一個獨立國定假日進行單日個體勾選；選擇放假者在月曆標示為綠底 🟢 且完工日順延 1 天，選擇上班者計為紅底 🔴 正常工作日 (預設雙倍薪資)。
  - 月嫂月度檔期視圖必須經由 REST API (`StaffMonthlyCalendarScheduleRouter`: GET /api/v1/staff/{staff_id}/monthly-schedule) 讀取，嚴禁在 UI 層直接執行 Python SQL 語法進行查詢。
  - 國定假日必須經由 `HolidayRouter` 的 GET `/api/v1/holidays` 讀取，並使用 `UIAdminApiContext` 產生的正式 headers；缺少 internal service key 或正式模式管理員 session 時必須停止該請求，不得裸送或偽造權限。
  - 多月嫂模式必須先選 case_no、再從該案件 API 回傳的正式指派中選 assignment_id；不得由 orders.staff_id、日期或姓名推測指派。
  - 多月嫂模式只透過 MultiCaregiverCaseAssignmentListRouter、MultiCaregiverScheduleReadRouter 與 MultiCaregiverScheduleRouter 讀寫；不得呼叫 legacy 排班 helper。
  - 不提供同日分時段、planned_hours 或 actual_hours 手動輸入；單日請假不自動延伸、覆寫或移動下一位月嫂的服務區段。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "py_compile", "ui\\pages\\03_calendar.py"], "cwd": "project", "timeout": 60, "expect_exit": 0}
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_calendar_ui_explicit_errors.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required


##### Module: EditOrderUI
- Sub Map: ui_layer
- Type: ui_component
- State: `planned`
- Source: ui/pages/order/editor.py::render_editor
- Dependencies: [OrderRouter, OrderScheduleCalculationRouter, MultiCaregiverCaseAssignmentListRouter, StaffRouter]
- Description: 單筆訂單 38 欄位動態試算與資料維護頁面。採用 st.columns 與帶邊框 Container 打造實體訂單單據視覺，具備 Formula Lock Guardrail，以及訂單變更→完整月嫂指派→帳務／排班預覽→明確套用的一致性工作流。
- Complexity: medium
- Input:
  - editable_order_change: 非取消訂單的完整訂單目標值，含排班關鍵欄位與客戶／訂單主資料。
  - assignment_plan: 使用者明確建立或調整的完整正式月嫂服務區段；既有有效指派未列入時，UI 必須明示其為取消候選。
  - applied_by: 非空操作識別；不得從月嫂姓名、訂單欄位或預設值推測。
- Output:
  - synchronization_preview: target_hours、差額、required_schedule_removals、帳務鎖定或人工覆核原因。
  - synchronization_apply_result: 成功套用後的正式指派與排班摘要，並觸發訂單頁重新載入。
- Algorithm:
  - 讀取目前案件的 clients.identity_status 並以唯讀欄位顯示，要求使用者提交完整 assignment_plan；不得以 orders.staff_id、媒合紀錄或第一筆候選推測指派。
  - 對 OrderScheduleCalculationRouter 送出排班與順延試算請求，對 OrderRouter preview 送出完整訂單目標值與指派計畫，清楚呈現時數差額、鎖定原因及 required_schedule_removals。
  - 「確定儲存」先要求有效且未過期的 preview；僅當 preview 可套用時，要求使用者逐筆明確確認全部 required_schedule_removals 與非空 applied_by，再呼叫 OrderRouter apply；HTTP 409／422 必須顯示原始原因而非呈現成功。
  - apply 成功後清除本頁同步草稿並 rerun 重新讀取訂單；CalendarUI 下次顯示時由正式指派與日排班 API 重新讀取，不保留舊排班快取。
- Invariants:
  - INV-EDIT-01: 修改輸入欄位時，費用與完工日必須即時連動試算，且金額統一無小數點 safe_int 呈現。
  - INV-EDIT-03: 所有由公式自動衍生之金額與時數欄位，預設必須為唯讀鎖定狀態。
  - INV-EDIT-04: 強制解鎖自動試算欄位時，必須顯性跳出警告告知公式連動失效風險。
  - 服務人員付款日與補助退款日必須依服務結束日及身分資格推導，並在「五、實收對帳、狀態與備註登錄區」以唯讀鎖定欄位顯示；不得寫入訂單或帳務資料。
  - INV-EDIT-05: 任何含 service_days、service_hours_per_day、start_date、end_date、actual_start_date 或 actual_end_date 的訂單變更，必須只經 OrderRouter 的 preview／apply 同步流程；不得先呼叫 db_service.update_order_full_details 或 `/full-details`。
  - 必須呼叫 `OrderScheduleCalculationRouter` (POST /api/v1/orders/schedule-calculation) 進行出勤排班與完工日順延試算。
  - 補助資格只能以 clients.identity_status 唯讀呈現；UI 不得提供修改控制項、不得傳送 identity_status 或 clients.identity_status，也不得讀取 clients.identity_status。
  - 訂金應收日期可為空值，空值不得以今天或第一期應收日自動補值；送出同步請求時須保留 null。
  - 多月嫂正式指派只能由 case_no 的正式指派 API 建立或選取；不得由 orders.staff_id、媒合紀錄、姓名、日期或列表第一筆推測。
  - apply 前必須明確呈現並確認 required_schedule_removals；遇帳務鎖定、人工時數覆寫或時數差額時，不得繞過、靜默降級或顯示成功。
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
- Description: Page 2 的入口；只經既有 FastAPI 載入 orders 與 staff，使用空 clients 清單作為既有 OrderUI 介面的暫時相容橋接，並處理初始化錯誤後將資料交給 OrderUI 殼層渲染。
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
  - 不修改 OrderUI 或 OrderUI_Tab2_Assign 的函式簽名；clients=[] 只是本輪原子遷移的相容橋接。
  - 不修改配對中心內部的 DbService 呼叫；該範圍屬後續 OrderUI_Tab2_Assign 節點。
  - 不新增或修改 API Router、Service、Tab 3、Tab 4 或 Tab 5。
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
