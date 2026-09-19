"""时间工具：所有时间戳统一为 UTC ISO8601 文本存储（可按字典序排序）。"""
from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


CATEGORY_ZH = {
    "feeding": "喂养",
    "sleep": "睡眠",
    "excretion": "排泄",
    "medication": "用药协助",
    "mood": "情绪观察",
    "note": "护理备注",
}
