"""服务层测试：预览/确认/延期/撤销/查询、幂等、审计与通知。"""
import unittest
from datetime import date, datetime, timezone

from app.models import ScheduleError
from tests.helpers import build_league


class PreviewConfirmTest(unittest.TestCase):
    def test_preview_then_confirm_lifecycle(self):
        svc = build_league()
        p = svc.preview("L1", operator="alice", reason="初稿",
                        idempotency_key="init")
        self.assertEqual("draft", p.status)
        self.assertEqual("initial", p.kind)
        self.assertEqual(12, len(p.matches))
        summary = svc.confirm("L1", version_id=p.version_id)
        self.assertEqual("confirmed", summary["status"])
        self.assertGreater(summary["notifications_created"], 0)
        versions = svc.list_versions("L1")
        self.assertEqual("confirmed", versions[0]["status"])
        self.assertEqual("alice", versions[0]["operator"])
        self.assertEqual("初稿", versions[0]["reason"])

    def test_initial_preview_idempotent_key_returns_same_draft(self):
        svc = build_league()
        p1 = svc.preview("L1", idempotency_key="init")
        p2 = svc.preview("L1", idempotency_key="init")
        self.assertEqual(p1.version_id, p2.version_id)

    def test_cannot_preview_twice_without_adjustment(self):
        svc = build_league()
        p = svc.preview("L1", idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        with self.assertRaises(ScheduleError):
            svc.preview("L1", idempotency_key="init-2")

    def test_confirm_unknown_version_fails(self):
        svc = build_league()
        with self.assertRaises(ScheduleError):
            svc.confirm("L1", version_id=999)


class PostponeTest(unittest.TestCase):
    def setUp(self):
        svc = build_league()
        p = svc.preview("L1", operator="alice", reason="初稿",
                        idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        self.svc = svc
        self.matches = p.matches
        self.target = p.matches[1].match_id
        self.original = p.matches[1].start_utc

    def test_postpone_only_reschedules_target_and_locks_others(self):
        draft = self.svc.postpone(
            "L1", [self.target], reason="暴雨", operator="bob",
            idempotency_key="storm",
            not_before_utc=datetime(2026, 3, 20, tzinfo=timezone.utc))
        self.assertEqual("postpone", draft.kind)
        by_id = {m.match_id: m for m in draft.matches}
        # 被延期的比赛不能早于下限
        self.assertGreaterEqual(
            by_id[self.target].start_utc,
            datetime(2026, 3, 20, tzinfo=timezone.utc))
        self.assertNotEqual(self.original, by_id[self.target].start_utc)
        self.assertEqual("rescheduled", by_id[self.target].change_type)
        # 其他场次原样锁定
        untouched = [m for mid, m in by_id.items() if mid != self.target]
        self.assertTrue(all(m.change_type == "unchanged" for m in untouched))
        self.assertTrue(all(m.status == "confirmed" for m in untouched))

    def test_duplicate_postpone_command_does_not_create_second_schedule(self):
        d1 = self.svc.postpone("L1", [self.target], reason="暴雨",
                               operator="bob", idempotency_key="storm")
        d2 = self.svc.postpone("L1", [self.target], reason="暴雨",
                               operator="bob", idempotency_key="storm")
        self.assertEqual(d1.version_id, d2.version_id)
        self.assertEqual(2, len(self.svc.list_versions("L1")))  # 初稿+1 草稿
        # 通知也只在确认后生成一份
        self.svc.confirm("L1", version_id=d1.version_id)
        notes = self.svc.get_notifications("L1", d1.version_id)
        self.assertEqual(
            len(notes),
            len({n["recipient"] + n["match_id"] for n in notes}))

    def test_postpone_requires_idempotency_key(self):
        with self.assertRaises(ScheduleError):
            self.svc.postpone("L1", [self.target], reason="x")

    def test_played_match_cannot_be_postponed(self):
        self.svc.kick_off("L1", self.matches[0].match_id)
        with self.assertRaises(ScheduleError):
            self.svc.postpone(
                "L1", [self.matches[0].match_id, self.target],
                reason="暴雨", idempotency_key="storm")
        # 失败后不产生草稿
        self.assertEqual(1, len(self.svc.list_versions("L1")))

    def test_played_match_stays_played_in_new_draft(self):
        self.svc.kick_off("L1", self.matches[0].match_id)
        d = self.svc.postpone("L1", [self.target], reason="暴雨",
                              idempotency_key="storm")
        status = self.svc._match_status(
            d.version_id, self.matches[0].match_id)
        self.assertEqual("played", status)
        self.svc.confirm("L1", version_id=d.version_id)
        self.assertEqual(
            "played",
            self.svc._match_status(d.version_id, self.matches[0].match_id))

    def test_postpone_round(self):
        # 按轮次整体延期
        d = self.svc.postpone("L1", round_no=1, reason="暴雨封场",
                              operator="bob", idempotency_key="round1")
        targets = [m for m in d.matches if m.round_no == 1]
        self.assertTrue(
            all(m.change_type == "rescheduled" for m in targets))
        others = [m for m in d.matches if m.round_no != 1]
        self.assertTrue(all(m.change_type == "unchanged" for m in others))

    def test_three_matches_postponed_together_stay_feasible(self):
        # 暴雨导致三场同时延期：全部不得回到原时段，且互相不冲突
        targets = [m.match_id for m in self.matches[:3]]
        originals = {m.match_id: (m.venue_id, m.start_utc)
                     for m in self.matches[:3]}
        d = self.svc.postpone(
            "L1", targets, reason="暴雨三连延期", operator="bob",
            idempotency_key="storm-3",
            not_before_utc=datetime(2026, 3, 20, tzinfo=timezone.utc))
        self.svc.confirm("L1", version_id=d.version_id)
        by_id = {m.match_id: m for m in d.matches}
        for mid in targets:
            self.assertNotEqual(
                originals[mid],
                (by_id[mid].venue_id, by_id[mid].start_utc))
        # 同一指令只有一份版本，受影响场次=3
        versions = [v for v in self.svc.list_versions("L1")
                    if v["idempotency_key"] == "storm-3"]
        self.assertEqual(1, len(versions))
        self.assertEqual(sorted(targets), versions[0]["affected_match_ids"])
        # 新版本中所有比赛仍然互不冲突
        occupied = set()
        for m in self.svc.get_team_schedule("L1", "t1")["matches"]:
            pass
        import itertools
        all_matches = []
        for rnd in range(1, 7):
            all_matches.extend(self.svc.get_round("L1", rnd).matches)
        for m1, m2 in itertools.combinations(all_matches, 2):
            if m1.venue_id == m2.venue_id:
                self.assertFalse(
                    m1.start_utc < m2.end_utc and m2.start_utc < m1.end_utc,
                    f"场地冲突: {m1.match_id} / {m2.match_id}")

    def test_round_view_resolution_has_chinese_explanation(self):
        d = self.svc.postpone(
            "L1", [self.target], reason="暴雨", operator="bob",
            idempotency_key="storm",
            not_before_utc=datetime(2026, 3, 20, tzinfo=timezone.utc))
        self.svc.confirm("L1", version_id=d.version_id)
        rv = self.svc.get_round("L1", 1)
        moved = next(m for m in rv.matches if m.match_id == self.target)
        self.assertTrue(moved.resolution)
        for note in moved.resolution:
            self.assertTrue(note["explanation"])
            # 每条解释都是中文文案，且能对应到原因代码
            for text in note["explanation"]:
                self.assertTrue(any("一" <= ch <= "鿿" for ch in text))

    def test_confirm_postpone_is_idempotent_on_repeat(self):
        d = self.svc.postpone("L1", [self.target], reason="暴雨",
                              idempotency_key="storm")
        first = self.svc.confirm("L1", version_id=d.version_id)
        second = self.svc.confirm("L1", version_id=d.version_id)
        self.assertFalse(first["duplicated"])
        self.assertTrue(second["duplicated"])
        self.assertEqual(0, second["notifications_created"])


class UndoGuardTest(unittest.TestCase):
    def test_undo_initial_version_is_rejected(self):
        svc = build_league()
        p = svc.preview("L1", idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        with self.assertRaises(ScheduleError):
            svc.undo("L1")

    def test_open_draft_blocks_second_initial_preview(self):
        svc = build_league()
        svc.preview("L1", idempotency_key="init")
        with self.assertRaises(ScheduleError):
            svc.preview("L1", idempotency_key="init-2")


class UndoTest(unittest.TestCase):
    def test_undo_restores_parent_schedule_but_keeps_history(self):
        svc = build_league()
        p = svc.preview("L1", idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        target = p.matches[1].match_id
        moved_time = p.matches[1].start_utc
        d = svc.postpone("L1", [target], reason="暴雨",
                         idempotency_key="storm")
        svc.confirm("L1", version_id=d.version_id)
        new_time = next(
            m.start_utc for m in svc.get_round("L1", 1).matches
            if m.match_id == target)
        self.assertNotEqual(moved_time, new_time)

        result = svc.undo("L1", operator="carol", reason="撤销错误延期")
        restored = next(
            m for m in svc.get_round("L1", 1).matches if m.match_id == target)
        self.assertEqual(moved_time, restored.start_utc)
        self.assertEqual([target], result["affected_match_ids"])
        # 历史版本与通知仍在
        versions = svc.list_versions("L1")
        self.assertEqual(["initial", "postpone", "undo"],
                         [v["kind"] for v in versions])
        old_notes = svc.get_notifications("L1", d.version_id)
        self.assertTrue(old_notes)

    def test_undo_audit_contains_reason_operator_affected(self):
        svc = build_league()
        p = svc.preview("L1", idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        d = svc.postpone("L1", [p.matches[1].match_id], reason="暴雨",
                         operator="bob", idempotency_key="storm")
        svc.confirm("L1", version_id=d.version_id)
        svc.undo("L1", operator="carol", reason="撤销错误延期")
        versions = {v["version_id"]: v for v in svc.list_versions("L1")}
        undo_v = [v for v in versions.values() if v["kind"] == "undo"][0]
        self.assertEqual("carol", undo_v["operator"])
        self.assertEqual("撤销错误延期", undo_v["reason"])
        self.assertEqual([p.matches[1].match_id],
                         undo_v["affected_match_ids"])
        # 延期版本同样记录原因、操作者、受影响场次
        post_v = [v for v in versions.values() if v["kind"] == "postpone"][0]
        self.assertEqual("bob", post_v["operator"])
        self.assertEqual([p.matches[1].match_id],
                         post_v["affected_match_ids"])


class QueryTest(unittest.TestCase):
    def setUp(self):
        svc = build_league()
        p = svc.preview("L1", idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        self.svc = svc
        self.draft = p

    def test_round_view_shows_version_notifications_intervals(self):
        rv = self.svc.get_round("L1", 1)
        self.assertEqual(1, rv.round_no)
        self.assertEqual("initial", rv.version_kind)
        self.assertEqual("confirmed", rv.version_status)
        self.assertEqual(2, len(rv.matches))
        # 初稿：每场 3 个待通知对象（主客队联系人 + 裁判）
        self.assertEqual(6, len(rv.pending_notifications))
        recipients = {n["recipient"] for n in rv.pending_notifications}
        self.assertIn("家长代表1", " ".join(recipients))
        self.assertIn("裁判甲", " ".join(recipients))
        # 第 1 轮是各队赛季首战，无前序间隔
        for m in rv.matches:
            self.assertIsNone(m.interval_from_prev_hours)
        # 后续轮次应有间隔数据（按球队）
        rv2 = self.svc.get_round("L1", 2)
        gaps = [m.interval_from_prev_hours for m in rv2.matches]
        self.assertTrue(all(g is not None for g in gaps))
        self.assertTrue(all(g >= 18.0 for g in gaps))

    def test_team_schedule_query(self):
        ts = self.svc.get_team_schedule("L1", "t1")
        self.assertEqual(3, ts["home_games"])
        self.assertEqual(3, ts["away_games"])
        self.assertEqual(6, len(ts["matches"]))
        # 时间升序，间隔满足休息规则
        for prev, nxt in zip(ts["matches"], ts["matches"][1:]):
            self.assertLess(prev["start_utc"], nxt["start_utc"])
            self.assertGreaterEqual(nxt["interval_from_prev_hours"], 18.0)

    def test_local_time_displayed_with_tz_name(self):
        rv = self.svc.get_round("L1", 1)
        for m in rv.matches:
            self.assertTrue(m.start_local)
            self.assertIn(m.tzname, ("EST", "EDT", "PST", "PDT",
                                     "CST", "CDT"))


class NotificationTest(unittest.TestCase):
    def test_unchanged_matches_generate_no_renotification(self):
        svc = build_league()
        p = svc.preview("L1", idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        target = p.matches[1].match_id
        d = svc.postpone("L1", [target], reason="暴雨",
                         idempotency_key="storm")
        svc.confirm("L1", version_id=d.version_id)
        notes = svc.get_notifications("L1", d.version_id)
        # 只有被重排的 1 场需要通知 * 3 个对象
        self.assertEqual(3, len(notes))
        self.assertTrue(all(n["match_id"] == target for n in notes))
        self.assertTrue(all(n["kind"] == "updated" for n in notes))

    def test_mark_sent(self):
        svc = build_league()
        p = svc.preview("L1", idempotency_key="init")
        svc.confirm("L1", version_id=p.version_id)
        pending = svc.get_notifications("L1", p.version_id, "pending")
        self.assertTrue(pending)
        svc.mark_notification_sent(pending[0]["id"])
        still = svc.get_notifications("L1", p.version_id, "pending")
        self.assertEqual(len(pending) - 1, len(still))


if __name__ == "__main__":
    unittest.main()
