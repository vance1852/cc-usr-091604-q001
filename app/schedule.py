"""赛程领域的最小起点。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduleDraft:
    """保存一份尚未确认的赛程草稿。"""

    name: str


class ScheduleService:
    """提供赛程服务的基础状态。"""

    def health(self) -> dict[str, str]:
        return {"service": "schedule", "status": "ok"}

