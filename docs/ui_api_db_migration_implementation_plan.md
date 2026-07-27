# UI API／DB 串接實作計畫

- 建立日期：2026-07-24
- 狀態：Phase 1 規劃草案，尚未通過人工 Checkpoint 1
- 範圍：Data Browser、訂單與媒合、訂單編輯器、行事曆、表單管理
- 架構原則：Streamlit UI → FastAPI Router → Domain Service → DB

## 1. 目標

將下列 UI 的正式 DB 讀寫改為經由 FastAPI：

- `ui/pages/01_data_browser.py`
- `ui/pages/02_orders.py`
- `ui/pages/order/editor.py`（取代已不存在的 `04_edit_order.py`）
- `ui/pages/03_calendar.py`
- `ui/pages/05_form_management.py`

保留原則：

- 純 UI 計算及格式化不需要 API。
- JSON／Excel 表單模板暫時維持 file-backed。
- 帳務交易繼續使用正式 client/staff payment API。
- 多月嫂排班以 `case_staff_assignments.id`／`assignment_id` 為唯一 ownership。
- 禁止重新依賴 legacy `orders.staff_id` 作為正式指派來源。
- API 失敗時，UI 不得偷偷退回直接呼叫 `DbService`。

## 2. Phase 0：修復 ADAD 架構狀態

目前 `ui/ui_system_map.yaml` 比根 `system_map.yaml` 新。修改任何架構契約前，必須先恢復同步：

```powershell
.venv\Scripts\python.exe .agents\skills\adad-workflow\scripts\compile_map.py
.venv\Scripts\python.exe .agents\skills\adad-workflow\scripts\check_source_binding.py
.venv\Scripts\python.exe .agents\skills\adad-workflow\scripts\check_domain_boundary.py
.venv\Scripts\python.exe .agents\skills\adad-workflow\scripts\resume_analysis.py
```

接著檢查：

- 本次涉及節點的既有 Task 是否 stale。
- `.agents/tasks/.source_locks` 是否鎖住相關來源檔。
- Task index 是否與重新編譯後的 map 一致。
- 工作區既有 `fixtures/db_snapshot_v2/v3/` 刪除不納入本次修改。

完成條件：

- `system_map.yaml` 編譯成功。
- Schema、source binding、domain boundary 全部通過。
- `read_context.py` 能正常讀取本次節點。
- 尚不自行核准任何 Checkpoint。

## 3. Phase 1：system_map 架構契約

### 3.1 既有 API 遷移節點

#### Page2TabNavigation

目標：

- 訂單改用 `GET /api/v1/orders`。
- 月嫂名單改用 `GET /api/v1/staff`。
- 移除目前未使用的全量 clients 載入。

新增不變量：

- UI 不得匯入或呼叫 `db_service`。
- 初始資料只透過既有 Router 載入。
- API 失敗時不可退回直連 DB。

#### FormManagementUI

目標：

- 訂單改用 `GET /api/v1/orders`。
- 客戶改用 `GET /api/v1/clients`。
- 月嫂契約維持 `GET /api/v1/contracts/staff/{case_no}`。

新增不變量：

- DB 資料不得由 UI 直接讀取。
- JSON／Excel 模板維持 file-backed。
- 本節點不修改模板儲存契約。

#### CalendarUI

目標：

- staff 改用 `GET /api/v1/staff`。
- orders 改用 `GET /api/v1/orders`。
- holidays 改用 `GET /api/v1/holidays`。
- 多月嫂區塊保留目前既有 assignment-aware API。

新增不變量：

- 不得從 `orders.staff_id` 推測正式指派。
- API 失敗不得退回 legacy DbService。
- 正式排班只能認 `assignment_id`。

#### OrderUI_Tab2_Assign

改接既有 API：

```text
GET  /api/v1/matches/recommend-staff
POST /api/v1/matches/{match_id}/send-info-1
POST /api/v1/matches/{match_id}/send-info-2
PUT  /api/v1/matches/{match_id}/reply
POST /api/v1/matches/{match_id}/send-resume
PUT  /api/v1/orders/{case_no}/status
```

新增不變量：

- UI 不得直接建立或更新 `matching_records`。
- 正式指派不得呼叫 legacy `/assign-staff`。
- 最終指派必須進入 assignment synchronization preview/apply。

### 3.2 新增 API 節點

#### MatchRecordListService／MatchRecordListRouter

```text
GET /api/v1/orders/{case_no}/matches
```

職責：

- 回傳指定案件的媒合紀錄。
- JOIN staff 提供 UI 需要的月嫂基本資料。
- 保留目前有效媒合紀錄篩選規則。
- Router 不直接寫 SQL。

#### MatchRecordCreateService／MatchRecordCreateRouter

```text
POST /api/v1/orders/{case_no}/matches
```

Payload：

```json
{
  "staff_id": 123
}
```

契約：

- 採 idempotent create-or-get。
- 相同 `case_no + staff_id` 重複請求回傳同一筆紀錄。
- 不因 UI 單純選取月嫂就預先建立紀錄。

#### DataBrowserAdminSchemas／Service／Router

```text
GET   /api/v1/admin/data-browser/{table}
PATCH /api/v1/admin/data-browser/{table}/{row_id}
```

GET 回傳：

```json
{
  "rows": [],
  "columns": [],
  "primary_key": "id",
  "editable_columns": [],
  "valid_options": {},
  "read_only": false
}
```

必要不變量：

- table name 由後端白名單控制。
- PATCH 欄位由後端白名單控制。
- 主鍵及系統欄位禁止修改。
- legacy `payments` 必須 fail-closed。
- read-only tables 不得 PATCH。
- 加入管理權限與異動稽核。
- holidays 寫入繼續使用既有 Holiday API。
- Router 不得直接公開泛用 `DbService.update_table_row()`。

#### StaffMonthlyCalendarService／Router

```text
GET /api/v1/staff/{staff_id}/monthly-schedule?year=2026&month=7
```

輸出：

```json
{
  "staff_id": 123,
  "year": 2026,
  "month": 7,
  "days": [
    {
      "work_date": "2026-07-01",
      "status": "working",
      "assignment_id": 456,
      "case_no": "C001",
      "client_name": "王小明",
      "is_double_pay": false,
      "notes": null
    }
  ]
}
```

必要不變量：

- 以 `assignment_id` 為排班 ownership。
- 不以 `orders.staff_id` 補推正式指派。
- 不得混入其他 assignment 的排班。
- legacy 無 `assignment_id` 資料必須明確標示或排除。

#### OrderScheduleCalculationContract

擴充既有：

```text
POST /api/v1/orders/calculate-schedule
```

新增 optional input：

```text
custom_leave_dates
custom_rest_weekdays
monthly_salary_base
```

要求：

- 保持既有 request 向後相容。
- Router 完整轉傳所有參數。
- 相同 input 下，API 結果須與目前 UI 直接呼叫 service 的結果一致。

#### OrderRestDateUpdateService／Router

候選 API：

```text
PUT /api/v1/orders/{case_no}/rest-dates
```

多月嫂較安全的候選形式：

```text
PUT /api/v1/assignment-schedules/{assignment_id}/rest-dates
```

Checkpoint 1 前必須決定採訂單層或 assignment 層，不能同時建立兩套來源。設計必須保證：

- 不刪除其他 assignment 的 `staff_schedule`。
- `custom_rest_dates`、實際結束日及日排班在同一 transaction 更新。
- 單月嫂 legacy 案件與正式多月嫂案件有明確邊界。
- 不直接把現有 `save_order_rest_dates()` 裸包成 API。

## 4. EditOrderUI 決策

現行 editor 已使用正式指派 API，保留：

```text
GET  /api/v1/cases/{case_no}/assignment-schedules
GET  /api/v1/staff
POST /api/v1/orders/{case_no}/assignment-synchronization/preview
POST /api/v1/orders/{case_no}/assignment-synchronization/apply
```

修改：

- client payment 資訊用 `GET /api/v1/client-payments/{case_no}` 顯示。
- 三期帳務欄位改成唯讀。
- 取消訂單使用 `PUT /api/v1/orders/{case_no}/status`。
- 不允許 editor 直接修改實收或付款交易。
- 不新增 summary PATCH。

暫不新增：

```text
PATCH /api/v1/client-payments/{case_no}/due-dates
```

只有業務明確確認訂單編輯頁需要人工修改應收日期時，才另行提出架構契約。

## 5. Phase 2：原子施工順序

每個項目必須分別完成 CP-1、Task、實作、驗證與 CP-2：

1. `Page2TabNavigation`
2. `FormManagementUI`
3. `CalendarUI` 既有 API 讀取遷移
4. `OrderUI_Tab2_Assign` 既有 API 操作遷移
5. `MatchRecordListService`
6. `MatchRecordListRouter`
7. `MatchRecordCreateService`
8. `MatchRecordCreateRouter`
9. `DataBrowserAdminSchemas`
10. `DataBrowserAdminService`
11. `DataBrowserAdminRouter`
12. `DataBrowserUI`
13. `StaffMonthlyCalendarService`
14. `StaffMonthlyCalendarRouter`
15. `CalendarUI` 月份檔期遷移
16. `OrderScheduleCalculationContract`
17. `CalendarUI` 出勤試算遷移
18. `OrderRestDateUpdateService`
19. `OrderRestDateUpdateRouter`
20. `CalendarUI` 休假保存遷移
21. `EditOrderUI` 帳務欄位唯讀修正

單節點流程：

```text
修改 system_map.md
→ compile_map.py
→ 人工 Checkpoint 1
→ generate_task.py
→ 單節點實作
→ lint / py_compile / pytest
→ submit Task
→ 人工 Checkpoint 2
→ 下一節點
```

## 6. 明確排除範圍

本次不處理：

- JSON／Excel 表單模板 DB 化。
- legacy `payments` API 復活。
- 舊 `orders.staff_id` 指派流程。
- 直接包裝 `assign_staff_to_order()`。
- 直接把 `save_order_rest_dates()` 暴露成 API。
- UI 在 API 失敗時退回 DbService。
- 未經人工 Checkpoint 自動把節點推進為 validated 或 deployed。

## 7. 驗證計畫

### 7.1 靜態驗證

- 目標 UI 不再 import `services.db_service`。
- 目標 UI 不再呼叫正式 DB helper。
- API router 全部只註冊一次。
- legacy `/api/v1/payments` 不得重新出現在 OpenAPI。

### 7.2 API 測試

每支新 API 至少驗證：

- 正常回應。
- 不存在的 case、staff 或 assignment。
- 不合法 table name。
- 不合法 PATCH 欄位。
- read-only table 寫入。
- 重複建立媒合紀錄的 idempotency。
- assignment 資料隔離。
- transaction rollback。

### 7.3 UI 測試

- API 成功時正確顯示。
- API 4xx/5xx 顯示錯誤且不直連 DB。
- 空資料不崩潰。
- 多月嫂案件不混入其他 assignment。
- editor 帳務欄位不再呈現「可改但不保存」。

### 7.4 回歸測試

- Page 2 五個 Tab。
- 配對推薦、發送及回覆。
- client/staff payment。
- accounts payable。
- subsidy reconciliation。
- Calendar 多月嫂讀取、產生及單日調整。
- Form contract context。
- Data Browser holidays CRUD。

## 8. 完成定義

完成後，正式資料流統一為：

```text
Streamlit UI
    ↓ HTTP
FastAPI Router
    ↓
Domain Service
    ↓
DB
```

UI 只保留：

- 表單輸入與顯示。
- 本地篩選及格式化。
- 純函式試算顯示。
- JSON／Excel 模板管理。

所有正式 DB 讀寫、權限、欄位白名單、交易原子性與 assignment ownership 集中在 API／service 層。
