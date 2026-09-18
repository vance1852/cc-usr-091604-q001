"""并发确认、跨日时段、重启复现与无解解释的专项测试。"""
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

from app.models import Rules, SchedulingImpossible
from app.service import ScheduleService
from tests.helpers import build_league


class ConcurrentConfirmTest(unittest.TestCase):
    def test_two_competing_drafts_only_one_confirms(self):
        svc = build_league()
        p = svc.preview("L1", idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        # 基于同一父版本各做一份延期草稿
        d1 = svc.postpone("L1", [p.matches[1].match_id], reason="暴雨-甲",
                          idempotency_key="storm-a")
        d2 = svc.postpone("L1", [p.matches[2].match_id], reason="暴雨-乙",
                          idempotency_key="storm-b")
        results: list = []
        errors: list = []

        def confirm(version_id):
            try:
                results.append(svc.confirm("L1", version_id=version_id))
            except Exception as e:  # noqa: BLE001 - 断言见下
                errors.append(e)

        t1 = threading.Thread(target=confirm, args=(d1.version_id,))
        t2 = threading.Thread(target=confirm, args=(d2.version_id,))
        t1.start()
        t2.start()
        t1.join(5)
        t2.join(5)

        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        # 输家必须是版本冲突错误，且当前版本只被推进一次
        from app.models import ScheduleConflict
        self.assertIsInstance(errors[0], ScheduleConflict)
        current = svc.list_versions("L1")
        confirmed = [v for v in current if v["status"] == "confirmed"]
        self.assertEqual(2, len(confirmed))  # 初稿 + 获胜的延期
        winner = results[0]["version_id"]
        self.assertEqual(
            winner, svc.list_versions("L1")[-1]["version_id"]
            if svc.list_versions("L1")[-1]["status"] == "confirmed"
            else winner)

    def test_concurrent_same_idempotency_key_collapses_to_one_draft(self):
        svc = build_league()
        p = svc.preview("L1", idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        drafts: list = []
        errors: list = []

        def postpone():
            try:
                d = svc.postpone("L1", [p.matches[1].match_id],
                                 reason="暴雨", idempotency_key="storm-x")
                drafts.append(d.version_id)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=postpone) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        # 无论成功还是唯一约束兜底，最终只有一份草稿
        self.assertEqual([], [str(e) for e in errors])
        self.assertEqual(1, len(set(drafts)))
        versions = [v for v in svc.list_versions("L1")
                    if v["kind"] == "postpone"]
        self.assertEqual(1, len(versions))


class CrossMidnightServiceTest(unittest.TestCase):
    def test_game_after_midnight_displayed_in_local_calendar(self):
        svc = ScheduleService(":memory:")
        svc.create_league("LX", "夜间联赛", "home_away",
                          Rules(game_minutes=90, rest_minutes=0,
                                max_consecutive_away=9))
        svc.add_venue("LX", "va", "海边球场", "Asia/Shanghai")
        svc.add_venue("LX", "vb", "山坡球场", "Asia/Shanghai")
        svc.add_team("LX", "a", "甲队", "va")
        svc.add_team("LX", "b", "乙队", "vb")
        # 23:00 开场，跨日到次日 01:30；双循环两场需要两个比赛日
        svc.add_slot("LX", "va", date(2026, 5, 1), 23 * 60,
                     24 * 60 + 90)
        svc.add_slot("LX", "vb", date(2026, 5, 1), 23 * 60,
                     24 * 60 + 90)
        svc.add_slot("LX", "va", date(2026, 5, 3), 23 * 60,
                     24 * 60 + 90)
        svc.add_slot("LX", "vb", date(2026, 5, 3), 23 * 60,
                     24 * 60 + 90)
        p = svc.preview("LX", idempotency_key="init")
        svc.confirm("LX", version_id=p.version_id)
        rv = svc.get_round("LX", 1)
        self.assertEqual(1, len(rv.matches))
        m = rv.matches[0]
        self.assertEqual("2026-05-01 23:00", m.start_local)
        # UTC 存储同日，但场馆当地结束时间已跨入次日凌晨
        from zoneinfo import ZoneInfo
        local_end = m.end_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        local_start = m.start_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        self.assertEqual(local_end.date(),
                         local_start.date() + timedelta(days=1))

    def test_cross_day_slot_does_not_double_book(self):
        svc = ScheduleService(":memory:")
        svc.create_league("LY", "夜间联赛2", "home_away",
                          Rules(game_minutes=90, rest_minutes=0,
                                max_consecutive_away=9))
        svc.add_venue("LY", "va", "球场", "UTC")
        for tid in ("a", "b", "c", "d"):
            svc.add_team("LY", tid, tid, None)
        # 单一场馆、单一跨日窗口 23:00-00:30 只够一场球
        svc.add_slot("LY", "va", date(2026, 5, 1), 23 * 60,
                     24 * 60 + 30)
        with self.assertRaises(SchedulingImpossible) as cm:
            svc.preview("LY", idempotency_key="init")
        reasons = {x for u in cm.exception.unscheduled
                   for r in u.rejections for x in r.reasons}
        self.assertTrue(reasons & {"slot_too_short", "venue_overlap",
                                   "no_feasible_completion"})


class RestartReproducibilityTest(unittest.TestCase):
    def test_schedule_survives_restart_and_resolution_is_reproducible(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            svc = build_league(path)
            p = svc.preview("L1", operator="alice", reason="初稿",
                            idempotency_key="init")
            svc.confirm("L1", version_id=p.version_id)
            before = [(m.match_id, m.venue_id, m.start_utc.isoformat())
                      for m in svc.get_round("L1", 1).matches]
            notes_before = {m.match_id: [(r["slot_id"], tuple(r["reasons"]))
                                         for r in m.resolution]
                            for m in svc.get_round("L1", 1).matches}
            svc.close()

            # 重启：用新连接打开同一文件
            svc2 = ScheduleService(path)
            current = svc2.list_versions("L1")
            self.assertEqual("confirmed", current[0]["status"])
            after = [(m.match_id, m.venue_id, m.start_utc.isoformat())
                     for m in svc2.get_round("L1", 1).matches]
            self.assertEqual(before, after)
            notes_after = {m.match_id: [(r["slot_id"], tuple(r["reasons"]))
                                        for r in m.resolution]
                           for m in svc2.get_round("L1", 1).matches}
            self.assertEqual(notes_before, notes_after)

            # 重启后仍可继续业务：延期 + 确认
            target = after[1][0]
            d = svc2.postpone(
                "L1", [target], reason="暴雨", operator="bob",
                idempotency_key="storm",
                not_before_utc=datetime(2026, 3, 20, tzinfo=timezone.utc))
            svc2.confirm("L1", version_id=d.version_id)
            history = svc2.list_versions("L1")
            self.assertEqual(["initial", "postpone"],
                             [v["kind"] for v in history])
            svc2.close()
        finally:
            os.unlink(path)

    def test_pure_engine_output_is_byte_stable(self):
        # 直接对引擎做两次独立构造，输出逐字节一致
        from tests.helpers import build_league
        svc = build_league()
        p1 = svc.preview("L1", idempotency_key="init")
        sig1 = [(m.match_id, m.venue_id, m.start_utc.isoformat(),
                 m.end_utc.isoformat()) for m in p1.matches]
        # 放弃草稿后用新键重排，输入相同 => 输出相同
        p2 = svc.preview  # 已存在初稿，直接用引擎在服务层另开联赛对比
        svc_b = build_league()
        p3 = svc_b.preview("L1", idempotency_key="init")
        sig3 = [(m.match_id, m.venue_id, m.start_utc.isoformat(),
                 m.end_utc.isoformat()) for m in p3.matches]
        self.assertEqual(sig1, sig3)


class GroupModeServiceTest(unittest.TestCase):
    def test_group_round_robin(self):
        svc = ScheduleService(":memory:")
        svc.create_league("LG", "小组联赛", "group",
                          Rules(rest_minutes=0))
        svc.add_venue("LG", "v", "球场", "UTC")
        for tid, g in [("a", "G1"), ("b", "G1"), ("c", "G1"),
                       ("d", "G2"), ("e", "G2")]:
            svc.add_team("LG", tid, tid, "v", group_name=g)
        for d in [date(2026, 6, d) for d in range(1, 8)]:
            svc.add_slot("LG", "v", d, 9 * 60, 21 * 60)
        p = svc.preview("LG", idempotency_key="init")
        # G1 三队 3 场 + G2 两队 1 场
        self.assertEqual(4, len(p.matches))
        svc.confirm("LG", version_id=p.version_id)
        rv = svc.get_round("LG", 1)
        # 第 1 轮：G1 一场 + G2 一场
        self.assertEqual(2, len(rv.matches))


if __name__ == "__main__":
    unittest.main()
