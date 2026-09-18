# 青少年联赛赛程编排服务

面向青少年足球联赛的确定性赛程编排服务：管理员录入球队、轮次、场馆可用时段与
不可比赛日期，服务自动生成主客场双循环或小组单循环赛程，并在场地冲突、球队连续
客场、最短休息时间、跨时区场馆加休等约束下给出**可解释**的排程结果。

## 能力一览

- **录入**：球队（含主场馆、家长联系人）、场馆（IANA 时区、不可比赛日期）、
  可用时段（本地墙钟表达，**支持跨午夜时段**）、轮次（主客场/小组循环）。
- **自动编排**：主客平衡的循环赛对阵生成；按 slot 的回溯式排程，满足
  - 同一场馆比赛时段不重叠（杜绝"同一球队进两个场地"）；
  - 球队不得同时段两场比赛；
  - 同队两场之间满足最短休息时间；
  - 连续客场上限；
  - 跨时区场馆之间追加休息时间；
  - 全局停赛日与场馆不可用日期（含跨日时段的结束日）。
- **可解释**：每场比赛带 `resolution_basis`（冲突消解依据），预览中给出每个被
  跳过候选的具体约束原因；排不进的场次返回按类别归纳的阻塞依据。
- **生命周期接口**：`plan_round`（预览）→ `confirm`（确认，乐观版本号）→
  `undo`（撤销）；`team_schedule`（按球队查询全部版本）；`round_view`
  （一轮总览：当前版本、消解依据、待通知对象、球队连续比赛间隔与客场链）。
- **延期处理**：`postpone(command_id, ...)`
  - 只重排**尚未开赛**的场次；已开赛（`mark_started`）的比赛原版本与通知
    记录原样保留（`retained`）；
  - 同一延期指令重复提交**幂等**，不产生第二份赛程；
  - 旧版本未发送的通知自动作废（`SUPERSEDED`），已发送通知保留历史，避免
    裁判/家长收到互相矛盾的时间；
  - 每次调整记录原因、操作者、受影响场次与调整前快照，支持撤销恢复。
- **持久化与可复现**：状态原子写入 JSON 文件；相同输入必然得到相同排程，
  服务重启后赛程、版本、通知与幂等记录均可复现。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：对阵生成与主客平衡、全部排程约束、跨日时间段、跨时区加休、
并发确认（8 线程仅 1 个成功）、延期幂等、已开赛锁定、通知作废与恢复、
撤销、按球队查询、审计记录，以及重启复现与六队联赛性能。

## 最小示例

```python
from datetime import date
from app.schedule import (ScheduleService, Team, Venue, VenueSession, RoundSpec)

svc = ScheduleService("league.json")
svc.add_venue(Venue("VSH", "上海体育中心", "Asia/Shanghai"))
svc.add_team(Team("A", "闪电队", "VSH", contacts=("parent-a@example.cn",)))
svc.add_team(Team("B", "雄鹰队", "VSH", contacts=("parent-b@example.cn",)))
svc.add_session(VenueSession("VSH", date(2026, 10, 10), 540, 1140))
svc.add_round(RoundSpec("R1", "单循环", (("A", "B"),)))

preview = svc.plan_round("R1", operator="alice", reason="赛季初排")
svc.assign_referee(preview["matches"][0]["match_id"], "裁判甲")
svc.confirm("plan-R1", operator="alice")
svc.send_pending("R1")                       # 通知裁判与家长
svc.round_view("R1")                          # 一轮总览
svc.team_schedule("A")                        # 按球队查询
svc.postpone("CMD-RAIN-1010", operator="bob",
             reason="暴雨延期", round_id="R1")  # 幂等延期
```

## 目录

- `app/schedule.py`：赛程领域模型、确定性排程器与应用服务（线程安全、可落盘）。
- `tests/test_schedule_service.py`：业务行为与并发/持久化测试。
- `tests/test_smoke.py`：基础健康检查。
