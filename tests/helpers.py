"""服务层测试共享夹具：一个跨时区的四队双循环联赛。"""
from datetime import date

from app.models import Rules
from app.service import ScheduleService

DAYS = [date(2026, 3, 7), date(2026, 3, 8), date(2026, 3, 14),
        date(2026, 3, 15), date(2026, 3, 21), date(2026, 3, 22),
        date(2026, 3, 28), date(2026, 3, 29)]


def build_league(path: str = ":memory:", rules: Rules | None = None) -> ScheduleService:
    svc = ScheduleService(path)
    svc.create_league("L1", "春季联赛", "home_away",
                      rules or Rules(game_minutes=90, rest_minutes=18 * 60,
                                     max_consecutive_away=2))
    svc.add_venue("L1", "v1", "东城球场", "America/New_York", "裁判甲")
    svc.add_venue("L1", "v2", "西城球场", "America/Los_Angeles", "裁判乙")
    svc.add_venue("L1", "v3", "南城球场", "America/Chicago", "裁判丙")
    svc.add_team("L1", "t1", "闪电", "v1", contact="家长代表1")
    svc.add_team("L1", "t2", "雷霆", "v2", contact="家长代表2")
    svc.add_team("L1", "t3", "风暴", "v3", contact="家长代表3")
    svc.add_team("L1", "t4", "巨浪", "v1", contact="家长代表4")
    for d in DAYS:
        for v in ("v1", "v2", "v3"):
            svc.add_slot("L1", v, d, 10 * 60, 14 * 60)
            svc.add_slot("L1", v, d, 15 * 60, 19 * 60)
    return svc
