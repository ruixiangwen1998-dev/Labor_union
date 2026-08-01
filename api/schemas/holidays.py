from pydantic import BaseModel, Field
from datetime import date

class HolidayCreateRequest(BaseModel):
    holiday_date: date = Field(..., description="假日日期")
    holiday_name: str = Field(..., description="假日名稱")
    is_double_pay_default: bool = Field(
        False,
        description="相容欄位；排班不會因國定假日自動套用雙倍薪資",
    )
