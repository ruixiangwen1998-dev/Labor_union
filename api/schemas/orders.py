from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field
from datetime import date

class OrderFullUpdateRequest(BaseModel):
    client_name: Optional[str] = Field(None, description="客戶姓名")
    service_days: int = Field(20, ge=1, description="服務天數")
    service_hours_per_day: int = Field(9, ge=1, le=24, description="每日服務時數")
    floor_fee: float = Field(0.0, description="樓層費用")
    start_date: Optional[date] = Field(None, description="預計服務開始日")
    actual_start_date: Optional[date] = Field(None, description="實際服務開始日")
    end_date: Optional[date] = Field(None, description="服務結束日")
    deposit_date: Optional[date] = Field(None, description="訂金收取日")

class OrderStatusUpdateRequest(BaseModel):
    status: str = Field(..., description="訂單狀態: 洽談中/訂單成立/服務中/訂單完成/訂單取消")
    cancel_reason: Optional[str] = Field(None, description="當狀態為訂單取消時的取消原因")

class ScheduleCalculationRequest(BaseModel):
    actual_start_date: date = Field(..., description="實際服務開始日")
    target_service_days: int = Field(20, ge=1, description="目標服務天數")
    service_mode: str = Field("週休1日", description="排休模式: 週休1日/週休2日/連續服務")
    custom_holiday_rest_dates: Optional[List[date]] = Field(None, description="自訂放假日期列表")
    custom_leave_dates: Optional[List[date]] = Field(None, description="自訂請假日期列表")
    custom_rest_weekdays: Optional[List[int]] = Field(None, description="固定排休星期列表")
    monthly_salary_base: Optional[float] = Field(None, description="月薪試算基準")

class AssignmentRestDatesUpdateRequest(BaseModel):
    rest_dates: List[str] = Field(..., description="自訂排休與國定假日放假日期列表 (YYYY-MM-DD)")
