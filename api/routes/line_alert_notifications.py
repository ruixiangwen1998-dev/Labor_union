"""
================================================================================
檔案名稱: api/routes/line_alert_notifications.py
功能說明: LINE 系統異常通知規則、通知對象、測試發送與派送紀錄管理 API
================================================================================
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from api.dependencies.admin_auth import require_line_manager, require_line_viewer
from api.schemas.base import BaseResponse
from api.schemas.line_alert_notifications import (
    AlertNotificationTargetCreate,
    AlertNotificationTargetUpdate,
    AlertNotificationTestRequest,
    LineAlertNotificationConfig,
)
from services.json_config_service import config_revision, read_config, write_config
from services.line_alert_notification_service import (
    AlertNotificationError,
    AlertNotificationNotFoundError,
    create_notification_target,
    create_test_delivery,
    delete_notification_target,
    get_alert_delivery,
    list_alert_deliveries,
    list_available_admin_targets,
    list_notification_targets,
    process_due_alert_deliveries,
    refresh_notification_target_cache,
    update_notification_target,
)


router = APIRouter(
    prefix="/api/v1/line/alert-notifications",
    tags=["LINE Alert Notifications"],
    dependencies=[Depends(require_line_viewer)],
)
CONFIG_LOCK = threading.RLock()


def _normalize_revision(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    return normalized.strip('"')


def _translate_error(exc: AlertNotificationError) -> None:
    status_code = 404 if isinstance(exc, AlertNotificationNotFoundError) else 409
    raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.get("/config", response_model=BaseResponse[dict])
def get_notification_config():
    config = read_config("line_alert_notifications", LineAlertNotificationConfig)
    return BaseResponse(
        data={
            "revision": config_revision("line_alert_notifications"),
            "config": config.model_dump(mode="json"),
        }
    )


@router.put(
    "/config",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_manager)],
)
def update_notification_config(
    payload: LineAlertNotificationConfig,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with CONFIG_LOCK:
        expected = _normalize_revision(if_match)
        current = config_revision("line_alert_notifications")
        if expected and expected != current:
            raise HTTPException(status_code=409, detail="異常通知設定已被其他人修改，請重新載入")
        write_config("line_alert_notifications", payload)
        revision = config_revision("line_alert_notifications")
    request.state.audit_action = "line.alert_notifications.config.update"
    request.state.audit_resource_type = "line_alert_notification_config"
    request.state.audit_resource_id = "default"
    return BaseResponse(
        data={"revision": revision, "config": payload.model_dump(mode="json")},
        message="異常通知設定已更新",
    )


@router.get("/targets", response_model=BaseResponse[list[dict]])
def get_targets():
    return BaseResponse(data=list_notification_targets())


@router.get("/available-admins", response_model=BaseResponse[list[dict]])
def get_available_admins():
    return BaseResponse(data=list_available_admin_targets())


@router.post(
    "/targets",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_manager)],
)
def add_target(payload: AlertNotificationTargetCreate, request: Request):
    try:
        result = create_notification_target(
            payload.model_dump(mode="json"),
            created_by_admin_user_id=request.state.admin_principal.id,
        )
    except AlertNotificationError as exc:
        _translate_error(exc)
    request.state.audit_action = "line.alert_notifications.target.create"
    request.state.audit_resource_type = "line_alert_notification_target"
    request.state.audit_resource_id = str(result["id"])
    return BaseResponse(data=result, message="通知對象已新增")


@router.put(
    "/targets/{target_id}",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_manager)],
)
def edit_target(
    target_id: int,
    payload: AlertNotificationTargetUpdate,
    request: Request,
):
    try:
        result = update_notification_target(target_id, payload.model_dump(mode="json"))
    except AlertNotificationError as exc:
        _translate_error(exc)
    request.state.audit_action = "line.alert_notifications.target.update"
    request.state.audit_resource_type = "line_alert_notification_target"
    request.state.audit_resource_id = str(target_id)
    return BaseResponse(data=result, message="通知對象已更新")


@router.delete(
    "/targets/{target_id}",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_manager)],
)
def remove_target(target_id: int, request: Request):
    try:
        delete_notification_target(target_id)
    except AlertNotificationError as exc:
        _translate_error(exc)
    request.state.audit_action = "line.alert_notifications.target.delete"
    request.state.audit_resource_type = "line_alert_notification_target"
    request.state.audit_resource_id = str(target_id)
    return BaseResponse(data={"id": target_id}, message="通知對象已刪除")


@router.post(
    "/test",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_manager)],
)
def send_test_notification(payload: AlertNotificationTestRequest, request: Request):
    try:
        delivery = create_test_delivery(payload.target_id)
        process_due_alert_deliveries(
            limit=1,
            only_delivery_id=int(delivery["id"]),
        )
        result = get_alert_delivery(int(delivery["id"]))
        refresh_notification_target_cache()
    except AlertNotificationError as exc:
        _translate_error(exc)
    request.state.audit_action = "line.alert_notifications.test"
    request.state.audit_resource_type = "line_alert_notification_target"
    request.state.audit_resource_id = str(payload.target_id)
    return BaseResponse(data=result, message="測試通知已處理")


@router.get("/deliveries", response_model=BaseResponse[list[dict]])
def get_deliveries(limit: int = Query(default=100, ge=1, le=500)):
    return BaseResponse(data=list_alert_deliveries(limit))
