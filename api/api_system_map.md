# API Layer Sub-System Map (Version 3.2)

> **Scope**: `api/` RESTful API 服務層  
> **Master Reference**: [`../system_map.md`](../system_map.md)

---

### API 功能層模組全覽表

### Domain: API
- Description: FastAPI routers, request and response schemas, and authorization dependencies.
- Allowed Dependencies: [Services]

##### Module: OrderRouter
- Sub Map: api_layer
- Source: api/routes/orders.py
- Type: api_router
- State: `planned`
- Description: 訂單、時程精算與明確多月嫂指派同步 API 路由；同步端點接收完整非取消訂單目標值，只委派已部署的 Preview／Apply 服務，不能直接寫入正式資料表。
- Dependencies: [DbService, OrderSchemas, OrderAssignmentSynchronizationPreviewService, OrderAssignmentSynchronizationApplyService]
- Complexity: medium
- Input:
  - case_no: path 中的 canonical order identifier。
  - preview_request: 包含排班關鍵欄位與可編輯訂單主資料完整目標值的 order_change，以及完整明確 assignment_plan。
  - apply_request: preview_request 加上完整 schedule_change_plan.remove_schedule_ids 與非空 applied_by。
- Output:
  - synchronization_preview: target hours、指派時數影響、required_schedule_removals、sync_status 與 blocking_reasons。
  - synchronization_apply_result: 已套用結果、排班生成摘要、時數確認與 audit_id。
- Algorithm:
  - `POST /{case_no}/assignment-synchronization/preview` 驗證完整非取消 order_change（不含 identity_status 或 clients.identity_status）與 HTTP payload 後，僅委派 OrderAssignmentSynchronizationPreviewService，並以 BaseResponse 回傳其完整結果。
  - `POST /{case_no}/assignment-synchronization/apply` 驗證完整移除計畫及 applied_by 後，僅委派 OrderAssignmentSynchronizationApplyService；未套用的 locked、requires_review 或 requires_allocation 結果須回傳明確 HTTP 409 與原因。
  - 服務層的 ValueError 必須轉成明確 HTTP 422；不得由 router 吞掉後回傳成功。
- Invariants:
  - 兩個同步端點不得直接呼叫 db_service 寫入 orders、case_staff_assignments、staff_schedule、付款、月結或稽核表；所有同步商業操作只能委派對應服務。
  - Preview 必須是唯讀；Apply request 必須同時提供完整 assignment_plan、schedule_change_plan.remove_schedule_ids 與非空 applied_by，缺少時不得呼叫 Apply 服務。
  - 同步端點不得自行寫入訂單或客戶主資料；更新必須由 Apply service 的單一 transaction 完成。EditOrderUI 不得以 `/full-details` 先寫入同一份同步變更。
  - API 不得接受 clients.identity_status 或 identity_status；身分資格只能由服務層依 case_no 關聯 clients.identity_status 讀取。
  - Router 不得建立第二個 FastAPI app 或重複註冊；必須沿用既有 `/api/v1/orders` router 與 BaseResponse 包裝。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_order_assignment_synchronization_router.py", "tests\\test_order_assignment_synchronization_app_routes.py", "-q", "-p", "no:cacheprovider", "--basetemp", "C:\\tmp\\pytest-order-assignment-router"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: MatchRouter
- Sub Map: api_layer
- Source: api/routes/matches.py
- Type: api_router
- State: `validated`
- Description: 案件與配對中心 API 路由。
- Dependencies: [DbService, MatchSchemas]
- Observability: not_required

##### Module: ScheduleRouter
- Sub Map: api_layer
- Source: api/routes/schedule.py
- Type: api_router
- State: `validated`
- Description: 月嫂服務人員行事曆與動態順延排班保存 API 路由。
- Dependencies: [DbService, ScheduleSchemas]
- Observability: not_required

##### Module: ScheduleSchemas
- Sub Map: api_layer
- Source: api/schemas/schedule.py
- Type: api_schema
- State: `validated`
- Description: 驗證案件排班日期、工作日與薪資日標記。
- Dependencies: []
- Observability: not_required

##### Module: PaymentRouter
- Sub Map: api_layer
- Source: api/routes/payments.py
- Type: api_router
- State: `validated`
- Description: legacy payments API source 退役模組；檔案不得再宣告 APIRouter、endpoint、HTTP 410 handler 或匯入舊 PaymentSchemas。
- Dependencies: []
- Invariants:
  - api/routes/payments.py 不得包含 APIRouter、router、get_all_payments、update_payment、_legacy_payments_removed 或 api.schemas.payments。
  - `/api/v1/payments` 不得出現在執行中 FastAPI OpenAPI。
- Verification: []
- Observability: not_required

##### Module: ClientRouter
- Sub Map: api_layer
- Source: api/routes/clients.py
- Type: api_router
- State: `validated`
- Description: 客戶名冊 API 路由。
- Dependencies: [DbService]
- Observability: not_required

##### Module: StaffRouter
- Sub Map: api_layer
- Source: api/routes/staff.py
- Type: api_router
- State: `validated`
- Description: 服務人員名冊 API 路由。
- Dependencies: [DbService]
- Observability: not_required

##### Module: HolidayRouter
- Sub Map: api_layer
- Source: api/routes/holidays.py
- Type: api_router
- State: `validated`
- Description: 僅供正式系統管理員操作的國定假日管理 API 路由。
- Dependencies: [DbService, AdminAuthorizationDependency]
- Input:
  - admin_principal: 由 require_system_admin 產生的可信 AdminPrincipal。
- Invariants:
  - GET、POST、DELETE 全部必須使用 require_system_admin；不得只保護寫入端點。
  - 缺少或錯誤 internal service key、失效 session、角色不足時必須由正式 dependency fail-closed。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_holiday_admin_route.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: ClientPaymentRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/client_payments.py
- Description: 提供 `/api/v1/client-payments` 的客戶收款與帳務摘要 API；退還補助款可查閱，解約退款功能不啟用。
- Invariants:
  - Payload 不接受任何月嫂帳務欄位。
  - 新增交易僅接受 deposit、first_payment、second_payment 階段；不得接受解約 refund。
  - 新增交易僅接受 receipt 與必要的 reversal；人工補登必須有非空原因。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_payment_routers.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0}
- Observability: not_required

##### Module: StaffPaymentRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/staff_payments.py
- Description: 提供 `/api/v1/staff-payments` 的月嫂應付與一次發薪實付 API。
- Invariants:
  - Payload 不接受任何客戶收款或退款欄位。
  - 人工補登付款交易必須有非空原因，且不得直接覆寫 staff_payments 摘要。
- Observability: not_required

##### Module: ContractContextRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/contracts.py
- Description: Read-only staff-service contract context by case_no and formal assignment.
- Complexity: medium
- Input:
  - case_no: canonical order identifier
  - assignment_id: optional formal assignment selector
- Output:
  - staff_contract_context: order, client, BeClass, selected assignment and staff facts
- Algorithm:
  - Read order and client contract facts by case_no, then BeClass by query_no = case_no.
  - Read formal case_staff_assignments; require assignment_id when more than one active assignment exists, and never infer the recipient from orders.staff_id.
  - Return null for approved-but-unmapped template fields; never write orders, templates, or payments.
- Invariants:
  - All reads use case_no and optional assignment_id; no orders.id or legacy payment view is used.
  - The endpoint is read-only and does not alter the original contract workbook.
  - Contract eligibility is read only from clients.identity_status; order facts must not select or return clients.identity_status.
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_contract_context_router.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: FinanceReportRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/finance_reports.py
- Description: Read-only accounts-payable and subsidy-reconciliation previews and XLSX downloads.
- Complexity: low
- Input:
  - target_month: YYYY-MM for accounts payable
  - reconciliation_period: year and optional quarter
- Output:
  - finance_reports: JSON previews and XLSX attachments
- Algorithm:
  - Delegate payable generation to AccountsPayableExport and reconciliation generation to SubsidyReconciliationRegister.
  - Return preview rows without workbook bytes in JSON endpoints; return workbook bytes only from explicit export endpoints.
  - Validate inputs at the API boundary and do not write payment, claim, refund, or order state.
- Invariants:
  - All endpoints are read-only.
  - 解約退款功能停用；但到期且未退還的 client subsidy-return 必須可在預覽與匯出中出現。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_finance_report_router.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: FinanceAlertRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/finance_alerts.py
- Dependencies: [FinanceAlertWorkflowService]
- Description: 提供財務警示清單、詳細資料、人工認領與解除端點；警示建立及正式交易修正不對 UI 開放。
- Complexity: medium
- Input:
  - filters: status、alert_code、source_domain 及分頁條件
  - workflow_action: alert_id、非空 operator reference 與 resolve reason
- Output:
  - alerts: 稽核清單、詳細快照與事件歷程
  - action_result: existing、claimed、resolved 或 conflict
- Invariants:
  - Router 只提供 list、detail、claim、resolve；不得提供任意 PATCH、任意事件建立或由 UI 建立警示的端點。
  - claim／resolve 必須委派 FinanceAlertWorkflowService，不得直接更新 finance_alerts、finance_alert_events 或任何正式帳務表。
  - conflict、not found 與 invalid transition 必須使用明確 HTTP 狀態；不得吞掉成成功。
  - resolve reason 與 operator reference 必須非空；本階段不在 B6 內另建 RBAC 或身份系統。
- Algorithm:
  - 驗證查詢與 action payload，將 list/detail/claim/resolve 委派給 FinanceAlertWorkflowService。
  - 回傳候選 snapshot、expected/actual/difference 與事件歷程；不推測候選或觸發正式核銷。
- Verification:
  - must_have_assertions
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_finance_alert_router.py", "-q", "-p", "no:cacheprovider", "--basetemp", "C:\\tmp\\pytest-b6-finance-alert-router"], "cwd": "project", "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: FinanceRouterRegistration
- Sub Map: api_layer
- Type: api_entrypoint
- State: `planned`
- Source: api/main.py
- Description: Register contract, finance-report, finance-alert and multi-caregiver schedule routers with the running FastAPI application；legacy payments router 不得再掛載。
- Dependencies: [MultiCaregiverScheduleRouter, MultiCaregiverScheduleReadRouter, MultiCaregiverCaseAssignmentListRouter]
- Complexity: low
- Invariants:
  - Register each new router exactly once without removing existing routers.
  - finance_alerts.router 必須只註冊一次；不得用另一個 FastAPI app 或重複 prefix 規避既有入口。
  - multi_caregiver_schedule.router 必須只註冊一次；不得建立另一個 FastAPI app、重複 prefix 或呼叫 legacy schedule router。
  - multi_caregiver_schedule_read.router 必須只註冊一次；不得建立另一個 FastAPI app、重複 prefix 或改以 legacy schedule router 提供查詢。
  - multi_caregiver_case_assignments.router 必須只註冊一次；不得建立另一個 FastAPI app、重複 prefix 或以 legacy 排班資料合成案件指派選單。
  - api.main 不得 import 或 include legacy payments.router；`/api/v1/payments` 必須從 OpenAPI 與執行中路由消失。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-c", "from pathlib import Path; s=Path('api/main.py').read_text(encoding='utf-8'); assert 'contracts.router' in s and 'finance_reports.router' in s and 'finance_alerts.router' in s and 'multi_caregiver_schedule.router' in s and 'multi_caregiver_schedule_read.router' in s and 'multi_caregiver_case_assignments.router' in s; assert s.count('app.include_router(finance_alerts.router)') == 1; assert s.count('app.include_router(multi_caregiver_schedule.router)') == 1; assert s.count('app.include_router(multi_caregiver_schedule_read.router)') == 1; assert s.count('app.include_router(multi_caregiver_case_assignments.router)') == 1; print('ADMIN ROUTERS REGISTERED')"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "ADMIN ROUTERS REGISTERED"}
- Observability: not_required
- Invariants:
  - INV-START-01: 腳本必須使用 Python 輪詢確認 MySQL 連線已可被接受，始可開始啟動後端與監控服務防止連線逾時崩潰。

##### Module: OrderSchemas
- Sub Map: api_layer
- Type: api_schema
- State: `planned`
- Source: api/schemas/orders.py
- Description: 訂單完整更新、狀態更新與排班試算的 API 請求資料模型；訂金應收日期可空，客戶身分資格不屬於可提交訂單欄位。
- Dependencies: []
- Invariants:
  - deposit_date 必須允許 null，且不得以今天或其他期款日期作為預設值。
  - 不得定義 clients.identity_status 或 identity_status 為可寫入的訂單 API 欄位。
- Observability: not_required

##### Module: MatchSchemas
- Sub Map: api_layer
- Type: api_schema
- State: `planned`
- Source: api/schemas/matches.py
- Description: 媒合回覆與服務人員指派的 API 請求資料模型。
- Dependencies: []
- Observability: not_required

##### Module: PaymentSchemas
- Sub Map: api_layer
- Type: api_schema
- State: `planned`
- Source: api/schemas/payments.py
- Description: legacy payments request schema 退役模組；不得再宣告 PaymentUpdateRequest、Pydantic model 或舊客戶/月嫂混合帳務欄位。
- Dependencies: []
- Invariants:
  - api/schemas/payments.py 不得包含 BaseModel、PaymentUpdateRequest、caregiver_fee、caregiver_paid_at 或三階段舊更新 payload。
- Verification: []
- Observability: not_required

##### Module: OrderScheduleCalculationRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/order_schedule_calculation.py
- Description: 出勤排班試算與順延完工日精算 API 路由。
- Dependencies: [OrderScheduleCalculationService, OrderSchemas]
- Complexity: low
- Input:
  - http_method: POST
  - path: /api/v1/orders/schedule-calculation
  - body: OrderScheduleCalculationRequest (含 start_date, service_days, custom_holiday_rest_dates, custom_leave_dates, custom_rest_weekdays 等)。
- Output:
  - response: BaseResponse[OrderScheduleCalculationResponse]。
- Idempotency:
  - 相同請求 body 試算回應一致結果，具備完全等冪性。
- Invariants:
  - Router 只能進行輸入驗證與 Service 委派，禁止直接執行 SQL 或修改排班。
  - 將 Service 領域錯誤精準映射至 HTTP 狀態碼 (404, 422, 500)。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests/test_order_schedule_calculation_service.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required


##### Module: AssignmentScheduleRestDateRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/assignment_schedule_rest_dates.py
- Description: 以 assignment_id 為專屬單元之月嫂排休與順延完工日更新 API 路由。
- Dependencies: [AssignmentScheduleRestDateService, OrderSchemas]
- Complexity: low
- Input:
  - http_method: PUT
  - path: /api/v1/assignment-schedules/{assignment_id}/rest-dates
  - assignment_id: 路徑參數 (int)。
  - body: AssignmentRestDatesUpdateRequest (含 rest_dates: list[str])。
  - auth_context: 認證權限依賴 (security dependency)。
- Output:
  - response: BaseResponse[Dict[str, Any]]。
- Idempotency:
  - 重複送出相同 assignment_id 與 rest_dates 具備等冪性，回應一致結果。
- Invariants:
  - Router 只能進行輸入驗證、認證/授權檢查與委派，禁止直接執行 SQL 或商業邏輯。
  - 必須將 Service 領域錯誤精準映射至 HTTP 狀態碼 (404, 409, 422, 500)。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests/test_assignment_rest_date_service.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required


##### Module: StaffMonthlyCalendarScheduleRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/staff_monthly_schedule.py
- Description: 月嫂月度檔期視圖 API 路由。
- Dependencies: [StaffMonthlyCalendarScheduleService]
- Complexity: low
- Input:
  - http_method: GET
  - path: /api/v1/staff/{staff_id}/monthly-schedule
  - staff_id: 路徑參數 (int)。
  - year: 查詢參數 (int)。
  - month: 查詢參數 (int, 1-12)。
- Output:
  - response: BaseResponse[Dict[str, Any]] (含 days 陣列與 schedule_map)。
- Idempotency:
  - 重複查詢結果一致，具備完全等冪性。
- Invariants:
  - 只能進行參數驗證與委派，不得直接寫 SQL 或商業邏輯。
  - 將 Service 領域錯誤精準映射為對應 HTTP 狀態碼 (404, 422, 500)。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests/test_staff_monthly_calendar_service.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required


##### Module: MatchRecordRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/match_records.py
- Description: 案件與月嫂媒合紀錄查詢與建立 API 路由。
- Dependencies: [MatchRecordIdempotentService, MatchSchemas]
- Complexity: low
- Input:
  - http_method: POST
  - path: /api/v1/match-records
  - body: MatchRecordCreateRequest (含 case_no, staff_id, response_type, notes)。
- Output:
  - response: BaseResponse[MatchRecordResponse]。
- Idempotency:
  - 相同 (case_no, staff_id) 重複發送具備等冪性，不拋出 500。
- Invariants:
  - Router 只能進行輸入驗證與 Service 委派，禁止直接執行 SQL 或商業邏輯。
  - 將 Service 領域錯誤精準映射至 HTTP 狀態碼 (404, 422, 500)。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests/test_match_record_service.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required


##### Module: DataBrowserAdminRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/data_browser_admin.py
- Description: 資料庫原始資料中繼權限查詢與單列微調稽核 API 路由 (須經認證/授權與 CP-1 批准始可掛載)。
- Dependencies: [DataBrowserAdminSchemaService, DataBrowserAdminAuditLogService, AdminAuthorizationDependency]
- Complexity: low
- Input:
  - http_method: GET / PATCH
  - path: /api/v1/admin/data-browser/{table} 或 /api/v1/admin/data-browser/{table}/{row_id_str}
  - table: 路徑參數 (str)。
  - row_id_str: 路徑參數 (str，支援整數識別碼與字串主鍵 case_no)。
  - body: DataBrowserPatchRequest (含 updates 字典)。
  - admin_principal: 由 require_system_admin 產生的可信 AdminPrincipal。
- Output:
  - response: BaseResponse[DataBrowserTableResponse] 或 BaseResponse[bool]。
- Idempotency:
  - 相同的 PATCH updates 請求重複送出具備等冪性。
- Invariants:
  - GET 與 PATCH 必須使用正式 require_system_admin，不得自行定義 Header 比對或僅依賴 URL prefix `/admin`。
  - 操作者 username 與 role 必須完全來自經驗證之 AdminPrincipal，嚴禁接受 UI body 或 X-Auth-Context 指定操作者。
  - 缺少或錯誤 internal service key、失效 session、角色不足時必須由正式 dependency 精準 fail-closed。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests/test_data_browser_admin_route.py", "tests/test_data_browser_admin_service.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required


##### Module: AdminAuthorizationDependency
- Sub Map: api_layer
- Type: api_dependency
- State: `planned`
- Source: api/dependencies/admin_auth.py
- Description: FastAPI 正式管理員授權依賴，統一驗證 internal service key、Bearer session 與最低角色。
- Dependencies: [AdminAuthService]
- Complexity: medium
- Input:
  - internal_api_key: X-Internal-API-Key header。
  - authorization: Authorization Bearer session token。
  - minimum_role: endpoint 所需最低角色。
- Output:
  - admin_principal: 經認證與角色授權的 AdminPrincipal。
- Algorithm:
  - 先要求 X-Internal-API-Key，設定缺失回 503，請求缺失或不符回 401。
  - 僅在明確 development bypass 條件成立時建立 development system_admin principal；其餘情況解析 Bearer token。
  - 將 token 委派 AdminAuthService 查詢有效 session，無有效 principal 回 401。
  - 依 endpoint 最低角色比對 principal.role，權限不足回 403；成功後把 principal 寫入 request.state 並回傳。
- Invariants:
  - INTERNAL_API_KEY 未設定回 503；缺少或錯誤 internal key 回 401。
  - 正式模式缺少、失效或過期 Bearer session 必須回 401；角色不足必須回 403。
  - 只有 development、dev、local、test 且 ENABLE_ADMIN_AUTH 明確為 false 時才允許 session bypass；internal service key 永遠不可 bypass。
  - 成功後必須把 principal 寫入 request.state，供統一 audit middleware 使用。
- Verification:
  - command: {"argv": [".venv\\Scripts\\python.exe", "-m", "pytest", "tests\\test_admin_auth_security.py", "-q"], "cwd": "project", "timeout": 60, "expect_exit": 0, "expect_stdout_contains": "passed"}
- Observability: not_required

##### Module: AdminAuthRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/admin_auth.py
- Description: 管理後台登入、目前身分、session 續期與登出 API 路由。
- Dependencies: [AdminAuthorizationDependency, AdminAuthSchemas, AdminAuthService]
- Complexity: low
- Observability: not_required

##### Module: LineAdminRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/line_admin.py
- Description: LINE 管理中心健康狀態、Worker 狀態與管理功能清單 API 路由。
- Dependencies: [AdminAuthorizationDependency, DbService]
- Complexity: low
- Observability: not_required

##### Module: LineTaskAdminRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/line_tasks.py
- Description: LINE 發送任務的查詢、立即執行、取消與失敗重送 API 路由。
- Dependencies: [AdminAuthorizationDependency, LineTaskSchemas, LineTaskAdminService]
- Complexity: low
- Observability: not_required

##### Module: LineRichMenuRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/line_rich_menus.py
- Description: LINE 下方選單圖片上傳、預覽、發布、發布紀錄與失敗重試 API 路由。
- Dependencies: [AdminAuthorizationDependency, LineConfigSchemas, LineRichMenuSchemas, JsonConfigService, LineRichMenuService, MediaStorageService]
- Complexity: low
- Observability: not_required

##### Module: LineReviewRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/line_reviews.py
- Description: LINE 月嫂身分申請與客戶帳號重新綁定的人工確認 API 路由。
- Dependencies: [AdminAuthorizationDependency, LineReviewSchemas, AdminAuthService, LineReviewService]
- Complexity: low
- Observability: not_required

##### Module: LineSystemConfigRouter
- Sub Map: api_layer
- Type: api_router
- State: `planned`
- Source: api/routes/line_system_config.py
- Description: LINE 訊息範本、排程、下方選單、LIFF 與客服設定的管理及公開讀取 API 路由。
- Dependencies: [AdminAuthorizationDependency, LineConfigSchemas, JsonConfigService, LineRichMenuService, LineLiffConfigService]
- Complexity: low
- Observability: not_required

##### Module: AdminAuthSchemas
- Sub Map: api_layer
- Type: api_schema
- State: `planned`
- Source: api/schemas/admin_auth.py
- Description: 管理後台登入、公開身分與 session 回應資料模型。
- Dependencies: []
- Complexity: low
- Observability: not_required

##### Module: LineConfigSchemas
- Sub Map: api_layer
- Type: api_schema
- State: `planned`
- Source: api/schemas/line_config.py
- Description: LINE 訊息範本、排程、下方選單、LIFF 與客服設定的資料模型及欄位驗證。
- Dependencies: []
- Complexity: low
- Observability: not_required

##### Module: LineReviewSchemas
- Sub Map: api_layer
- Type: api_schema
- State: `planned`
- Source: api/schemas/line_reviews.py
- Description: LINE 人工確認核准與拒絕操作的輸入資料模型。
- Dependencies: []
- Complexity: low
- Observability: not_required

##### Module: LineRichMenuSchemas
- Sub Map: api_layer
- Type: api_schema
- State: `planned`
- Source: api/schemas/line_rich_menus.py
- Description: LINE 下方選單發布與重試操作的輸入資料模型。
- Dependencies: []
- Complexity: low
- Observability: not_required

##### Module: LineTaskSchemas
- Sub Map: api_layer
- Type: api_schema
- State: `planned`
- Source: api/schemas/line_tasks.py
- Description: LINE 發送任務取消、立即執行與重送操作的輸入資料模型。
- Dependencies: []
- Complexity: low
- Observability: not_required
