"""青少年联赛赛程编排服务。

领域概览
========

- :class:`Team` / :class:`Venue` / :class:`VenueSession` / :class:`RoundSpec`
  由管理员录入：球队（含主场与家长联系人）、场馆（含时区与不可比赛日期）、
  场馆可用时段（本地墙钟、允许跨午夜）、轮次（主客场双循环或小组单循环）。
- :class:`ScheduleService` 提供预览（plan/preview）、确认（confirm）、
  撤销（undo）、延期（postpone，幂等）、按球队查询（team_schedule）、
  一轮总览（round_view）等接口。
- 排程约束：场地不冲突、球队不撞场、最短休息时间、连续客场上限、
  跨时区场馆加休、全局/场馆不可比赛日期、跨日时段。
- 所有调整记录原因、操作者、受影响场次；确认后生成待通知对象（裁判/家长），
  通知记录随版本保留，延期不影响已开赛场次及其历史通知。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# 常量与异常
# ---------------------------------------------------------------------------

STATUS_PREVIEW = "PREVIEW"        # 草稿，尚未确认
STATUS_SCHEDULED = "SCHEDULED"    # 已确认
STATUS_POSTPONED = "POSTPONED"    # 被延期指令替换（历史版本保留）
STATUS_STARTED = "STARTED"        # 已开赛，锁定
STATUS_CANCELLED = "CANCELLED"    # 撤销后作废（历史中保留）
STATUS_SUPERSEDED = "SUPERSEDED"  # 旧版本未发出的通知，已被新版本取代


class ScheduleError(Exception):
    """赛程服务领域错误基类。"""


class NotFoundError(ScheduleError):
    """引用的实体不存在。"""


class PlanStateError(ScheduleError):
    """计划状态不允许该操作（如重复确认）。"""


# ---------------------------------------------------------------------------
# 录入对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleDraft:
    """保存一份尚未确认的赛程草稿（兼容早期接口）。"""

    name: str


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    home_venue_id: str | None = None
    # 家长通知对象（邮箱/手机号等不透明标识）
    contacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Venue:
    id: str
    name: str
    timezone: str  # IANA 时区名，如 "Asia/Shanghai"
    # 该场馆不可比赛的本地日期
    unavailable_dates: frozenset[date] = frozenset()


@dataclass(frozen=True)
class VenueSession:
    """场馆可用时段，按场馆本地墙钟表达，允许跨午夜。

    ``end_minute`` 可以大于 1440，例如 23:00-次日 01:00 表示为
    start_minute=1380, end_minute=1500。
    """

    venue_id: str
    local_date: date
    start_minute: int
    end_minute: int


@dataclass(frozen=True)
class RoundSpec:
    """轮次定义。

    大组主客场循环：``groups`` 只放一个组、``double_leg=True``。
    小组循环赛：``groups`` 放多个小组、``double_leg=False``。
    """

    id: str
    name: str
    groups: tuple[tuple[str, ...], ...]
    double_leg: bool = False
    match_minutes: int = 60


@dataclass(frozen=True)
class SchedulingSettings:
    min_rest_minutes: int = 660              # 同队两场比赛最短休息
    max_consecutive_away: int = 2            # 连续客场上限
    cross_tz_extra_rest_minutes: int = 240   # 跨时区场馆追加休息
    kickoff_step_minutes: int = 15           # 开球时间网格


# ---------------------------------------------------------------------------
# 排程结果对象
# ---------------------------------------------------------------------------


@dataclass
class Fixture:
    """对阵（主客队确定，时间/场地未排定）。

    ``slot`` 是循环赛的轮次槽位（贝格尔轮转的第几个比赛日），
    同一 slot 内每队至多出现一次，slot 按时间顺序排布。
    """

    id: str
    round_id: str
    group_index: int
    home_team_id: str
    away_team_id: str
    slot: int = 0

    def involves(self, team_id: str) -> bool:
        return team_id in (self.home_team_id, self.away_team_id)

    def is_away(self, team_id: str) -> bool:
        return team_id == self.away_team_id


@dataclass
class Match:
    """一场已排定（或尝试排定）的比赛，带版本号与解释依据。"""

    id: str
    round_id: str
    home_team_id: str
    away_team_id: str
    venue_id: str
    start_utc: datetime
    end_utc: datetime
    version: int
    status: str = STATUS_PREVIEW
    reasons: list[str] = field(default_factory=list)
    referee: str | None = None

    def involves(self, team_id: str) -> bool:
        return team_id in (self.home_team_id, self.away_team_id)

    def is_away(self, team_id: str) -> bool:
        return team_id == self.away_team_id

    def opponent(self, team_id: str) -> str:
        if team_id == self.home_team_id:
            return self.away_team_id
        if team_id == self.away_team_id:
            return self.home_team_id
        raise NotFoundError(f"球队 {team_id} 不在比赛 {self.id} 中")


@dataclass
class UnscheduledFixture:
    fixture_id: str
    round_id: str
    home_team_id: str
    away_team_id: str
    blockers: list[str]


@dataclass
class Notification:
    id: str
    match_id: str
    version: int
    recipient: str
    kind: str  # "referee" | "parent"
    status: str = "pending"  # "pending" | "sent"
    created_at: datetime | None = None
    sent_at: datetime | None = None


# ---------------------------------------------------------------------------
# 对阵生成（贝格尔轮转法，主客平衡，确定可复现）
# ---------------------------------------------------------------------------


def _round_robin_fixtures(spec: RoundSpec) -> list[Fixture]:
    """生成循环赛对阵。

    采用固定枢轴轮转得到每个 slot（比赛日）的完美匹配，再逐场贪心
    决定主客方向，使每队主、客次数差不超过 1；双循环第二回合主客对调。
    整个过程确定，可复现。
    """
    fixtures: list[Fixture] = []
    for group_index, group in enumerate(spec.groups):
        teams = sorted(group)
        if len(teams) < 2:
            continue
        rotating = list(teams)
        if len(rotating) % 2 == 1:
            rotating = rotating + [None]  # 奇数队插入 bye
        n = len(rotating)
        legs: list[tuple[int, str, str]] = []  # (slot, home, away)
        home_count = {t: 0 for t in teams}
        for slot in range(n - 1):
            edges = [(rotating[i], rotating[n - 1 - i])
                     for i in range(n // 2)]
            # 同 slot 内对阵不共享球队，按 id 排序后逐场平衡主客
            for x, y in sorted(edges, key=lambda e: tuple(
                    t if t is not None else "~" for t in e)):
                if x is None or y is None:
                    continue
                home, away = (x, y) if (
                    home_count[x], x) <= (home_count[y], y) else (y, x)
                home_count[home] += 1
                legs.append((slot, home, away))
            # 固定首位，其余顺时针轮转
            rotating = [rotating[0], rotating[-1], *rotating[1:-1]]
        if spec.double_leg:
            # 第二回合：同序 slot 偏移、主客对调
            legs = legs + [(slot + (n - 1), away, home)
                           for slot, home, away in legs]
        for slot, home, away in legs:
            fixtures.append(
                Fixture(
                    id=f"{spec.id}:{home}__{away}",
                    round_id=spec.id,
                    group_index=group_index,
                    home_team_id=home,
                    away_team_id=away,
                    slot=slot,
                )
            )
    # 确定性排序
    fixtures.sort(key=lambda fx: (fx.slot, fx.group_index, fx.id))
    return fixtures


# ---------------------------------------------------------------------------
# 排程器
# ---------------------------------------------------------------------------


def _session_window(session: VenueSession, tzname: str) -> tuple[datetime, datetime]:
    """把本地时段（可跨午夜）换算成 UTC 半开区间。"""
    tz = ZoneInfo(tzname)
    d = session.local_date
    midnight = datetime(d.year, d.month, d.day, tzinfo=tz)
    start = midnight + timedelta(minutes=session.start_minute)
    end = midnight + timedelta(minutes=session.end_minute)
    if end <= start:
        raise ScheduleError(
            f"场馆 {session.venue_id} 在 {session.local_date} 的时段结束必须晚于开始"
        )
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _overlaps(start_a: datetime, end_a: datetime,
              start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


@dataclass
class _Placement:
    session: VenueSession
    venue_id: str
    start_utc: datetime
    end_utc: datetime


class _Scheduler:
    """贪心 + 全量约束校验的确定性排程器。

    候选开球时间按 (时间, 主场优先, 场馆 id) 排序；每一次跳过候选都记录
    约束原因，使最终排程可解释。输入相同则输出必然相同。
    """

    def __init__(self, venues: dict[str, Venue], sessions: list[VenueSession],
                 settings: SchedulingSettings, blackout_dates: set[date],
                 locked: list[Match], durations: dict[str, int]):
        self._venues = venues
        self._settings = settings
        self._locked = sorted(locked, key=lambda m: (m.start_utc, m.id))
        self._durations = durations
        self._blackout_dates = set(blackout_dates)
        self._all_sessions = list(sessions)
        self._team_home: dict[str, str | None] = {}
        self.by_venue: dict[str, list[Match]] = {}
        self.by_team: dict[str, list[Match]] = {}
        for m in self._locked:
            self.by_venue.setdefault(m.venue_id, []).append(m)
            for t in (m.home_team_id, m.away_team_id):
                self.by_team.setdefault(t, []).append(m)
        # 预计算合法时段（剔除不可比赛日期，含跨日结束日）
        self._placements: dict[str, list[_Placement]] = {}
        for session in sorted(sessions, key=lambda s: (s.local_date, s.venue_id,
                                                       s.start_minute)):
            venue = venues.get(session.venue_id)
            if venue is None:
                continue
            end_day_offset = session.end_minute // 1440
            end_local_date = session.local_date + timedelta(days=end_day_offset)
            if session.local_date in venue.unavailable_dates:
                continue
            if end_local_date in venue.unavailable_dates:
                continue
            if session.local_date in blackout_dates or end_local_date in blackout_dates:
                continue
            start_utc, end_utc = _session_window(session, venue.timezone)
            self._placements.setdefault(session.venue_id, []).append(
                _Placement(session, session.venue_id, start_utc, end_utc)
            )

    # -- 约束校验 ---------------------------------------------------------

    def _check_venue(self, venue_id: str, start: datetime, end: datetime,
                     self_id: str | None) -> str | None:
        for other in self.by_venue.get(venue_id, []):
            if other.id == self_id:
                continue
            if _overlaps(start, end, other.start_utc, other.end_utc):
                return f"场地冲突：与 {other.id}（{other.home_team_id} vs {other.away_team_id}）占用时段重叠"
        return None

    def _tz_extra(self, venue_a: str, when_a: datetime,
                  venue_b: str, when_b: datetime) -> int:
        va, vb = self._venues[venue_a], self._venues[venue_b]
        off_a = when_a.astimezone(ZoneInfo(va.timezone)).utcoffset()
        off_b = when_b.astimezone(ZoneInfo(vb.timezone)).utcoffset()
        if off_a != off_b:
            return self._settings.cross_tz_extra_rest_minutes
        return 0

    def _check_team(self, fixture: Fixture, team_id: str,
                    start: datetime, end: datetime,
                    venue_id: str) -> str | None:
        for other in self.by_team.get(team_id, []):
            if other.id == fixture.id:
                continue
            if _overlaps(start, end, other.start_utc, other.end_utc):
                return f"球队 {team_id} 同时段已有比赛 {other.id}"
            if end <= other.start_utc:
                gap = (other.start_utc - end).total_seconds() / 60
                required = self._settings.min_rest_minutes + self._tz_extra(
                    venue_id, start, other.venue_id, other.start_utc)
                when = f"（跨时区场馆 {venue_id}→{other.venue_id}，含加休）" if required > self._settings.min_rest_minutes else ""
                if gap < required:
                    return (f"球队 {team_id} 休息不足：距下一场 {other.id} 仅 {gap:.0f} 分钟，"
                            f"要求 ≥ {required} 分钟{when}")
            elif other.end_utc <= start:
                gap = (start - other.end_utc).total_seconds() / 60
                required = self._settings.min_rest_minutes + self._tz_extra(
                    other.venue_id, other.start_utc, venue_id, start)
                when = f"（跨时区场馆 {other.venue_id}→{venue_id}，含加休）" if required > self._settings.min_rest_minutes else ""
                if gap < required:
                    return (f"球队 {team_id} 休息不足：距上一场 {other.id} 仅 {gap:.0f} 分钟，"
                            f"要求 ≥ {required} 分钟{when}")
        return None

    def _away_chain_violation(self, fixture: Fixture, start: datetime) -> str | None:
        """模拟插入后，客队含此场在内的连续客场链不得超过上限。

        连续指时间上相邻的比赛之间没有该队的主场隔开（跨 slot 也成立）。
        """
        cap = self._settings.max_consecutive_away
        for team_id in (fixture.home_team_id, fixture.away_team_id):
            if not fixture.is_away(team_id):
                continue
            existing = self.by_team.get(team_id, [])
            earlier = sorted((o for o in existing if o.end_utc <= start),
                             key=lambda m: (m.start_utc, m.id))
            later = sorted((o for o in existing if o.start_utc >= start),
                           key=lambda m: (m.start_utc, m.id))
            chain = 1
            for other in reversed(earlier):
                if other.is_away(team_id):
                    chain += 1
                else:
                    break
            for other in later:
                if other.is_away(team_id):
                    chain += 1
                else:
                    break
            if chain > cap:
                return f"球队 {team_id} 将连续 {chain} 场客场，超过上限 {cap}"
        return None

    # -- 候选与主流程 -----------------------------------------------------

    NODE_BUDGET = 50_000

    def _candidate_list(self, fixture: Fixture,
                        not_before: datetime | None) -> list[_Placement]:
        duration = timedelta(minutes=self._durations[fixture.round_id])
        step = timedelta(minutes=self._settings.kickoff_step_minutes)
        raw: list[_Placement] = []
        for venue_id, placements in self._placements.items():
            for p in placements:
                ko = p.start_utc
                while ko + duration <= p.end_utc:
                    if not_before is None or ko >= not_before:
                        raw.append(_Placement(p.session, venue_id, ko,
                                              ko + duration))
                    ko = ko + step
        home_venue = self._team_home.get(fixture.home_team_id)
        raw.sort(key=lambda p: (0 if p.venue_id == home_venue else 1,
                                p.start_utc, p.venue_id))
        return raw

    def _evaluate(self, fixture: Fixture, cand: _Placement) -> list[str]:
        reasons: list[str] = []
        reason = self._check_venue(cand.venue_id, cand.start_utc,
                                   cand.end_utc, fixture.id)
        if reason:
            reasons.append(reason)
        for team_id in (fixture.home_team_id, fixture.away_team_id):
            reason = self._check_team(fixture, team_id, cand.start_utc,
                                      cand.end_utc, cand.venue_id)
            if reason:
                reasons.append(reason)
                break
        if not reasons:
            reason = self._away_chain_violation(fixture, cand.start_utc)
            if reason:
                reasons.append(reason)
        return reasons

    def _commit(self, fixture: Fixture, cand: _Placement) -> Match:
        match = Match(
            id=fixture.id, round_id=fixture.round_id,
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            venue_id=cand.venue_id, start_utc=cand.start_utc,
            end_utc=cand.end_utc, version=0, status=STATUS_PREVIEW)
        self.by_venue.setdefault(cand.venue_id, []).append(match)
        for t in (match.home_team_id, match.away_team_id):
            self.by_team.setdefault(t, []).append(match)
        return match

    def _rollback(self, fixture: Fixture, match: Match) -> None:
        self.by_venue[match.venue_id].remove(match)
        for t in (match.home_team_id, match.away_team_id):
            self.by_team[t].remove(match)

    def schedule(self, fixtures: list[Fixture], team_home: dict[str, str | None],
                 not_before: datetime | None = None
                 ) -> tuple[list[Match], list[UnscheduledFixture], list[str]]:
        self._team_home = team_home
        ordered = sorted(fixtures, key=lambda fx: (fx.slot, fx.group_index,
                                                   fx.id))
        candidates = {fx.id: self._candidate_list(fx, not_before)
                      for fx in ordered}
        by_id = {fx.id: fx for fx in ordered}
        self._nodes = 0
        assignment: dict[str, _Placement] = {}
        match_of: dict[str, Match] = {}

        def make_dfs(branch_cap: int):
            def dfs(remaining: list[Fixture], stuck: int) -> bool:
                if not remaining:
                    return True
                if stuck >= len(remaining):
                    return False  # 完整一轮无人可排，判定不可行
                self._nodes += 1
                if self._nodes > self.NODE_BUDGET:
                    return False
                fixture, rest = remaining[0], remaining[1:]
                feasible: list[_Placement] = []
                chain_only = False
                for cand in candidates[fixture.id]:
                    reasons = self._evaluate(fixture, cand)
                    if not reasons:
                        feasible.append(cand)
                        # 候选已按场馆/时间排序，只保留最早若干个即可，
                        # 显著压缩搜索分支
                        if len(feasible) >= branch_cap:
                            break
                    elif all("客场" in r for r in reasons):
                        chain_only = True
                if feasible:
                    for cand in feasible:
                        match = self._commit(fixture, cand)
                        assignment[fixture.id] = cand
                        match_of[fixture.id] = match
                        if dfs(rest, 0):
                            return True
                        self._rollback(fixture, match)
                        assignment.pop(fixture.id, None)
                        match_of.pop(fixture.id, None)
                    return False
                # 无可行候选：若全部只因连续客场阻塞，则把该场延后重试，
                # 让其他对阵（可能包含该队的主场）先排，打断客场链
                if chain_only:
                    return dfs(rest + [fixture], stuck + 1)
                return False
            return dfs

        ok = False
        # 分档放宽分支宽度：优先快速找到紧凑解，紧约束时再扩大搜索
        for cap in (6, 16, 10_000):
            self._nodes = 0
            assignment.clear()
            match_of.clear()
            self._reset_tables()
            if make_dfs(cap)(ordered, 0):
                ok = True
                break

        if not ok:
            # 贪心尽力排：排入尽可能多的场次，并给出未排入场次的阻塞依据
            greedy = _Scheduler(self._venues,
                                self._all_sessions,
                                self._settings, self._blackout_dates,
                                self._locked, self._durations)
            greedy._team_home = team_home
            return greedy._greedy(ordered, candidates_fn=lambda fx:
                                  self._candidate_list(fx, not_before))

        return self._finalize(by_id, assignment, match_of, not_before)

    def _finalize(self, by_id, assignment, match_of, not_before) -> tuple[
            list[Match], list[UnscheduledFixture], list[str]]:
        placed: list[Match] = []
        resolutions: list[str] = []
        cand_cache = {fid: self._candidate_list(fx, not_before)
                      for fid, fx in by_id.items()}
        for fid in sorted(assignment, key=lambda x:
                          (assignment[x].start_utc, x)):
            fixture = by_id[fid]
            cand = assignment[fid]
            match = match_of[fid]
            # 在最终完整解上重新评估更早候选，生成消解依据
            skipped: list[str] = []
            for earlier in sorted(cand_cache[fid],
                                  key=lambda c: (c.start_utc, c.venue_id)):
                if earlier.start_utc >= cand.start_utc:
                    break
                # 临时取下本场比赛再评估（_check_* 会按 id 忽略自身，
                # 但链式统计包含自身；此处仅做解释，直接忽略自身比赛）
                self._rollback(fixture, match)
                reasons = self._evaluate_with(fixture, earlier,
                                              ignore_id=fixture.id)
                self._commit_existing(match)
                if reasons and len(skipped) < 3:
                    local = earlier.start_utc.astimezone(
                        ZoneInfo(self._venues[earlier.venue_id].timezone))
                    skipped.append(
                        f"{self._venues[earlier.venue_id].name} "
                        f"{local.strftime('%Y-%m-%d %H:%M')}：{reasons[0]}")
            self._annotate(fixture, cand, cand_cache[fid], match, skipped)
            placed.append(match)
            resolutions.append(
                f"✓ {fid} → {cand.venue_id} {cand.start_utc.isoformat()}"
                + ("（" + "；".join(skipped[:2]) + "）" if skipped else ""))
        placed.sort(key=lambda m: (m.start_utc, m.id))
        return placed, [], resolutions

    def _commit_existing(self, match: Match) -> None:
        self.by_venue.setdefault(match.venue_id, []).append(match)
        for t in (match.home_team_id, match.away_team_id):
            self.by_team.setdefault(t, []).append(match)

    def _reset_tables(self) -> None:
        """清空已提交比赛，仅保留锁定比赛（供分档重试时使用）。"""
        self.by_venue = {}
        self.by_team = {}
        for m in self._locked:
            self.by_venue.setdefault(m.venue_id, []).append(m)
            for t in (m.home_team_id, m.away_team_id):
                self.by_team.setdefault(t, []).append(m)

    def _evaluate_with(self, fixture: Fixture, cand: _Placement,
                       ignore_id: str) -> list[str]:
        """与 _evaluate 相同，但链式统计忽略指定比赛（用于解释回溯）。"""
        reasons: list[str] = []
        reason = self._check_venue(cand.venue_id, cand.start_utc,
                                   cand.end_utc, ignore_id)
        if reason:
            reasons.append(reason)
        for team_id in (fixture.home_team_id, fixture.away_team_id):
            reason = self._check_team(fixture, team_id, cand.start_utc,
                                      cand.end_utc, cand.venue_id)
            if reason:
                reasons.append(reason)
                break
        if not reasons:
            reason = self._away_chain_violation_except(fixture, cand.start_utc,
                                                       ignore_id)
            if reason:
                reasons.append(reason)
        return reasons

    def _away_chain_violation_except(self, fixture: Fixture, start: datetime,
                                     ignore_id: str) -> str | None:
        cap = self._settings.max_consecutive_away
        for team_id in (fixture.home_team_id, fixture.away_team_id):
            if not fixture.is_away(team_id):
                continue
            existing = [o for o in self.by_team.get(team_id, [])
                        if o.id != ignore_id]
            earlier = sorted((o for o in existing if o.end_utc <= start),
                             key=lambda m: (m.start_utc, m.id))
            later = sorted((o for o in existing if o.start_utc >= start),
                           key=lambda m: (m.start_utc, m.id))
            chain = 1
            for other in reversed(earlier):
                if other.is_away(team_id):
                    chain += 1
                else:
                    break
            for other in later:
                if other.is_away(team_id):
                    chain += 1
                else:
                    break
            if chain > cap:
                return f"球队 {team_id} 将连续 {chain} 场客场，超过上限 {cap}"
        return None

    # -- 不可行时的尽力贪心 -----------------------------------------------

    @staticmethod
    def _reason_category(reason: str) -> str:
        if "场地冲突" in reason:
            return "场地冲突"
        if "同时段" in reason:
            return "球队撞场"
        if "休息不足" in reason:
            return "休息不足"
        if "连续" in reason and "客场" in reason:
            return "连续客场超限"
        return "其他"

    def _greedy(self, ordered, *, candidates_fn):
        """尽力贪心排程，用于不可行场景下产出部分赛程与解释。"""
        placed: list[Match] = []
        unscheduled: list[UnscheduledFixture] = []
        resolutions: list[str] = []
        for fixture in ordered:
            chosen = None
            skipped: list[str] = []
            seen_categories: set[str] = set()
            for cand in candidates_fn(fixture):
                reasons = self._evaluate(fixture, cand)
                if not reasons:
                    chosen = cand
                    break
                # 每类约束保留最早一条，让运营员看到全部冲突类型
                category = self._reason_category(reasons[0])
                if category not in seen_categories and len(seen_categories) < 5:
                    local = cand.start_utc.astimezone(
                        ZoneInfo(self._venues[cand.venue_id].timezone))
                    skipped.append(
                        f"{self._venues[cand.venue_id].name} "
                        f"{local.strftime('%Y-%m-%d %H:%M')}：{reasons[0]}")
                    seen_categories.add(category)
            if chosen is None:
                unscheduled.append(UnscheduledFixture(
                    fixture_id=fixture.id, round_id=fixture.round_id,
                    home_team_id=fixture.home_team_id,
                    away_team_id=fixture.away_team_id,
                    blockers=skipped or ["无任何可用场馆时段"]))
                resolutions.append(
                    f"⚠ {fixture.id} 未能排入："
                    + ("；".join(skipped) if skipped else "无可用时段"))
                continue
            match = self._commit(fixture, chosen)
            self._annotate(fixture, chosen, candidates_fn(fixture), match,
                           skipped)
            placed.append(match)
            resolutions.append(
                f"✓ {fixture.id} → {chosen.venue_id} "
                f"{chosen.start_utc.isoformat()}"
                + ("（" + "；".join(skipped[:2]) + "）" if skipped else ""))
        placed.sort(key=lambda m: (m.start_utc, m.id))
        return placed, unscheduled, resolutions

    def _annotate(self, fixture, chosen, candidate_list, match, skipped):
        home_venue = self._team_home.get(fixture.home_team_id)
        basis = [
            f"无场地/球队冲突，开球 {chosen.start_utc.isoformat()}，"
            f"场馆 {chosen.venue_id}"]
        if home_venue and chosen.venue_id != home_venue:
            basis.append(f"首选主场 {home_venue} 无可行时段，"
                         f"改用 {chosen.venue_id}")
        if any(c.start_utc < chosen.start_utc for c in candidate_list):
            basis.append("最早时段违反约束，已顺延（见冲突消解记录）")
        basis.extend(f"跳过：{s}" for s in skipped[:2])
        match.reasons = basis


# ---------------------------------------------------------------------------
# 序列化助手
# ---------------------------------------------------------------------------


def _dt_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt_parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _d_iso(d: date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------


class ScheduleService:
    """赛程编排应用服务（线程安全，可落盘、可复现）。"""

    def __init__(self, path: str | None = None, *, clock=None,
                 settings: SchedulingSettings | None = None):
        self._lock = threading.RLock()
        self._path = path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.settings = settings or SchedulingSettings()

        self.teams: dict[str, Team] = {}
        self.venues: dict[str, Venue] = {}
        self.sessions: list[VenueSession] = []
        self.rounds: dict[str, RoundSpec] = {}
        self.blackout_dates: set[date] = set()

        self._version = 0
        self._seq = 0
        self._active: dict[str, Match] = {}
        self._history: dict[str, list[Match]] = {}
        self._plans: dict[str, dict] = {}
        self._notifications: dict[str, Notification] = {}
        self._adjustments: list[dict] = []
        self._commands: dict[str, dict] = {}

        if path and os.path.exists(path):
            self._load()

    # -- 基础 -------------------------------------------------------------

    def health(self) -> dict[str, str]:
        return {"service": "schedule", "status": "ok"}

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    # -- 录入 -------------------------------------------------------------

    def add_team(self, team: Team) -> Team:
        with self._lock:
            if team.id in self.teams:
                raise ScheduleError(f"球队 {team.id} 已存在")
            if team.home_venue_id and team.home_venue_id not in self.venues:
                raise NotFoundError(f"主场馆 {team.home_venue_id} 不存在")
            self.teams[team.id] = team
            self._save()
            return team

    def add_venue(self, venue: Venue) -> Venue:
        with self._lock:
            if venue.id in self.venues:
                raise ScheduleError(f"场馆 {venue.id} 已存在")
            ZoneInfo(venue.timezone)  # 提前校验时区
            self.venues[venue.id] = venue
            self._save()
            return venue

    def add_session(self, session: VenueSession) -> VenueSession:
        with self._lock:
            if session.venue_id not in self.venues:
                raise NotFoundError(f"场馆 {session.venue_id} 不存在")
            if session.end_minute <= session.start_minute:
                raise ScheduleError("时段结束必须晚于开始（跨日请让 end_minute>1440）")
            self.sessions.append(session)
            self.sessions.sort(key=lambda s: (s.local_date, s.venue_id,
                                              s.start_minute))
            self._save()
            return session

    def add_round(self, spec: RoundSpec) -> RoundSpec:
        with self._lock:
            if spec.id in self.rounds:
                raise ScheduleError(f"轮次 {spec.id} 已存在")
            for group in spec.groups:
                for t in group:
                    if t not in self.teams:
                        raise NotFoundError(f"球队 {t} 不存在")
            self.rounds[spec.id] = spec
            self._save()
            return spec

    def add_blackout_date(self, d: date) -> None:
        with self._lock:
            self.blackout_dates.add(d)
            self._save()

    def fixtures_of(self, round_id: str) -> list[Fixture]:
        spec = self._require_round(round_id)
        return _round_robin_fixtures(spec)

    # -- 预览 -------------------------------------------------------------

    def plan_round(self, round_id: str, *, operator: str,
                   reason: str = "初次编排", referees: dict[str, str] | None = None
                   ) -> dict:
        """生成一轮的预览赛程（不改变已确认版本）。"""
        with self._lock:
            spec = self._require_round(round_id)
            if any(m.round_id == round_id and m.status in
                   (STATUS_SCHEDULED, STATUS_STARTED)
                   for m in self._active.values()):
                raise ScheduleError(
                    f"轮次 {round_id} 已有确认比赛，如需改动请使用 postpone")
            fixtures = _round_robin_fixtures(spec)
            locked = [m for mid, m in self._active.items()
                      if m.round_id != round_id and m.status != STATUS_CANCELLED]
            matches, unscheduled, resolutions = self._run_scheduler(
                fixtures, locked, not_before=None)
            for m in matches:
                if referees and m.id in referees:
                    m.referee = referees[m.id]
            plan_id = f"plan-{round_id}"
            plan = {
                "id": plan_id, "round_id": round_id,
                "status": STATUS_PREVIEW, "operator": operator, "reason": reason,
                "created_at": self._now(), "base_version": self._version,
                "matches": matches, "unscheduled": unscheduled,
                "resolutions": resolutions,
            }
            self._plans[plan_id] = plan
            self._save()
            return self._plan_view(plan_id)

    def preview(self, plan_id: str) -> dict:
        with self._lock:
            return self._plan_view(plan_id)

    # -- 确认（并发安全） --------------------------------------------------

    def confirm(self, plan_id: str, *, operator: str,
                expected_version: int | None = None,
                reason: str | None = None) -> dict:
        """确认预览计划，生成新版本与待通知记录。

        并发确认同一计划时，只有一个线程成功，其余抛 :class:`PlanStateError`。
        """
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise NotFoundError(f"计划 {plan_id} 不存在")
            if plan["status"] != STATUS_PREVIEW:
                raise PlanStateError(
                    f"计划 {plan_id} 状态为 {plan['status']}，不可重复确认")
            if expected_version is not None and expected_version != self._version:
                raise PlanStateError(
                    f"版本已变化：期望 {expected_version}，当前 {self._version}")
            if reason:
                plan["reason"] = reason

            self._version += 1
            version = self._version
            before: dict[str, Match | None] = {}
            affected: list[str] = []
            for match in plan["matches"]:
                mid = match.id
                before[mid] = self._active.get(mid)
                confirmed = replace(match, version=version,
                                    status=STATUS_SCHEDULED)
                self._active[mid] = confirmed
                self._history.setdefault(mid, []).append(confirmed)
                affected.append(mid)
                self._ensure_notifications(confirmed)

            plan["status"] = STATUS_SCHEDULED
            plan["version"] = version
            adjustment = self._record_adjustment(
                kind="confirm", operator=operator, reason=plan["reason"],
                affected=affected, before=before, plan_id=plan_id)
            self._save()
            return {
                "plan_id": plan_id, "version": version,
                "adjustment_id": adjustment["id"],
                "match_ids": affected,
                "unscheduled": [u.fixture_id for u in plan["unscheduled"]],
            }

    # -- 撤销 -------------------------------------------------------------

    def undo(self, *, operator: str, reason: str = "运营员撤销") -> dict:
        """撤销最近一次调整（确认或延期），恢复其之前的活动版本。"""
        with self._lock:
            if not self._adjustments:
                raise ScheduleError("没有可撤销的调整")
            adj = self._adjustments[-1]
            if adj["kind"] == "undo":
                raise ScheduleError("最近一条记录已是撤销")
            for mid, before_dict in adj["before"].items():
                current = self._active.pop(mid, None)
                if current is not None:
                    current.status = STATUS_CANCELLED
                if before_dict is None:
                    # 之前不存在：活动表中移除（历史仍保留 CANCELLED 版本）
                    continue
                # 复用历史中的同一版本对象，仅把状态从 POSTPONED 复位，
                # 保证按球队查询时当前版本与版本列表始终对齐
                hist = self._history.get(mid, [])
                target = next((h for h in hist
                               if h.version == before_dict["version"]), None)
                if target is not None:
                    target.status = STATUS_SCHEDULED
                    self._active[mid] = target
                else:  # pragma: no cover - 防御性
                    restored = self._match_from_dict(before_dict)
                    restored.status = STATUS_SCHEDULED
                    self._active[mid] = restored
            # 撤销连带删除该调整生成的通知，并恢复此前被作废的旧通知
            for nid in adj.get("notification_ids", []):
                self._notifications.pop(nid, None)
            for n_dict in adj.get("superseded_notifications", []):
                restored = self._notification_from_dict(n_dict)
                restored.status = "pending"
                self._notifications[restored.id] = restored
            if adj["kind"] == "confirm" and adj.get("plan_id"):
                plan = self._plans.get(adj["plan_id"])
                if plan:
                    plan["status"] = STATUS_PREVIEW
                    plan.pop("version", None)
            if adj["kind"] == "postpone" and adj.get("command_id"):
                # 撤销延期后，同一指令允许再次提交
                self._commands.pop(adj["command_id"], None)
            adj["undone"] = True
            self._record_adjustment(
                kind="undo", operator=operator,
                reason=f"撤销 {adj['id']}：{reason}",
                affected=list(adj["before"].keys()), before={})
            self._save()
            return {"undone_adjustment_id": adj["id"],
                    "affected": list(adj["before"].keys())}

    # -- 延期（仅未开赛、幂等、保留历史） ----------------------------------

    def mark_started(self, match_id: str) -> Match:
        with self._lock:
            match = self._require_active_match(match_id)
            match.status = STATUS_STARTED
            self._save()
            return match

    def postpone(self, command_id: str, *, operator: str, reason: str,
                 round_id: str | None = None,
                 match_ids: list[str] | None = None,
                 not_before: datetime | None = None) -> dict:
        """处理延期指令。

        - 只重排尚未开赛（且状态为 SCHEDULED）的场次；已开赛的保留在
          ``retained`` 中，原版本与通知记录原样保留。
        - 相同 ``command_id`` 重复提交直接返回首次结果，不产生第二份赛程。
        """
        with self._lock:
            if command_id in self._commands:
                replay = self._commands[command_id]
                return {**replay, "replayed": True}

            now = self._now()
            not_before = not_before or now
            candidates = self._select_matches(round_id, match_ids)
            to_move: list[Match] = []
            retained: list[str] = []
            for m in candidates:
                if m.status == STATUS_STARTED or m.start_utc <= now:
                    retained.append(m.id)
                else:
                    to_move.append(m)

            # 旧记录置为 POSTPONED（历史与通知均保留）
            locked = [m for mid, m in self._active.items()
                      if mid not in {x.id for x in to_move}]
            # 从轮次规格重新取对阵（保留 slot/小组信息，保证搜索效率与
            # 可复现性），仅保留需要延期的场次
            move_ids = {x.id for x in to_move}
            fixtures = []
            for rid in {m.round_id for m in to_move}:
                spec = self.rounds.get(rid)
                if spec is None:
                    # 轮次定义缺失时退化为重建（不影响正确性）
                    fixtures.extend(
                        Fixture(id=m.id, round_id=m.round_id, group_index=0,
                                home_team_id=m.home_team_id,
                                away_team_id=m.away_team_id)
                        for m in to_move if m.round_id == rid)
                    continue
                fixtures.extend(f for f in _round_robin_fixtures(spec)
                                if f.id in move_ids)
            matches, unscheduled, resolutions = self._run_scheduler(
                fixtures, locked, not_before=not_before)
            for m in matches:
                old = next(x for x in to_move if x.id == m.id)
                m.referee = old.referee
                m.reasons.insert(
                    0, f"延期重排（指令 {command_id}：{reason}）；"
                       f"原排期 {old.venue_id} {old.start_utc.isoformat()} 已作废")

            self._version += 1
            version = self._version
            before: dict[str, Match | None] = {
                m.id: m for m in to_move}  # 含暂时无法排入的场次
            for old in to_move:
                old.status = STATUS_POSTPONED
            superseded = self._supersede_notifications(
                [m.id for m in to_move])
            new_ids: list[str] = []
            for match in matches:
                mid = match.id
                confirmed = replace(match, version=version,
                                    status=STATUS_SCHEDULED)
                self._active[mid] = confirmed
                self._history.setdefault(mid, []).append(confirmed)
                new_ids.append(mid)
                self._ensure_notifications(confirmed)
            # 暂时无法排入的场次：从活动表移除，历史中保留 POSTPONED 版本
            rescheduled_ids = set(new_ids)
            for old in to_move:
                if old.id not in rescheduled_ids:
                    self._active.pop(old.id, None)
            # 受影响场次（含待重排），撤销时按 before 恢复
            affected = [m.id for m in to_move]

            adjustment = self._record_adjustment(
                kind="postpone", operator=operator, reason=reason,
                affected=affected, before=before, command_id=command_id,
                superseded=superseded,
                extra={"retained": retained,
                       "unscheduled": [u.fixture_id for u in unscheduled],
                       "resolutions": resolutions})
            result = {
                "command_id": command_id, "version": version,
                "adjustment_id": adjustment["id"],
                "rescheduled": new_ids,
                "retained": retained,
                "unscheduled": [u.fixture_id for u in unscheduled],
                "resolutions": resolutions,
                "replayed": False,
            }
            self._commands[command_id] = result
            self._save()
            return result

    # -- 查询 -------------------------------------------------------------

    def team_schedule(self, team_id: str) -> dict:
        """按球队查询：每个对阵的全部版本、当前状态与通知记录。"""
        with self._lock:
            if team_id not in self.teams:
                raise NotFoundError(f"球队 {team_id} 不存在")
            entries = []
            for mid, history in sorted(self._history.items()):
                if not history or not history[0].involves(team_id):
                    continue
                versions = []
                for m in history:
                    versions.append({
                        "version": m.version, "status": m.status,
                        "venue_id": m.venue_id,
                        "start_utc": m.start_utc.isoformat(),
                        "end_utc": m.end_utc.isoformat(),
                        "start_local": self._local_time(m.venue_id, m.start_utc),
                        "opponent": m.opponent(team_id),
                        "role": "away" if m.is_away(team_id) else "home",
                        "referee": m.referee,
                        "reasons": list(m.reasons),
                        "notifications": sorted((
                            self._notification_dict(n)
                            for n in self._notifications.values()
                            if n.match_id == m.id and n.version == m.version),
                            key=lambda x: (x["kind"], x["recipient"])),
                    })
                active = self._active.get(mid)
                entries.append({
                    "match_id": mid,
                    "current_version": active.version if active else None,
                    "current_status": active.status if active else "INACTIVE",
                    "versions": versions,
                })
            entries.sort(key=lambda e: e["match_id"])
            return {"team_id": team_id, "team_name": self.teams[team_id].name,
                    "matches": entries}

    def round_view(self, round_id: str) -> dict:
        """运营员一轮总览。

        包含：当前版本、每场的冲突消解依据、待通知对象（裁判/家长）、
        球队连续比赛间隔与连续客场链。
        """
        with self._lock:
            spec = self._require_round(round_id)
            current = sorted(
                (m for m in self._active.values() if m.round_id == round_id),
                key=lambda m: (m.start_utc, m.id))
            version = max((m.version for m in current), default=self._version)
            pending_refs: set[str] = set()
            pending_parents: set[str] = set()
            for m in current:
                for n in self._notifications.values():
                    if n.match_id == m.id and n.version == m.version \
                            and n.status == "pending":
                        if n.kind == "referee":
                            pending_refs.add(f"{n.recipient}（{m.id}）")
                        else:
                            pending_parents.add(n.recipient)
            intervals = self._team_intervals(round_id)
            return {
                "round_id": round_id, "round_name": spec.name,
                "current_version": version,
                "matches": [self._match_view(m) for m in current],
                "pending_notifications": {
                    "referees": sorted(pending_refs),
                    "parents": sorted(pending_parents),
                },
                "team_intervals": intervals,
            }

    def adjustments(self) -> list[dict]:
        with self._lock:
            return [dict(a, before_summary={
                mid: (m["status"] if m else None)
                for mid, m in a["before"].items()}) for a in self._adjustments]

    # -- 裁判与通知 --------------------------------------------------------

    def assign_referee(self, match_id: str, referee: str) -> None:
        with self._lock:
            in_plan = False
            for plan in self._plans.values():
                for m in plan.get("matches", []):
                    if m.id == match_id and plan["status"] == STATUS_PREVIEW:
                        m.referee = referee
                        in_plan = True
            if match_id not in self._active and not in_plan:
                raise NotFoundError(f"比赛 {match_id} 不存在")
            if match_id in self._active:
                m = self._active[match_id]
                old_referee = m.referee
                m.referee = referee
                # 更换裁判：旧的未发送裁判通知作废，避免错发
                if old_referee and old_referee != referee:
                    for n in self._notifications.values():
                        if (n.match_id == match_id and n.version == m.version
                                and n.kind == "referee" and n.status == "pending"):
                            n.status = STATUS_SUPERSEDED
                self._ensure_notifications(m, referee_override=referee)
            self._save()

    def pending_notifications(self, round_id: str | None = None) -> list[dict]:
        with self._lock:
            out = []
            for n in self._notifications.values():
                if n.status != "pending":
                    continue
                if round_id and (n.match_id not in self._active or
                                 self._active[n.match_id].round_id != round_id):
                    continue
                out.append(self._notification_dict(n))
            return sorted(out, key=lambda x: (x["match_id"], x["recipient"]))

    def send_pending(self, round_id: str | None = None) -> list[dict]:
        """把待通知对象标记为已发送（真实系统中对接短信/邮件网关）。"""
        with self._lock:
            sent = []
            now = self._now()
            for n in list(self._notifications.values()):
                if n.status != "pending":
                    continue
                if round_id and (n.match_id not in self._active or
                                 self._active[n.match_id].round_id != round_id):
                    continue
                n.status = "sent"
                n.sent_at = now
                sent.append(self._notification_dict(n))
            self._save()
            return sent

    # -- 内部工具 ----------------------------------------------------------

    def _require_round(self, round_id: str) -> RoundSpec:
        spec = self.rounds.get(round_id)
        if spec is None:
            raise NotFoundError(f"轮次 {round_id} 不存在")
        return spec

    def _require_active_match(self, match_id: str) -> Match:
        m = self._active.get(match_id)
        if m is None:
            raise NotFoundError(f"活动比赛 {match_id} 不存在")
        return m

    def _select_matches(self, round_id, match_ids) -> list[Match]:
        if match_ids is not None:
            return [self._require_active_match(mid) for mid in match_ids]
        if round_id is not None:
            self._require_round(round_id)
            return [m for m in self._active.values() if m.round_id == round_id]
        raise ScheduleError("必须指定 round_id 或 match_ids")

    def _run_scheduler(self, fixtures, locked, not_before):
        team_home = {tid: t.home_venue_id for tid, t in self.teams.items()}
        scheduler = _Scheduler(
            self.venues, self.sessions, self.settings,
            set(self.blackout_dates), locked,
            durations={rid: spec.match_minutes
                       for rid, spec in self.rounds.items()})
        return scheduler.schedule(fixtures, team_home, not_before)

    def _ensure_notifications(self, match: Match, *, referee_override=None) -> None:
        referee = referee_override if referee_override is not None else match.referee

        def has_active(kind, recipient=None):
            return any(
                n.match_id == match.id and n.version == match.version
                and n.kind == kind and n.status != STATUS_SUPERSEDED
                and (recipient is None or n.recipient == recipient)
                for n in self._notifications.values())

        if referee and not has_active("referee"):
            nid = self._next_id("n")
            self._notifications[nid] = Notification(
                id=nid, match_id=match.id, version=match.version,
                recipient=referee, kind="referee", created_at=self._now())
        for team_id in (match.home_team_id, match.away_team_id):
            team = self.teams.get(team_id)
            if not team:
                continue
            for contact in team.contacts:
                if has_active("parent", contact):
                    continue
                nid = self._next_id("n")
                self._notifications[nid] = Notification(
                    id=nid, match_id=match.id, version=match.version,
                    recipient=contact, kind="parent", created_at=self._now())

    def _supersede_notifications(self, match_ids: list[str]) -> list[dict]:
        """旧版本未发出的通知标记作废（防止延期后发出互相矛盾的时间）。

        已发送的通知保留为历史记录。返回被作废通知的快照，供撤销时恢复。
        """
        targets = set(match_ids)
        snap: list[dict] = []
        for n in self._notifications.values():
            if n.match_id in targets and n.status == "pending":
                snap.append(self._notification_to_dict(n))
                n.status = STATUS_SUPERSEDED
        return snap

    def _record_adjustment(self, *, kind, operator, reason, affected, before,
                           plan_id=None, command_id=None, superseded=None,
                           extra=None) -> dict:
        self._seq += 1
        adj = {
            "id": f"adj-{self._seq}", "seq": self._seq, "kind": kind,
            "operator": operator, "reason": reason, "created_at": self._now(),
            "affected": list(affected),
            "before": {mid: (self._match_to_dict(m) if m else None)
                       for mid, m in before.items()},
            "notification_ids": [],
            "undone": False,
        }
        if plan_id:
            adj["plan_id"] = plan_id
        if command_id:
            adj["command_id"] = command_id
        if superseded:
            adj["superseded_notifications"] = superseded
        if extra:
            adj.update(extra)
        # 记录本调整新生成的通知，供撤销时清理
        adj["notification_ids"] = [
            n.id for n in self._notifications.values()
            if n.match_id in set(affected)
            and (kind != "confirm" or n.version == self._version)
            and (kind != "postpone" or n.version == self._version)]
        self._adjustments.append(adj)
        return adj

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def _local_time(self, venue_id: str, dt_utc: datetime) -> str:
        venue = self.venues[venue_id]
        return dt_utc.astimezone(ZoneInfo(venue.timezone)).strftime(
            "%Y-%m-%d %H:%M %Z")

    def _match_view(self, m: Match) -> dict:
        venue = self.venues[m.venue_id]
        tz = ZoneInfo(venue.timezone)
        return {
            "match_id": m.id, "round_id": m.round_id,
            "home_team_id": m.home_team_id, "away_team_id": m.away_team_id,
            "venue_id": m.venue_id, "venue_name": venue.name,
            "version": m.version, "status": m.status, "referee": m.referee,
            "start_utc": m.start_utc.isoformat(),
            "end_utc": m.end_utc.isoformat(),
            "start_local": m.start_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "resolution_basis": list(m.reasons),
        }

    def _plan_view(self, plan_id: str) -> dict:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"计划 {plan_id} 不存在")
        return {
            "plan_id": plan["id"], "round_id": plan["round_id"],
            "status": plan["status"], "operator": plan["operator"],
            "reason": plan["reason"],
            "current_version": self._version,
            "confirmed_version": plan.get("version"),
            "matches": [self._match_view(m) for m in plan["matches"]],
            "unscheduled": [vars(u) for u in plan["unscheduled"]],
            "resolutions": list(plan["resolutions"]),
            "created_at": plan["created_at"].isoformat(),
        }

    def _team_intervals(self, round_id: str) -> dict:
        result: dict[str, dict] = {}
        for team_id in sorted(self.teams):
            ms = sorted((m for m in self._active.values()
                         if m.round_id == round_id and m.involves(team_id)),
                        key=lambda m: (m.start_utc, m.id))
            gaps = []
            away_runs: list[list[str]] = []
            current_run: list[str] = []
            for prev, cur in zip(ms, ms[1:]):
                gap_min = (cur.start_utc - prev.end_utc).total_seconds() / 60
                required = self.settings.min_rest_minutes + self._tz_extra(
                    prev.venue_id, prev.start_utc, cur.venue_id, cur.start_utc)
                gaps.append({
                    "from_match": prev.id, "to_match": cur.id,
                    "gap_minutes": round(gap_min, 1),
                    "required_minutes": required,
                    "cross_timezone": required > self.settings.min_rest_minutes,
                    "ok": gap_min + 1e-9 >= required,
                })
            for m in ms:
                if m.is_away(team_id):
                    current_run.append(m.id)
                else:
                    if current_run:
                        away_runs.append(current_run)
                    current_run = []
            if current_run:
                away_runs.append(current_run)
            result[team_id] = {
                "gaps": gaps,
                "away_runs": away_runs,
                "max_consecutive_away": max((len(r) for r in away_runs),
                                            default=0),
            }
        return result

    def _tz_extra(self, va_id, when_a, vb_id, when_b) -> int:
        va, vb = self.venues[va_id], self.venues[vb_id]
        off_a = when_a.astimezone(ZoneInfo(va.timezone)).utcoffset()
        off_b = when_b.astimezone(ZoneInfo(vb.timezone)).utcoffset()
        if off_a != off_b:
            return self.settings.cross_tz_extra_rest_minutes
        return 0

    # -- 持久化 ------------------------------------------------------------

    def _save(self) -> None:
        if not self._path:
            return
        state = {
            "version": self._version, "seq": self._seq,
            "settings": {f.name: getattr(self.settings, f.name)
                         for f in fields(self.settings)},
            "teams": [vars(t) | {"contacts": list(t.contacts)}
                      for t in self.teams.values()],
            "venues": [{"id": v.id, "name": v.name, "timezone": v.timezone,
                        "unavailable_dates": sorted(_d_iso(d)
                                                    for d in v.unavailable_dates)}
                       for v in self.venues.values()],
            "sessions": [vars(s) | {"local_date": _d_iso(s.local_date)}
                         for s in self.sessions],
            "rounds": [{"id": r.id, "name": r.name,
                        "groups": [list(g) for g in r.groups],
                        "double_leg": r.double_leg,
                        "match_minutes": r.match_minutes}
                       for r in self.rounds.values()],
            "blackout_dates": sorted(_d_iso(d) for d in self.blackout_dates),
            "active": {mid: self._match_to_dict(m)
                       for mid, m in self._active.items()},
            "history": {mid: [self._match_to_dict(m) for m in hist]
                        for mid, hist in self._history.items()},
            "plans": {pid: self._plan_to_dict(p) for pid, p in self._plans.items()},
            "notifications": {nid: self._notification_to_dict(n)
                              for nid, n in self._notifications.items()},
            "adjustments": [self._adjustment_to_dict(a) for a in self._adjustments],
            "commands": self._commands,
        }
        directory = os.path.dirname(os.path.abspath(self._path))
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, sort_keys=True,
                          indent=2, default=str)
            os.replace(tmp, self._path)
        except BaseException:  # pragma: no cover - 防御性
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _load(self) -> None:
        with open(self._path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        self._version = state["version"]
        self._seq = state.get("seq", 0)
        self.settings = SchedulingSettings(**state["settings"])
        self.teams = {}
        for t in state["teams"]:
            self.teams[t["id"]] = Team(
                id=t["id"], name=t["name"], home_venue_id=t["home_venue_id"],
                contacts=tuple(t.get("contacts", [])))
        self.venues = {}
        for v in state["venues"]:
            self.venues[v["id"]] = Venue(
                id=v["id"], name=v["name"], timezone=v["timezone"],
                unavailable_dates=frozenset(date.fromisoformat(d)
                                            for d in v["unavailable_dates"]))
        self.sessions = [
            VenueSession(venue_id=s["venue_id"],
                         local_date=date.fromisoformat(s["local_date"]),
                         start_minute=s["start_minute"],
                         end_minute=s["end_minute"])
            for s in state["sessions"]]
        self.rounds = {}
        for r in state["rounds"]:
            self.rounds[r["id"]] = RoundSpec(
                id=r["id"], name=r["name"],
                groups=tuple(tuple(g) for g in r["groups"]),
                double_leg=r["double_leg"], match_minutes=r["match_minutes"])
        self.blackout_dates = {date.fromisoformat(d)
                               for d in state.get("blackout_dates", [])}
        self._active = {mid: self._match_from_dict(m)
                        for mid, m in state["active"].items()}
        self._history = {
            mid: [self._match_from_dict(m) for m in hist]
            for mid, hist in state["history"].items()}
        self._plans = {pid: self._plan_from_dict(p)
                       for pid, p in state.get("plans", {}).items()}
        self._notifications = {
            nid: self._notification_from_dict(n)
            for nid, n in state.get("notifications", {}).items()}
        self._adjustments = [self._adjustment_from_dict(a)
                             for a in state.get("adjustments", [])]
        self._commands = state.get("commands", {})

    def _match_to_dict(self, m: Match) -> dict:
        return {"id": m.id, "round_id": m.round_id,
                "home_team_id": m.home_team_id, "away_team_id": m.away_team_id,
                "venue_id": m.venue_id, "start_utc": _dt_iso(m.start_utc),
                "end_utc": _dt_iso(m.end_utc), "version": m.version,
                "status": m.status, "reasons": list(m.reasons),
                "referee": m.referee}

    def _match_from_dict(self, d: dict) -> Match:
        return Match(id=d["id"], round_id=d["round_id"],
                     home_team_id=d["home_team_id"],
                     away_team_id=d["away_team_id"], venue_id=d["venue_id"],
                     start_utc=_dt_parse(d["start_utc"]),
                     end_utc=_dt_parse(d["end_utc"]), version=d["version"],
                     status=d["status"], reasons=list(d.get("reasons", [])),
                     referee=d.get("referee"))

    def _plan_to_dict(self, p: dict) -> dict:
        return {"id": p["id"], "round_id": p["round_id"], "status": p["status"],
                "operator": p["operator"], "reason": p["reason"],
                "created_at": _dt_iso(p["created_at"]),
                "base_version": p.get("base_version"),
                "version": p.get("version"),
                "matches": [self._match_to_dict(m) for m in p["matches"]],
                "unscheduled": [vars(u) for u in p["unscheduled"]],
                "resolutions": list(p["resolutions"])}

    def _plan_from_dict(self, d: dict) -> dict:
        return {"id": d["id"], "round_id": d["round_id"], "status": d["status"],
                "operator": d["operator"], "reason": d["reason"],
                "created_at": _dt_parse(d["created_at"]),
                "base_version": d.get("base_version"),
                "version": d.get("version"),
                "matches": [self._match_from_dict(m) for m in d["matches"]],
                "unscheduled": [UnscheduledFixture(**u)
                                for u in d.get("unscheduled", [])],
                "resolutions": list(d.get("resolutions", []))}

    def _notification_to_dict(self, n: Notification) -> dict:
        return {"id": n.id, "match_id": n.match_id, "version": n.version,
                "recipient": n.recipient, "kind": n.kind, "status": n.status,
                "created_at": _dt_iso(n.created_at),
                "sent_at": _dt_iso(n.sent_at)}

    def _notification_from_dict(self, d: dict) -> Notification:
        return Notification(id=d["id"], match_id=d["match_id"],
                            version=d["version"], recipient=d["recipient"],
                            kind=d["kind"], status=d["status"],
                            created_at=_dt_parse(d.get("created_at")),
                            sent_at=_dt_parse(d.get("sent_at")))

    def _notification_dict(self, n: Notification) -> dict:
        return self._notification_to_dict(n)

    def _adjustment_to_dict(self, a: dict) -> dict:
        out = dict(a)
        out["created_at"] = _dt_iso(a["created_at"])
        return out

    def _adjustment_from_dict(self, d: dict) -> dict:
        out = dict(d)
        out["created_at"] = _dt_parse(d["created_at"])
        return out
