# LINE 訂單群組邀請、月嫂 LIFF 驗證、主動監控與雙向恢復

## 更新摘要

本次只記錄目前 `origin/LINE-Bot-Wen` 尚未包含的 LINE 功能：

1. 月嫂透過 LIFF 輸入基本資料，和既有月嫂資料比對後交由工會人員人工核准並綁定 LINE。
2. LINE 系統改為背景主動監控，不需要打開管理頁才開始檢查。
3. 開發環境的 Monitor 與服務監督器改成同層獨立程序，可互相偵測、受控重啟，取消外層批次 watchdog。
4. 訂單可綁定 LINE 服務群組，安全地轉送一次性邀請並追蹤媽媽與月嫂加入狀態。

對應尚未推送的主要提交：

- `3d69704 feat: add LIFF staff verification and approval binding`
- `c12b036 feat: add active monitoring and mutual service recovery`

## 新增檔案

### 月嫂 LIFF 驗證

- `line/static/staff_verification.html`：月嫂輸入姓名、身分證字號與生日的 LIFF 頁面。
- `api/routes/line_staff_verification.py`：驗證頁狀態查詢與資料送出 API。
- `api/schemas/line_staff_verification.py`：月嫂驗證 API 請求格式。
- `services/line_staff_verification_service.py`：一次性連結、LINE 身分確認、既有月嫂資料比對與結果保存。
- `db/schema_parts/98_line_staff_verification.sql`：月嫂驗證欄位及索引 migration。
- `tests/test_line_staff_verification.py`：資料比對、送出、人工核准及 LINE 綁定測試。

### LINE 主動監控與程序恢復

- `line/monitor.py`：獨立主動監控程序，並在服務監督器失聯時執行受控重啟。
- `services/line_health_checks.py`：FastAPI、DB、Worker、任務、LINE API、公開入口、LIFF、設定、磁碟及監督器心跳檢查。
- `services/line_monitor_service.py`：監控排程、防抖、心跳、DB／本機快照及異常／恢復事件。
- `services/runtime_supervision_service.py`：同層程序單例鎖、PID 解析、命令列驗證、安全終止與獨立重啟工具。
- `api/routes/line_monitoring.py`、`api/schemas/line_monitoring.py`：受保護的監控狀態與事件 API。
- `ui/components/line_health_monitor.py`：LINE 管理中心健康狀態與異常紀錄介面。
- `config/line_monitoring.json`：檢查間隔、防抖及警戒門檻。
- `db/schema_parts/97_line_active_monitoring.sql`：程序心跳、健康狀態與監控事件 migration。
- `tests/test_line_monitoring.py`：監控、心跳、防抖、API、單例鎖及雙向恢復測試。

## 主要修改

### 月嫂驗證與人工核准

- `line/line_bot.py`：收到「我是月嫂」時建立申請並傳送驗證頁連結。
- `line/static/gateway.html`：由既有 LIFF Gateway 安全導向月嫂驗證頁，避免 redirect URI HTTP 400。
- `services/line_review_service.py`：核准時綁定 `staff_id`、LINE 帳號、`staff` 角色與對應 Rich Menu。
- `ui/components/line_review_manager.py`：顯示送出資料、比對結果及遮蔽後的既有月嫂資料。
- `db/schema.sql`：擴充 `line_confirmation_requests` 驗證、比對、期限與嘗試次數欄位。

### 主動監控與雙向恢復

- `line/worker.py`：每 15 秒更新 Worker 心跳及工作迴圈進度。
- `start_fastapi_ngrok.py`：管理 FastAPI、ngrok、Streamlit，檢查 Monitor 快照並在失聯時重啟 Monitor。
- `start.bat`：直接啟動 Monitor 與服務監督器兩個同層程序；不再重複啟動 Streamlit。
- `online.bat`：正式／連線測試模式獨立啟動 LINE Monitor，不啟動 ngrok。
- `api/main.py`：掛載監控 API；Worker 仍由 FastAPI lifespan 管理。
- `api/routes/line_admin.py`：管理中心總覽改讀 Monitor 已保存的狀態。
- `ui/api_clients/line_api_client.py`、`ui/pages/07_line_management.py`：串接並顯示主動監控狀態及事件。
- `db/schema.sql`：新增 `service_heartbeats`、`system_health_status`，擴充 `system_alerts`。
- `.gitignore`：忽略 DB 故障時的 `.monitor_state` 本機快照及程序標記。

## 執行方式

開發環境使用：

```bat
start.bat
```

啟動後：

```text
LINE Monitor
  ↕ 心跳、PID、快照與受控重啟
服務監督器
  ├─ FastAPI
  ├─ ngrok
  └─ Streamlit
```

- 兩個同層程序都有單例鎖，防止重複啟動。
- 重啟前會核對 PID 的實際命令列，避免誤終止其他程序。
- 正常 Ctrl+C 會留下停機標記，不會被另一方誤判為故障。
- 異常、重啟及恢復會寫入 `system_alerts`，並顯示在 LINE 管理中心。

## 驗證結果

- LINE／監控／LIFF／任務／審查／啟動回歸：60 項通過。
- 完整測試：812 項通過。
- 另有 4 項既有訂單／表單 Streamlit AppTest 因固定 3 秒時限逾時，與本次 LINE 修改無關且和前次結果相同。
- migration 已套用開發 DB，未清空既有資料。
- 未建立或遺留一次性 Python 檔案。

## 2026-08-04 訂單 LINE 服務群組綁定與安全邀請轉送

### 新增功能

- 工會人員可在 LINE 群組輸入「綁定訂單 案件編號」，將目前群組綁到指定訂單。
- 只有「發送邀請連結 LINE網址」完整指令會把邀請 Flex 卡片可靠發送給媽媽與月嫂。
- 追蹤預期成員的邀請、加入、離開與群組生命週期；LINE 管理中心和訂單配對頁可直接查看進度。
- 邀請網址只在待處理／自動重試期間短暫保存，終態或 24 小時逾期即遮蔽，且不寫入明文 Webhook 紀錄或管理 API。

### 新增檔案

- `services/line_order_group_service.py`：權限、綁定、邀請任務、Flex、成員事件與敏感資料遮蔽。
- `api/routes/line_order_groups.py`、`api/schemas/line_order_groups.py`：群組清單、明細與解除綁定 API。
- `ui/components/line_order_group_manager.py`：服務人員可讀的訂單群組管理介面。
- `db/schema_parts/107_line_order_groups.sql`：群組生命週期與預期成員 migration。
- `tests/test_line_order_groups.py`：命令、網址驗證、Flex、遮蔽及 Schema 回歸。

### 主要修改檔案

- `line/line_bot.py`：加入群組提示、綁定／邀請指令與 memberJoined／memberLeft／leave 處理。
- `line/worker.py`：發送群組邀請 Flex、24 小時逾期處理及任務終態遮蔽。
- `services/webhook_event_service.py`、`services/line_task_admin_service.py`：Webhook 與管理 API 隱藏邀請網址，取消後立即清除且禁止重送已遮蔽任務。
- `api/main.py`、`ui/api_clients/line_api_client.py`：掛載並串接新 API。
- `ui/pages/07_line_management.py`、`ui/pages/order/tab2_assign.py`：新增群組管理頁與訂單群組狀態。
- `db/schema.sql`、`config/README_CONFIG.md`：補齊完整 Schema 與操作規格。
- `.gitignore`：忽略本機 `backups/`，避免含開發資料的 SQL 備份被誤提交。

### 驗證結果

- 新功能與既有 LINE 任務、通知、後台綁定、月嫂驗證、監控及 API 共 57 項測試通過；完整測試 1604 項通過。
- Python compileall 與 Git diff 格式檢查通過。
- 已先備份 `union_db`，再只套用 `107_line_order_groups.sql` 增量 migration；未執行會重建整個 DB 的 `scripts/init_db.py`，既有管理員帳號保留。
- 未建立或遺留一次性 Python 檔案。

## 2026-08-02 工會人員雙頁 Rich Menu 與管理中心快捷訊息

對應提交：`c3bcdf0 feat: add union staff rich menu workspace`

### 新增功能

- 工會人員 Rich Menu 分為「快捷訊息」與「工會後台」兩頁，使用 Rich Menu Alias 原生切換。
- LINE 管理中心可把訊息設為「傳給媽媽」、「傳給月嫂」或「群組工具說明」，並調整顯示順序。
- 工會人員私訊點選分類時，Bot 驗證已綁定身分後送出 Quick Reply；訊息由既有 Worker 任務可靠發送。
- 新增工會人員 LIFF 安全入口，使用 LINE ID Token 與後台綁定資料確認身分，不把內部 API 金鑰交給瀏覽器。

### 新增檔案

- `line/static/union_staff_portal.html`：工會人員手機 LIFF 功能入口與導覽。

### 主要修改檔案

- `config/line_menu.json`：新增工會人員雙頁選單、Alias、切頁與功能入口按鈕。
- `config/message_templates.json`：新增媽媽、月嫂及群組工具三類示範快捷訊息。
- `api/schemas/line_config.py`：驗證快捷分類、雙頁群組、Alias 與 `richmenuswitch` 動作。
- `services/line_rich_menu_service.py`：群組發布順序、相依檢查、Alias 建立／更新與入口頁綁定。
- `api/routes/line_rich_menus.py`、`ui/api_clients/line_api_client.py`：新增雙頁群組發布 API 串接。
- `ui/components/line_rich_menu_manager.py`：編輯切頁按鈕並一次套用同組選單。
- `ui/components/line_message_manager.py`：以服務人員易懂欄位管理快捷分類及排序。
- `line/line_bot.py`、`line/worker.py`：工會身分檢查、Quick Reply 任務與複合訊息發送。
- `line/static/gateway.html`：由既有 LIFF Gateway 導向工會後台入口。
- `config/README_CONFIG.md`：補充雙頁選單與快捷訊息設定規格。
- `tests/test_line_rich_menu_management.py`、`tests/test_line_message_management.py`、`tests/test_line_liff_management.py`：新增回歸驗證。
- `.gitignore`：納入工作區既有異動，不再忽略 `start.bat`，讓團隊可同步一鍵啟動檔。

### 驗證結果與範圍

- Rich Menu、訊息管理與 LIFF 目標測試：31 項通過。
- 擴大 LINE／LIFF／任務／安全回歸：65 項通過；1 項既有監督器測試受本機正常關機標記影響而顯示 maintenance，未擅自刪除該執行狀態。
- 工會 LIFF 目前完成安全入口和功能導覽；訂單、排休、審查及訊息的詳細資料操作仍待後續串接現有 FastAPI API。
- 未修改資料庫 Schema，未建立或遺留一次性 Python 檔案。

## 2026-08-01 工會人員 LINE 與後台帳號安全綁定

### 新增功能

- 工會人員在官方帳號一對一聊天室輸入「綁定後台帳號」，取得 15 分鐘有效的一次性 LIFF 連結。
- LIFF 驗證 LINE 身分及既有後台帳密；成功後綁定 `admin_users.linked_line_user_id` 並切換 LINE 工會人員選單，原後台權限保持不變。
- 防止群組散播綁定連結、Token 重放、錯誤帳密暴力嘗試、月嫂身分被覆蓋，以及同一後台／LINE 帳號重複綁定。

### 新增檔案

- `services/line_admin_binding_service.py`：一次性 Token、帳密核對、綁定交易、Rich Menu 任務與稽核。
- `api/routes/line_admin_binding.py`、`api/schemas/line_admin_binding.py`：公開 LIFF 綁定狀態與完成 API。
- `line/static/union_staff_binding.html`：工會人員後台帳密綁定頁。
- `db/schema_parts/105_line_admin_binding.sql`：可重複執行的綁定請求表 migration。
- `tests/test_line_admin_binding.py`：錯誤密碼、成功綁定、權限保留、Rich Menu 任務與 Token 重放測試。

### 修改檔案

- `line/line_bot.py`：新增私訊綁定指令、群組防護、綁定 LIFF 頁路由及設定輸出。
- `line/static/gateway.html`：白名單導向工會人員綁定頁。
- `api/main.py`：掛載新綁定 API。
- `db/schema.sql`：新增 `line_admin_binding_requests`。
- `api/schemas/line_config.py`、`config/liff_settings.json`、`ui/components/line_liff_manager.py`：加入可管理的工會人員綁定頁文字與密碼欄位型別。
- `services/line_liff_config_service.py`、`api/routes/line_system_config.py`：舊版 LIFF 歷史還原時自動補齊新的必要綁定頁。
- `tests/test_line_liff_management.py`：加入新頁面與 LIFF 身分傳遞安全回歸。
- `.env.example`、`config/README_CONFIG.md`：補充選填的專用 LIFF ID、共用 Gateway 與三重驗證安全邊界。

### 驗證結果

- 本機開發 `union_db` 已使用固定 v3 fixture 成功重建。
- 綁定／LIFF／既有月嫂驗證目標測試：21 項通過。
- 完整測試：1574 項通過、0 項失敗；6 個既有棄用警告。
- 未建立或遺留一次性 Python 檔案。

## 2026-08-01 合併後追加修復

### 功能修正

- 新增監控用 DB Schema 主動檢查，會確認 `system_alerts`、`service_monitor_alerts`、`service_heartbeats` 與 `system_health_status` 的關鍵欄位。
- 監控資料寫入或讀取 DB 失敗時，不再靜默忽略或誤顯示正常；API 與 LINE 管理中心會顯示監控資料保存異常。
- 異常事件無法讀取時，管理中心不再顯示成「目前沒有異常紀錄」。
- 修復 `95_multi_caregiver_schedule.sql` 對 MySQL `CHECK_CONSTRAINTS.TABLE_NAME` 不存在欄位的引用，避免 `reset_DB.bat` 在 DROP 後中斷。

### 本次修改檔案

- `services/line_health_checks.py`：新增資料庫結構檢查。
- `services/line_monitor_service.py`：揭露持久化及讀取錯誤，並在本機快照標記 degraded／critical。
- `api/schemas/line_monitoring.py`：新增監控資料保存狀態欄位。
- `config/line_monitoring.json`：啟用 `database_schema` 定期檢查。
- `ui/components/line_health_monitor.py`：顯示 DB 結構及監控紀錄保存異常。
- `db/schema_parts/95_multi_caregiver_schedule.sql`：修復 CHECK constraint metadata 查詢。
- `tests/test_line_monitoring.py`、`tests/test_init_db_schema_parts.py`：新增回歸測試。

### 驗證結果

- 本機 `union_db` 已使用固定 v3 fixture 成功完整重建。
- 監控／migration／DB 重建目標測試：20 項通過。
- 完整測試：1569 項通過，0 項失敗，6 個既有棄用警告。
- 未建立或遺留一次性 Python 檔案。

## 2026-08-02 系統異常主動通知

### 新增功能

- Monitor 主動把嚴重異常、異常升級、持續提醒與恢復訊息傳給指定工會人員或 LINE 群組。
- 工會 LINE 主管／系統管理員可在群組輸入「綁定異常通知群組」完成 groupId 安全綁定，不需在後台手動貼工程識別碼。
- LINE 管理中心可設定通知等級、恢復提醒、重複提醒間隔、元件開關、個人通知對象，並測試發送及查看派送結果。
- LINE API 暫時失敗會使用固定 Retry Key 退避重試；MySQL 故障時由 Monitor 使用本機目標快取送出 DB 異常與恢復通知。

### 新增檔案

- services/line_alert_notification_service.py：通知對象、事件轉換、去重、派送、重試與 DB 故障備援。
- api/routes/line_alert_notifications.py、api/schemas/line_alert_notifications.py：通知管理 API 與輸入驗證。
- ui/components/line_alert_notification_manager.py：服務人員可操作的通知規則、對象、測試及紀錄介面。
- config/line_alert_notifications.json：通知等級、恢復、提醒、重試與元件開關。
- db/schema_parts/106_line_alert_notifications.sql：通知對象與可靠派送 migration。
- tests/test_line_alert_notifications.py：權限、資料格式、冪等派送、指定測試與 DB 故障備援測試。

### 主要修改檔案

- line/monitor.py：每次監控週期分派 LINE 異常通知；DB 無法使用時切換本機快取。
- line/line_bot.py：Bot join／leave 事件及群組綁定／解除指令。
- api/main.py：掛載通知 API，並移除重複的稽核 middleware。
- services/line_health_checks.py、services/line_monitor_service.py：監控新 Schema／設定檔並修正空 details 導致的 API 500。
- ui/api_clients/line_api_client.py、ui/pages/07_line_management.py：串接管理 API 與使用狀態頁。
- db/schema.sql、config/README_CONFIG.md、services/json_config_service.py：加入資料表、設定註冊及操作說明。

### 驗證結果

- 開發 DB 已套用 migration，未清空或重建假資料。
- 通知與監控整合測試：20 項通過；擴大 LINE／權限／任務／LIFF 回歸：63 項通過。
- 含 Monitor 啟動與中斷修正的完整測試：1596 項通過，0 項失敗，6 個既有棄用警告。
- 未建立或遺留一次性 Python 檔案。

## 2026-08-02 開發服務監督器中斷判定修正

### 功能修正

- FastAPI、ngrok 與 Streamlit 在 Windows 使用獨立 process group，避免子程序 reload 或控制事件波及 supervisor。
- `KeyboardInterrupt` 不再直接宣稱使用者按下 Ctrl+C；改為記錄 UTC 時間、PID、父 PID 與來源未確認提示。
- 只有操作人明確輸入 `y` 才寫入 `development_supervisor` 正常關閉標記；未確認中斷不抑制 Monitor 救援，並於 1 秒後重建受管服務。
- 保留 Streamlit 預設自動開啟瀏覽器行為，關閉瀏覽器分頁不作為伺服器停止條件。
- 監控測試的 shutdown marker 改用 pytest 臨時目錄，避免讀寫實際運行狀態。
- 更新工會人員 Rich Menu 已發布 ID，並補齊前次 Rich Menu 提交紀錄。

### 本次修改檔案

- `start_fastapi_ngrok.py`：子程序群組隔離、人工停止確認、未確認中斷恢復與診斷輸出。
- `tests/test_development_supervisor_interrupt_handling.py`：新增程序群組、瀏覽器行為及中斷分類回歸測試。
- `tests/test_line_monitoring.py`：隔離測試用正常關閉標記。
- `config/rich_menu_ids.json`：記錄工會人員目前 Rich Menu ID。
- `history/git_push.md`：更新本次提交與前次 Rich Menu 紀錄。

### 驗證結果

- Supervisor／LINE Monitor 針對性測試：16 項通過。
- 完整測試：1582 項通過，0 項失敗，6 個既有棄用警告。
- 未加入 Streamlit `--server.headless true`，啟動後仍會自動開啟瀏覽器供本機測試。
- 未建立或遺留一次性 Python 檔案。

## 2026-08-02 Monitor 自動帶起與關閉判定修正

### 功能修正

- 新的 supervisor 工作階段會消耗上一輪 `line_monitor.shutdown`，避免不存在的 Monitor 被舊 marker 誤判為健康。
- Monitor 健康檢查以本次工作階段啟動時間為基準，不再接受上一輪殘留快照。
- 找不到 Monitor 時的探索等待由 75 秒縮短為 20 秒；已知存在正常關閉 marker 時直接補啟動。
- Monitor 收到 SIGINT 時要求操作人輸入 `y`，只有確認後才寫入正常關閉 marker。
- 未確認 SIGINT 會繼續監控；SIGTERM 視為外部異常終止，以非零狀態結束且不寫正常關閉 marker，交由 supervisor 恢復。
- Monitor 中斷紀錄新增 signal、UTC 時間、PID 與父 PID，避免把控制事件誤稱為人工 Ctrl+C。

### 本次修改檔案

- `start_fastapi_ngrok.py`：清除 stale Monitor marker、建立本次快照時間基準並縮短缺少 Monitor 的探索時間。
- `line/monitor.py`：分流 SIGINT／SIGTERM、加入人工確認與中斷診斷。
- `tests/test_development_supervisor_interrupt_handling.py`：新增 stale marker、缺少 Monitor、SIGINT 與 SIGTERM 回歸測試。

### 驗證結果

- Supervisor／LINE Monitor 針對性測試：21 項通過。
- 完整測試：1596 項通過，0 項失敗，6 個既有棄用警告。
- 未建立或遺留一次性 Python 檔案。
## 工會人員 Rich Menu 簡化為單頁後台入口

### 功能調整

- 移除工會人員原有的「快捷訊息」Rich Menu 頁及雙頁切換按鈕。
- 保留工會人員後台入口，提供系統狀態、訂單、排休、待確認申請及訊息發送功能。
- 媽媽與月嫂原有 Rich Menu 不受影響。
- LINE 管理中心同步移除已失效的快捷選單分類與排序設定。

### 主要修改檔案

- `config/line_menu.json`：工會人員選單改為單頁後台入口並重新配置底部訊息發送按鈕。
- `line/line_bot.py`：移除快捷選單 Postback 與 Quick Reply 產生邏輯。
- `ui/components/line_message_manager.py`：移除工會快捷選單顯示分類與排序欄位。
- `ui/components/line_rich_menu_manager.py`：更新單頁選單功能說明，移除雙頁提示。
- `api/schemas/line_config.py`、`config/message_templates.json`、`config/README_CONFIG.md`：移除快捷選單專用範本欄位與文件說明。
- `tests/test_line_rich_menu_management.py`、`tests/test_line_message_management.py`：更新單頁選單與無殘留快捷設定的回歸測試。

### 驗證結果

- LINE 選單、訊息、工會帳號綁定及任務管理測試：21 項通過。
- 未直接變更 LINE 平台線上選單；部署後需在 LINE 管理中心重新套用工會人員後台選單。
- 未建立或遺留一次性 Python 檔案。
