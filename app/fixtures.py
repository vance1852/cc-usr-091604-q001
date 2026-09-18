"""对阵生成：主客场双循环与分组单循环。

使用“固定首位轮转法”，输出按球队 id 排序后生成，因此在相同输入下
永远产出相同的轮次、对阵与主客分配（重启后可复现）。
"""
from __future__ import annotations

from app.models import GameRequest, Team


def _pair_rounds(ids: list[str]) -> list[list[tuple[str, str]]]:
    """固定首位轮转法生成每轮的无序对阵（不含主客方向）。"""
    arr = sorted(ids)
    if len(arr) % 2 == 1:
        arr = arr + ["__BYE__"]
    half = len(arr) // 2
    rounds: list[list[tuple[str, str]]] = []
    for _ in range(len(arr) - 1):
        pairs: list[tuple[str, str]] = []
        for i in range(half):
            a, b = arr[i], arr[len(arr) - 1 - i]
            if a != "__BYE__" and b != "__BYE__":
                pairs.append((a, b))
        rounds.append(pairs)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    return rounds


def _orient(rounds: list[list[tuple[str, str]]]) -> list[list[tuple[str, str]]]:
    """为无序对阵确定主客方向。

    同轮内每支球队最多出现一次，因此各对阵的方向互不干扰，可以
    逐对阵独立地选取代价更小的方向：优先缩短当前客场连段、其次平衡
    累计主客数。平局时按 id 字典序方向，保证确定性。
    """
    away_run: dict[str, int] = {}
    home_cnt: dict[str, int] = {}
    away_cnt: dict[str, int] = {}

    def cost(home: str, away: str) -> int:
        run = away_run.get(away, 0) + 1
        balance = (home_cnt.get(home, 0) + 1 - away_cnt.get(home, 0)) \
            + (away_cnt.get(away, 0) + 1 - home_cnt.get(away, 0))
        # 客场连段权重最高，其次主客失衡
        return run * 100 + abs(balance)

    out: list[list[tuple[str, str]]] = []
    for pairs in rounds:
        oriented: list[tuple[str, str]] = []
        for a, b in pairs:
            ab = cost(a, b)  # a 主 b 客
            ba = cost(b, a)
            if ab <= ba:
                home, away = a, b
            else:
                home, away = b, a
            oriented.append((home, away))
            away_run[away] = away_run.get(away, 0) + 1
            away_run[home] = 0
            home_cnt[home] = home_cnt.get(home, 0) + 1
            away_cnt[away] = away_cnt.get(away, 0) + 1
        out.append(oriented)
    return out


def _circle_rounds(ids: list[str]) -> list[list[tuple[str, str]]]:
    """单循环：每个 id 两两相遇一次，方向已按主客平衡确定。"""
    return _orient(_pair_rounds(ids))


def _request(league_id: str, round_no: int, home: str, away: str,
             group: str | None) -> GameRequest:
    gid = f"{league_id}-r{round_no}-{home}-v-{away}"
    return GameRequest(id=gid, round_no=round_no, home_id=home,
                       away_id=away, group_name=group)


def home_away_fixtures(league_id: str, teams: list[Team]) -> list[GameRequest]:
    """主客场双循环。

    第二循环交换主客，并将轮次**倒序**排列：半程衔接处每支球队的
    主客方向必然翻转，不会在跨半程时产生额外的连续客场；同时每一
    对阵的两回合被拉开到整个赛季的两端附近。
    """
    ids = [t.id for t in teams]
    single = _circle_rounds(ids)
    requests: list[GameRequest] = []
    for r, pairs in enumerate(single, start=1):
        for home, away in pairs:
            requests.append(_request(league_id, r, home, away, None))
    next_round = len(single) + 1
    for pairs in reversed(single):
        for home, away in pairs:
            # 镜像轮：主客互换
            requests.append(_request(league_id, next_round, away, home, None))
        next_round += 1
    return requests


def group_fixtures(league_id: str, teams: list[Team]) -> list[GameRequest]:
    """分组单循环：每个小组内部各打一轮单循环，轮次编号全联赛共用。"""
    groups: dict[str, list[str]] = {}
    for t in teams:
        key = t.group_name or "默认组"
        groups.setdefault(key, []).append(t.id)
    requests: list[GameRequest] = []
    for group_name in sorted(groups):
        for r, pairs in enumerate(_circle_rounds(groups[group_name]), start=1):
            for home, away in pairs:
                requests.append(_request(league_id, r, home, away, group_name))
    return requests


def generate(league_id: str, teams: list[Team], mode: str) -> list[GameRequest]:
    if mode == "home_away":
        return home_away_fixtures(league_id, teams)
    if mode == "group":
        return group_fixtures(league_id, teams)
    raise ValueError(f"未知赛制: {mode}（支持 home_away / group）")
