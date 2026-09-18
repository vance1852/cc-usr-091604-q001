"""确定性排程引擎。

输入：球队、场馆、可用时段（可跨日）、不可比赛日期、规则、对阵请求，
以及已经锁定不能移动的比赛（已确认/已开赛）。

输出：所有比赛排下后返回 PlannedMatch 列表与每场球的候选尝试记录；
排不下则抛出 SchedulingImpossible，逐场给出每个候选被拒绝的依据。

可复现性：所有遍历顺序均按 id / UTC 时间排序；求解只依赖输入数据，
不读取墙钟、不使用随机数。相同输入必然得到相同输出。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import (
    Candidate,
    GameRequest,
    PlannedMatch,
    Rejection,
    Rules,
    Slot,
    Team,
    UnscheduledMatch,
    Venue,
)
from app.models import SchedulingImpossible


class Engine:
    def __init__(self, league_id: str, teams: list[Team], venues: list[Venue],
                 slots: list[Slot], blackout_days: set[tuple], rules: Rules):
        self.league_id = league_id
        self.teams = {t.id: t for t in teams}
        self.venues = {v.id: v for v in venues}
        self.rules = rules
        self.blackouts = blackout_days  # set[(scope, scope_id, date)]

        self.candidates: list[Candidate] = sorted(
            self._build_candidates(slots),
            key=lambda c: (c.start_utc, c.venue_id, c.slot_id),
        )

    # ------------------------------------------------------------------
    # 时段换算（含跨日：end_minute 可大于 1440）
    # ------------------------------------------------------------------
    def _build_candidates(self, slots: list[Slot]) -> list[Candidate]:
        out: list[Candidate] = []
        for s in slots:
            tz = ZoneInfo(self.venues[s.venue_id].timezone)
            duration = s.end_minute - s.start_minute
            if duration < self.rules.game_minutes:
                continue
            local_start = datetime(s.day.year, s.day.month, s.day.day) + \
                timedelta(minutes=s.start_minute)
            start_utc = local_start.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))
            out.append(Candidate(s.id, s.venue_id, start_utc,
                                 start_utc + timedelta(minutes=duration)))
        return out

    def _is_blackout(self, venue_id: str, start_utc: datetime,
                     end_utc: datetime | None = None) -> bool:
        tz = ZoneInfo(self.venues[venue_id].timezone)
        local_days = {start_utc.astimezone(tz).date()}
        if end_utc is not None:
            local_days.add(end_utc.astimezone(tz).date())
        for d in local_days:
            if ("venue", venue_id, d) in self.blackouts or \
                    ("league", "*", d) in self.blackouts:
                return True
        # 联赛停赛日同时按 UTC 日期兜底，避免跨时区场馆钻空子
        utc_days = {start_utc.date()}
        if end_utc is not None:
            utc_days.add(end_utc.date())
        return any(("league", "*", d) in self.blackouts for d in utc_days)

    def _tz_offset_hours(self, venue_id: str, when: datetime) -> float:
        tz = ZoneInfo(self.venues[venue_id].timezone)
        return when.astimezone(tz).utcoffset().total_seconds() / 3600

    # ------------------------------------------------------------------
    # 可行性检查：收集该候选的全部拒绝原因（可解释）
    # ------------------------------------------------------------------
    def _check(self, req: GameRequest, cand: Candidate,
               venue_occ: dict[str, list[PlannedMatch]],
               team_matches: dict[str, list[PlannedMatch]],
               placed: dict[str, PlannedMatch],
               prev_req: dict[str, dict[str, str | None]]) -> list[str]:
        r = self.rules
        reasons: list[str] = []
        game_end = cand.start_utc + timedelta(minutes=r.game_minutes)

        if game_end > cand.end_utc:
            reasons.append("slot_too_short")
        if self._is_blackout(cand.venue_id, cand.start_utc, game_end):
            reasons.append("blackout")

        home = self.teams[req.home_id]
        if r.require_home_venue and home.home_venue_id is not None \
                and home.home_venue_id != cand.venue_id:
            reasons.append("not_home_venue")

        if req.not_before_utc is not None and \
                cand.start_utc < req.not_before_utc:
            reasons.append("before_floor")
        if (cand.venue_id, cand.start_utc) in req.forbidden:
            reasons.append("forbidden_slot")

        # 场馆占用冲突
        for m in venue_occ.get(cand.venue_id, []):
            if cand.start_utc < m.end_utc and m.start_utc < game_end:
                reasons.append("venue_overlap")
                break

        for team_id, is_away in ((req.home_id, False), (req.away_id, True)):
            # 与该队所有已存在比赛（含锁定场）的时间/休息/时区约束
            for m in team_matches.get(team_id, []):
                if cand.start_utc < m.end_utc and m.start_utc < game_end:
                    reasons.append("team_overlap")
                else:
                    # 两场不重叠时的首尾间隔
                    gap = (cand.start_utc - m.end_utc) if m.end_utc <= cand.start_utc \
                        else (m.start_utc - game_end)
                    if gap < timedelta(minutes=r.rest_minutes):
                        reasons.append("rest")
                if self._crosses_timezone(m.venue_id, cand.venue_id,
                                          m, cand.start_utc, game_end):
                    reasons.append("cross_timezone_rest")

            # 轮次顺序：该队上一轮比赛必须已经结束
            prev_id = prev_req[team_id].get(req.id)
            if prev_id is not None:
                prev_m = placed.get(prev_id)
                if prev_m is not None and cand.start_utc < prev_m.end_utc:
                    reasons.append("before_floor")

            # 连续客场：沿对阵顺序向前数连续客场场数
            if is_away:
                run = 1
                cur = req.id
                while True:
                    pid = prev_req[team_id].get(cur)
                    if pid is None:
                        break
                    pm = placed.get(pid)
                    if pm is None or pm.away_id != team_id:
                        break
                    run += 1
                    cur = pid
                if run > r.max_consecutive_away:
                    reasons.append("consecutive_away")

        return _dedup(reasons)

    def _crosses_timezone(self, v1: str, v2: str,
                          m: PlannedMatch, start_utc: datetime,
                          game_end: datetime) -> bool:
        r = self.rules
        # 用两场开赛时刻的实际 UTC 偏移差（含夏令时）衡量跨时区
        off1 = self._tz_offset_hours(v1, m.start_utc)
        off2 = self._tz_offset_hours(v2, start_utc)
        if abs(off2 - off1) < r.tz_threshold_hours:
            return False
        if m.start_utc < start_utc:
            gap = start_utc - m.end_utc
        else:
            gap = m.start_utc - game_end
        return gap < timedelta(minutes=r.tz_extra_rest_minutes)

    # ------------------------------------------------------------------
    # 求解
    # ------------------------------------------------------------------
    def solve(self, requests: list[GameRequest],
              locked: list[PlannedMatch] | None = None) -> tuple[
                  list[PlannedMatch], dict[str, list[Rejection]],
                  list[PlannedMatch]]:
        locked = locked or []
        order = [rq.id for rq in sorted(requests,
                                        key=lambda x: (x.round_no, x.id))]
        req_by_id = {rq.id: rq for rq in requests}

        # 每队在“对阵顺序”上的前驱对阵。把锁定场按轮次插入同一序列，
        # 保证只重排部分轮次时，连续客场/轮次顺序仍以完整赛季为上下文。
        chain: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for m in locked:
            chain[m.home_id].append((m.round_no, m.id))
            chain[m.away_id].append((m.round_no, m.id))
        for rq in requests:
            chain[rq.home_id].append((rq.round_no, rq.id))
            chain[rq.away_id].append((rq.round_no, rq.id))
        prev_req: dict[str, dict[str, str | None]] = defaultdict(dict)
        for team_id, seq in chain.items():
            seq.sort(key=lambda x: (x[0], x[1]))
            prev_id: str | None = None
            for _, rid in seq:
                prev_req[team_id][rid] = prev_id
                prev_id = rid

        venue_occ: dict[str, list[PlannedMatch]] = defaultdict(list)
        team_matches: dict[str, list[PlannedMatch]] = defaultdict(list)
        placed: dict[str, PlannedMatch] = {}
        locked_out: list[PlannedMatch] = []

        for m in sorted(locked, key=lambda m: m.start_utc):
            venue_occ[m.venue_id].append(m)
            team_matches[m.home_id].append(m)
            team_matches[m.away_id].append(m)
            placed[m.id] = m
            locked_out.append(m)

        # 每次成功放置前，被否定候选的留痕（供预览解释）
        trace: dict[str, list[Rejection]] = defaultdict(list)
        failed_at: dict[str, list[Rejection]] = defaultdict(list)
        result: dict[str, PlannedMatch] = {}
        budget = self.rules.max_search_nodes
        exhausted = False

        def backtrack(step: int) -> bool:
            nonlocal budget, exhausted
            if exhausted:
                return False
            budget -= 1
            if budget <= 0:
                exhausted = True
                return False
            if step == len(order):
                return True
            rq = req_by_id[order[step]]
            rejections: list[Rejection] = []
            for cand in self._candidates_for(rq):
                reasons = self._check(rq, cand, venue_occ, team_matches,
                                      placed, prev_req)
                if reasons:
                    rejections.append(Rejection(
                        cand.slot_id, cand.venue_id, cand.start_utc, reasons))
                    continue
                match = PlannedMatch(
                    rq.id, rq.round_no, rq.home_id, rq.away_id,
                    cand.venue_id, cand.start_utc,
                    cand.start_utc + timedelta(minutes=self.rules.game_minutes),
                    rq.group_name)
                venue_occ[cand.venue_id].append(match)
                team_matches[rq.home_id].append(match)
                team_matches[rq.away_id].append(match)
                placed[rq.id] = match
                if backtrack(step + 1):
                    result[rq.id] = match
                    if rejections:
                        trace[rq.id] = rejections
                    return True
                venue_occ[cand.venue_id].pop()
                team_matches[rq.home_id].pop()
                team_matches[rq.away_id].pop()
                del placed[rq.id]
                # 局部可行但会导致后续无解，同样留痕以便解释
                rejections.append(Rejection(
                    cand.slot_id, cand.venue_id, cand.start_utc,
                    ["no_feasible_completion"]))
            failed_at[rq.id] = rejections
            return False

        if not backtrack(0):
            unscheduled = []
            for rid in order:
                if rid in result:
                    continue
                rq = req_by_id[rid]
                rej = failed_at.get(rid, [])
                if not rej and exhausted:
                    # 搜索预算耗尽前根本没轮到它：解释为何没有结论
                    rej = [Rejection(-1, "", datetime.min.replace(
                        tzinfo=ZoneInfo("UTC")), ["search_budget"])]
                unscheduled.append(UnscheduledMatch(rq, rej))
            raise SchedulingImpossible(unscheduled)

        scheduled = [result[rid] for rid in order]
        decisions = {rid: ev for rid, ev in trace.items()}
        return scheduled, decisions, locked_out

    def _candidates_for(self, req: GameRequest) -> list[Candidate]:
        home = self.teams[req.home_id]
        if self.rules.require_home_venue and home.home_venue_id is not None:
            return [c for c in self.candidates
                    if c.venue_id == home.home_venue_id]
        return list(self.candidates)


def _dedup(reasons: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in reasons:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
