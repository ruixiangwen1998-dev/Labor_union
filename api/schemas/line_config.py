"""
================================================================================
檔案名稱: api/schemas/line_config.py
功能說明: LINE 訊息、快捷分類、排程、雙頁下方選單與 LIFF 設定的資料格式及安全驗證規則
================================================================================
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator


class TemplateVariable(BaseModel):
    name: str = Field(min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    required: bool = True
    description: str = ""


class MessageTemplate(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=100)
    category: Literal[
        "webhook_reply", "push", "scheduled_push", "customer_service"
    ]
    message_type: Literal["text", "flex"] = "text"
    enabled: bool = True
    content: str | dict[str, Any]
    variables: list[TemplateVariable] = []
    usage: list[Literal["webhook", "push", "schedule", "customer_service"]] = []
    quick_menu_audience: Literal["customer", "staff", "group_help"] | None = None
    quick_menu_enabled: bool = False
    quick_menu_order: int = Field(default=100, ge=0, le=9999)

    @model_validator(mode="after")
    def validate_content_type(self):
        if self.message_type == "text" and not isinstance(self.content, str):
            raise ValueError("text template content must be a string")
        if self.message_type == "flex" and not isinstance(self.content, dict):
            raise ValueError("flex template content must be an object")
        if self.quick_menu_enabled and not self.quick_menu_audience:
            raise ValueError("quick menu template requires quick_menu_audience")
        return self


class MessageTemplatesConfig(BaseModel):
    version: int = Field(default=1, ge=1)
    templates: list[MessageTemplate]

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.templates]
        if len(ids) != len(set(ids)):
            raise ValueError("message template ids must be unique")
        return self


class MessageTemplatePreviewRequest(BaseModel):
    variables: dict[str, str] = {}


class MessageTemplateDraftPreviewRequest(BaseModel):
    template: MessageTemplate
    variables: dict[str, str] = {}


class MessageScheduleStep(BaseModel):
    day: int = Field(ge=0, le=365)
    send_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    template_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")


class MessageSchedule(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    trigger: Literal["follow"] = "follow"
    restart_on_refollow: bool = False
    steps: list[MessageScheduleStep] = Field(min_length=1)


class MessageSchedulesConfig(BaseModel):
    version: int = Field(default=1, ge=1)
    timezone: str = Field(min_length=1)
    schedules: list[MessageSchedule]

    @model_validator(mode="after")
    def unique_ids(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        ids = [item.id for item in self.schedules]
        if len(ids) != len(set(ids)):
            raise ValueError("message schedule ids must be unique")
        for schedule in self.schedules:
            days = [step.day for step in schedule.steps]
            if len(days) != len(set(days)):
                raise ValueError(f"schedule {schedule.id} contains duplicate days")
        return self


class MenuBounds(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class MenuAction(BaseModel):
    type: Literal["message", "uri", "postback", "richmenuswitch"]
    text: str | None = None
    uri: str | None = None
    uri_source: Literal["literal", "liff"] = "literal"
    data: str | None = None
    rich_menu_alias_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9_-]+$",
    )

    @model_validator(mode="after")
    def validate_action_value(self):
        if self.type == "message" and not self.text:
            raise ValueError("message action requires text")
        if self.type == "uri" and self.uri_source == "literal" and not self.uri:
            raise ValueError("literal uri action requires uri")
        if self.type == "uri" and self.uri_source == "literal" and self.uri:
            if urlparse(self.uri).scheme.lower() not in {"http", "https"}:
                raise ValueError("literal uri action only supports http or https")
        if self.type == "postback" and not self.data:
            raise ValueError("postback action requires data")
        if self.type == "richmenuswitch":
            if not self.rich_menu_alias_id:
                raise ValueError("rich menu switch action requires rich_menu_alias_id")
            if not self.data:
                raise ValueError("rich menu switch action requires data")
        return self


class RichMenuButton(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label: str = Field(min_length=1, max_length=30)
    text_color: str = "#FFFFFF"
    background_color: str = "#4A90E2"
    bounds: MenuBounds
    action: MenuAction


class RichMenuSize(BaseModel):
    width: Literal[2500] = 2500
    height: Literal[843, 1686] = 843


class RichMenuAppearance(BaseModel):
    background_color: str = "#F5F5F5"
    image_mode: Literal["generated", "uploaded"] = "generated"
    image_path: str | None = None
    image_asset_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_uploaded_image(self):
        if self.image_mode == "uploaded" and not (self.image_asset_id or self.image_path):
            raise ValueError("uploaded image mode requires an image asset")
        return self


class RichMenuDefinition(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=300)
    audience_role: Literal["customer", "staff", "union_staff"]
    enabled: bool = True
    selected: bool = True
    set_as_default: bool = False
    menu_group_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    rich_menu_alias_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9_-]+$",
    )
    is_group_entry: bool = False
    chat_bar_text: str = Field(min_length=1, max_length=14)
    size: RichMenuSize = RichMenuSize()
    appearance: RichMenuAppearance = RichMenuAppearance()
    buttons: list[RichMenuButton] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_buttons(self):
        ids = [button.id for button in self.buttons]
        if len(ids) != len(set(ids)):
            raise ValueError("rich menu button ids must be unique")
        for button in self.buttons:
            if button.bounds.x + button.bounds.width > self.size.width:
                raise ValueError(f"button {button.id} exceeds menu width")
            if button.bounds.y + button.bounds.height > self.size.height:
                raise ValueError(f"button {button.id} exceeds menu height")
        for index, button in enumerate(self.buttons):
            for other in self.buttons[index + 1 :]:
                separated = (
                    button.bounds.x + button.bounds.width <= other.bounds.x
                    or other.bounds.x + other.bounds.width <= button.bounds.x
                    or button.bounds.y + button.bounds.height <= other.bounds.y
                    or other.bounds.y + other.bounds.height <= button.bounds.y
                )
                if not separated:
                    raise ValueError(f"buttons {button.id} and {other.id} overlap")
        if self.set_as_default and self.audience_role != "customer":
            raise ValueError("only the customer menu can be the default menu")
        if self.menu_group_id and not self.rich_menu_alias_id:
            raise ValueError("grouped rich menu requires rich_menu_alias_id")
        if self.is_group_entry and not self.menu_group_id:
            raise ValueError("group entry rich menu requires menu_group_id")
        return self


class LineMenusConfig(BaseModel):
    version: int = Field(default=1, ge=1)
    menus: list[RichMenuDefinition]

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.menus]
        if len(ids) != len(set(ids)):
            raise ValueError("rich menu ids must be unique")
        aliases = [item.rich_menu_alias_id for item in self.menus if item.rich_menu_alias_id]
        if len(aliases) != len(set(aliases)):
            raise ValueError("rich menu alias ids must be unique")
        enabled = [item for item in self.menus if item.enabled]
        for role in {item.audience_role for item in enabled}:
            role_menus = [item for item in enabled if item.audience_role == role]
            if len(role_menus) == 1:
                continue
            group_ids = {item.menu_group_id for item in role_menus}
            if None in group_ids or len(group_ids) != 1:
                raise ValueError(
                    "multiple enabled rich menus for one role must belong to the same group"
                )
            entries = [item for item in role_menus if item.is_group_entry]
            if len(entries) != 1:
                raise ValueError("each enabled rich menu group requires exactly one entry menu")
        defaults = [item for item in enabled if item.set_as_default]
        if len(defaults) != 1:
            raise ValueError("exactly one enabled default rich menu is required")
        return self


class LiffOption(BaseModel):
    value: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)


class LiffNavigationAction(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]*$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    icon: str = Field(default="", max_length=16)
    path: str = Field(min_length=1, max_length=500)
    enabled: bool = True
    order: int = Field(ge=0)

    @model_validator(mode="after")
    def safe_path(self):
        parsed = urlparse(self.path)
        if self.path.startswith("/"):
            return self
        if parsed.scheme not in {"https"} or not parsed.netloc:
            raise ValueError("LIFF action path must be a relative path or HTTPS URL")
        return self


class LiffField(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]*$")
    label: str = Field(min_length=1, max_length=100)
    type: Literal[
        "text", "password", "textarea", "phone", "email", "date", "number",
        "single_choice", "multiple_choice", "boolean"
    ]
    required: bool = False
    enabled: bool = True
    order: int = Field(ge=0)
    placeholder: str = Field(default="", max_length=200)
    help_text: str = Field(default="", max_length=500)
    system_field: bool = False
    options: list[LiffOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def choices_require_options(self):
        if self.type in {"single_choice", "multiple_choice"} and not self.options:
            raise ValueError("choice field requires options")
        return self


class LiffPage(BaseModel):
    page_type: Literal["navigation", "bind", "registration", "admin_binding"]
    enabled: bool = True
    title: str = Field(min_length=1, max_length=200)
    subtitle: str = Field(default="", max_length=1000)
    submit_button: str = Field(default="送出", max_length=100)
    success_title: str = Field(default="送出成功", max_length=200)
    success_description: str = Field(default="", max_length=2000)
    loading_text: str = Field(default="資料傳送中，請稍候...", max_length=200)
    content: dict[str, str] = Field(default_factory=dict)
    actions: list[LiffNavigationAction] = Field(default_factory=list)
    fields: list[LiffField] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_fields(self):
        ids = [item.id for item in self.fields]
        if len(ids) != len(set(ids)):
            raise ValueError("LIFF field ids must be unique")
        action_ids = [item.id for item in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("LIFF action ids must be unique")
        if self.page_type == "navigation" and not self.actions:
            raise ValueError("navigation page requires at least one action")
        return self


class LiffTheme(BaseModel):
    primary_color: str = Field(default="#4A90E2", pattern=r"^#[0-9A-Fa-f]{6}$")
    primary_hover_color: str = Field(default="#357ABD", pattern=r"^#[0-9A-Fa-f]{6}$")
    background: str = Field(default="#EEF2F7", min_length=1, max_length=200)
    text_color: str = Field(default="#334E68", pattern=r"^#[0-9A-Fa-f]{6}$")
    muted_text_color: str = Field(default="#627D98", pattern=r"^#[0-9A-Fa-f]{6}$")
    font_family: str = Field(default="'Noto Sans TC', sans-serif", min_length=1, max_length=200)

    @model_validator(mode="after")
    def safe_css_values(self):
        forbidden = {";", "{", "}", "<", ">"}
        if any(char in self.background for char in forbidden):
            raise ValueError("background contains unsafe CSS characters")
        if any(char in self.font_family for char in forbidden):
            raise ValueError("font_family contains unsafe CSS characters")
        return self


class LiffSettingsConfig(BaseModel):
    version: int = Field(default=2, ge=2)
    theme: LiffTheme
    pages: dict[str, LiffPage]

    @model_validator(mode="after")
    def validate_page_contracts(self):
        required_pages = {
            "gateway": "navigation",
            "bind": "bind",
            "registration": "registration",
            "union_staff_binding": "admin_binding",
        }
        missing = sorted(set(required_pages) - set(self.pages))
        if missing:
            raise ValueError(f"missing required LIFF pages: {', '.join(missing)}")
        for page_id, page_type in required_pages.items():
            if self.pages[page_id].page_type != page_type:
                raise ValueError(f"{page_id} must use page_type={page_type}")

        required_fields = {
            "bind": {"name": "text", "phone": "phone"},
            "registration": {
                "name": "text",
                "phone": "phone",
                "expected_date": "date",
                "service_days": "number",
                "address": "text",
            },
            "union_staff_binding": {
                "username": "text",
                "password": "password",
            },
        }
        for page_id, contract in required_fields.items():
            fields = {field.id: field for field in self.pages[page_id].fields}
            for field_id, field_type in contract.items():
                field = fields.get(field_id)
                if not field:
                    raise ValueError(f"{page_id} is missing system field {field_id}")
                if field.type != field_type or not field.system_field:
                    raise ValueError(f"{page_id}.{field_id} violates the system field contract")
                if not field.enabled or not field.required:
                    raise ValueError(f"{page_id}.{field_id} must remain enabled and required")
        return self


class ServiceStatus(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label: str
    color: str


class BusinessHours(BaseModel):
    timezone: str = "Asia/Taipei"
    weekdays: dict[str, dict[str, str]]


class CustomerServiceSettings(BaseModel):
    business_hours: BusinessHours
    auto_assign: bool = False
    idle_timeout_minutes: int = Field(default=30, ge=1)


class CustomerServiceConfig(BaseModel):
    version: int = Field(default=1, ge=1)
    settings: CustomerServiceSettings
    statuses: list[ServiceStatus]
    default_messages: dict[str, str]
