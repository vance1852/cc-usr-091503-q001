"""请求体的 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

SubjectType = Literal["mother", "baby"]
Category = Literal["feeding", "sleep", "excretion", "medication", "mood", "note"]


class StaffIn(BaseModel):
    name: str = Field(min_length=1)
    role: Literal["nurse", "leader"] = "nurse"


class MotherIn(BaseModel):
    name: str = Field(min_length=1)
    room: Optional[str] = None
    notes: Optional[str] = None


class BabyIn(BaseModel):
    mother_id: int
    name: str = Field(min_length=1)
    sex: Optional[str] = None
    birth_at: Optional[datetime] = None
    notes: Optional[str] = None


class ShiftIn(BaseModel):
    staff_id: int
    started_at: Optional[datetime] = None  # 缺省为当前时间


class ShiftEndIn(BaseModel):
    ended_at: Optional[datetime] = None  # 缺省为当前时间


class EventIn(BaseModel):
    """照护事件。idempotency_key 由客户端生成，重复提交不会产生第二条记录。"""

    idempotency_key: str = Field(min_length=1, max_length=128)
    subject_type: SubjectType
    subject_id: int
    category: Category
    occurred_at: datetime  # 实际发生时间；录入时间由服务端记录
    payload: dict = Field(default_factory=dict)
    needs_followup: bool = False  # 需要下一班持续关注
    is_important: bool = False    # 重要提醒：被明确签收前不消失
    is_abnormal: bool = False     # 异常变化：进入组长视图


class HandoverIn(BaseModel):
    shift_id: int
    incoming_staff_id: int


class ManualItemIn(BaseModel):
    description: str = Field(min_length=1)


class CorrectionIn(BaseModel):
    """对已签署时段内事件的更正：amend 修改内容，void 作废。"""

    action: Literal["amend", "void"]
    reason: str = Field(min_length=1)
    new_payload: Optional[dict] = None


class MissedEventIn(BaseModel):
    """已签署交接的漏录补登：以 add 更正事件的形式进入时间线。"""

    reason: str = Field(min_length=1)
    event: EventIn


class FamilyReviewIn(BaseModel):
    shareable: bool
