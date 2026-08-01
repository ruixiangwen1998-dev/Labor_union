# API 與 Server 共用整頓計畫

## 文件目的

本文件記錄 Labor_union 管理端 API、Server 與資料查詢能力的後續整頓方向。

本計畫不屬於目前「多月嫂排班 UX 改善」Task 24 的直接施工範圍。後續必須先完成 ADAD 架構節點、依賴分析與人類 Checkpoint-1，才能修改正式程式碼。

核心目標：

1. 最大化既有 FastAPI Router、Service、query engine 與 DB infrastructure 的復用。
2. 將可安全參數化的篩選、排序與分頁收斂為共用能力。
3. 避免為不同頁面或 API 重複建立相同查詢與 transaction 邏輯。
4. 保留正式業務 API 的強型別契約、權限、ownership 與 transaction boundary。
5. 禁止把正式業務操作退化為可任意指定 table、column 或 SQL 的通用 CRUD。

---

## 已確認原則

### 1. 同一 Server 可以服務不同 API

不同 API 可以共用：

- Database connection／cursor lifecycle
- Read-only session
- Filter normalization
- Date-range validation
- Pagination
- Stable ordering
- Result serialization
- Response envelope
- Admin authentication infrastructure
- Case／assignment ownership resolver
- Availability engine
- Assignment-plan validation engine
- Schedule read／generation engine
- Assignment payroll reconciliation engine

不同 API 仍須保留各自的 request／response schema，不得全部改成 `Dict[str, Any]`。

### 2. 簡單篩選使用 GET query parameters

適用情境：

- 單一狀態
- `case_no`
- `assignment_id`
- `staff_id`
- 單一日期或簡單日期區間
- 搜尋字串
- 分頁

範例：

```http
GET /api/v1/orders?status=服務中&staff_id=12&page=1&page_size=50
```

### 3. 複雜篩選使用 POST `/search`

適用情境：

- 多個服務區段
- 多個 staff IDs
- 多組狀態
- 複合日期範圍
- Availability／lock 條件
- Partial／complete combinations
- 多條件排序與分頁

範例：

```http
POST /api/v1/caregiver-segment-availability/search
```

```json
{
  "case_no": "115000001",
  "segments": [
    {
      "start_date": "2026-08-01",
      "end_date": "2026-08-15"
    },
    {
      "start_date": "2026-08-16",
      "end_date": "2026-08-31"
    }
  ],
  "staff_ids": [10, 20, 30],
  "include_partial_matches": true,
  "page": 1,
  "page_size": 50,
  "sort": "availability"
}
```

### 4. API 傳業務參數，不傳任意 SQL identifiers

可以由 client 傳入：

- `case_no`
- `assignment_id`
- `staff_id`／`staff_ids`
- 日期與日期區間
- 預先定義的狀態 enum
- 搜尋字串
- 布林篩選條件
- 頁碼與 page size
- Server registry 已定義的 sort key
- Server registry 已定義的 resource key

只能由 Server-side registry 決定：

- 實體 table／view
- Column
- Primary key
- Projection
- JOIN
- 實際 `ORDER BY` 欄位與方向
- Editable fields
- Row-level authorization
- 是否允許 `FOR UPDATE`
- 是否允許寫入
- Transaction ownership
- Audit strategy

絕不可直接接受 client 傳入：

- 任意 table name
- 任意 column list
- 任意 SQL
- 任意 WHERE expression
- 任意 JOIN
- 任意 aggregate expression
- 任意 ORDER BY expression
- 任意 update expression
- 任意 stored procedure、schema 或 database 名稱
- 任意 cursor／transaction 選項

---

## 目標架構

```text
Client API Request
    ↓
Pydantic Request Model
    ↓
Router Authentication and Authorization
    ↓
Business Resource / Operation Key
    ↓
Server-side Resource Registry
    ↓
Domain Adapter or Existing Service
    ↓
Fixed SQL Fragments with Bound Values
    ↓
MySQL
```

禁止架構：

```text
Client table + columns + where + SQL
    ↓
Generic CRUD
    ↓
Formal business tables
```

### Resource Registry 建議內容

```python
@dataclass(frozen=True)
class ResourceAdapter:
    resource: str
    read_role: str
    write_role: str | None
    request_model: type[BaseModel]
    response_model: type[BaseModel]
    reader: Callable
    writer: Callable | None
    transaction_mode: Literal[
        "read_only",
        "service_owned",
        "caller_owned",
    ]
```

Registry 必須由 Server 靜態建立：

```python
RESOURCE_REGISTRY = {
    "case_assignments": ResourceAdapter(
        resource="case_assignments",
        read_role="admin_viewer",
        write_role=None,
        reader=list_case_schedule_assignments,
        transaction_mode="read_only",
        request_model=CaseAssignmentSearchRequest,
        response_model=CaseAssignmentSearchResponse,
    ),
    "assignment_schedule": ResourceAdapter(
        resource="assignment_schedule",
        read_role="admin_viewer",
        write_role="admin_manager",
        reader=get_assignment_schedule,
        writer=adjust_assignment_schedule_day,
        transaction_mode="service_owned",
        request_model=AssignmentScheduleRequest,
        response_model=AssignmentScheduleResponse,
    ),
}
```

必要限制：

1. Resource 使用 enum 或 `Literal`，不接受任意 table name。
2. Adapter 固定綁定既有 Service，不由 request 決定函式或 SQL。
3. Filters 由各自 Pydantic model 驗證。
4. Table、column 與 projection 只來自 Server registry。
5. 所有資料值使用 DB parameter binding。
6. 每個 write adapter 明確宣告 transaction ownership。
7. 跨表操作必須呼叫正式 domain orchestration Service。
8. Registry 不得成為可由 API 任意串接 internal helpers 的 service locator。

---

## 現況盤點

### Data Browser

目前正式註冊：

- `api/routes/data_browser_admin.py`
- 具有 `require_system_admin`
- 使用 Server-side table allowlist
- 財務、排班與 assignment 等高風險表維持 read-only
- 正式更新寫入 audit

目前重複候選：

- `api/routes/data_browser.py`
- 與正式 Router 使用相同 `/api/v1/admin/data-browser` prefix
- 沒有相同的 system-admin dependency
- 目前未由 `api.main` 註冊

後續方向：

1. 保留有 system-admin 認證與 audit 的正式 Router。
2. 將未註冊舊 Router 列為退役候選。
3. 新增重複 `path + method` 啟動檢查或測試。
4. Data Browser 僅作 system-admin 管理工具，不作正式業務寫入入口。

### Registry 漂移

目前 table／primary-key／editable-column 規則分散於：

- `services/data_browser_admin_schema_service.py`
- `services/data_browser_admin_service.py`
- `services/db_service.py`

已觀察到不同 allowlist、editable columns 與 read-only 定義可能不一致。

後續方向：

1. 建立單一 `DataBrowserResourceRegistry`。
2. Registry 同時擁有：
   - resource key
   - physical table／view
   - primary key
   - projection
   - allowed filters
   - allowed sort keys
   - editable columns
   - read/write role
   - audit policy
3. Generic executor 必須再次驗證 registry，不只依賴 Router 或上層 Service。
4. 避免廣泛使用 `SELECT *`，改由 registry 或 domain query 定義 projection。
5. 增加 bounded pagination 與 stable ordering。

### Generic update 風險

`services/db_service.py::update_table_row` 會動態組合 table 與 column identifiers。

目前正式 Data Browser 呼叫鏈有上層 allowlist，因此不是已確認的直接漏洞；但底層函式本身不能視為安全的通用 public write API。

後續方向：

1. Generic executor 自身必須 fail-closed 驗證 table、primary key 與 editable columns。
2. 禁止其他 Router 直接將 client updates 傳入該函式。
3. 正式業務表的更新必須經 domain Service。

---

## 多月嫂 UX 可共用能力

### 適合共用 read/query infrastructure

- 案件 assignment 清單
- Assignment schedule read
- Staff monthly calendar
- Segment availability
- Matching-plan read
- 休假／代班事件摘要
- Assignment payroll reconciliation read
- UI capability flags

### 適合共用 assignment-plan engine

- 分頁一：順延／代班 preview
- 分頁三：區段新增、移除、換人、改期 preview
- 對應 Apply 的 rules validation
- Gap／overlap
- 歷史 ownership 保護
- 最多四個有效 assignments

對外 API 必須維持不同強型別 request：

```text
LeaveSubstitutionPreviewRequest
StaffingPlanPreviewRequest
```

兩者可正規化為同一個內部 command：

```text
AssignmentPlanCommand
```

不得對外暴露萬用 `operation_kind + Dict[str, Any]` 契約。

### 不可通用 CRUD 化

- Assignment transition
- 日期鎖 acquisition／release／cancellation／conversion
- 休假、順延與代班套用
- Matching-plan version 建立
- 訂單取消
- Staff payment 建立
- Payroll reconciliation 寫入門禁
- 月結與付款交易
- Append-only events

這些可以共用 mutex、event writer、query helper、reconciliation 與 transaction helper，但必須保留各自的 domain Service 與 API contract。

---

## 認證與授權整頓

### 現況風險

目前未確認 `api.main` 存在涵蓋全部管理端業務 API 的全域管理員認證 middleware。

已觀察到部分 Orders、assignment schedule、rest-date、matching 與 staff-payment Router 沒有一致的 `Depends(require_...)` 宣告。

CORS 不是身分驗證，不能作為 API authorization。

### 建議方向

1. 管理端 read Router 統一加入 `admin_viewer` dependency。
2. 管理端 write Router 統一加入 `admin_manager` 或更精確 capability。
3. Data Browser 維持 `system_admin`。
4. 優先以 Router-level dependency 套用共通認證，不依賴每個 endpoint 個別記得加入。
5. 建立測試列舉所有管理端 operation，確認 read／write／system-admin 權限分類。
6. 保留既有正式 header 契約，不以 CORS 或 UI 隱藏取代認證。

此項屬跨 API 架構整頓，不得未經獨立 ADAD 節點與 Checkpoint 混入多月嫂 UI Task。

---

## 建議後續 ADAD 節點

以下只是 backlog 建議，尚未建立正式節點：

### 1. DataBrowserResourceRegistry

目標：

- 收斂 table、primary key、projection、editable fields、read-only 與 audit 規則。
- Generic executor 內層再次 fail-closed。

### 2. LegacyDataBrowserRouterRetirement

目標：

- 退役未註冊且缺少 system-admin dependency 的舊 Data Browser Router。
- 防止相同 `path + method` 被重複註冊。

### 3. AdminAPIAuthorizationBoundary

目標：

- 統一管理端 Router 的 viewer／manager／system-admin dependency。
- 建立完整 operation inventory 與認證測試。

### 4. SharedReadQueryInfrastructure

目標：

- 收斂 pagination、filter normalization、stable ordering 與 deterministic serialization。
- Domain Service 仍負責固定 projection、JOIN、ownership 與 response schema。

### 5. AssignmentPlanCommandNormalization

目標：

- 將不同強型別 API request 正規化成共用 assignment-plan command。
- Preview／Apply 共用現有 assignment rules，不重複實作 gap、overlap、歷史與四段上限。

---

## 驗收原則

每個正式整頓節點至少需證明：

1. Client 無法傳入任意 table、column、JOIN、WHERE 或 SQL。
2. 所有資料值使用 parameter binding。
3. Registry 未登記 resource、filter 或 sort key 時 fail-closed。
4. Page size 有明確上限。
5. Stable ordering 可重現。
6. Read／write／system-admin 權限分類完整。
7. Generic executor 與上層 Service 都執行 allowlist 防護。
8. 正式業務寫入仍經 domain Service。
9. 跨表 operation 保留既有 transaction、mutex、idempotency 與 audit/event-last。
10. 不新增第二套 assignment、schedule、availability、lock、payroll 或 payment engine。
11. 既有 API 相容性有 focused tests。
12. 未註冊舊 Router 不可被誤掛載。

---

## 目前決策

1. Task 24 暫不施工。
2. 先以既有 API、Router 與 Service 最大復用原則重排 Task 24–35。
3. 本整頓計畫獨立於多月嫂 UX 主線，除非某項是主線的直接阻塞，不得偷偷擴入目前 Task。
4. 後續若正式啟動本計畫，必須先讀取最新 SSOT、API map、Service map、實際 server registration、Task snapshots 與工作區狀態。
5. 不得只依本文件直接修改程式碼；本文件是規劃依據，不是 Checkpoint 授權。
