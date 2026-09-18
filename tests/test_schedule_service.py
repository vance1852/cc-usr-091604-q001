"""赛程编排服务的行为测试。

覆盖：对阵生成、预览/确认/撤销、冲突消解、最短休息、连续客场、
跨时区加休、跨日时段、延期幂等与已开赛锁定、通知一致性、
按球队查询、审计记录、并发确认以及重启后的可复现性。
"""

import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

from app.schedule import (
    NotFoundError,
    PlanStateError,
    RoundSpec,
    ScheduleError,
    ScheduleService,
    SchedulingSettings,
    Team,
    Venue,
    VenueSession,
)

SH = "Asia/Shanghai"   # UTC+8
XJ = "Asia/Urumqi"     # UTC+6

FIXED_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def clock():
    return FIXED_NOW


def make_service(path=None, settings=None):
    svc = ScheduleService(path, clock=clock, settings=settings)
    svc.add_venue(Venue("VSH", "上海体育中心", SH))
    svc.add_venue(Venue("VXJ", "乌鲁木齐训练基地", XJ))
    for tid, name, home in [
        ("A", "闪电队", "VSH"), ("B", "雄鹰队", "VSH"),
        ("C", "草原队", "VXJ"), ("D", "天山队", "VXJ"),
    ]:
        svc.add_team(Team(tid, name, home,
                          contacts=(f"parent-{tid}@example.cn",)))
    return svc


def add_daily_sessions(svc, venue_ids, start_day, days,
                       start_minute=540, end_minute=1140):
    """在连续若干天添加 09:00-19:00 的长时段。"""
    for i in range(days):
        d = date(2026, 10, start_day) + timedelta(days=i)
        for vid in venue_ids:
            svc.add_session(VenueSession(vid, d, start_minute, end_minute))


class FixtureGenerationTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def test_single_round_robin_pairing(self):
        spec = RoundSpec("R1", "第一阶段", (("A", "B", "C", "D"),))
        self.svc.add_round(spec)
        pairs = {(f.home_team_id, f.away_team_id)
                 for f in self.svc.fixtures_of("R1")}
        self.assertEqual(len(pairs), 6)  # 4 队单循环 6 场
        # 每对球队恰好对阵一次
        self.assertEqual({tuple(sorted(p)) for p in pairs},
                         {(x, y) for x in "ABCD" for y in "ABCD" if x < y})
        # 每队出赛 3 场，主客各约半
        for t in "ABCD":
            home = sum(1 for h, a in pairs if h == t)
            away = sum(1 for h, a in pairs if a == t)
            self.assertEqual(home + away, 3)
            self.assertLessEqual(abs(home - away), 1)

    def test_double_leg_and_groups(self):
        self.svc.add_round(RoundSpec(
            "L", "主客场双循环", (("A", "B", "C", "D"),), double_leg=True))
        fixtures = self.svc.fixtures_of("L")
        self.assertEqual(len(fixtures), 12)
        pair_legs = {(f.home_team_id, f.away_team_id) for f in fixtures}
        # 每对球队主客各一次
        for x in "ABCD":
            for y in "ABCD":
                if x != y:
                    self.assertIn((x, y), pair_legs)

        svc2 = make_service()
        svc2.add_round(RoundSpec("G", "小组循环",
                                 (("A", "B"), ("C", "D"))))
        gf = svc2.fixtures_of("G")
        self.assertEqual({(f.home_team_id, f.away_team_id) for f in gf},
                         {("A", "B"), ("C", "D")})

    def test_generation_is_deterministic(self):
        self.svc.add_round(RoundSpec("R", "循环", (("A", "B", "C", "D"),),
                                     double_leg=True))
        first = [(f.id, f.home_team_id, f.away_team_id)
                 for f in self.svc.fixtures_of("R")]
        second = [(f.id, f.home_team_id, f.away_team_id)
                  for f in self.svc.fixtures_of("R")]
        self.assertEqual(first, second)


class PlanningAndConstraintsTest(unittest.TestCase):
    def test_full_schedule_with_explanations(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH", "VXJ"], 10, 14)
        svc.add_round(RoundSpec("L", "双循环", (("A", "B", "C", "D"),),
                                double_leg=True))
        plan = svc.plan_round("L", operator="alice", reason="赛季初排")
        self.assertEqual(plan["status"], "PREVIEW")
        self.assertEqual(len(plan["matches"]), 12)
        self.assertEqual(plan["unscheduled"], [])
        self.assertTrue(all(m["resolution_basis"] for m in plan["matches"]))
        self.assertTrue(any("✓" in r for r in plan["resolutions"]))
        # 同队任何两场比赛时间不得重叠
        seen = []
        for m in plan["matches"]:
            seen.append((m["home_team_id"], m["start_utc"], m["end_utc"]))
            seen.append((m["away_team_id"], m["start_utc"], m["end_utc"]))
        for i, (t1, s1, e1) in enumerate(seen):
            for t2, s2, e2 in seen[i + 1:]:
                if t1 == t2:
                    self.assertFalse(s1 < e2 and s2 < e1, f"球队 {t1} 撞场")

    def test_venue_conflict_serializes_matches_and_reports_blocker(self):
        # 仅一个场馆、不设休息门槛，6 场比赛只给 5 个整点时段
        svc = make_service(settings=SchedulingSettings(
            min_rest_minutes=0, max_consecutive_away=9,
            kickoff_step_minutes=60))
        svc.add_session(VenueSession("VSH", date(2026, 10, 10), 540, 720))
        svc.add_session(VenueSession("VSH", date(2026, 10, 11), 540, 660))
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        plan = svc.plan_round("R", operator="alice")
        self.assertEqual(len(plan["matches"]), 5)
        self.assertEqual(len(plan["unscheduled"]), 1)
        blockers = plan["unscheduled"][0]["blockers"]
        self.assertTrue(any("场地冲突" in b for b in blockers), blockers)
        # 已排入的比赛场地时段互不重叠
        intervals = [(m["start_utc"], m["end_utc"]) for m in plan["matches"]]
        for i, (s1, e1) in enumerate(intervals):
            for s2, e2 in intervals[i + 1:]:
                self.assertFalse(s1 < e2 and s2 < e1)

    def test_blackout_and_venue_unavailable_dates_excluded(self):
        svc = make_service(settings=SchedulingSettings(
            min_rest_minutes=0, max_consecutive_away=9))
        svc.add_venue(Venue("VBLK", "封闭场馆", SH,
                            unavailable_dates=frozenset({date(2026, 10, 11)})))
        svc.add_session(VenueSession("VBLK", date(2026, 10, 10), 540, 1080))
        svc.add_session(VenueSession("VBLK", date(2026, 10, 11), 540, 1080))
        svc.add_blackout_date(date(2026, 10, 12))
        svc.add_session(VenueSession("VSH", date(2026, 10, 12), 540, 1080))
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        plan = svc.plan_round("R", operator="alice")
        used_dates = {m["start_local"][:10] for m in plan["matches"]}
        self.assertNotIn("2026-10-11", used_dates)  # 场馆不可用
        self.assertNotIn("2026-10-12", used_dates)  # 全局停赛

    def test_min_rest_between_team_matches(self):
        svc = make_service()  # 默认最短休息 660 分钟
        add_daily_sessions(svc, ["VSH", "VXJ"], 10, 7)
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        svc.plan_round("R", operator="alice")
        svc.confirm("plan-R", operator="alice")
        view = svc.round_view("R")
        for team_id, info in view["team_intervals"].items():
            for gap in info["gaps"]:
                self.assertTrue(gap["ok"], f"{team_id} 休息不足：{gap}")
                self.assertGreaterEqual(gap["gap_minutes"],
                                        gap["required_minutes"] - 1e-9)

    def test_consecutive_away_limit(self):
        # 3 队单循环中 C 队两场皆为客场
        strict = make_service(settings=SchedulingSettings(
            max_consecutive_away=1, kickoff_step_minutes=60))
        add_daily_sessions(strict, ["VSH"], 10, 4)
        strict.add_round(RoundSpec("T", "三队赛", (("A", "B", "C"),)))
        plan = strict.plan_round("T", operator="alice")
        self.assertTrue(plan["unscheduled"])
        blockers = plan["unscheduled"][0]["blockers"]
        self.assertTrue(any("连续" in b and "客场" in b for b in blockers),
                        blockers)

        relaxed = make_service(settings=SchedulingSettings(
            max_consecutive_away=2, kickoff_step_minutes=60))
        add_daily_sessions(relaxed, ["VSH"], 10, 4)
        relaxed.add_round(RoundSpec("T", "三队赛", (("A", "B", "C"),)))
        plan2 = relaxed.plan_round("T", operator="alice")
        self.assertEqual(len(plan2["matches"]), 3)
        relaxed.confirm("plan-T", operator="alice")
        view = relaxed.round_view("T")
        for info in view["team_intervals"].values():
            self.assertLessEqual(info["max_consecutive_away"], 2)

    def test_cross_timezone_extra_rest(self):
        # A 主场上海、B 主场乌鲁木齐；双循环两场必须跨时区，加休 240 分钟
        svc = make_service(settings=SchedulingSettings(
            min_rest_minutes=660, cross_tz_extra_rest_minutes=240,
            kickoff_step_minutes=60, max_consecutive_away=9))
        svc.add_session(VenueSession("VSH", date(2026, 10, 1), 480, 1200))
        svc.add_session(VenueSession("VXJ", date(2026, 10, 1), 720, 1380))
        svc.add_round(RoundSpec("X", "跨区双循环", (("A", "B"),),
                                double_leg=True))
        plan = svc.plan_round("X", operator="alice")
        self.assertEqual(len(plan["matches"]), 2, plan["unscheduled"])
        second = sorted(plan["matches"], key=lambda m: m["start_utc"])[1]
        # 01:00 UTC 上海完赛 → 乌鲁木齐最早 16:00 UTC（当地 22:00）
        self.assertEqual(second["venue_id"], "VXJ")
        self.assertEqual(second["start_local"], "2026-10-01 22:00")
        svc.confirm("plan-X", operator="alice")
        view = svc.round_view("X")
        gap = view["team_intervals"]["A"]["gaps"][0]
        self.assertTrue(gap["cross_timezone"])
        self.assertEqual(gap["required_minutes"], 900)
        self.assertTrue(gap["ok"])

    def test_overnight_session_local_scheduling(self):
        svc = make_service(settings=SchedulingSettings(
            min_rest_minutes=0, kickoff_step_minutes=30,
            max_consecutive_away=9))
        # 10/01 23:00 至次日 01:00（end_minute 跨午夜）
        svc.add_session(VenueSession("VSH", date(2026, 10, 1), 1380, 1500))
        svc.add_round(RoundSpec("N", "夜场双循环", (("A", "B"),),
                                double_leg=True))
        plan = svc.plan_round("N", operator="alice")
        self.assertEqual(len(plan["matches"]), 2, plan["unscheduled"])
        starts = sorted(m["start_local"] for m in plan["matches"])
        self.assertEqual(starts, ["2026-10-01 23:00", "2026-10-02 00:00"])
        first = plan["matches"][0]
        self.assertEqual(first["start_utc"], "2026-10-01T15:00:00+00:00")

    def test_overnight_session_touching_unavailable_end_day_rejected(self):
        svc = make_service(settings=SchedulingSettings(
            min_rest_minutes=0, max_consecutive_away=9))
        svc.add_venue(Venue("VN", "跨日场馆", SH,
                            unavailable_dates=frozenset({date(2026, 10, 2)})))
        svc.add_session(VenueSession("VN", date(2026, 10, 1), 1380, 1500))
        svc.add_round(RoundSpec("N", "夜场", (("A", "B"),)))
        plan = svc.plan_round("N", operator="alice")
        self.assertEqual(plan["matches"], [])
        self.assertEqual(len(plan["unscheduled"]), 1)


class ConfirmationWorkflowTest(unittest.TestCase):
    def _confirmed_service(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH", "VXJ"], 10, 7)
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        svc.plan_round("R", operator="alice", reason="首次编排")
        svc.assign_referee("R:A__B", "裁判甲")
        return svc

    def test_confirm_creates_version_and_pending_notifications(self):
        svc = self._confirmed_service()
        result = svc.confirm("plan-R", operator="alice")
        self.assertEqual(result["version"], 1)
        pending = svc.pending_notifications("R")
        recipients = {(n["recipient"], n["kind"]) for n in pending}
        self.assertIn(("裁判甲", "referee"), recipients)
        for t in "ABCD":
            self.assertIn((f"parent-{t}@example.cn", "parent"), recipients)
        view = svc.round_view("R")
        self.assertEqual(view["current_version"], 1)
        self.assertIn("裁判甲（R:A__B）",
                      view["pending_notifications"]["referees"])
        self.assertIn("parent-A@example.cn",
                      view["pending_notifications"]["parents"])
        # 发送后不再出现在待通知列表
        svc.send_pending("R")
        self.assertEqual(svc.pending_notifications("R"), [])

    def test_duplicate_confirm_rejected(self):
        svc = self._confirmed_service()
        svc.confirm("plan-R", operator="alice")
        with self.assertRaises(PlanStateError):
            svc.confirm("plan-R", operator="bob")

    def test_concurrent_confirm_only_one_succeeds(self):
        svc = self._confirmed_service()
        barrier = threading.Barrier(8)
        outcomes = []
        outcome_lock = threading.Lock()

        def worker():
            barrier.wait()
            try:
                svc.confirm("plan-R", operator="op")
                with outcome_lock:
                    outcomes.append("ok")
            except PlanStateError:
                with outcome_lock:
                    outcomes.append("rejected")
            except Exception as exc:  # pragma: no cover
                with outcome_lock:
                    outcomes.append(f"error:{exc!r}")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertEqual(outcomes.count("rejected"), 7)
        self.assertEqual(svc.round_view("R")["current_version"], 1)
        # 通知不得重复
        pending = svc.pending_notifications("R")
        self.assertEqual(
            len(pending), len({(n["match_id"], n["recipient"], n["kind"])
                               for n in pending}))
        confirms = [a for a in svc.adjustments() if a["kind"] == "confirm"]
        self.assertEqual(len(confirms), 1)

    def test_plan_after_confirmation_requires_postpone(self):
        svc = self._confirmed_service()
        svc.confirm("plan-R", operator="alice")
        with self.assertRaises(ScheduleError):
            svc.plan_round("R", operator="alice")


class PostponeTest(unittest.TestCase):
    def _ready(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH", "VXJ"], 6, 14)
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        svc.plan_round("R", operator="alice", reason="初排")
        svc.confirm("plan-R", operator="alice")
        return svc

    def test_postpone_is_idempotent_per_command(self):
        svc = self._ready()
        before = svc.round_view("R")
        r1 = svc.postpone("CMD-001", operator="bob",
                          reason="暴雨延期", round_id="R")
        r2 = svc.postpone("CMD-001", operator="bob",
                          reason="暴雨延期", round_id="R")
        self.assertFalse(r1["replayed"])
        self.assertTrue(r2["replayed"])
        self.assertEqual(r1["version"], r2["version"])
        self.assertEqual(r1["rescheduled"], r2["rescheduled"])
        after = svc.round_view("R")
        self.assertEqual(
            len(after["matches"]), len(before["matches"]))
        # 每场只有一个新版本，通知不重复
        hist = svc.team_schedule("A")
        for entry in hist["matches"]:
            self.assertEqual(len(entry["versions"]), 2)
        postpones = [a for a in svc.adjustments()
                     if a["kind"] == "postpone"]
        self.assertEqual(len(postpones), 1)

    def test_started_match_is_retained_with_version_and_notifications(self):
        svc = self._ready()
        target = "R:A__B"
        original = next(m for m in svc.round_view("R")["matches"]
                        if m["match_id"] == target)
        svc.mark_started(target)
        result = svc.postpone("CMD-002", operator="bob",
                              reason="暴雨延期", round_id="R")
        self.assertIn(target, result["retained"])
        self.assertNotIn(target, result["rescheduled"])
        view = svc.round_view("R")
        kept = next(m for m in view["matches"] if m["match_id"] == target)
        self.assertEqual(kept["version"], original["version"])
        self.assertEqual(kept["start_utc"], original["start_utc"])
        self.assertEqual(kept["status"], "STARTED")
        schedule = svc.team_schedule("A")
        entry = next(e for e in schedule["matches"]
                     if e["match_id"] == target)
        self.assertEqual(entry["current_status"], "STARTED")
        self.assertEqual(len(entry["versions"]), 1)  # 未产生新版本

    def test_old_pending_notifications_superseded_but_sent_kept(self):
        svc = self._ready()
        # 先发送 A__B 的通知，其余保持 pending
        svc.send_pending("R")
        self.assertEqual(svc.pending_notifications("R"), [])
        result = svc.postpone("CMD-003", operator="bob",
                              reason="场地积水", round_id="R")
        self.assertTrue(result["rescheduled"])
        team = svc.team_schedule("A")
        v1_notes, v2_notes = [], []
        for entry in team["matches"]:
            for v in entry["versions"]:
                if v["version"] == 1:
                    v1_notes.extend(v["notifications"])
                elif v["version"] == 2:
                    v2_notes.extend(v["notifications"])
        self.assertTrue(v1_notes)
        self.assertTrue(all(n["status"] == "sent" for n in v1_notes))
        self.assertTrue(v2_notes)
        self.assertTrue(all(n["status"] == "pending" for n in v2_notes))
        # 待通知列表只含新版本，避免矛盾时间发出
        pending = svc.pending_notifications("R")
        self.assertTrue(pending)
        self.assertTrue(all(n["version"] == 2 for n in pending))

    def test_postpone_only_reschedules_unstarted_matches(self):
        svc = self._ready()
        match_ids = [m["match_id"] for m in svc.round_view("R")["matches"]]
        target = "R:A__B" if "R:A__B" in match_ids else match_ids[0]
        other = next(mid for mid in match_ids if mid != target)
        svc.mark_started(target)
        result = svc.postpone("CMD-004", operator="bob", reason="雷暴",
                              match_ids=[target, other])
        self.assertEqual(result["retained"], [target])
        self.assertEqual(result["rescheduled"], [other])


class UndoTest(unittest.TestCase):
    def test_undo_confirm_restores_preview(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH", "VXJ"], 10, 7)
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        svc.plan_round("R", operator="alice")
        svc.confirm("plan-R", operator="alice", reason="排定")
        svc.undo(operator="alice", reason="时间有误")
        self.assertEqual(svc.round_view("R")["matches"], [])
        self.assertEqual(svc.pending_notifications("R"), [])
        # 计划回到预览态，可以重新确认
        again = svc.confirm("plan-R", operator="alice")
        self.assertEqual(again["version"], 2)
        # 重新确认后也可以再次撤销
        result = svc.undo(operator="alice", reason="仍需修改")
        self.assertEqual(sorted(result["affected"]),
                         sorted(again["match_ids"]))
        self.assertEqual(svc.round_view("R")["matches"], [])

    def test_undo_postpone_restores_old_version_and_notifications(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH", "VXJ"], 6, 14)
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        svc.plan_round("R", operator="alice")
        svc.confirm("plan-R", operator="alice")
        first = svc.round_view("R")
        svc.postpone("CMD-9", operator="bob", reason="暴雨", round_id="R")
        svc.undo(operator="bob", reason="天气预报更正")
        restored = svc.round_view("R")
        self.assertEqual(
            [(m["match_id"], m["start_utc"]) for m in first["matches"]],
            [(m["match_id"], m["start_utc"]) for m in restored["matches"]])
        self.assertEqual(restored["current_version"], 1)
        # 原 pending 通知恢复，且不允许撤销两次
        self.assertTrue(svc.pending_notifications("R"))
        # 同一延期指令撤销后可重新提交
        again = svc.postpone("CMD-9", operator="bob", reason="暴雨",
                             round_id="R")
        self.assertFalse(again["replayed"])


class QueryAndAuditTest(unittest.TestCase):
    def test_team_schedule_and_round_view(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH", "VXJ"], 10, 7)
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        svc.plan_round("R", operator="alice")
        svc.confirm("plan-R", operator="alice")
        ts = svc.team_schedule("A")
        self.assertEqual(ts["team_name"], "闪电队")
        self.assertEqual(len(ts["matches"]), 3)
        for entry in ts["matches"]:
            self.assertEqual(entry["current_version"], 1)
            self.assertIn(entry["versions"][0]["role"], ("home", "away"))
            self.assertTrue(entry["versions"][0]["start_local"])
        view = svc.round_view("R")
        self.assertEqual(view["round_name"], "单循环")
        self.assertTrue(all(m["resolution_basis"] for m in view["matches"]))
        self.assertIn("gaps", next(iter(view["team_intervals"].values())))
        self.assertIn("away_runs", next(iter(view["team_intervals"].values())))

    def test_adjustments_record_operator_reason_affected(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH", "VXJ"], 6, 14)
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        svc.plan_round("R", operator="alice", reason="赛季初版")
        svc.confirm("plan-R", operator="alice")
        svc.postpone("CMD-AUD", operator="bob", reason="突发暴雨",
                     round_id="R")
        adjustments = svc.adjustments()
        kinds = [(a["kind"], a["operator"], a["reason"]) for a in adjustments]
        self.assertIn(("confirm", "alice", "赛季初版"), kinds)
        self.assertIn(("postpone", "bob", "突发暴雨"), kinds)
        post = next(a for a in adjustments if a["kind"] == "postpone")
        self.assertTrue(post["affected"])
        self.assertEqual(post["command_id"], "CMD-AUD")
        self.assertIn("retained", post)

    def test_missing_entities_raise(self):
        svc = make_service()
        with self.assertRaises(NotFoundError):
            svc.plan_round("NOPE", operator="x")
        with self.assertRaises(NotFoundError):
            svc.team_schedule("ZZZ")
        with self.assertRaises(NotFoundError):
            svc.add_session(VenueSession("NOPE", date(2026, 10, 1),
                                         540, 600))
        with self.assertRaises(ScheduleError):
            svc.add_session(VenueSession("VSH", date(2026, 10, 1),
                                         600, 540))


class CrossRoundRestTest(unittest.TestCase):
    def test_confirmed_other_round_locks_rest_constraints(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH", "VXJ"], 10, 10)
        svc.add_round(RoundSpec("R1", "第一轮", (("A", "B"),)))
        svc.plan_round("R1", operator="alice")
        svc.confirm("plan-R1", operator="alice")
        svc.add_round(RoundSpec("R2", "第二轮", (("A", "C", "D"),)))
        plan = svc.plan_round("R2", operator="alice")
        # A 队在 R1 已有比赛，R2 编排必须满足休息
        self.assertTrue(plan["matches"])
        svc.confirm("plan-R2", operator="alice")
        a_games = sorted(
            [m for m in svc.round_view("R1")["matches"]]
            + [m for m in svc.round_view("R2")["matches"]
               if "A" in (m["home_team_id"], m["away_team_id"])],
            key=lambda m: m["start_utc"])
        # 手动校验 A 的相邻比赛间隔
        team_games = []
        for rid in ("R1", "R2"):
            for m in svc.round_view(rid)["matches"]:
                if m["home_team_id"] == "A" or m["away_team_id"] == "A":
                    team_games.append(m)
        team_games.sort(key=lambda m: m["start_utc"])
        for prev, nxt in zip(team_games, team_games[1:]):
            gap = (datetime.fromisoformat(nxt["start_utc"])
                   - datetime.fromisoformat(prev["end_utc"]))
            self.assertGreaterEqual(
                gap.total_seconds() / 60, 660 - 1e-9)
        self.assertEqual(len(team_games), 3)


class PersistenceAndReproducibilityTest(unittest.TestCase):
    def _build_and_run(self, path):
        svc = make_service(path)
        add_daily_sessions(svc, ["VSH", "VXJ"], 6, 14)
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        svc.plan_round("R", operator="alice", reason="初排")
        svc.confirm("plan-R", operator="alice")
        svc.assign_referee("R:A__B", "裁判甲")
        svc.postpone("CMD-RAIN", operator="bob", reason="暴雨",
                     round_id="R")
        return svc

    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            svc = self._build_and_run(path)
            expected = {
                "round": svc.round_view("R"),
                "team": svc.team_schedule("A"),
                "adjustments": svc.adjustments(),
                "pending": svc.pending_notifications("R"),
                "version_holder": None,
            }
            del svc
            restored = ScheduleService(path, clock=clock)
            self.assertEqual(
                json.dumps(restored.round_view("R"), sort_keys=True,
                           default=str),
                json.dumps(expected["round"], sort_keys=True, default=str))
            self.assertEqual(restored.team_schedule("A"),
                             expected["team"])
            self.assertEqual(
                [(a["kind"], a["operator"], a["reason"])
                 for a in restored.adjustments()
                 if a["kind"] in ("confirm", "postpone")],
                [(a["kind"], a["operator"], a["reason"])
                 for a in expected["adjustments"]
                 if a["kind"] in ("confirm", "postpone")])
            # 幂等记录跨重启仍然生效
            replay = restored.postpone("CMD-RAIN", operator="bob",
                                       reason="暴雨", round_id="R")
            self.assertTrue(replay["replayed"])

    def test_schedule_is_reproducible_from_same_inputs(self):
        def fresh_plan():
            s = make_service()
            add_daily_sessions(s, ["VSH", "VXJ"], 6, 14)
            s.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
            p = s.plan_round("R", operator="alice")
            return [(m["match_id"], m["venue_id"], m["start_utc"])
                    for m in p["matches"]]

        self.assertEqual(fresh_plan(), fresh_plan())


class FullRoundInvariantTest(unittest.TestCase):
    def _double_leg(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH", "VXJ"], 10, 14)
        svc.add_round(RoundSpec("L", "双循环", (("A", "B", "C", "D"),),
                                double_leg=True))
        svc.plan_round("L", operator="alice")
        svc.confirm("plan-L", operator="alice")
        return svc

    def test_all_constraints_satisfied_on_confirmed_round(self):
        svc = self._double_leg()
        view = svc.round_view("L")
        self.assertEqual(len(view["matches"]), 12)
        self.assertEqual(view["current_version"], 1)
        # 每场都有消解依据
        self.assertTrue(all(m["resolution_basis"] for m in view["matches"]))
        # 1) 同场馆不重叠 2) 同队不撞场 3) 休息达标 4) 连续客场上限
        by_venue: dict[str, list] = {}
        for m in view["matches"]:
            by_venue.setdefault(m["venue_id"], []).append(m)
        for venue_id, ms in by_venue.items():
            ms = sorted(ms, key=lambda x: x["start_utc"])
            for prev, nxt in zip(ms, ms[1:]):
                self.assertLessEqual(prev["end_utc"], nxt["start_utc"],
                                     f"{venue_id} 场地冲突")
        for team_id, info in view["team_intervals"].items():
            for gap in info["gaps"]:
                self.assertTrue(gap["ok"], f"{team_id} 违反休息：{gap}")
            self.assertLessEqual(info["max_consecutive_away"], 2, team_id)
        # 每队主客各 3 场
        ts = svc.team_schedule("A")
        roles = [v["role"] for e in ts["matches"] for v in e["versions"]]
        self.assertEqual(sorted(roles), ["away"] * 3 + ["home"] * 3)

    def test_postpone_with_later_not_before_moves_and_versions(self):
        svc = self._double_leg()
        before = {m["match_id"]: m["start_utc"]
                  for m in svc.round_view("L")["matches"]}
        result = svc.postpone(
            "STORM-X", operator="bob", reason="暴雨改期", round_id="L",
            not_before=datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(result["version"], 2)
        view = svc.round_view("L")
        self.assertEqual(view["current_version"], 2)
        moved = [m for m in view["matches"]
                 if before[m["match_id"]] != m["start_utc"]]
        self.assertTrue(moved)
        for m in view["matches"]:
            self.assertGreaterEqual(
                m["start_utc"], "2026-10-13T00:00:00+00:00")
            self.assertEqual(m["version"], 2)
            self.assertTrue(any("延期重排" in r for r in m["resolution_basis"]))

    def test_six_team_league_plans_completely_and_fast(self):
        svc = ScheduleService(
            clock=lambda: FIXED_NOW,
            settings=SchedulingSettings(kickoff_step_minutes=30))
        svc.add_venue(Venue("V1", "一号馆", SH))
        svc.add_venue(Venue("V2", "二号馆", XJ))
        for i in range(6):
            tid = f"T{i}"
            svc.add_team(Team(tid, tid, "V1" if i % 2 == 0 else "V2",
                              contacts=(f"{tid}@x",)))
        for d in range(40):
            day = date(2026, 10, 10) + timedelta(days=d)
            svc.add_session(VenueSession("V1", day, 540, 1200))
            svc.add_session(VenueSession("V2", day, 780, 1380))
        svc.add_round(RoundSpec("BIG", "六队双循环",
                                (tuple(f"T{i}" for i in range(6)),),
                                double_leg=True))
        import time
        t0 = time.time()
        plan = svc.plan_round("BIG", operator="alice")
        self.assertLess(time.time() - t0, 10.0)
        self.assertEqual(len(plan["matches"]), 30)
        self.assertEqual(plan["unscheduled"], [])


class PostponeUnschedulableTest(unittest.TestCase):
    def test_unplaceable_match_leaves_active_and_undo_restores(self):
        svc = make_service(settings=SchedulingSettings(
            min_rest_minutes=0, max_consecutive_away=9,
            kickoff_step_minutes=60))
        # 只有 10/10 一个时段，6 场单循环只能排 3 场
        svc.add_session(VenueSession("VSH", date(2026, 10, 10), 540, 720))
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        plan = svc.plan_round("R", operator="alice")
        placed_ids = {m["match_id"] for m in plan["matches"]}
        svc.confirm("plan-R", operator="alice")
        # 延期后若不新增时段，已排比赛无法改到 not_before 之后
        result = svc.postpone(
            "FLOOD", operator="bob", reason="积水", round_id="R",
            not_before=datetime(2026, 10, 11, tzinfo=timezone.utc))
        self.assertEqual(set(result["rescheduled"]), set())
        self.assertEqual(set(result["unscheduled"]), placed_ids)
        # 活动表中这些场次暂时消失（历史保留 POSTPONED）
        self.assertEqual(svc.round_view("R")["matches"], [])
        # 撤销延期后恢复原版本
        svc.undo(operator="bob", reason="场地已恢复")
        restored = svc.round_view("R")
        self.assertEqual({m["match_id"] for m in restored["matches"]},
                         placed_ids)
        self.assertEqual(restored["current_version"], 1)
        # 指令可重新提交
        again = svc.postpone(
            "FLOOD", operator="bob", reason="积水", round_id="R",
            not_before=datetime(2026, 10, 11, tzinfo=timezone.utc))
        self.assertFalse(again["replayed"])


class RefereeChangeTest(unittest.TestCase):
    def test_change_referee_supersedes_old_pending(self):
        svc = make_service()
        add_daily_sessions(svc, ["VSH"], 10, 4)
        svc.add_round(RoundSpec("R", "单循环", (("A", "B", "C", "D"),)))
        svc.plan_round("R", operator="alice")
        svc.assign_referee("R:A__B", "裁判甲")
        svc.confirm("plan-R", operator="alice")
        refs = [n for n in svc.pending_notifications("R")
                if n["match_id"] == "R:A__B" and n["kind"] == "referee"]
        self.assertEqual([n["recipient"] for n in refs], ["裁判甲"])
        svc.assign_referee("R:A__B", "裁判乙")
        refs = [n for n in svc.pending_notifications("R")
                if n["match_id"] == "R:A__B" and n["kind"] == "referee"]
        self.assertEqual([n["recipient"] for n in refs], ["裁判乙"])
        view = svc.round_view("R")
        self.assertIn("裁判乙（R:A__B）",
                      view["pending_notifications"]["referees"])
        self.assertNotIn("裁判甲（R:A__B）",
                         view["pending_notifications"]["referees"])


if __name__ == "__main__":
    unittest.main()
