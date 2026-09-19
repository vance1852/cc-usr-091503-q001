"""API 请求模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SubjectType = Literal["mother", "baby"]
Category = Literal["feeding", "sleep", "excretion", "medication", "mood", "note"]
StaffRole = Literal["nurse", "lead_nurse"]


class StaffIn(BaseModel):
    name: str
    role: StaffRole = "nurse"


class MotherIn(BaseModel):
    name: str
    room: str | None = None
    notes: str | None = None


class BabyIn(BaseModel):
    mother_id: int
    name: str
    birth_date: str | None = None
    notes: str | None = None


class ShiftIn(BaseModel):
    staff_id: int
    start_at: datetime
    end_at: datetime
    label: str | None = None


class EventIn(BaseModel):
    """照护事件。occurred_at 为实际发生时间；录入时间由服务端落库，
    迟到补录（离线后同步）因此天然保留两个时间。"""

    client_request_id: str = Field(min_length=1, max_length=128)
    subject_type: SubjectType
    subject_id: int
    category: Category
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    is_important: bool = False
    requires_followup: bool = False
    abnormal: bool = False
    shareable: bool = False


class HandoverIn(BaseModel):
    shift_id: int
    incoming_staff_id: int


class CorrectionIn(BaseModel):
    event_id: int | None = None
    description: str = Field(min_length=1)


class CenterReviewIn(BaseModel):
    """中心确认；可同时调整是否允许对家属共享。"""

    shareable: bool | None = None
