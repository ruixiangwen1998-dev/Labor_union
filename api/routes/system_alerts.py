"""Non-finance "process reminder" alert endpoints, backed by system_alerts.

Mirrors api/routes/finance_alerts.py's shape, but system_alerts is a mutable
rolling-update table (see services/system_alert_service.py) rather than
finance_alerts' immutable event-sourced one, so there's no equivalent of the
append-only event history here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator

from api.schemas.base import BaseResponse
from services.anomaly_alert_detection import run_process_alert_scan
from services.db_service import get_connection
from services.system_alert_service import (
    claim_system_alert,
    get_system_alert,
    list_system_alerts,
    resolve_system_alert,
)


router = APIRouter(prefix="/api/v1/system-alerts", tags=["System Alerts"])
assert router.prefix == "/api/v1/system-alerts", "system alert route prefix must remain stable"


class ClaimSystemAlertRequest(BaseModel):
    operator: str = Field(..., min_length=1, max_length=100)

    @field_validator("operator")
    @classmethod
    def operator_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("operator is required")
        return value


class ResolveSystemAlertRequest(ClaimSystemAlertRequest):
    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason is required")
        return value


def _workflow_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if message == "alert_id does not exist":
        return HTTPException(status_code=404, detail=message)
    return HTTPException(status_code=400, detail=message)


@router.get("", response_model=BaseResponse[list[dict[str, Any]]])
def list_alerts(
    status: str | None = Query(default=None),
    alert_code: str | None = Query(default=None),
    source_domain: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            alerts = list_system_alerts(
                cursor,
                status=status,
                alert_code=alert_code,
                source_domain=source_domain,
                limit=limit,
                offset=offset,
            )
        return BaseResponse(data=alerts, message="System alerts retrieved")
    except ValueError as exc:
        raise _workflow_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/scan", response_model=BaseResponse[dict[str, Any]])
def scan_alerts():
    """Run every wired non-finance anomaly detector and roll-update system_alerts."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            summary = run_process_alert_scan(cursor)
        conn.commit()
        return BaseResponse(data=summary, message="Anomaly scan completed")
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/{alert_id}", response_model=BaseResponse[dict[str, Any]])
def get_alert(alert_id: int = Path(..., ge=1)):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            alert = get_system_alert(cursor, alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="alert_id does not exist")
        return BaseResponse(data=alert, message="System alert retrieved")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        conn.close()


def _run_action(action):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            result = action(cursor)
        if result["result"] == "conflict":
            raise HTTPException(status_code=409, detail="system alert workflow conflict")
        conn.commit()
        return BaseResponse(data=result, message="System alert workflow updated")
    except HTTPException:
        conn.rollback()
        raise
    except ValueError as exc:
        conn.rollback()
        raise _workflow_error(exc) from exc
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/{alert_id}/claim", response_model=BaseResponse[dict[str, Any]])
def claim_alert(
    request: ClaimSystemAlertRequest,
    alert_id: int = Path(..., ge=1),
):
    return _run_action(
        lambda cursor: claim_system_alert(cursor, alert_id=alert_id, operator=request.operator)
    )


@router.post("/{alert_id}/resolve", response_model=BaseResponse[dict[str, Any]])
def resolve_alert(
    request: ResolveSystemAlertRequest,
    alert_id: int = Path(..., ge=1),
):
    return _run_action(
        lambda cursor: resolve_system_alert(
            cursor,
            alert_id=alert_id,
            operator=request.operator,
            reason=request.reason,
        )
    )
