"""赛程领域的基础对象与规则定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class MatchStatus(str, Enum):
    PROPOSED = "proposed"      # 草稿中
    CONFIRMED = "confirmed"    # 已确认
    POSTPONED = "postponed"    # 被延期（旧版本留痕）
    PLAYED = "played"          # 已开赛/完赛，锁定不可改
    UNSCHEDULED = "unscheduled"  # 引擎未能排出


class VersionKind(str, Enum):
    INITIAL = "initial"
    POSTPONE = "postpone"
    UNDO = "undo"


class NotificationStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"


class ScheduleError(Exception):
    """业务规则错误基类。"""


class NotFoundError(ScheduleError):
    pass


class ScheduleConflict(ScheduleError):
    """并发确认时版本已被他人推进。"""


class SchedulingImpossible(ScheduleError):
    """存在怎么排都放不下的比赛，details 给出每场的拒绝依据。"""

    def __init__(self, unscheduled: list["UnscheduledMatch"]):
        super().__init__(f"{len(unscheduled)} 场比赛没有可行时段")
        self.unscheduled = unscheduled


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    home_venue_id: str | None = None
    group_name: str | None = None  # 小组赛模式下所属小组


@dataclass(frozen=True)
class Venue:
    id: str
    name: str
    timezone: str  # IANA 名称，如 America/Los_Angeles


@dataclass(frozen=True)
class Slot:
    """场馆可用时段，时间为场馆当地的“墙上时钟”。

    end_minute 可以超过 1440，表示跨日时段（例如 23:00-01:30 => 1380-1530）。
    """

    id: int
    venue_id: str
    day: date
    start_minute: int
    end_minute: int


@dataclass(frozen=True)
class Blackout:
    scope: str        # 'league' 或 'venue'
    scope_id: str     # league 时为联赛 id，venue 时为场馆 id
    day: date
    reason: str = ""


@dataclass(frozen=True)
class Rules:
    """排程规则，全部可由管理员配置。"""

    game_minutes: int = 90
    rest_minutes: int = 18 * 60          # 同一球队两场球之间的最短休息
    max_consecutive_away: int = 2        # 最多连续客场数
    tz_threshold_hours: int = 2          # 主客场馆时差达到该值算跨时区
    tz_extra_rest_minutes: int = 12 * 60  # 跨时区比赛额外休息
    require_home_venue: bool = True      # 有主场馆的球队是否必须在主场馆作赛
    max_search_nodes: int = 500_000      # 回溯节点上限（防御性）


@dataclass(frozen=True)
class GameRequest:
    """待排的一场球：主客队与轮次已确定，时间场馆待定。"""

    id: str
    round_no: int
    home_id: str
    away_id: str
    group_name: str | None = None
    not_before_utc: datetime | None = None  # 延期重排时不得早于该时刻
    # 延期重排时已作废的候选 (venue_id, start_utc)，不得再放回原时段
    forbidden: frozenset = frozenset()


@dataclass(frozen=True)
class Candidate:
    """一个具体可放球的 UTC 时间段。"""

    slot_id: int
    venue_id: str
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class Rejection:
    """候选时段被拒绝的可解释依据。"""

    slot_id: int
    venue_id: str
    start_utc: datetime
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlannedMatch:
    id: str
    round_no: int
    home_id: str
    away_id: str
    venue_id: str
    start_utc: datetime
    end_utc: datetime
    group_name: str | None = None


@dataclass(frozen=True)
class UnscheduledMatch:
    request: GameRequest
    rejections: list[Rejection]


@dataclass(frozen=True)
class PlanResult:
    scheduled: list[PlannedMatch]
    decisions: dict[str, list[Rejection]]  # 每场球尝试过但拒绝的候选
    unscheduled: list[UnscheduledMatch]
    locked: list[PlannedMatch] = field(default_factory=list)


# 拒绝原因代码 -> 中文解释，运营端直接展示
REASON_TEXT = {
    "venue_overlap": "该场馆此时段已有比赛，场地冲突",
    "team_overlap": "球队此时已有其他比赛，时间冲突",
    "rest": "少于球队最短休息时间",
    "consecutive_away": "会超过允许的连续客场上限",
    "blackout": "落在联赛/场馆不可比赛日期",
    "cross_timezone_rest": "跨时区旅行需要额外休息时间",
    "not_home_venue": "主队有主场馆，比赛必须安排在主队主场",
    "before_floor": "早于允许的最早开赛时间（轮次顺序或延期下限）",
    "forbidden_slot": "原时段已因本次延期作废，不得再排入",
    "search_budget": "可选组合过多且始终无法同时满足约束，已停止搜索",
    "slot_too_short": "可用时段长度不足以完成比赛",
    "no_feasible_completion": "选此候选会导致后续轮次无处可排（回溯已放弃）",
}
