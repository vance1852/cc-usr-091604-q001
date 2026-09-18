"""排程引擎约束测试：场地冲突、连续客场、休息、跨时区、跨日时段、
停赛日、锁定场、确定性与无解解释。"""
import unittest
from datetime import date, datetime, timedelta, timezone

from app.engine import Engine
from app.models import (
    GameRequest, PlannedMatch, Rules, Slot, Team, Venue,
    SchedulingImpossible,
)

UTC = timezone.utc


def dt(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def make_engine(*, teams, venues, slots, blackouts=None, rules=None):
    return Engine("L", teams, venues, slots, blackouts or set(),
                  rules or Rules())


def req(rid, rnd, home, away, floor=None):
    return GameRequest(rid, rnd, home, away, None, floor)


def sid_factory():
    n = 0
    def nxt():
        nonlocal n
        n += 1
        return n
    return nxt


class EngineBasicsTest(unittest.TestCase):
    def setUp(self):
        self.teams = [Team("a", "A", "va"), Team("b", "B", "vb"),
                      Team("c", "C", "vc"), Team("d", "D", "vd")]
        self.venues = [
            Venue("va", "VA", "UTC"), Venue("vb", "VB", "UTC"),
            Venue("vc", "VC", "UTC"), Venue("vd", "VD", "UTC")]
        self.next_id = sid_factory()

    def slots_each_venue(self, days, s=9 * 60, e=21 * 60):
        out = []
        for d in days:
            for v in ("va", "vb", "vc", "vd"):
                out.append(Slot(self.next_id(), v, d, s, e))
        return out

    def test_no_venue_or_team_overlap_and_feasible_round(self):
        slots = self.slots_each_venue(
            [date(2026, 4, 4), date(2026, 4, 5)])
        eng = make_engine(teams=self.teams, venues=self.venues, slots=slots)
        reqs = [req("m1", 1, "a", "b"), req("m2", 1, "c", "d")]
        planned, _, _ = eng.solve(reqs, [])
        self.assertEqual(2, len(planned))
        # 同一场馆时间段不重叠
        occ = {}
        for m in planned:
            for x in occ.get(m.venue_id, []):
                self.assertFalse(
                    m.start_utc < x.end_utc and x.start_utc < m.end_utc)
            occ.setdefault(m.venue_id, []).append(m)
        # 球队不能同时出现在两场
        seen = {}
        for m in planned:
            for t in (m.home_id, m.away_id):
                self.assertNotIn(t, seen)
                seen[t] = m

    def test_min_rest_between_rounds(self):
        # 每天一个窗口；两轮必须间隔 >= 20 小时
        slots = self.slots_each_venue(
            [date(2026, 4, 4), date(2026, 4, 5), date(2026, 4, 6)])
        rules = Rules(rest_minutes=20 * 60)
        eng = make_engine(teams=self.teams, venues=self.venues,
                          slots=slots, rules=rules)
        reqs = [req("m1", 1, "a", "b"), req("m2", 2, "b", "c")]
        planned, _, _ = eng.solve(reqs, [])
        b1 = next(m for m in planned if m.id == "m1")
        b2 = next(m for m in planned if m.id == "m2")
        gap = abs((b2.start_utc - b1.end_utc).total_seconds())
        self.assertGreaterEqual(gap, 20 * 3600 - 1)

    def test_rest_violation_makes_impossible_with_explanations(self):
        # 所有球队同一天各只有 2 小时窗口，两场球休息肯定不够
        slots = []
        nid = sid_factory()
        for v in ("va", "vb", "vc", "vd"):
            slots.append(Slot(nid(), v, date(2026, 4, 4),
                              9 * 60, 11 * 60))
        rules = Rules(rest_minutes=24 * 60, require_home_venue=True)
        eng = make_engine(teams=self.teams, venues=self.venues,
                          slots=slots, rules=rules)
        reqs = [req("m1", 1, "a", "b"), req("m2", 2, "b", "a")]
        with self.assertRaises(SchedulingImpossible) as cm:
            eng.solve(reqs, [])
        unscheduled = cm.exception.unscheduled
        # 至少有一场记录了拒绝原因，且原因能映射到中文解释
        all_reasons = {x for u in unscheduled for r in u.rejections
                       for x in r.reasons}
        self.assertTrue(all_reasons & {"rest", "no_feasible_completion"})

    def test_consecutive_away_limit(self):
        # 构造连续三个客场序列：b 第 1、2、3 轮都是客队
        slots = self.slots_each_venue(
            [date(2026, 4, d) for d in range(4, 10)])
        rules = Rules(max_consecutive_away=2)
        eng = make_engine(teams=self.teams, venues=self.venues,
                          slots=slots, rules=rules)
        reqs = [
            req("m1", 1, "a", "b"),
            req("m2", 2, "c", "b"),
            req("m3", 3, "d", "b"),
        ]
        with self.assertRaises(SchedulingImpossible) as cm:
            eng.solve(reqs, [])
        reasons = {x for u in cm.exception.unscheduled
                   for r in u.rejections for x in r.reasons}
        self.assertIn("consecutive_away", reasons)

    def test_cross_midnight_slot_converts_to_utc(self):
        # UTC 场馆 23:00-01:30 跨日：end_minute=1530
        venues = [Venue("va", "VA", "UTC")]
        teams = [Team("a", "A", "va"), Team("b", "B", None)]
        slots = [Slot(1, "va", date(2026, 4, 4), 23 * 60,
                      24 * 60 + 90)]
        rules = Rules(require_home_venue=False)
        eng = make_engine(teams=teams, venues=venues, slots=slots,
                          rules=rules)
        planned, _, _ = eng.solve([req("m1", 1, "a", "b")], [])
        self.assertEqual(planned[0].start_utc, dt(2026, 4, 4, 23))
        self.assertEqual(planned[0].end_utc, dt(2026, 4, 5, 0, 30))

    def test_cross_midnight_venue_overlap(self):
        # 同一跨日窗口放两场 90 分钟球，窗口 150 分钟只容一场
        venues = [Venue("va", "VA", "UTC")]
        teams = [Team("a", "A", "va"), Team("b", "B", None),
                 Team("c", "C", None), Team("d", "D", None)]
        slots = [Slot(1, "va", date(2026, 4, 4), 23 * 60,
                      24 * 60 + 90)]
        rules = Rules(require_home_venue=False, rest_minutes=0)
        eng = make_engine(teams=teams, venues=venues, slots=slots,
                          rules=rules)
        with self.assertRaises(SchedulingImpossible) as cm:
            eng.solve([req("m1", 1, "a", "b"),
                       req("m2", 2, "c", "d")], [])
        reasons = {x for u in cm.exception.unscheduled
                   for r in u.rejections for x in r.reasons}
        self.assertIn("venue_overlap", reasons)

    def test_timezone_extra_rest(self):
        # va 在 UTC-10（如檀香山），vb 在 UTC+0；相差 10 小时
        venues = [Venue("va", "VA", "Pacific/Honolulu"),
                  Venue("vb", "VB", "UTC")]
        teams = [Team("a", "A", "va"), Team("b", "B", "vb"),
                 Team("c", "C", None)]
        nid = sid_factory()
        slots = [
            # 檀香山当地 4/5 18:00 = UTC 4/6 04:00
            Slot(nid(), "va", date(2026, 4, 5), 18 * 60, 22 * 60),
            Slot(nid(), "vb", date(2026, 4, 5), 18 * 60, 22 * 60),
        ]
        rules = Rules(require_home_venue=True, rest_minutes=0,
                      tz_threshold_hours=2, tz_extra_rest_minutes=24 * 60)
        eng = make_engine(teams=teams, venues=venues, slots=slots,
                          rules=rules)
        # b 第 1 轮在 vb(UTC) 18:00 主场，第 2 轮作客檀香山（UTC
        # 次日 04:00），相隔仅 8.5 小时却跨 10 个时区 -> 无解
        reqs = [req("m1", 1, "b", "c"), req("m2", 2, "a", "b")]
        with self.assertRaises(SchedulingImpossible) as cm:
            eng.solve(reqs, [])
        reasons = {x for u in cm.exception.unscheduled
                   for r in u.rejections for x in r.reasons}
        self.assertIn("cross_timezone_rest", reasons)

    def test_blackout_blocks_day(self):
        slots = self.slots_each_venue([date(2026, 4, 4), date(2026, 4, 5)])
        blackouts = {("league", "*", date(2026, 4, 4))}
        eng = make_engine(teams=self.teams, venues=self.venues,
                          slots=slots, blackouts=blackouts)
        planned, _, _ = eng.solve([req("m1", 1, "a", "b")], [])
        # 只能落在 4 月 5 日（UTC 口径与 UTC 场馆一致）
        self.assertEqual(planned[0].start_utc.date(), date(2026, 4, 5))

    def test_venue_blackout_uses_local_calendar(self):
        # 檀香山场馆 4 月 4 日停赛；其当地 4/4 18:00 = UTC 4/5 04:00
        venues = [Venue("va", "VA", "Pacific/Honolulu")]
        teams = [Team("a", "A", "va"), Team("b", "B", None)]
        nid = sid_factory()
        slots = [
            Slot(nid(), "va", date(2026, 4, 4), 18 * 60, 22 * 60),
            Slot(nid(), "va", date(2026, 4, 5), 18 * 60, 22 * 60),
        ]
        blackouts = {("venue", "va", date(2026, 4, 4))}
        rules = Rules(require_home_venue=False)
        eng = make_engine(teams=teams, venues=venues, slots=slots,
                          blackouts=blackouts, rules=rules)
        planned, _, _ = eng.solve([req("m1", 1, "a", "b")], [])
        local = planned[0].start_utc.astimezone(
            __import__("zoneinfo").ZoneInfo("Pacific/Honolulu"))
        self.assertEqual(local.date(), date(2026, 4, 5))

    def test_locked_matches_are_immovable_and_respected(self):
        slots = self.slots_each_venue(
            [date(2026, 4, d) for d in range(4, 10)])
        eng = make_engine(teams=self.teams, venues=self.venues, slots=slots)
        locked = PlannedMatch("old", 1, "a", "b", "va",
                              dt(2026, 4, 4, 10), dt(2026, 4, 4, 11, 30))
        # b 在锁定场作客，第 2 轮再作客 c（主场 vc）：休息 18h 必须满足
        planned, _, locked_out = eng.solve(
            [req("m2", 2, "c", "b")], [locked])
        self.assertEqual(locked_out, [locked])
        self.assertEqual(1, len(planned))
        self.assertEqual("vc", planned[0].venue_id)
        self.assertGreaterEqual(
            planned[0].start_utc - locked.end_utc,
            timedelta(hours=18) - timedelta(seconds=1))

    def test_locked_away_game_counts_toward_consecutive_run(self):
        slots = self.slots_each_venue(
            [date(2026, 4, d) for d in range(4, 12)])
        rules = Rules(max_consecutive_away=2)
        eng = make_engine(teams=self.teams, venues=self.venues,
                          slots=slots, rules=rules)
        # b 第 1 轮已在客场（锁定场），第 2、3 轮又要作客 -> 第 3 场无解
        locked = PlannedMatch("old", 1, "a", "b", "va",
                              dt(2026, 4, 4, 10), dt(2026, 4, 4, 11, 30))
        with self.assertRaises(SchedulingImpossible) as cm:
            eng.solve([req("m2", 2, "c", "b"),
                       req("m3", 3, "d", "b")], [locked])
        reasons = {x for u in cm.exception.unscheduled
                   for r in u.rejections for x in r.reasons}
        self.assertIn("consecutive_away", reasons)

    def test_deterministic_across_runs(self):
        slots = self.slots_each_venue(
            [date(2026, 4, d) for d in range(4, 12)])
        # 手工对阵，注意不产生三连客（b 最多两连客，d/c 主客交替）
        # 直接用确定性的双循环生成器，保证输入本身合法
        from app import fixtures
        reqs = fixtures.generate("L", self.teams, "home_away")
        results = []
        for _ in range(3):
            eng = make_engine(teams=self.teams, venues=self.venues,
                              slots=slots)
            planned, _, _ = eng.solve(reqs, [])
            results.append([(m.id, m.venue_id, m.start_utc.isoformat())
                            for m in planned])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])


if __name__ == "__main__":
    unittest.main()
