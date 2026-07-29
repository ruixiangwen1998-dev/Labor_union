"""
================================================================================
檔案名稱: api/main.py
功能說明: FastAPI 主程序，統一掛載 LINE、LIFF、管理介面與其他後端 API，並管理 LINE Worker 生命週期
================================================================================
"""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import (
    admin_auth,
    assignment_schedule_rest_dates,
    client_payments,
    clients,
    contracts,
    data_browser_admin,
    finance_alerts,
    finance_reports,
    holidays,
    line_admin,
    line_monitoring,
    line_rich_menus,
    line_reviews,
    line_staff_verification,
    line_system_config,
    line_tasks,
    match_records,
    matches,
    multi_caregiver_case_assignments,
    multi_caregiver_schedule,
    multi_caregiver_schedule_read,
    order_schedule_calculation,
    orders,
    schedule,
    staff,
    staff_monthly_schedule,
    staff_payments,
)

from api.schemas.base import BaseResponse
from line.line_bot import router as line_router
from line.worker import start_worker, stop_worker
from services.admin_auth_service import record_admin_audit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _allowed_origins() -> list[str]:
    configured = os.getenv("ALLOWED_ORIGINS", "").strip()
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return ["http://localhost:8501", "http://127.0.0.1:8501"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    worker_task = start_worker()
    try:
        yield
    finally:
        await stop_worker(worker_task)


app = FastAPI(
    title="Labor Union Webhook & API",
    description="LINE, LIFF, BreezySign and labor union administration API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="line/static"), name="static")

# LINE/LIFF/webhook endpoints are a child router of this central application.
app.include_router(line_router)
app.include_router(admin_auth.router)
app.include_router(line_admin.router)
app.include_router(line_monitoring.router)
app.include_router(line_tasks.router)
app.include_router(line_rich_menus.router)
app.include_router(line_reviews.router)
app.include_router(line_staff_verification.router)

# Existing administration API routers.
app.include_router(orders.router)
app.include_router(order_schedule_calculation.router)
app.include_router(assignment_schedule_rest_dates.router)
app.include_router(matches.router)
app.include_router(match_records.router)
app.include_router(schedule.router)
app.include_router(multi_caregiver_case_assignments.router)
app.include_router(multi_caregiver_schedule.router)
app.include_router(multi_caregiver_schedule_read.router)
app.include_router(clients.router)
app.include_router(staff.router)
app.include_router(staff_monthly_schedule.router)
app.include_router(holidays.router)
app.include_router(line_system_config.router)
app.include_router(line_system_config.public_router)
app.include_router(client_payments.router)
app.include_router(staff_payments.router)
app.include_router(contracts.router)
app.include_router(finance_reports.router)
app.include_router(finance_alerts.router)
app.include_router(data_browser_admin.router)





@app.middleware("http")
async def audit_authenticated_mutations(request: Request, call_next):
    """Persist authenticated management changes without storing request secrets."""
    response = await call_next(request)
    principal = getattr(request.state, "admin_principal", None)
    is_preview = request.url.path.endswith("/preview")
    if principal and request.method in {"POST", "PUT", "PATCH", "DELETE"} and not is_preview:
        try:
            await asyncio.to_thread(
                record_admin_audit,
                principal=principal,
                action=getattr(request.state, "audit_action", "api.mutation"),
                request_path=request.url.path,
                http_method=request.method,
                result_status=response.status_code,
                ip_address=request.client.host if request.client else None,
                resource_type=getattr(request.state, "audit_resource_type", None),
                resource_id=getattr(request.state, "audit_resource_id", None),
                details=getattr(request.state, "audit_details", None),
            )
        except Exception as exc:
            print(f"[Admin Audit] Failed to record request: {exc}")
    return response


@app.middleware("http")
async def audit_authenticated_mutations(request: Request, call_next):
    """Persist authenticated management changes without storing request secrets."""
    response = await call_next(request)
    principal = getattr(request.state, "admin_principal", None)
    is_preview = request.url.path.endswith("/preview")
    if principal and request.method in {"POST", "PUT", "PATCH", "DELETE"} and not is_preview:
        try:
            await asyncio.to_thread(
                record_admin_audit,
                principal=principal,
                action=getattr(request.state, "audit_action", "api.mutation"),
                request_path=request.url.path,
                http_method=request.method,
                result_status=response.status_code,
                ip_address=request.client.host if request.client else None,
                resource_type=getattr(request.state, "audit_resource_type", None),
                resource_id=getattr(request.state, "audit_resource_id", None),
                details=getattr(request.state, "audit_details", None),
            )
        except Exception as exc:
            print(f"[Admin Audit] Failed to record request: {exc}")
    return response


@app.get("/health", response_model=BaseResponse[dict], tags=["Health"])
def api_health_check():
    return BaseResponse(
        data={"status": "healthy", "service": "Labor Union API"},
        message="API Server is running normally",
    )
