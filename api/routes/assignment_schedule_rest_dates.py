"""
================================================================================
檔案名稱: api/routes/assignment_schedule_rest_dates.py
功能說明: 以 assignment_id 為唯一權屬之月嫂排休更新 API 路由 (AssignmentScheduleRestDateRouter)
================================================================================
"""

from datetime import date
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from starlette import status

from api.dependencies.admin_auth import require_system_admin
from api.schemas.base import BaseResponse
from api.schemas.orders import (
    AssignmentLeaveResolutionBatchApplyRequest,
    AssignmentLeaveResolutionBatchPreviewRequest,
    AssignmentLeaveResolutionApplyRequest,
    AssignmentLeaveResolutionPreviewRequest,
    AssignmentRestDatesUpdateRequest,
)
from services import assignment_schedule_rest_date_service
from services.admin_auth_service import AdminPrincipal
from services.assignment_schedule_rest_date_service import (
    apply_assignment_leave_resolution_batch,
    apply_assignment_leave_resolution,
    preview_assignment_leave_resolution,
    preview_assignment_leave_resolution_batch,
)
from services.assignment_schedule_rest_date_service import (
    AssignmentLeaveResolutionDomainError,
)

router = APIRouter(
    prefix="/api/v1/assignment-schedules",
    tags=["Assignment Schedules 排班與排休"],
)


@router.post(
    "/{assignment_id}/rest-dates/leave-resolution/batch-preview",
    response_model=BaseResponse[Dict[str, Any]],
)
def preview_assignment_leave_resolution_batch_route(
    req: AssignmentLeaveResolutionBatchPreviewRequest,
    assignment_id: int = Path(..., gt=0),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """Preview multiple leave dates as one canonical atomic batch."""
    del principal
    try:
        request = req.model_dump(mode="json")
        if request.get("original_assignment_id") != assignment_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="assignment_id does not match original_assignment_id",
            )
        result = preview_assignment_leave_resolution_batch(request)
        return BaseResponse(data=result, message="成功預覽多日期休假順延／代班")
    except HTTPException:
        raise
    except AssignmentLeaveResolutionDomainError:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error during batch leave preview",
        ) from exc


@router.post(
    "/{assignment_id}/rest-dates/leave-resolution/batch-apply",
    response_model=BaseResponse[Dict[str, Any]],
)
def apply_assignment_leave_resolution_batch_route(
    req: AssignmentLeaveResolutionBatchApplyRequest,
    assignment_id: int = Path(..., gt=0),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """Atomically apply a fresh multi-date leave/defer/substitution preview."""
    try:
        payload = req.model_dump(mode="json")
        _require_matching_assignment_id(assignment_id, payload)
        if _canonical_identity(principal) != payload["actor"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=_error_payload(
                    status_code=status.HTTP_403_FORBIDDEN,
                    status_name="authorization",
                    reason="actor does not match authenticated principal",
                    result={
                        "expected": _canonical_identity(principal),
                        "received": payload["actor"],
                    },
                ),
            )
        result = apply_assignment_leave_resolution_batch(payload)
        if result.get("status") in {"applied", "idempotent_replay"}:
            return BaseResponse(data=result, message="成功套用多日期休假順延／代班")
        if result.get("status") == "rejected":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_error_payload(
                    status_code=status.HTTP_409_CONFLICT,
                    status_name="rejected",
                    reason=str(
                        result.get("business_conflicts", {}).get(
                            "status", "preview_not_ready"
                        )
                    ),
                    result=result,
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_error_payload(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                status_name="internal_error",
                reason="unsupported batch apply status",
                result=result,
            ),
        )
    except HTTPException:
        raise
    except AssignmentLeaveResolutionDomainError:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=_error_status_for_apply_error(exc),
            detail=_error_payload(
                status_code=_error_status_for_apply_error(exc),
                status_name="validation_error",
                reason=str(exc),
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_error_payload(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                status_name="internal_error",
                reason=str(exc),
            ),
        ) from exc


def _http_status_for_service_result(result: Dict[str, Any]) -> int:
    match result.get("status"):
        case "not_found":
            return status.HTTP_404_NOT_FOUND
        case "validation_error":
            return status.HTTP_422_UNPROCESSABLE_ENTITY
        case "conflict" | "locked":
            return status.HTTP_409_CONFLICT
        case "ok":
            return status.HTTP_200_OK
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _http_status_for_leave_preview(result: Dict[str, Any]) -> int:
    match result.get("status"):
        case "ready" | "requires_review":
            return status.HTTP_200_OK
        case "blocked" | "conflict" | "locked" | "stale":
            return status.HTTP_409_CONFLICT
        case "not_found":
            return status.HTTP_404_NOT_FOUND
        case "validation_error":
            return status.HTTP_422_UNPROCESSABLE_ENTITY
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _is_successful(result: Dict[str, Any]) -> bool:
    return bool(result.get("success")) and result.get("status") == "ok"


def _canonical_identity(principal: AdminPrincipal | None) -> str:
    if principal is None:
        return ""
    return str(principal.username or "").strip()


def _error_payload(
    *,
    status_code: int,
    status_name: str,
    reason: str,
    result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    payload = {
        "status": str(status_name),
        "reason": reason,
        "status_code": status_code,
    }
    if result is not None:
        payload["service_result"] = result
    return payload


def _error_status_for_apply_rejected(reason: str) -> int:
    normalized = (reason or "").lower()
    if normalized in {"preview_not_ready", "preview_fingerprint_mismatch"}:
        return status.HTTP_409_CONFLICT
    if "identity" in normalized and "event_key" in normalized:
        return status.HTTP_409_CONFLICT
    if "fingerprint" in normalized:
        return status.HTTP_409_CONFLICT
    if "conflict" in normalized or "locked" in normalized or "stale" in normalized:
        return status.HTTP_409_CONFLICT
    return status.HTTP_409_CONFLICT


def _error_status_for_apply_error(error: Exception) -> int:
    message = (str(error) or "").lower()
    if "event_key" in message and "different request identity" in message:
        return status.HTTP_409_CONFLICT
    if "preview_fingerprint" in message:
        return status.HTTP_409_CONFLICT
    if "locked" in message or "conflict" in message:
        return status.HTTP_409_CONFLICT
    return status.HTTP_422_UNPROCESSABLE_ENTITY


def _require_matching_assignment_id(
    assignment_id: int,
    request_payload: Dict[str, Any],
) -> None:
    if assignment_id != request_payload.get("original_assignment_id"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_payload(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                status_name="validation_error",
                reason="assignment_id does not match original_assignment_id",
            ),
        )


@router.put("/{assignment_id}/rest-dates", response_model=BaseResponse[Dict[str, Any]])
def save_assignment_rest_dates(
    req: AssignmentRestDatesUpdateRequest,
    assignment_id: int = Path(..., description="服務指派識別碼 assignment_id"),
):
    """更新特定指派 (assignment_id) 之排休與動態順延完工日"""
    try:
        result = assignment_schedule_rest_date_service.save_assignment_rest_dates(
            assignment_id=assignment_id,
            rest_dates=req.rest_dates,
        )
        if not _is_successful(result):
            status_code = _http_status_for_service_result(result)
            raise HTTPException(status_code=status_code, detail=result.get("message"))
        return BaseResponse(data=result, message="成功更新指派排休與動態完工日")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/{assignment_id}/rest-dates/leave-resolution/preview",
    response_model=BaseResponse[Dict[str, Any]],
)
def preview_assignment_leave_resolution_route(
    req: AssignmentLeaveResolutionPreviewRequest,
    assignment_id: int = Path(..., description="服務指派識別碼 assignment_id"),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """產生單日休假順延／代班的機器可讀預覽。"""
    del principal
    try:
        payload = req.model_dump()
        _require_matching_assignment_id(assignment_id, payload)
        result = preview_assignment_leave_resolution(
            case_no=payload["case_no"],
            original_assignment_id=payload["original_assignment_id"],
            original_schedule_id=payload["original_schedule_id"],
            work_date=payload["work_date"].isoformat()
            if isinstance(payload["work_date"], date)
            else payload["work_date"],
            resolution_type=payload["resolution_type"],
            substitute_staff_id=payload["substitute_staff_id"],
        )
        status_code = _http_status_for_leave_preview(result)
        if status_code != status.HTTP_200_OK:
            raise HTTPException(
                status_code=status_code,
                detail=_error_payload(
                    status_code=status_code,
                    status_name=str(result.get("status")),
                    reason=result.get("message", "preview rejected"),
                    result=result,
                ),
            )
        return BaseResponse(data=result, message="成功預覽單日休假順延／代班")
    except HTTPException:
        raise
    except AssignmentLeaveResolutionDomainError:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_payload(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                status_name="validation_error",
                reason=str(exc),
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_error_payload(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                status_name="internal_error",
                reason=str(exc),
            ),
        )


@router.post(
    "/{assignment_id}/rest-dates/leave-resolution/apply",
    response_model=BaseResponse[Dict[str, Any]],
)
def apply_assignment_leave_resolution_route(
    req: AssignmentLeaveResolutionApplyRequest,
    assignment_id: int = Path(..., description="服務指派識別碼 assignment_id"),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """套用單日休假順延／代班：僅回傳 canonical 服務結果，不處理商務邏輯。"""
    try:
        payload = req.model_dump()
        _require_matching_assignment_id(assignment_id, payload)
        if _canonical_identity(principal) != payload["actor"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=_error_payload(
                    status_code=status.HTTP_403_FORBIDDEN,
                    status_name="authorization",
                    reason="actor does not match authenticated principal",
                    result={"expected": _canonical_identity(principal), "received": payload["actor"]},
                ),
            )
        result = apply_assignment_leave_resolution(payload)
        match result.get("status"):
            case "applied" | "idempotent_replay":
                return BaseResponse(data=result, message="成功套用單日休假順延／代班")
            case "rejected":
                reject_reason = str(result.get("reason", "rejected"))
                status_code = _error_status_for_apply_rejected(reject_reason)
                raise HTTPException(
                    status_code=status_code,
                    detail=_error_payload(
                        status_code=status_code,
                        status_name="rejected",
                        reason=reject_reason,
                        result=result,
                    ),
                )
            case _:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=_error_payload(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        status_name="internal_error",
                        reason="unsupported apply status",
                        result=result,
                    ),
                )
    except HTTPException:
        raise
    except AssignmentLeaveResolutionDomainError:
        raise
    except ValueError as exc:
        status_code = _error_status_for_apply_error(exc)
        raise HTTPException(
            status_code=status_code,
            detail=_error_payload(
                status_code=status_code,
                status_name="validation_error",
                reason=str(exc),
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_error_payload(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                status_name="internal_error",
                reason=str(exc),
            ),
        )
