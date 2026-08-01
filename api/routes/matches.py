"""
================================================================================
檔案名稱: api/routes/matches.py
功能說明: 訂單媒合 API，管理月嫂推薦、意願回覆、訂單資訊通知、履歷傳送與定案指派
================================================================================
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Dict, List, Literal
from services.caregiver_matching_plan_service import create_matching_plan_version
from services.caregiver_matching_communication_service import (
    cancel_matching_plan,
    get_active_matching_plan_state,
    get_matching_plan_contact_state,
    record_matching_plan_willingness,
    send_matching_plan_information,
    send_matching_plan_resumes,
)
from api.dependencies.admin_auth import require_system_admin
from api.schemas.base import BaseResponse
from api.schemas.matches import MatchReplyRequest, MatchAssignRequest, MatchCreateRequest
from services.admin_auth_service import AdminPrincipal
from services.legacy_caregiver_matching_service import (
    assign_legacy_staff_to_order,
    recommend_legacy_staff,
    record_legacy_matching_reply,
    send_legacy_matching_information,
    send_legacy_resume_for_case,
    send_legacy_resume_to_client,
)

router = APIRouter(prefix="/api/v1", tags=["Matches 案件配對與 LINE 訊息推播"])


class MatchingPlanEventIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str = Field(..., min_length=1, max_length=100)
    actor: str = Field(..., min_length=1, max_length=100)


class MatchingPlanInformationRequest(MatchingPlanEventIdentity):
    info_type: Literal[1, 2]


class MatchingPlanWillingnessRequest(MatchingPlanEventIdentity):
    willingness: Literal["pending", "willing", "unwilling"]


class MatchingPlanResumeRequest(MatchingPlanEventIdentity):
    note: str = Field(..., min_length=1, max_length=1000)


class MatchingPlanCancellationRequest(MatchingPlanEventIdentity):
    reason: str = Field(..., min_length=1, max_length=255)


def _require_matching_actor(principal: AdminPrincipal, actor: str) -> None:
    if str(principal.username or "").strip() != actor.strip():
        raise HTTPException(status_code=403, detail="actor does not match authenticated principal")


@router.get(
    "/orders/{case_no}/matching-plans/{plan_id}/contact-state",
    response_model=BaseResponse[Dict[str, Any]],
)
def get_matching_plan_contact_state_route(
    case_no: str,
    plan_id: int,
    principal: AdminPrincipal = Depends(require_system_admin),
):
    del principal
    try:
        return BaseResponse(
            data=get_matching_plan_contact_state(case_no, plan_id),
            message="成功讀取配對聯繫與意願狀態",
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get(
    "/orders/{case_no}/matching-plans/active",
    response_model=BaseResponse[Dict[str, Any]],
)
def get_active_matching_plan_state_route(
    case_no: str,
    principal: AdminPrincipal = Depends(require_system_admin),
):
    del principal
    try:
        return BaseResponse(
            data=get_active_matching_plan_state(case_no),
            message="成功讀取目前有效配對方案",
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/orders/{case_no}/matching-plans/{plan_id}/segments/{segment_id}/information",
    response_model=BaseResponse[Dict[str, Any]],
)
def send_matching_plan_information_route(
    req: MatchingPlanInformationRequest,
    case_no: str,
    plan_id: int,
    segment_id: int,
    principal: AdminPrincipal = Depends(require_system_admin),
):
    _require_matching_actor(principal, req.actor)
    try:
        return BaseResponse(
            data=send_matching_plan_information(
                case_no, plan_id, segment_id, req.info_type, req.event_key, req.actor
            ),
            message=f"訂單資訊-{req.info_type} 已建立可靠發送任務",
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put(
    "/orders/{case_no}/matching-plans/{plan_id}/segments/{segment_id}/willingness",
    response_model=BaseResponse[Dict[str, Any]],
)
def record_matching_plan_willingness_route(
    req: MatchingPlanWillingnessRequest,
    case_no: str,
    plan_id: int,
    segment_id: int,
    principal: AdminPrincipal = Depends(require_system_admin),
):
    _require_matching_actor(principal, req.actor)
    try:
        return BaseResponse(
            data=record_matching_plan_willingness(
                case_no,
                plan_id,
                segment_id,
                req.willingness,
                req.event_key,
                req.actor,
            ),
            message="成功更新月嫂意願",
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/orders/{case_no}/matching-plans/{plan_id}/resumes",
    response_model=BaseResponse[Dict[str, Any]],
)
def send_matching_plan_resumes_route(
    req: MatchingPlanResumeRequest,
    case_no: str,
    plan_id: int,
    principal: AdminPrincipal = Depends(require_system_admin),
):
    _require_matching_actor(principal, req.actor)
    try:
        return BaseResponse(
            data=send_matching_plan_resumes(
                case_no, plan_id, req.note, req.event_key, req.actor
            ),
            message="已逐位建立履歷與備註的可靠發送任務",
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/orders/{case_no}/matching-plans/{plan_id}/cancel",
    response_model=BaseResponse[Dict[str, Any]],
)
def cancel_matching_plan_route(
    req: MatchingPlanCancellationRequest,
    case_no: str,
    plan_id: int,
    principal: AdminPrincipal = Depends(require_system_admin),
):
    _require_matching_actor(principal, req.actor)
    try:
        return BaseResponse(
            data=cancel_matching_plan(
                case_no, plan_id, req.event_key, req.actor, req.reason
            ),
            message="已取消目前配對組合並保留歷史",
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/orders/{case_no}/matching-plans",
    response_model=BaseResponse[Dict[str, Any]],
)
def create_matching_plan_version_route(
    case_no: str = Path(..., description="案件編號"),
    segments: List[Dict[str, Any]] = Body(...),
    created_by: str = Body(...),
    as_of: str = Body(...),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """建立或冪等取得正式多月嫂配對計畫版本。"""
    _require_matching_actor(principal, created_by)
    try:
        result = create_matching_plan_version(
            case_no=case_no,
            segments=segments,
            created_by=str(principal.username or "").strip(),
            as_of=as_of,
        )
        return BaseResponse(data=result, message="成功建立多月嫂配對計畫版本")
    except ValueError as error:
        message = str(error)
        if message == "case not found":
            status_code = 404
        elif message in {
            "case is not in negotiation stage",
            "case is not editable while an accepted plan exists",
            "case has an active availability lock",
        }:
            status_code = 409
        else:
            status_code = 422
        raise HTTPException(status_code=status_code, detail=message) from error
    except Exception:
        raise HTTPException(status_code=500, detail="建立多月嫂配對計畫版本失敗")


@router.get("/matches/recommend-staff", response_model=BaseResponse[list[dict]])


def recommend_staff(
    case_no: str,
    filter_region: bool = True,
    filter_schedule: bool = True,
    filter_babies: bool = True,
    filter_time: bool = True,
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """智慧粗篩比對月嫂推薦引擎 API (比對 clients.city/address 與檔期 7 天預留備用期)"""
    del principal
    try:
        data = recommend_legacy_staff(
            case_no=case_no,
            filter_region=filter_region,
            filter_schedule=filter_schedule,
            filter_babies=filter_babies,
            filter_time=filter_time
        )
        return BaseResponse(data=data, message="成功計算月嫂智慧粗篩推薦名單")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/matches/{match_id}/send-info-1", response_model=BaseResponse[Dict[str, Any]])
def send_info_1(
    match_id: int = Path(..., description="配對紀錄 ID"),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """發送訂單資訊-1 (粗篩卡片)。若月嫂綁定 staff.line_user_id，同步進行 LINE 實體推播"""
    del principal
    try:
        result = send_legacy_matching_information(match_id, 1)
        return BaseResponse(data=result["data"], message=result["message"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/matches/{match_id}/send-info-2", response_model=BaseResponse[Dict[str, Any]])
def send_info_2(
    match_id: int = Path(..., description="配對紀錄 ID"),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """發送訂單資訊-2 (精篩照護圖譜)。若月嫂綁定 staff.line_user_id，同步進行 LINE 實體推播"""
    del principal
    try:
        result = send_legacy_matching_information(match_id, 2)
        return BaseResponse(data=result["data"], message=result["message"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/matches/{match_id}/reply", response_model=BaseResponse[bool])
def reply_matching_inquiry(
    req: MatchReplyRequest,
    match_id: int = Path(..., description="配對紀錄 ID"),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """更新月嫂意願回覆狀態 (1: 願意, 0: 拒絕, NULL: 待回覆)"""
    del principal
    try:
        success = record_legacy_matching_reply(match_id, req.accepted)
        return BaseResponse(data=success, message="成功更新月嫂接案意願狀態")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/matches/{match_id}/send-resume", response_model=BaseResponse[bool])
def send_resume_to_client(
    match_id: int = Path(..., description="配對紀錄 ID"),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """傳送去識別化月嫂履歷圖卡給客戶 LINE 帳號"""
    del principal
    try:
        return BaseResponse(
            data=send_legacy_resume_to_client(match_id),
            message="已成功將去識別化月嫂履歷傳送給客戶 LINE 帳號",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/orders/{case_no}/send-resume", response_model=BaseResponse[Dict[str, Any]])
def send_resume_for_case(
    case_no: str = Path(..., description="案件編號"),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """找出該案件中已被接受但履歷尚未發送的候選人，發送履歷並記錄時間 (供異常警示中心一鍵使用)。"""
    del principal
    try:
        match_id = send_legacy_resume_for_case(case_no)
        if match_id is None:
            raise HTTPException(status_code=404, detail="找不到已接受媒合且履歷尚未發送的候選人")
        return BaseResponse(data={"match_id": match_id}, message="履歷已發送並記錄")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/orders/{case_no}/assign-staff", response_model=BaseResponse[bool])
def assign_staff_to_order(
    req: MatchAssignRequest,
    case_no: str = Path(..., description="案件編號"),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """成立訂單並定案指派服務人員/月嫂"""
    del principal
    try:
        success = assign_legacy_staff_to_order(case_no=case_no, staff_id=req.staff_id)
        return BaseResponse(data=success, message="成功定案指派月嫂，訂單狀態升級為訂單成立！")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
