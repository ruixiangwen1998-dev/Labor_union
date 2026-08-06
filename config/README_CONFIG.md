# LINE／LIFF 可編輯設定規格

本目錄保存可由 Web 或 UI 管理端透過 FastAPI 修改的靜態設定。前端不應直接讀寫檔案，而應串接 `/api/config` API；後端會使用 Pydantic 驗證並以原子替換方式寫入 JSON。

## 設定檔

### `message_templates.json`

統一管理 Webhook 回覆、主動推播、排程推播與私人客服常用回覆。

- `id`：程式使用的穩定識別碼。
- `category`：`webhook_reply`、`push`、`scheduled_push` 或 `customer_service`。
- `message_type`：`text` 或 `flex`。
- `content`：文字或 Flex JSON。
- `variables`：可替換參數，例如 `{bind_url}`。
- `usage`：允許使用此範本的功能。
API：

```text
GET    /api/config/message-templates
PUT    /api/config/message-templates
POST   /api/config/message-templates
GET    /api/config/message-templates/{template_id}
PUT    /api/config/message-templates/{template_id}
DELETE /api/config/message-templates/{template_id}
POST   /api/config/message-templates/{template_id}/preview
```

### `line_menu.json`

管理多組 Rich Menu 的尺寸、顏色、按鈕區域及 LINE Action。

- `audience_role`：明確對應 `customer`、`staff` 或 `union_staff`。
- `appearance.image_mode`：`generated` 使用設定產圖；`uploaded` 使用受控媒體資產。
- `appearance.image_asset_id`：上傳圖片對應的 MySQL `media_assets.id`。
- `menu_group_id`：將多頁選單組成同一個發布群組。
- `rich_menu_alias_id`：LINE 平台用於頁面切換的穩定 Alias。
- `is_group_entry`：同組唯一入口頁；只有入口頁會綁定至該角色的 LINE 使用者。

Action 支援：

- `message`：點擊後向官方帳號傳文字。
- `uri`：開啟固定 URL。
- `uri`＋`uri_source: liff`：開啟目前設定的 LIFF。
- `postback`：傳送 postback data。
- `richmenuswitch`：使用 Alias 在同一組 Rich Menu 頁面間切換。

API：

```text
GET    /api/config/line-menus
GET    /api/config/line-menus/state
PUT    /api/config/line-menus
POST   /api/config/line-menus
GET    /api/config/line-menus/{menu_id}
PUT    /api/config/line-menus/{menu_id}
DELETE /api/config/line-menus/{menu_id}
POST   /api/config/line-menus/{menu_id}/preview
POST   /api/config/line-menus/{menu_id}/publish
POST   /api/v1/line/rich-menus/preview
POST   /api/v1/line/rich-menus/{menu_id}/images
POST   /api/v1/line/rich-menus/groups/{group_id}/publish
GET    /api/v1/line/rich-menus/publications
GET    /api/v1/line/rich-menus/publications/{publication_id}
POST   /api/v1/line/rich-menus/publications/{publication_id}/retry
```

儲存與發布分開。修改 JSON 不會立即更動 LINE；發布接口會建立持久化工作並喚醒 Worker，
單頁選單只發布指定 Menu；雙頁選單則會先發布其他頁、建立或更新 Alias，最後才套用入口頁。
若同組頁面未完整發布，入口頁不會取代使用者目前的選單。設定更新須帶 `If-Match` revision，
舊畫面會收到 409。

圖片上傳會檢查實際 JPEG／PNG 格式、尺寸與檔案大小，再重新編碼為 JPEG。圖片本體位於
`MEDIA_STORAGE_ROOT`（正式環境建議 NAS 或受控磁碟），MySQL `media_assets` 保存路徑、
MIME、大小、尺寸與 SHA-256；不將圖片 BLOB 存入 MySQL，也不提交 Git。

### `liff_settings.json`

管理 LIFF 共用主題、入口選擇頁、舊客戶綁定頁、新客戶登記頁及動態問題。

- `gateway` 使用 `actions` 管理入口卡片文字、圖示與相對路徑／HTTPS 連結。
- `bind` 與 `registration` 使用 `fields` 管理表單欄位。
- `system_field: true` 是後端必要欄位，API 禁止刪除、停用或改變必要型別。
- 自訂問題使用 `system_field: false`，可由前端新增、修改、排序與刪除。
- 選擇題必須提供 `options`。
- 自訂答案保存至既有 `beclass_records.survey_details` JSON，不必每次修改 DB schema。
- `liff_settings_history.json` 最多保存 20 個修改前快照，供管理介面人工還原。
- Runtime API 只輸出啟用中的頁面、欄位與入口，並以 ETag／revision 防止載入舊設定。

API：

```text
GET    /api/config/liff
GET    /api/config/liff/runtime?page={page_id}
GET    /api/config/liff/state
POST   /api/config/liff/validate
GET    /api/config/liff/history
POST   /api/config/liff/rollback/{revision}
PUT    /api/config/liff
PUT    /api/config/liff/theme
PUT    /api/config/liff/pages/{page_id}
POST   /api/config/liff/pages/{page_id}/fields
PUT    /api/config/liff/pages/{page_id}/fields/{field_id}
DELETE /api/config/liff/pages/{page_id}/fields/{field_id}
```

除公開讀取與 Runtime API 外，管理接口均需管理員權限及內部服務金鑰。修改與還原必須帶
`If-Match` revision；其他管理員先儲存時會回 409，不會靜默覆蓋。

### `customer_service.json`

目前只保存私人客服的靜態設定：服務時間、狀態顯示、閒置時間及固定回覆。聊天訊息、客服指派、已讀狀態與標籤不應存 JSON，後續應存 MySQL。

API：

```text
GET /api/config/customer-service
PUT /api/config/customer-service
```

## 訊息管理中心（5.2）

前端讀取訊息範本與內容 revision：

```text
GET /api/config/message-templates/state
```

新增、修改、刪除時會把 revision 放入 `If-Match` Header。內容已被其他人更新時回傳 409，
前端必須重新載入。草稿預覽使用：

```text
POST /api/config/message-templates/preview
```

啟用中的 `message_schedules.json` 若仍引用某個範本，該範本不可停用或刪除。JSON 寫入採
同程序鎖與原子檔案替換；目前正式架構為單一 FastAPI 程序，未來多程序時應改用集中式
設定儲存或分散式鎖。

### `message_schedules.json`

管理新好友 D+1、D+2、D+3 等排程。排程只引用 `message_templates.json` 中已啟用的範本 ID，顯示時區預設為 `Asia/Taipei`。

```text
GET /api/config/message-schedules
GET /api/config/message-schedules/state
PUT /api/config/message-schedules
```

`state` 會同時回傳設定與 SHA-256 revision；管理前端更新時以 `If-Match` 帶回 revision，
若設定已被其他人更新會回傳 409，避免覆蓋新版。後端會檢查 IANA 時區、時間格式、
重複天數及啟用中的範本是否存在；儲存排程不會立即補發或修改歷史任務，只影響之後建立的任務。

`restart_on_refollow=true` 表示使用者解除封鎖或重新加入時，取消既有尚未發送的 onboarding
任務並依當次 follow 事件重新建立；設為 `false` 時沿用首次建立的穩定冪等規則。

### `rich_menu_ids.json`

由 Rich Menu 發布器寫入的 LINE 平台 ID，不是前端可編輯設定。

重新綁定待審資料不再存放於 `config`。月嫂驗證與客戶重新綁定均保存在 MySQL `line_confirmation_requests`，`config` 目錄只保存可由管理介面維護的靜態設定。

## 圖片與附件儲存建議（後續工作）

目前 `db/schema.sql` 沒有圖片、附件或媒體資料表。本次不修改 DB。

後續建議建立共用 `media_assets` 表，Rich Menu 圖片與 LINE 用戶照片共用，以欄位分類：

```text
id
category            rich_menu / line_user_upload / contract / other
owner_type          line_user / menu / case / message
owner_id
storage_provider    local / nas / s3
storage_key
original_filename
mime_type
file_size
sha256
line_message_id
created_at
expires_at
deleted_at
```

不建議將圖片二進位直接存 MySQL BLOB。建議優先順序：

1. 正式環境：S3 相容物件儲存，例如 Cloudflare R2、AWS S3 或 MinIO。
2. 地端環境：NAS 或專用媒體目錄，DB 只保存路徑與中繼資料。
3. 開發環境：專案外的 writable media 目錄，避免把用戶照片提交 Git。

LINE 用戶照片應在 Webhook 收到 message ID 後下載至受控儲存區，再建立 `media_assets` 紀錄；不要長期依賴 LINE 暫時下載網址。

## 安全注意事項

- 第五階段 5.1 已加入資料庫管理員登入、短時效 Session、角色權限與操作稽核。
- `/api/config` 的管理讀取至少需要 `line_viewer`；新增、修改、刪除與發布需要 `line_manager`。
- 公開 LIFF 頁使用 `GET /api/config/liff/runtime`；`GET /api/config/liff` 維持舊版相容，其餘 LIFF 管理接口受保護。
- 正式環境由 LIFF 傳送 ID Token，FastAPI 使用 `LINE_LOGIN_CHANNEL_ID` 向 LINE 驗證後才採用 token 中的使用者 ID；不信任瀏覽器自行提交的 `line_user_id`。
- API 只操作固定白名單檔案，不能由前端傳入任意檔案路徑。
- Rich Menu 發布會呼叫 LINE API，應限制為管理員操作。
- 月嫂驗證查詢及角色管理底層接口仍需使用 `X-Internal-API-Key`；Web/UI 經由後端 Client 呼叫，不把金鑰交給瀏覽器。
- 工會人員帳號綁定頁不使用 `X-Internal-API-Key`，因瀏覽器不得持有內部金鑰；它改以短效一次性 Token、LINE ID Token 與後台帳密三者共同驗證。帳密只即時核對既有 scrypt hash，不會保存或建立後台 Session。

相關環境變數：

```env
INTERNAL_API_KEY=<固定長隨機值>
API_BASE_URL=http://127.0.0.1:8000
ADMIN_SESSION_MINUTES=30
ENABLE_ADMIN_AUTH=true
ALLOWED_ORIGINS=http://localhost:8501,http://127.0.0.1:8501
MEDIA_STORAGE_ROOT=.local_media
MEDIA_STORAGE_PROVIDER=local
LINE_LOGIN_CHANNEL_ID=<LIFF 所屬的 LINE Login Channel ID>
LINE_ADMIN_BINDING_LIFF_ID=<選填；工會人員綁定專用 LIFF ID>
LIFF_REQUIRE_ID_TOKEN=true
LINE_REVIEW_STALE_HOURS=24
```

`ENABLE_ADMIN_AUTH=false` 僅能在 `APP_ENV=development/dev/local/test` 略過帳號登入；正式環境
即使誤設為 `false` 仍會強制驗證。略過登入不會關閉 `X-Internal-API-Key`。

## 工會工作人員統一待審接口

月嫂資格驗證與舊客戶重新綁定可由同一個工作人員佇列取得：

```text
GET  /api/line/staff/review-requests
GET  /api/line/staff/review-requests?request_type=client_rebind
GET  /api/line/staff/review-requests?request_type=staff_verification
POST /api/line/staff/review-requests/{request_type}/{request_id}/approve
POST /api/line/staff/review-requests/{request_type}/{request_id}/reject
```

以上接口一律要求：

```http
X-Internal-API-Key: <INTERNAL_API_KEY>
```

`client_rebind` 的 approve 會更新客戶 LINE 綁定，reject 會保留原綁定並通知申請者。`staff_verification` 的 approve 會直接將 LINE 角色切換為 `staff` 並綁定月嫂選單，reject 則保留原角色並通知申請者。兩種請求共用 MySQL `line_confirmation_requests`，不產生月嫂驗證碼。

舊版 `/api/line/rebind_requests`、`approve`、`reject` 接口暫時保留相容性，但現在同樣要求內部 API Key。

開發環境可設定：

```env
ENABLE_REBIND_CONSOLE_REVIEW=true
```

開發時，Webhook提交月嫂身分或重新綁定申請後，會向專案根目錄`start_fastapi_ngrok.py`在`127.0.0.1`建立的臨時入口推送一次通知，終端隨即接受`y`核准、`n`拒絕，不會固定輪詢待審API。啟動器只在啟動時補查一次既有待審資料。此功能由`ENABLE_LINE_REVIEW_CONSOLE`控制，正式環境`APP_ENV=production`時強制停用。

正式 Web/UI 使用具管理員 Session 與角色權限的新接口：

```text
GET  /api/v1/line/review-requests/summary
GET  /api/v1/line/review-requests
GET  /api/v1/line/review-requests/{request_id}
POST /api/v1/line/review-requests/{request_id}/approve
POST /api/v1/line/review-requests/{request_id}/reject
```

清單與詳細資料至少需要 `line_agent`；核准／拒絕需要 `line_manager`。兩組接口最後都呼叫 `services/line_review_service.py`，因此交易鎖、資料衝突檢查、LINE 任務與狀態結果一致。`LINE_REVIEW_STALE_HOURS` 只控制管理頁逾時提醒門檻，不會自動拒絕或核准申請。

## LINE 主動健康監控

`config/line_monitoring.json` 管理檢查間隔、連續失敗／恢復門檻及各項警戒值，不保存金鑰。獨立程序執行：

```bash
python -m line.monitor
```

開發環境由 `start.bat` 將 `line.monitor` 與 `start_fastapi_ngrok.py` 啟動成兩個同層獨立程序，不使用逐層包覆的 watchdog 批次檔。兩者都有作業系統單例鎖，重複執行 `start.bat` 時不會建立第二套 Monitor 或服務監督器。

`start_fastapi_ngrok.py` 管理 FastAPI、ngrok 與 Streamlit，檢查程序退出、FastAPI health、Streamlit health、ngrok HTTPS Tunnel；程序仍在但連續三次檢查失敗也視為卡住。單項服務依 1、3、10 秒最多自動重啟三次。它也會檢查 Monitor 的程序心跳與 `.monitor_state/line_health.json`，必要時驗證既有 PID 後重啟 Monitor。

Monitor 持續檢查 FastAPI、MySQL、Worker 心跳、任務隊列、LINE API、公開入口、LIFF、JSON 設定與磁碟空間。服務監督器每 15 秒另寫入自身 PID、子服務 PID 與心跳；Monitor 經防抖判定其失聯後，會安全清理已登記的 FastAPI／ngrok／Streamlit 與舊監督器 PID，再以獨立程序重啟，最多三次。重啟前會核對 PID 的實際命令列，避免誤終止其他程序。

開發者以 Ctrl+C 正常關閉其中一個程序時，會在 `.monitor_state` 留下正常停機標記，另一方不會把人工停機誤判為故障並強制重啟；下次人工啟動該程序時會自動清除自己的停機標記。

正式環境目前仍由 `online.bat` 分別啟動服務，不能依賴開著的終端視窗提供高可用。正式部署應改由 Windows Service／Task Scheduler、Linux systemd 或容器平台 restart policy 管理程序；Monitor 負責偵測與紀錄，不取代作業系統層的服務管理。

目前狀態寫入 `system_health_status`，Worker／Monitor 心跳寫入 `service_heartbeats`；異常與恢復事件使用擴充後的 `system_alerts`。若 MySQL 正在故障，狀態仍會以原子方式保存至被 Git 忽略的 `.monitor_state/line_health.json`，讓 FastAPI 可回報 DB 異常。

管理查詢接口：

```text
GET /api/v1/line/monitoring/status
GET /api/v1/line/monitoring/events
```

兩者都需要內部 API Key 與管理員權限。管理中心只讀取 Monitor 已保存的結果，不會因打開頁面才開始逐項檢查，也沒有固定前端輪詢。

Monitor 會依 config/line_alert_notifications.json 將 service_monitor_alerts 的嚴重異常、
升級、持續提醒與恢復狀態轉成可靠派送紀錄，再直接呼叫 LINE Push API。這段不經過
FastAPI 內的 Worker，因此 FastAPI 或 Worker 本身中斷時，Monitor 仍可發出通知；
LINE API 暫時失敗時會以相同 X-Line-Retry-Key 進行退避重試。

通知對象保存於 line_alert_notification_targets，派送紀錄保存於
line_alert_deliveries。個人通知只能選擇已綁定 LINE 的 admin_users。群組通知不讓
管理介面手動貼入 groupId；先將官方 Bot 邀請進群組，再由已綁定的 line_manager
或 system_admin 在群組輸入「綁定異常通知群組」或「解除異常通知群組」。

管理 API 位於 /api/v1/line/alert-notifications，包含設定、通知對象、測試發送與
派送紀錄。讀取至少需要 line_viewer；修改、刪除及測試需要 line_manager。

若 MySQL 正在故障，Monitor 會改讀 .monitor_state/line_alert_targets.json 的最近一次
通知對象快取，並以 .monitor_state/line_alert_fallback_state.json 防止每 15 秒重複
傳送相同 DB 異常。快取只保存 LINE 目的地與通知偏好，不保存密碼、Token 或會員個資。

## 訂單 LINE 服務群組

工會人員需先完成後台帳號與 LINE 綁定，並具備 `line_agent` 以上權限。Bot 被加入媽媽、
月嫂與工會的服務群組後，依序輸入：

```text
綁定訂單 115000001
發送邀請連結 https://line.me/ti/g/...
```

只有第二種完整指令會觸發轉送；單獨貼網址視為一般訊息。邀請網址會短暫保存在
`line_tasks.payload_json` 供 Worker 重試，送達、取消、永久失敗或超過 24 小時後即
遮蔽。Webhook 收件匣、終端機及管理 API 不顯示明文網址。

管理 API 位於 `/api/v1/line/order-groups`。清單與明細至少需要 `line_viewer`；解除
綁定需要 `line_manager`。服務人員不必輸入 groupId，主要綁定入口仍是 LINE 群組指令。
