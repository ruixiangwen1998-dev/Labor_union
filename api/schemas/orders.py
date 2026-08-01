from typing import Optional, List, Any, Dict, Literal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from datetime import date

class OrderFullUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: Optional[str] = Field(None, description="客戶姓名")

class OrderStatusUpdateRequest(BaseModel):
    status: str = Field(..., description="訂單狀態: 洽談中/訂單成立/服務中/訂單完成/訂單取消")
    cancel_reason: Optional[str] = Field(None, description="當狀態為訂單取消時的取消原因")


class OrderLockCancellationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str = Field(..., description="本次取消冪等鍵")
    actor: str = Field(..., description="操作人員識別")
    cancel_reason: str = Field(..., description="取消原因")

    @field_validator("event_key", "actor", "cancel_reason")
    @classmethod
    def normalize_required_text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("must be a non-empty string")
        return normalized


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


class AssignmentLeaveResolutionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_no: str = Field(..., description="案件編號")
    original_assignment_id: StrictInt = Field(..., gt=0, description="原始指派 ID")
    original_schedule_id: StrictInt = Field(..., gt=0, description="原始單日排班 ID")
    work_date: date = Field(..., description="休假或代班日期")
    resolution_type: Literal["defer_following_assignments", "substitute"] = Field(
        ..., description="處理方式：順延後續指派或建立單日代班"
    )
    substitute_staff_id: Optional[StrictInt] = Field(
        None, gt=0, description="代班月嫂 ID；僅代班處理時必填"
    )

    @field_validator("case_no")
    @classmethod
    def normalize_case_no(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("must be a non-empty string")
        return normalized

    @model_validator(mode="after")
    def validate_substitute_staff(self):
        if self.resolution_type == "substitute" and self.substitute_staff_id is None:
            raise ValueError("substitute_staff_id is required for substitute resolution")
        if (
            self.resolution_type == "defer_following_assignments"
            and self.substitute_staff_id is not None
        ):
            raise ValueError(
                "substitute_staff_id is not allowed for defer_following_assignments"
            )
        return self


class AssignmentLeaveResolutionApplyRequest(
    AssignmentLeaveResolutionPreviewRequest
):
    preview_fingerprint: str = Field(
        ..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    event_key: str = Field(..., description="本次套用操作的冪等鍵")
    actor: str = Field(..., description="操作人員識別")
    reason: str = Field(..., description="休假、順延或代班原因")

    @field_validator("event_key", "actor", "reason")
    @classmethod
    def normalize_apply_text(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("must be a non-empty string")
        return normalized


class AssignmentLeaveResolutionBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_schedule_id: StrictInt = Field(..., gt=0)
    work_date: date
    resolution_type: Literal["defer_following_assignments", "substitute"]
    substitute_staff_id: Optional[StrictInt] = Field(None, gt=0)
    is_double_pay: StrictBool = Field(
        False,
        description="代班日是否套用雙倍薪；代班預設否，順延時必須為否",
    )

    @model_validator(mode="after")
    def validate_substitute_staff(self):
        if self.resolution_type == "substitute" and self.substitute_staff_id is None:
            raise ValueError("substitute_staff_id is required for substitute resolution")
        if (
            self.resolution_type == "defer_following_assignments"
            and self.substitute_staff_id is not None
        ):
            raise ValueError(
                "substitute_staff_id is not allowed for defer_following_assignments"
            )
        if self.resolution_type == "defer_following_assignments" and self.is_double_pay:
            raise ValueError("is_double_pay is not allowed for defer_following_assignments")
        return self


class AssignmentLeaveResolutionBatchPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[
        "assignment-leave-substitution-batch-preview/v1"
    ]
    case_no: str = Field(..., min_length=1, max_length=50)
    original_assignment_id: StrictInt = Field(..., gt=0)
    items: List[AssignmentLeaveResolutionBatchItem] = Field(..., min_length=1)

    @field_validator("case_no")
    @classmethod
    def normalize_case_no(cls, value: Any) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must be a non-empty string")
        return normalized

    @model_validator(mode="after")
    def validate_unique_items(self):
        schedule_ids = [item.original_schedule_id for item in self.items]
        work_dates = [item.work_date for item in self.items]
        if len(schedule_ids) != len(set(schedule_ids)):
            raise ValueError("original_schedule_id must be unique")
        if len(work_dates) != len(set(work_dates)):
            raise ValueError("work_date must be unique")
        return self


class AssignmentLeaveResolutionBatchApplyRequest(
    AssignmentLeaveResolutionBatchPreviewRequest
):
    contract_version: Literal[
        "assignment-leave-substitution-batch-apply/v1"
    ]
    preview_fingerprint: str = Field(
        ..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    batch_key: str = Field(..., min_length=1, max_length=100)
    actor: str = Field(..., min_length=1, max_length=100)
    reason: str = Field(..., min_length=1, max_length=255)

    @field_validator("batch_key", "actor", "reason")
    @classmethod
    def normalize_batch_apply_text(cls, value: Any) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must be a non-empty string")
        return normalized
