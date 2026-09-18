# 青少年联赛赛程编排服务

面向青少年足球联赛的确定性赛程编排服务：管理员录入球队、场馆可用时段
（可跨日、跨时区）和不可比赛日期后，自动生成主客场双循环或分组单循环
赛程，并在**场地冲突、连续客场、最短休息、跨时区旅行**等规则下给出
**可解释**的排程结果。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

共 47 个用例，覆盖：约束可行性、无解的中文解释、并发确认、跨日时段、
跨时区夏令时、幂等延期、撤销留痕，以及**重启后排程逐字节复现**。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `app/models.py` | 领域对象与规则（`Rules`、`GameRequest`、拒绝原因中文词典） |
| `app/fixtures.py` | 确定性对阵生成：固定首位轮转 + 主客平衡定向（双循环镜像轮倒序，杜绝跨半程三连客） |
| `app/engine.py` | 确定性回溯排程引擎，逐候选收集拒绝依据；锁定场不可移动 |
| `app/service.py` | SQLite 版本化应用服务：录入/预览/确认/延期/撤销/查询、通知、审计、乐观锁 |
| `tests/` | 单元与集成测试 |

## 规则（`Rules`，均可配置）

- `game_minutes`：比赛时长（默认 90）
- `rest_minutes`：同队两场最短休息（默认 18 小时）
- `max_consecutive_away`：最多连续客场（默认 2）
- `tz_threshold_hours` / `tz_extra_rest_minutes`：相邻两场场馆时差达到
  阈值时，必须额外休息（默认时差 ≥2h 需额外 12h）
- `require_home_venue`：主队有主场馆时必须在主场馆作赛

约束被违反时，预览会直接失败并返回 `SchedulingImpossible`，逐场列出
每个候选时段被哪些规则拒绝（如「少于球队最短休息时间」）。局部可行但
会导致后续轮次无解的候选标记为「回溯已放弃」。

## 典型流程

```python
from datetime import date, datetime, timezone
from app.service import ScheduleService
from app.models import Rules

svc = ScheduleService("league.db")          # 也可用 ":memory:"
svc.create_league("L1", "春季联赛", "home_away",
                  Rules(rest_minutes=18*60, max_consecutive_away=2))
svc.add_venue("L1", "v1", "东城球场", "America/New_York", "裁判甲")
svc.add_team("L1", "t1", "闪电", "v1", contact="家长代表1")
svc.add_slot("L1", "v1", date(2026, 3, 7), 10*60, 14*60)   # 当地 10:00-14:00
svc.add_slot("L1", "v1", date(2026, 5, 1), 23*60, 24*60+90) # 跨日 23:00-01:30
svc.add_blackout("L1", date(2026, 3, 14), reason="中考占用")

draft = svc.preview("L1", operator="alice", reason="初稿",
                    idempotency_key="init-2026")
svc.confirm("L1", version_id=draft.version_id)

# 暴雨：三场同时延期，必须携带幂等键；已开赛的比赛会被整体拒绝
svc.kick_off("L1", draft.matches[0].match_id)
d = svc.postpone("L1", [m.match_id for m in draft.matches[1:4]],
                 reason="暴雨封场", operator="bob",
                 idempotency_key="storm-0314",
                 not_before_utc=datetime(2026, 3, 20, tzinfo=timezone.utc))
svc.confirm("L1", version_id=d.version_id)   # 并发确认只有一个成功
svc.undo("L1", operator="carol", reason="误操作，撤销")  # 恢复父版本

rv = svc.get_round("L1", 1)          # 版本信息、冲突消解依据、待通知对象、连赛间隔
svc.get_team_schedule("L1", "t1")    # 按球队查询
```

## 关键设计

- **确定性**：排程只依赖输入数据，遍历顺序按 id/UTC 排序，不读墙钟、
  不用随机数。相同输入在任意进程、重启后结果一致；轮次视图里的
  「冲突消解依据」也是查看时即时重算，永远与当时版本一致。
- **版本链不可变**：`versions.parent_version` 串起初稿/延期/撤销；
  历史版本的比赛与通知记录永久保留。延期时未点名场次以锁定场写入
  新版本，原时间原场馆保留；`played` 状态跨版本保持。
- **幂等**：`(league_id, idempotency_key)` 唯一约束。同一延期指令
  重复提交返回同一草稿，绝不产生第二份赛程；重复确认不重复发通知。
- **并发**：确认使用 `BEGIN IMMEDIATE` + 父版本乐观锁，后到的确认
  收到 `ScheduleConflict`，必须基于最新版本重新排程。
- **延期语义**：被延期场次的原 (场馆, 开赛时间) 进入 `forbidden`，
  引擎不会把比赛放回已被暴雨作废的时段；`not_before_utc` 给出最早
  可开赛时间。
- **时区/跨日**：时段以场馆当地墙钟录入，`end_minute > 1440` 即跨日；
  内部统一换算为 UTC，展示时还原本地时间与时区缩写；停赛日按场馆
  本地日历（覆盖比赛起止两端）判断。
- **通知**：确认时为每场*发生变化*的比赛生成主客队联系人 + 裁判三类
  待通知记录；锁定不变的场次不重复通知。
- **审计**：每次调整都保存原因、操作者与受影响场次（`adjustments` 表）。
