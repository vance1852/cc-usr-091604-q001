"""对阵生成测试：赛制完备性、主客平衡与确定性。"""
import unittest

from app import fixtures
from app.models import Team


def team(ids):
    return [Team(i, i) for i in ids]


class FixtureTest(unittest.TestCase):
    def test_single_round_robin_pairs_and_bye(self):
        for n in range(3, 9):
            with self.subTest(n=n):
                reqs = fixtures._circle_rounds([t.id for t in team(
                    [f"t{i}" for i in range(n)])])
                pairs = [(h, a) for rnd in reqs for h, a in rnd]
                # 每两支球队恰好相遇一次
                keys = {tuple(sorted(p)) for p in pairs}
                self.assertEqual(len(keys), n * (n - 1) // 2)
                # 每队每轮至多一场
                for rnd in reqs:
                    appeared = [x for p in rnd for x in p]
                    self.assertEqual(len(appeared), len(set(appeared)))
                # 每队参赛场次 = n-1（偶数队）或 n-1（奇数去掉轮空）
                counts = {t: 0 for t in [f"t{i}" for i in range(n)]}
                for h, a in pairs:
                    counts[h] += 1
                    counts[a] += 1
                self.assertTrue(all(c in (n - 1,) for c in counts.values()))

    def test_double_round_robin_home_away_symmetry(self):
        teams = team(["a", "b", "c", "d", "e"])
        reqs = fixtures.home_away_fixtures("L", teams)
        seen: dict[tuple[str, str], int] = {}
        for r in reqs:
            seen[(r.home_id, r.away_id)] = r.round_no
        # 每个有序主客组合恰好一次 => 每对球队各在双方主场打一次
        for h in teams:
            for a in teams:
                if h.id != a.id:
                    self.assertIn((h.id, a.id), seen)
        # 每队主客数相等
        for t in teams:
            home = sum(1 for r in reqs if r.home_id == t.id)
            away = sum(1 for r in reqs if r.away_id == t.id)
            self.assertEqual(home, away)
            self.assertEqual(home + away, 2 * (len(teams) - 1))

    def test_no_three_consecutive_away_in_double_round_robin(self):
        teams = team(["a", "b", "c", "d"])
        reqs = fixtures.home_away_fixtures("L", teams)
        for t in teams:
            sides = [(r.round_no, "A" if r.away_id == t.id else "H")
                     for r in reqs]
            sides.sort()
            run = 0
            for _, s in sides:
                run = run + 1 if s == "A" else 0
                self.assertLessEqual(run, 2, f"{t.id} 连续客场超过 2 场")

    def test_group_robin_only_within_group(self):
        teams = [
            Team("a", "a", group_name="G1"), Team("b", "b", group_name="G1"),
            Team("c", "c", group_name="G1"), Team("d", "d", group_name="G2"),
            Team("e", "e", group_name="G2"),
        ]
        reqs = fixtures.group_fixtures("L", teams)
        group_of = {t.id: t.group_name for t in teams}
        for r in reqs:
            self.assertEqual(group_of[r.home_id], group_of[r.away_id])
            self.assertEqual(r.group_name, group_of[r.home_id])
        # G1 单循环 3 场，G2 单循环 1 场
        g1 = [r for r in reqs if r.group_name == "G1"]
        g2 = [r for r in reqs if r.group_name == "G2"]
        self.assertEqual(len(g1), 3)
        self.assertEqual(len(g2), 1)

    def test_deterministic_ids_and_order(self):
        t1 = fixtures.home_away_fixtures("L", team(["a", "b", "c", "d"]))
        t2 = fixtures.home_away_fixtures("L", team(["a", "b", "c", "d"]))
        self.assertEqual([r.id for r in t1], [r.id for r in t2])
        # id 稳定：相同对阵永远得到相同 match_id
        self.assertTrue(all(x.id.startswith("L-r") for x in t1))
        self.assertEqual(len({x.id for x in t1}), len(t1))


if __name__ == "__main__":
    unittest.main()
