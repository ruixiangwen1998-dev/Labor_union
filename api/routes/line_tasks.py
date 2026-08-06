"""
================================================================================
檔案名稱: api/routes/line_tasks.py
功能說明: LINE 發送任務管理 API，提供查詢、立即執行、取消及失敗重送等安全操作
================================================================================
"""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.dependencies.admin_auth import (
    require_line_agent,
    require_line_manager,
    require_line_viewer,
)
from api.schemas.base import BaseResponse
from api.schemas.line_tasks import LineTaskActionRequest
from line.worker import wake_worker, worker_is_running
from services.line_task_admin_service import (
    LineTaskNotFoundError,
    LineTaskStateConflictError,
    cancel_line_task,
    get_line_task,
    get_line_task_summary,
    list_line_tasks,
    retry_line_task,
    run_line_task_now,
)


router = APIRouter(
    prefix="/api/v1/line/tasks",
    tags=["LINE Tasks"],
    dependencies=[Depends(require_line_viewer)],
)


def _raise_task_error(exc: Exception) -> NoReturn:
    if isinstance(exc, LineTaskNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, LineTaskStateConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


def _set_task_audit(
    request: Request,
    *,
    action: str,
    task_id: int,
    reason: str,
) -> None:
    request.state.audit_action = action
    request.state.audit_resource_type = "line_task"
    request.state.audit_resource_id = str(task_id)
    request.state.audit_details = {"reason": reason.strip()} if reason.strip() else None


@router.get("/summary", response_model=BaseResponse[dict])
def task_summary():
    summary = get_line_task_summary()
    summary["worker_running"] = worker_is_running()
    return BaseResponse(data=summary)


@router.get("", response_model=BaseResponse[dict])
def task_list(
    status: str | None = None,
    task_type: str | None = None,
    user_id: str | None = None,
    onboarding_only: bool = False,
    scheduled_from: datetime | None = None,
    scheduled_to: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    try:
        result = list_line_tasks(
            status=status,
            task_type=task_type,
            user_id=user_id,
            onboarding_only=onboarding_only,
            scheduled_from=scheduled_from,
            scheduled_to=scheduled_to,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        _raise_task_error(exc)
    return BaseResponse(data=result)


@router.get("/{task_id}", response_model=BaseResponse[dict])
def task_detail(task_id: int):
    try:
        result = get_line_task(task_id)
    except LineTaskNotFoundError as exc:
        _raise_task_error(exc)
    return BaseResponse(data=result)


@router.post(
    "/{task_id}/cancel",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_agent)],
)
def cancel_task(task_id: int, payload: LineTaskActionRequest, request: Request):
    try:
        result = cancel_line_task(task_id)
    except (LineTaskNotFoundError, LineTaskStateConflictError, ValueError) as exc:
        _raise_task_error(exc)
    _set_task_audit(
        request,
        action="line.task.cancel",
        task_id=task_id,
        reason=payload.reason,
    )
    return BaseResponse(data=result, message="任務已取消")


@router.post(
    "/{task_id}/run-now",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_manager)],
)
def run_task_now(task_id: int, payload: LineTaskActionRequest, request: Request):
    try:
        result = run_line_task_now(task_id)
    except (LineTaskNotFoundError, LineTaskStateConflictError) as exc:
        _raise_task_error(exc)
    _set_task_audit(
        request,
        action="line.task.run_now",
        task_id=task_id,
        reason=payload.reason,
    )
    wake_worker()
    return BaseResponse(data=result, message="任務已排入立即執行")


@router.post(
    "/{task_id}/retry",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_agent)],
)
def retry_task(task_id: int, payload: LineTaskActionRequest, request: Request):
    try:
        result = retry_line_task(task_id)
    except (LineTaskNotFoundError, LineTaskStateConflictError, ValueError) as exc:
        _raise_task_error(exc)
    _set_task_audit(
        request,
        action="line.task.retry",
        task_id=task_id,
        reason=payload.reason,
    )
    wake_worker()
    return BaseResponse(data=result, message="失敗任務已重新排入")
