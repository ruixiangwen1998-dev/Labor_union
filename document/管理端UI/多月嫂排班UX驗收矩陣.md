# 多月嫂排班 UX 驗收矩陣

權威來源：

1. `多月嫂排班UX目標指南.md`
2. `多月嫂排班UX改善討論紀錄.md`

狀態定義：`通過`＝已有 live code 與相稱的 DB／Service／API／UI 測試；`不適用`＝本次直接實作對照實驗明確排除。

| 驗收項 | 規格要求 | Live 實作與測試證據 | 狀態 |
|---|---|---|---|
| AC-001.1 | 僅保留三個產品分頁 | `ui/pages/03_calendar.py::show`；`test_scheduling_page_owns_exact_three_product_tabs` | 通過 |
| AC-001.2 | 單月、上下月、本月、年月直選且保留月嫂 | `_render_staff_calendar` 的獨立 view year/month state；UI shell tests | 通過 |
| AC-001.3 | 同日多筆、待成立與正式服務分色 | 月度 Service `days` 保留每筆 assignment/lock；月曆逐筆顯示客戶、狀態、月嫂；calendar tests | 通過 |
| AC-001.4 | 舊訂單頁不保留配對／正式配置入口 | `ui/pages/02_orders.py` 固定四個訂單/帳務分頁；`test_order_ui_shell_ownership.py` | 通過 |
| AC-001.5 | 未套用休假草稿切月前可放棄或留頁 | `_calendar_has_unsaved_leave_changes`、`calendar_pending_month` 二次選擇；UI shell test | 通過 |
| AC-002.1 | 單人完整可行優先；否則 2/3/4 段且預設 2 | `matching_center.py` 先查 1 段，無完整人選才呈現 `[2,3,4]`；availability tests | 通過 |
| AC-002.2 | 草稿可暫時空缺／重疊／超界，候選依日期與前段更新 | UI 本機重篩 cached segment candidates、排除已選月嫂；Service 回報 draft diagnostics 而不在編輯時拒絕 | 通過 |
| AC-002.3 | 無完整組合仍顯示部分人力、未覆蓋日期與原因 | availability `partial`／`segment_candidates`／`conflicts`；UI 與 Service tests | 通過 |
| AC-002.4 | 聯繫與履歷分離，發送前重查最新檔期 | communication Service 對 info-1/2 發送前重跑完整 availability；各動作獨立 API/UI | 通過 |
| AC-002.5 | 多人未全員願意不得送履歷 | `send_matching_plan_resumes` 在任何 write 前執行 all-willing gate；Service/API tests | 通過 |
| AC-002.6 | 單人可補寄；多人只能全員同意後整批逐位寄送 | 每段衍生穩定 idempotency key；單段/多段使用同一可重播流程，多段保留全員 gate | 通過 |
| AC-002.7 | 共用備註；多人內容揭露共同完成 | 單一 `resume_note`；Service 自動補入「由多位月嫂共同完成」；communication tests | 通過 |
| AC-002.8 | 正式送出前驗證唯一月嫂、連續、完整覆蓋、無縫無重疊 | matching-plan Service 僅接受 latest complete combination；plan/service/router tests | 通過 |
| AC-003.1 | 分頁二、三共用 assignment-aware 規則 | matching conversion 與 staffing preview/apply 共用 `MultiCaregiverAssignmentRules`／assignment transition；focused rules tests | 通過 |
| AC-003.2 | 正式配置 1–4 行、一行一 assignment、預設現有數量 | `case_staffing.py` 與 assignment read API；UI/service tests | 通過 |
| AC-003.3 | 減段只列取消候選，確認 Preview 後才 Apply | staffing UI 的取消候選、preview/fingerprint/apply；synchronization tests | 通過 |
| AC-003.4 | 日調整與區段調整分工，最多四個 active assignment | calendar batch leave 與 staffing 分離；domain row-limit protection | 通過 |
| AC-003.5 | 歷史服務日不可改寫正式歸屬／工時／薪資 | preview/apply 以 DB current date 與 locked facts 阻擋；rules/synchronization tests | 通過 |
| AC-003.6 | 已開始區段只允許未來生效切段 | assignment transition 以 effective date 截斷舊段並保留 D-1；tests | 通過 |
| AC-003.7 | 付款、月結、人工時數鎖阻擋改寫；取消草稿零寫入 | synchronization/leave locked-facts 與 transaction tests | 通過 |
| AC-004.1 | assignment 明細完整列出前後日期與各類天數／時數 | `multi_caregiver_schedule_read.py` 與月曆精算表；read/UI tests | 通過 |
| AC-004.2 | 全案前後範圍、目標量、實際量與缺口 | read Service `summary` 與 UI metrics；read tests | 通過 |
| AC-004.3 | 缺口、重疊、超界可定位並重算 | summary diagnostics 連回 assignment；read/rules tests | 通過 |
| AC-005.1 | 多休假日逐列選順延／代班，代班必填人員 | calendar UI 與 strict API schemas；route/UI tests | 通過 |
| AC-005.2 | Preview 重算後段並重查正式與等待訂金占用 | locked snapshot + pure transition + fresh authorization preview；batch tests | 通過 |
| AC-005.3 | 無人代班採順延補足且不提前結束 | defer mutation projection 依精確缺日向後補足；batch tests | 通過 |
| AC-005.4 | 候選先同案既有月嫂，再列外部月嫂 | calendar UI 分組排序；UI shell/source tests | 通過 |
| AC-005.5 | lineage 保存原 assignment、日期、代班、操作者與時間 | append-only per-day event schema + payload；schema/apply tests | 通過 |
| AC-005.6 | 多日期一次 Preview/fingerprint/Apply，全成或全敗 | batch header、locked-facts authorization、單一 DB transaction；batch apply tests | 通過 |
| AC-005.7 | 每日獨立事件以穩定 batch_key 關聯且重播不重複 | batch event uniqueness/replay validation；schema/apply tests | 通過 |
| AC-006.1 | 順延限同案並保持 assignment 邊界、後段重排 | pure transition/mutation executor；batch transition tests | 通過 |
| AC-006.2 | 代班建立獨立 assignment，原/代班同日不重複 | substitute mutation + schedule invariant；integration tests | 通過 |
| AC-006.3 | assignment 與 service_end_date 不跨案混淆 | case/assignment/schedule ownership validation；read/rules tests | 通過 |
| AC-007.1 | 代班及國定假日雙倍薪預設 false，假日不得自動加倍；個別約定可由 UI 對指定排班日明確覆寫 | strict batch item `is_double_pay`、checkbox 與 executor；batch/UI tests | 通過 |
| AC-007.2 | 客戶付款不變；代班增加與原段減少抵銷 | payroll reconciliation 不寫 client payment；reconciliation tests | 通過 |
| AC-007.3 | 樓層費按代班天數分配且全案守恆 | assignment payroll reconciliation；payment tests | 通過 |
| AC-007.4 | 每次調整 Preview 重算 actual_hours；不守恆時阻擋 Apply，薪資建立／確認再次阻擋 | synchronization、conversion、payroll reconciliation tests | 通過 |
| AC-008.1 | 鎖定、回復、取消釋放、轉正式均為單 API/交易 | lock acquisition/release/cancellation/conversion Services 與固定 routes；transaction tests | 通過 |
| AC-008.2 | 鎖定衝突回報月嫂/日期且溝通歷史保留 | normalized conflict rows；events append-only；lock tests | 通過 |
| AC-008.3 | 訂金確認後再次驗證 lock、plan、日期、最新占用與本案歸屬 | conversion 在 staff mutex 下鎖讀 ledger、plan、segments、lock/days、assignments/schedules；tests | 通過 |
| AC-008.4 | 回復只限洽談中且零訂金，UI 二次確認並保留歷史 | release Service fail-closed ledger validation；matching UI；release tests | 通過 |
| AC-008.5 | 訂金後禁止回復；轉正式原子建立 assignments/schedules 並成立訂單 | payment writer 遇 active lock 不先成立；conversion 同 transaction 建檔、解除 lock、更新訂單；lifecycle tests | 通過 |
| AC-008.6 | 回復未綁定與訂單取消分流 | matching UI 只呼叫 release；鎖定後取消導向既有訂單取消流程；cancellation tests | 通過 |

`MCSUX-AC-009` 的 ADAD Checkpoint／SSOT lifecycle 條目，本次依使用者明確要求的直接實作對照實驗不執行；它不作為本分支產品完成的阻擋條件。

## 2026-07-30 補充改善驗收

| 改善項 | 改善後契約 | Live 實作與測試證據 | 狀態 |
|---|---|---|---|
| IMP-001 | 原始與 worktree 啟動檔不修改；合併後沿用正式啟動方式 | 使用者明確排除本輪修改 | 不適用 |
| IMP-002 | `/calendar` 直接入口與主導覽都顯示同一個三分頁產品頁 | `ui/pages/03_calendar.py::show` 與 `__main__` entrypoint；UI shell/browser 驗收 | 通過 |
| IMP-003 | Order assignment Preview／Apply 都要求 system-admin，Apply actor 綁定 principal | `api/routes/orders.py`；router spoofing tests | 通過 |
| IMP-004 | Matching plan 建立要求 system-admin，created_by 綁定 principal，ValueError 穩定映射 404／409／422 | `api/routes/matches.py`；`test_caregiver_matching_plan_router.py` | 通過 |
| IMP-005 | 單日與 Batch leave Preview／Apply 都要求 system-admin；typed domain error 由中央 handler 唯一映射 | `api/routes/assignment_schedule_rest_dates.py`、`api/exception_handlers/assignment_leave_resolution.py`；handler/route tests | 通過 |
| IMP-006 | `/full-details` 不再寫入服務日期、天數、時數、樓層費與訂金日期；正式人力敏感欄位只走 Preview／Apply | `api/schemas/orders.py`、`services/db_service.py`、`ui/pages/order/editor.py`；editor/router tests | 通過 |
| IMP-007 | 公開多人 availability 只接受 2／3／4 段；單人完整覆蓋改用專用 eligibility endpoint，segment_count=1 僅為內部委派 | `api/routes/caregiver_segment_availability.py`、`ui/pages/order/tab2_assign.py`；router/UI tests | 通過 |
| IMP-008 | 代班 lineage 採 event-only canonical source；不在 case_staff_assignments 重複建立第二套 lineage 欄位 | append-only batch/event schema；`system_map.md` 現況契約 | 通過 |
| IMP-009 | MatchRouter 不直接取得 DB connection 或執行 SQL；原四步單月嫂相容行為移至 LegacyCaregiverMatchingService | `services/legacy_caregiver_matching_service.py`；service source-boundary tests | 通過 |
| IMP-010 | 舊 `OrderUI_Tab2_Assign` 架構節點改名為 `LegacySingleCaregiverMatchingRenderer`，模組路徑保留相容 | `ui/ui_system_map.md` 編譯結果；source-binding check | 通過 |
| IMP-011 | 專案根目錄具備 `system_map.schema.json`，編譯 IR 通過 JSON Schema、source binding 與 domain boundary | `system_map.schema.json`；ADAD compiler/validator checks | 通過 |
| IMP-012 | 年月標題與 selectbox 同步；正式服務日不得同時顯示可接案 | `03_calendar.py` 的單一 view state 與 JSON 日期鍵正規化；helper/UI/browser tests | 通過 |
| IMP-013 | 當月正式服務日關聯的已完成訂單保留在出勤精算選單，並以唯讀方式查看 | 賴琪 `#115000026` 顯示實際區間 2026-06-05～2026-07-03；browser 驗收 | 通過 |
| IMP-014 | 單月嫂正式 gate 不變；非 production 提供零寫入的 2／3／4 段測試預覽 | development preview button、disabled 聯繫、no-write 提示；UI/browser tests | 通過 |
| IMP-015 | 未來 legacy 案件可從零筆正式 assignment 建立第一份完整配置；legacy staff_id 只作 UI 建議 | Rules／Synchronization bootstrap tests；案件 `115000002` browser Preview | 通過 |
| IMP-016 | 排班時數未達目標時 Preview 顯示 requires_allocation 與差額，但不得 Apply；每次 Apply 都以最新資料重算所有有效 assignment 的 actual_hours 加總，精確等於訂單計畫時數才可寫入 | Case staffing、synchronization、conversion、schedule adjustment、leave/substitution tests | 通過 |
| IMP-017 | 國定假日只標示日期、不自動套用雙倍薪；新排班預設 false，人工例外須指定 assignment/schedule/date 並留備註 | holiday schema/API 預設、batch executor、規格文件與回歸測試 | 通過 |
| IMP-018 | 多月嫂轉正式時，個別 actual_hours 由 assignment-owned 工作日生成，不直接複製 planned_hours；全案 actual 合計須等於訂單目標時數 | conversion service 與差異案例測試 | 通過 |
| IMP-019 | 薪資隨成功排班結果自動計算，不設人工薪資確認時間點；應付款／月結從最新正式 assignment-owned 排班重算並再次守恆 | payroll reconciliation、payment service、leave/substitution payroll snapshot tests | 通過 |

最終驗證證據（2026-07-30）：

- 產品範圍 service／API／UI／schema／integration 測試：`795 passed`；國定假日雙倍薪新契約聚焦驗證 `2 passed`。
- `/calendar` 瀏覽器驗收：月份標題／選單同步；正式服務日無「可接案」矛盾；已完成賴琪訂單可選；多人測試預覽零寫入；案件人力配置 Preview 顯示「調整前／調整後」，且不再出現 `current assignments missing ownership on 2026-10-10`。
- System Map：編譯成功；JSON Schema、source binding、domain boundary 全部通過，且無 orphan、misplaced、untracked 或 unbound module。
