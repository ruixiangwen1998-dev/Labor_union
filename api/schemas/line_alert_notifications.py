"""
================================================================================
檔案名稱: api/schemas/line_alert_notifications.py
功能說明: LINE 系統異常通知規則、通知對象與測試發送 API 的資料驗證模型
================================================================================
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


AlertSeverity = Literal["warning", "critical"]
AlertTargetType = Literal["user", "group"]


class LineAlertNotificationConfig(BaseModel):
    version: Literal[1] = 1
    enabled: bool = True
    minimum_severity: AlertSeverity = "critical"
    notify_recovery: bool = True
    repeat_after_minutes: int = Field(default=60, ge=0, le=10080)
    max_retries: int = Field(default=5, ge=1, le=10)
    retry_base_seconds: int = Field(default=30, ge=5, le=3600)
    components: dict[str, bool] = Field(default_factory=dict)


class AlertNotificationTargetCreate(BaseModel):
    target_type: AlertTargetType
    admin_user_id: int | None = Field(default=None, ge=1)
    line_target_id: str | None = Field(default=None, min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    minimum_severity: AlertSeverity = "critical"
    notify_recovery: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def validate_target_identity(self):
        if self.target_type == "user":
            if not self.admin_user_id or self.line_target_id:
                raise ValueError("個人通知必須選擇一個工會後台帳號")
        elif not self.line_target_id or self.admin_user_id:
            raise ValueError("群組通知必須提供 LINE 群組 ID")
        return self


class AlertNotificationTargetUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    minimum_severity: AlertSeverity = "critical"
    notify_recovery: bool = True
    enabled: bool = True


class AlertNotificationTestRequest(BaseModel):
    target_id: int = Field(ge=1)

