"""赛程服务：录入、预览、确认、延期、撤销、查询。

持久化使用 SQLite，所有写操作在一个事务内完成：
- 版本链 versions(parent_version) 不可变，历史版本与其通知记录永久保留；
- (league_id, idempotency_key) 唯一约束保证同一延期指令重复提交
  不会产生第二份赛程；
- 确认采用乐观锁（parent_version 必须仍是当前版本），并发确认时
  只有一个事务成功，其余得到 ScheduleConflict。

排程本身（引擎输入输出）不含墙钟与随机数，因此重启后用同样的数据
重新排，结果字节级一致。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app import fixtures
from app.engine import Engine
from app.models import (
    GameRequest,
    MatchStatus,
    PlannedMatch,
    REASON_TEXT,
    Rules,
    Slot,
    Team,
    Venue,
    VersionKind,
    NotificationStatus,
)
from app.models import SchedulingImpossible, ScheduleConflict, ScheduleError

SCHEMA = """
CREATE TABLE IF NOT EXISTS leagues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    current_version INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    name TEXT NOT NULL,
    home_venue_id TEXT,
    group_name TEXT,
    contact TEXT
);
CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    referee_contact TEXT
);
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    day TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS blackouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    day TEXT NOT NULL,
    reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_version INTEGER,
    reason TEXT DEFAULT '',
    operator TEXT DEFAULT '',
    idempotency_key TEXT,
    not_before_utc TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(league_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS matches (
    version_id INTEGER NOT NULL,
    match_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    home_id TEXT NOT NULL,
    away_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    group_name TEXT,
    status TEXT NOT NULL,
    change_type TEXT NOT NULL,
    PRIMARY KEY (version_id, match_id)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    match_id TEXT NOT NULL,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS adjustments (
    version_id INTEGER PRIMARY KEY,
    league_id TEXT NOT NULL,
    affected_json TEXT NOT NULL
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _iso_day(d: date) -> str:
    return d.isoformat()


@dataclass
class MatchView:
    match_id: str
    round_no: int
    home_id: str
    away_id: str
    venue_id: str
    start_utc: datetime
    end_utc: datetime
    status: str
    change_type: str
    group_name: str | None = None
    start_local: str = ""
    tzname: str = ""
    resolution: list[dict] = field(default_factory=list)  # 冲突消解依据
    interval_from_prev_hours: float | None = None  # 距该队上一场

    def to_dict(self) -> dict:
        return {
            "match_id": self.match_id, "round_no": self.round_no,
            "home_id": self.home_id, "away_id": self.away_id,
            "venue_id": self.venue_id,
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "status": self.status, "change_type": self.change_type,
            "group_name": self.group_name,
            "start_local": self.start_local, "tzname": self.tzname,
            "resolution": self.resolution,
            "interval_from_prev_hours": self.interval_from_prev_hours,
        }


@dataclass
class Preview:
    version_id: int
    league_id: str
    kind: str
    parent_version: int | None
    status: str
    matches: list[MatchView]
    decisions: dict
    affected_match_ids: list[str]


@dataclass
class RoundView:
    league_id: str
    round_no: int
    version_id: int
    version_kind: str
    version_status: str
    matches: list[MatchView]
    pending_notifications: list[dict]
    team_intervals: dict  # team_id -> [{match_id, prev, hours}]


class ScheduleService:
    """面向管理员/运营员的应用服务。"""

    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(versions)")}
        if "not_before_utc" not in cols:
            self.conn.execute(
                "ALTER TABLE versions ADD COLUMN not_before_utc TEXT")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _txn(self):
        """串行化所有写操作并提交，保证重启后数据不丢。"""
        with self._lock:
            try:
                yield
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    @contextmanager
    def _txn_immediate(self):
        """BEGIN IMMEDIATE：行锁级并发确认，第二个事务被判定为过期版本。"""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # ==================================================================
    # 录入
    # ==================================================================
    def create_league(self, league_id: str, name: str, mode: str,
                      rules: Rules | None = None) -> None:
        if mode not in ("home_away", "group"):
            raise ScheduleError("mode 必须是 home_away 或 group")
        with self._txn():
            self.conn.execute(
                "INSERT INTO leagues(id,name,mode,rules_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (league_id, name, mode,
                 json.dumps(rules.__dict__ if rules else Rules().__dict__),
                 _utcnow().isoformat()))

    def add_team(self, league_id: str, team_id: str, name: str,
                 home_venue_id: str | None = None,
                 group_name: str | None = None,
                 contact: str | None = None) -> None:
        with self._txn():
            self.conn.execute(
                "INSERT INTO teams VALUES(?,?,?,?,?,?)",
                (team_id, league_id, name, home_venue_id, group_name, contact))

    def add_venue(self, league_id: str, venue_id: str, name: str,
                  tzname: str, referee_contact: str | None = None) -> None:
        # 提前校验时区名，避免排程时才失败
        ZoneInfo(tzname)
        with self._txn():
            self.conn.execute(
                "INSERT INTO venues VALUES(?,?,?,?,?)",
                (venue_id, league_id, name, tzname, referee_contact))

    def add_slot(self, league_id: str, venue_id: str, day: date,
                 start_minute: int, end_minute: int) -> int:
        """登记场馆可用时段；end_minute 可超过 1440 表示跨日。"""
        if end_minute <= start_minute:
            raise ScheduleError("时段结束必须晚于开始")
        with self._txn():
            cur = self.conn.execute(
                "INSERT INTO slots(league_id,venue_id,day,start_minute,end_minute)"
                " VALUES(?,?,?,?,?)",
                (league_id, venue_id, _iso_day(day), start_minute, end_minute))
            return cur.lastrowid

    def add_blackout(self, league_id: str, day: date,
                     scope: str = "league", scope_id: str | None = None,
                     reason: str = "") -> None:
        with self._txn():
            self.conn.execute(
                "INSERT INTO blackouts(league_id,scope,scope_id,day,reason)"
                " VALUES(?,?,?,?,?)",
                (league_id, scope, scope_id or league_id, _iso_day(day), reason))

    # ==================================================================
    # 读取上下文
    # ==================================================================
    def _rules(self, league_id: str) -> Rules:
        row = self.conn.execute(
            "SELECT rules_json FROM leagues WHERE id=?", (league_id,)).fetchone()
        return Rules(**json.loads(row["rules_json"]))

    def _mode(self, league_id: str) -> str:
        return self.conn.execute(
            "SELECT mode FROM leagues WHERE id=?", (league_id,)).fetchone()["mode"]

    def _teams(self, league_id: str) -> list[Team]:
        return [Team(r["id"], r["name"], r["home_venue_id"], r["group_name"])
                for r in self.conn.execute(
                    "SELECT * FROM teams WHERE league_id=? ORDER BY id",
                    (league_id,))]

    def _venues(self, league_id: str) -> list[Venue]:
        return [Venue(r["id"], r["name"], r["timezone"])
                for r in self.conn.execute(
                    "SELECT * FROM venues WHERE league_id=? ORDER BY id",
                    (league_id,))]

    def _slots(self, league_id: str) -> list[Slot]:
        rows = self.conn.execute(
            "SELECT * FROM slots WHERE league_id=? ORDER BY id", (league_id,))
        return [Slot(r["id"], r["venue_id"], date.fromisoformat(r["day"]),
                     r["start_minute"], r["end_minute"])
                for r in rows]

    def _blackouts(self, league_id: str) -> set[tuple]:
        out = set()
        for r in self.conn.execute(
                "SELECT scope,scope_id,day FROM blackouts WHERE league_id=?",
                (league_id,)):
            sid = "*" if r["scope"] == "league" else r["scope_id"]
            scope = "league" if r["scope"] == "league" else "venue"
            out.add((scope, sid, date.fromisoformat(r["day"])))
        return out

    def _engine(self, league_id: str) -> Engine:
        return Engine(league_id, self._teams(league_id), self._venues(league_id),
                      self._slots(league_id), self._blackouts(league_id),
                      self._rules(league_id))

    def _current_version_id(self, league_id: str) -> int | None:
        return self.conn.execute(
            "SELECT current_version FROM leagues WHERE id=?",
            (league_id,)).fetchone()["current_version"]

    def _version_matches(self, version_id: int) -> list[PlannedMatch]:
        rows = self.conn.execute(
            "SELECT * FROM matches WHERE version_id=? ORDER BY round_no,match_id",
            (version_id,))
        return [PlannedMatch(
            r["match_id"], r["round_no"], r["home_id"], r["away_id"],
            r["venue_id"], _parse_dt(r["start_utc"]), _parse_dt(r["end_utc"]),
            r["group_name"]) for r in rows]

    # ==================================================================
    # 预览（初稿 / 延期共用同一套引擎入口）
    # ==================================================================
    def preview(self, league_id: str, operator: str = "admin",
                reason: str = "初始排程",
                idempotency_key: str | None = None) -> Preview:
        """生成初始赛程草稿（不确认、不发通知）。"""
        with self._txn():
            existing = self._find_draft(league_id, idempotency_key)
            if existing is not None:
                return existing
            current = self._current_version_id(league_id)
            if current is not None:
                raise ScheduleError("联赛已有赛程，调整请使用 postpone/undo")
            open_draft = self.conn.execute(
                "SELECT id FROM versions WHERE league_id=? AND status='draft' "
                "LIMIT 1", (league_id,)).fetchone()
            if open_draft is not None:
                raise ScheduleError(
                    f"已有未确认草稿（版本 {open_draft['id']}），"
                    "请先确认或基于它继续")
            requests = fixtures.generate(league_id, self._teams(league_id),
                                         self._mode(league_id))
            engine = self._engine(league_id)
            try:
                planned, decisions, _ = engine.solve(requests, [])
            except SchedulingImpossible:
                raise
            return self._save_draft(
                league_id, VersionKind.INITIAL, None, reason, operator,
                idempotency_key, planned, decisions,
                affected=[m.id for m in planned])

    def postpone(self, league_id: str, match_ids: list[str] | None = None,
                 round_no: int | None = None, reason: str = "",
                 operator: str = "admin",
                 idempotency_key: str | None = None,
                 not_before_utc: datetime | None = None) -> Preview:
        """登记延期并生成重排草稿。

        - 只重排尚未开赛（CONFIRMED）的场次；已开赛（PLAYED）的场次
          若被点名会整体报错，什么都不改动；
        - 未被点名的场次作为锁定场次，时间场馆原样保留；
        - 同一 idempotency_key 重复提交直接返回原草稿，不二次排程。
        """
        if not idempotency_key:
            raise ScheduleError("延期指令必须带 idempotency_key")
        with self._txn():
            existing = self._find_draft(league_id, idempotency_key)
            if existing is not None:
                return existing
            base = self._current_version_id(league_id)
            if base is None:
                raise ScheduleError("尚未生成初始赛程，无需延期")
            current_matches = self._version_matches(base)
            by_id = {m.id: m for m in current_matches}
            if round_no is not None:
                targets = {m.id for m in current_matches if m.round_no == round_no}
            else:
                targets = set(match_ids or [])
            unknown = targets - set(by_id)
            if unknown:
                raise ScheduleError(f"未知场次: {sorted(unknown)}")
            started = [mid for mid in sorted(targets)
                       if self._match_status(base, mid) == MatchStatus.PLAYED.value]
            if started:
                raise ScheduleError(
                    f"以下场次已开赛，不能延期: {started}")
            if not targets:
                raise ScheduleError("延期指令没有匹配到任何场次")

            locked = [m for m in current_matches if m.id not in targets]
            locked_status = {m.id: self._match_status(base, m.id)
                             for m in locked}
            requests = []
            for mid in sorted(targets):
                m = by_id[mid]
                # 原时段因本次延期作废：新赛程不得再放回同一时间同一场馆
                forbidden = frozenset({(m.venue_id, m.start_utc)})
                requests.append(GameRequest(
                    mid, m.round_no, m.home_id, m.away_id, m.group_name,
                    not_before_utc, forbidden))
            engine = self._engine(league_id)
            planned, decisions, locked_out = engine.solve(requests, locked)
            # 输出顺序：锁定场（原序）+ 重排场
            ordered = locked_out + planned
            return self._save_draft(
                league_id, VersionKind.POSTPONE, base, reason, operator,
                idempotency_key, ordered, decisions,
                affected=sorted(targets), postponed_targets=sorted(targets),
                locked_status=locked_status,
                not_before_utc=not_before_utc)

    def _match_status(self, version_id: int, match_id: str) -> str:
        return self.conn.execute(
            "SELECT status FROM matches WHERE version_id=? AND match_id=?",
            (version_id, match_id)).fetchone()["status"]

    # ==================================================================
    # 确认（乐观锁 + 通知生成）
    # ==================================================================
    def confirm(self, league_id: str, version_id: int | None = None,
                idempotency_key: str | None = None,
                operator: str | None = None) -> dict:
        """确认草稿。并发确认只有一个成功，其余抛 ScheduleConflict。"""
        with self._txn_immediate():
            v = self._get_version_row(league_id, version_id, idempotency_key)
            if v["status"] == "confirmed":
                # 幂等：同一指令重复确认直接返回，不产生新版本/通知
                return self._confirm_summary(v["id"], duplicated=True)

            current = self._current_version_id(league_id)
            if v["parent_version"] != current:
                raise ScheduleConflict(
                    f"版本 {v['id']} 基于版本 {v['parent_version']}，"
                    f"当前已为版本 {current}，请基于最新版本重新排程")

            self.conn.execute(
                "UPDATE versions SET status='confirmed' WHERE id=?",
                (v["id"],))
            # 草稿中的 PROPOSED 转 CONFIRMED；锁定场保持 CONFIRMED/PLAYED
            self.conn.execute(
                "UPDATE matches SET status='confirmed' "
                "WHERE version_id=? AND status='proposed'", (v["id"],))
            self.conn.execute(
                "UPDATE leagues SET current_version=? WHERE id=?",
                (v["id"], league_id))
            self._generate_notifications(league_id, v["id"])
            return self._confirm_summary(v["id"])

    def _get_version_row(self, league_id, version_id, idempotency_key):
        if version_id is not None:
            row = self.conn.execute(
                "SELECT * FROM versions WHERE id=? AND league_id=?",
                (version_id, league_id)).fetchone()
        elif idempotency_key is not None:
            row = self.conn.execute(
                "SELECT * FROM versions WHERE league_id=? AND idempotency_key=?",
                (league_id, idempotency_key)).fetchone()
        else:
            raise ScheduleError("确认需要 version_id 或 idempotency_key")
        if row is None:
            raise ScheduleError("找不到对应草稿版本")
        return row

    def _generate_notifications(self, league_id: str, version_id: int) -> None:
        kind_row = self.conn.execute(
            "SELECT kind FROM versions WHERE id=?", (version_id,)).fetchone()
        is_initial = kind_row["kind"] == VersionKind.INITIAL.value
        matches = self.conn.execute(
            "SELECT * FROM matches WHERE version_id=?", (version_id,)).fetchall()
        venues = {r["id"]: r for r in self.conn.execute(
            "SELECT * FROM venues WHERE league_id=?", (league_id,))}
        teams = {r["id"]: r for r in self.conn.execute(
            "SELECT * FROM teams WHERE league_id=?", (league_id,))}
        now = _utcnow().isoformat()
        for m in matches:
            if m["change_type"] == "unchanged":
                continue  # 锁定保留的场次不重复通知
            notice_kind = "new" if is_initial else "updated"
            recipients = []
            for t in (m["home_id"], m["away_id"]):
                recipients.append(teams[t]["contact"] or f"team:{t}")
            v = venues[m["venue_id"]]
            recipients.append(
                v["referee_contact"] or f"referee:{m['venue_id']}")
            for target in recipients:
                self.conn.execute(
                    "INSERT INTO notifications(version_id,match_id,recipient,"
                    "kind,status,created_at) VALUES(?,?,?,?,?,?)",
                    (version_id, m["match_id"], target,
                     notice_kind, NotificationStatus.PENDING.value, now))

    def _confirm_summary(self, version_id: int, duplicated: bool = False) -> dict:
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE version_id=?",
            (version_id,)).fetchone()["c"]
        return {"version_id": version_id, "status": "confirmed",
                "notifications_created": 0 if duplicated else n,
                "duplicated": duplicated}

    # ==================================================================
    # 撤销
    # ==================================================================
    def undo(self, league_id: str, operator: str = "admin",
             reason: str = "运营员撤销上一次调整") -> dict:
        """撤销当前版本：生成一个恢复父版本赛程的新版本（历史仍保留）。"""
        with self._txn_immediate():
            cur_id = self._current_version_id(league_id)
            if cur_id is None:
                raise ScheduleError("还没有已确认的版本可撤销")
            cur = self.conn.execute(
                "SELECT * FROM versions WHERE id=?", (cur_id,)).fetchone()
            parent = cur["parent_version"]
            if parent is None:
                raise ScheduleError(
                    "当前是初始版本，没有更早版本可恢复；"
                    "如需整体重排请用 postpone")
            now = _utcnow().isoformat()
            cur2 = self.conn.execute(
                "INSERT INTO versions(league_id,kind,status,parent_version,"
                "reason,operator,created_at) VALUES(?,?,?,?,?,?,?)",
                (league_id, VersionKind.UNDO.value, "confirmed", parent,
                 reason, operator, now))
            new_id = cur2.lastrowid
            affected: list[str] = []
            if parent is not None:
                src_rows = self.conn.execute(
                    "SELECT * FROM matches WHERE version_id=?",
                    (parent,)).fetchall()
                for r in src_rows:
                    now_row = self.conn.execute(
                        "SELECT start_utc,venue_id FROM matches "
                        "WHERE version_id=? AND match_id=?",
                        (cur_id, r["match_id"])).fetchone()
                    same = now_row is not None and \
                        now_row["start_utc"] == r["start_utc"] and \
                        now_row["venue_id"] == r["venue_id"]
                    change = "unchanged" if same else "restored"
                    if not same:
                        affected.append(r["match_id"])
                    self.conn.execute(
                        "INSERT INTO matches(version_id,match_id,round_no,"
                        "home_id,away_id,venue_id,start_utc,end_utc,"
                        "group_name,status,change_type) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (new_id, r["match_id"], r["round_no"], r["home_id"],
                         r["away_id"], r["venue_id"], r["start_utc"],
                         r["end_utc"], r["group_name"],
                         MatchStatus.CONFIRMED.value, change))
            self.conn.execute(
                "INSERT INTO adjustments(version_id,league_id,affected_json)"
                " VALUES(?,?,?)",
                (new_id, league_id, json.dumps(sorted(affected))))
            self.conn.execute(
                "UPDATE leagues SET current_version=? WHERE id=?",
                (new_id, league_id))
            self._generate_notifications(league_id, new_id)
            return {"version_id": new_id, "restored_parent": parent,
                    "affected_match_ids": sorted(affected)}

    # ==================================================================
    # 查询：轮次视图、球队视图
    # ==================================================================
    def get_round(self, league_id: str, round_no: int,
                  version_id: int | None = None) -> RoundView:
        with self._lock:
            vid = version_id or self._current_version_id(league_id)
            if vid is None:
                raise ScheduleError("还没有赛程版本")
            return self._build_round_view(league_id, vid, round_no)

    def get_team_schedule(self, league_id: str, team_id: str,
                          version_id: int | None = None) -> dict:
        with self._lock:
            vid = version_id or self._current_version_id(league_id)
            if vid is None:
                raise ScheduleError("还没有赛程版本")
            venues = {v.id: v for v in self._venues(league_id)}
            rows = self.conn.execute(
                "SELECT * FROM matches WHERE version_id=? AND "
                "(home_id=? OR away_id=?) ORDER BY start_utc",
                (vid, team_id, team_id)).fetchall()
            matches = []
            prev_end = None
            for r in rows:
                m = self._row_to_view(r, venues, {})
                if prev_end is not None:
                    m.interval_from_prev_hours = round(
                        (m.start_utc - prev_end).total_seconds() / 3600, 1)
                matches.append(m)
                prev_end = m.end_utc
            return {
                "league_id": league_id, "team_id": team_id,
                "version_id": vid,
                "matches": [m.to_dict() for m in matches],
                "home_games": sum(1 for r in rows if r["home_id"] == team_id),
                "away_games": sum(1 for r in rows if r["away_id"] == team_id),
            }

    def list_versions(self, league_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM versions WHERE league_id=? ORDER BY id",
                (league_id,)).fetchall()
            out = []
            for r in rows:
                affected = self.conn.execute(
                    "SELECT affected_json FROM adjustments WHERE version_id=?",
                    (r["id"],)).fetchone()
                out.append({
                    "version_id": r["id"], "kind": r["kind"],
                    "status": r["status"], "parent_version": r["parent_version"],
                    "reason": r["reason"], "operator": r["operator"],
                    "idempotency_key": r["idempotency_key"],
                    "created_at": r["created_at"],
                    "affected_match_ids": json.loads(
                        affected["affected_json"]) if affected else [],
                })
            return out

    def get_notifications(self, league_id: str,
                          version_id: int | None = None,
                          status: str | None = None) -> list[dict]:
        with self._lock:
            vid = version_id or self._current_version_id(league_id)
            q = ("SELECT n.* FROM notifications n JOIN versions v "
                 "ON n.version_id=v.id WHERE v.league_id=?")
            params: list = [league_id]
            if vid is not None:
                q += " AND n.version_id=?"
                params.append(vid)
            if status:
                q += " AND n.status=?"
                params.append(status)
            q += " ORDER BY n.id"
            return [dict(r) for r in self.conn.execute(q, params)]

    def mark_notification_sent(self, notification_id: int) -> None:
        with self._txn():
            self.conn.execute(
                "UPDATE notifications SET status=?, sent_at=? WHERE id=?",
                (NotificationStatus.SENT.value, _utcnow().isoformat(),
                 notification_id))

    def kick_off(self, league_id: str, match_id: str) -> None:
        """登记比赛已开赛，此后任何版本都不能再动它。"""
        with self._txn():
            vid = self._current_version_id(league_id)
            self.conn.execute(
                "UPDATE matches SET status=? WHERE version_id=? AND match_id=?",
                (MatchStatus.PLAYED.value, vid, match_id))

    # ==================================================================
    # 草稿存取与视图组装
    # ==================================================================
    def _find_draft(self, league_id: str,
                    idem_key: str | None) -> Preview | None:
        if not idem_key:
            return None
        row = self.conn.execute(
            "SELECT id FROM versions WHERE league_id=? AND idempotency_key=?",
            (league_id, idem_key)).fetchone()
        if row is None:
            return None
        return self._load_preview(league_id, row["id"])

    def _save_draft(self, league_id, kind, parent, reason, operator,
                    idem_key, planned, decisions, affected,
                    postponed_targets=None, locked_status=None,
                    not_before_utc=None) -> Preview:
        now = _utcnow().isoformat()
        try:
            cur = self.conn.execute(
                "INSERT INTO versions(league_id,kind,status,parent_version,"
                "reason,operator,idempotency_key,not_before_utc,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (league_id, kind.value, "draft", parent, reason, operator,
                 idem_key,
                 not_before_utc.isoformat() if not_before_utc else None, now))
        except sqlite3.IntegrityError:
            # 并发下同键已插入：返回已有草稿
            row = self.conn.execute(
                "SELECT id FROM versions WHERE league_id=? AND idempotency_key=?",
                (league_id, idem_key)).fetchone()
            return self._load_preview(league_id, row["id"])
        version_id = cur.lastrowid
        targets = set(postponed_targets or [])
        locked_status = locked_status or {}
        for m in planned:
            if kind == VersionKind.POSTPONE and m.id not in targets:
                change = "unchanged"
                # 已开赛的锁定场保持 PLAYED（确认时不得被改成 confirmed）
                status = locked_status.get(m.id, MatchStatus.CONFIRMED.value)
            else:
                change = "new" if kind == VersionKind.INITIAL else "rescheduled"
                status = MatchStatus.PROPOSED.value
            self.conn.execute(
                "INSERT INTO matches(version_id,match_id,round_no,home_id,"
                "away_id,venue_id,start_utc,end_utc,group_name,status,change_type)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, m.id, m.round_no, m.home_id, m.away_id,
                 m.venue_id, m.start_utc.isoformat(), m.end_utc.isoformat(),
                 m.group_name, status, change))
        self.conn.execute(
            "INSERT INTO adjustments(version_id,league_id,affected_json)"
            " VALUES(?,?,?)",
            (version_id, league_id, json.dumps(sorted(affected))))
        return self._load_preview(league_id, version_id, decisions)

    def _load_preview(self, league_id: str, version_id: int,
                      decisions_override=None) -> Preview:
        v = self.conn.execute("SELECT * FROM versions WHERE id=?",
                              (version_id,)).fetchone()
        venues = {x.id: x for x in self._venues(league_id)}
        rows = self.conn.execute(
            "SELECT * FROM matches WHERE version_id=? ORDER BY round_no,match_id",
            (version_id,)).fetchall()
        views = [self._row_to_view(r, venues, {}) for r in rows]
        affected = self.conn.execute(
            "SELECT affected_json FROM adjustments WHERE version_id=?",
            (version_id,)).fetchone()
        return Preview(
            version_id=version_id, league_id=league_id, kind=v["kind"],
            parent_version=v["parent_version"], status=v["status"],
            matches=views, decisions=decisions_override or {},
            affected_match_ids=json.loads(
                affected["affected_json"]) if affected else [])

    def _build_round_view(self, league_id: str, version_id: int,
                          round_no: int) -> RoundView:
        v = self.conn.execute("SELECT * FROM versions WHERE id=?",
                              (version_id,)).fetchone()
        venues = {x.id: x for x in self._venues(league_id)}
        rows = self.conn.execute(
            "SELECT * FROM matches WHERE version_id=? AND round_no=? "
            "ORDER BY start_utc,match_id",
            (version_id, round_no)).fetchall()

        # 冲突消解依据：相对父版本，哪些候选被规则拒绝（重排场）
        decisions = self._resolution_notes(league_id, version_id)
        views = [self._row_to_view(r, venues, decisions) for r in rows]

        # 球队连续比赛间隔（全版本内按 UTC 排序计算）
        all_rows = self.conn.execute(
            "SELECT * FROM matches WHERE version_id=? ORDER BY start_utc",
            (version_id,)).fetchall()
        prev_end: dict[str, datetime] = {}
        intervals: dict[str, list] = {}
        by_match: dict[str, float | None] = {}
        for r in all_rows:
            for t in (r["home_id"], r["away_id"]):
                start = _parse_dt(r["start_utc"])
                end = _parse_dt(r["end_utc"])
                gap = round((start - prev_end[t]).total_seconds() / 3600, 1) \
                    if t in prev_end else None
                intervals.setdefault(t, []).append(
                    {"match_id": r["match_id"], "hours_after_prev": gap})
                by_match[f"{t}:{r['match_id']}"] = gap
                prev_end[t] = end
        for mv in views:
            gap = by_match.get(f"{mv.home_id}:{mv.match_id}")
            mv.interval_from_prev_hours = gap

        pending = [dict(r) for r in self.conn.execute(
            "SELECT n.match_id,n.recipient,n.kind,n.status "
            "FROM notifications n JOIN matches m "
            "ON n.version_id=m.version_id AND n.match_id=m.match_id "
            "WHERE n.version_id=? AND m.round_no=? AND n.status='pending' "
            "ORDER BY n.id",
            (version_id, round_no))]
        return RoundView(
            league_id=league_id, round_no=round_no, version_id=version_id,
            version_kind=v["kind"], version_status=v["status"],
            matches=views, pending_notifications=pending,
            team_intervals=intervals)

    def _resolution_notes(self, league_id: str, version_id: int) -> dict:
        """重算被调整场次的候选拒绝记录，作为“冲突消解依据”。

        草稿确认后引擎 trace 不入库（含大量被放弃候选），查看轮次时
        以当时的对阵与锁定场即时重算——输入不变，输出必然一致。
        """
        v = self.conn.execute("SELECT * FROM versions WHERE id=?",
                              (version_id,)).fetchone()
        if v["kind"] == VersionKind.UNDO.value:
            # 撤销是整体恢复父版本，不经过引擎选择，没有候选取舍依据
            return {}
        if v["kind"] == VersionKind.INITIAL.value or v["parent_version"] is None:
            # 初稿：用全部对阵空锁重算 trace
            requests = fixtures.generate(league_id, self._teams(league_id),
                                         self._mode(league_id))
            try:
                _, decisions, _ = self._engine(league_id).solve(requests, [])
            except SchedulingImpossible:
                return {}
            return decisions
        # 延期/撤销版本：目标=与父版本时间不同的场次，其余锁定
        parent_rows = {r["match_id"]: r for r in self.conn.execute(
            "SELECT * FROM matches WHERE version_id=?",
            (v["parent_version"],)).fetchall()}
        current_rows = {r["match_id"]: r for r in self.conn.execute(
            "SELECT * FROM matches WHERE version_id=?",
            (version_id,)).fetchall()}
        targets = []
        for mid, r in current_rows.items():
            p = parent_rows.get(mid)
            if p is None or p["start_utc"] != r["start_utc"] or \
                    p["venue_id"] != r["venue_id"]:
                targets.append(r)
        if not targets:
            return {}
        # 作废时段：延期版作废的是父版本原时段；撤销版作废的是
        # 当前（被撤销）版本占用的时段。两种情况下都禁止引擎退回。
        forbidden_source = parent_rows if v["kind"] == \
            VersionKind.POSTPONE.value else current_rows
        floor = _parse_dt(v["not_before_utc"]) if v["not_before_utc"] \
            else None
        requests = []
        for r in targets:
            src = forbidden_source[r["match_id"]]
            requests.append(GameRequest(
                r["match_id"], r["round_no"], r["home_id"], r["away_id"],
                r["group_name"], floor,
                frozenset({(src["venue_id"], _parse_dt(src["start_utc"]))})))
        locked_ids = set(parent_rows) - {r["match_id"] for r in targets}
        locked = [PlannedMatch(
            r["match_id"], r["round_no"], r["home_id"], r["away_id"],
            r["venue_id"], _parse_dt(r["start_utc"]), _parse_dt(r["end_utc"]),
            r["group_name"])
            for mid, r in parent_rows.items() if mid in locked_ids]
        try:
            _, decisions, _ = self._engine(league_id).solve(requests, locked)
        except SchedulingImpossible:
            return {}
        return decisions

    def _row_to_view(self, row, venues: dict[str, Venue],
                     decisions: dict) -> MatchView:
        start = _parse_dt(row["start_utc"])
        end = _parse_dt(row["end_utc"])
        venue = venues.get(row["venue_id"])
        local_str, tzname = "", ""
        if venue:
            z = ZoneInfo(venue.timezone)
            local = start.astimezone(z)
            local_str = local.strftime("%Y-%m-%d %H:%M")
            tzname = local.tzname() or venue.timezone
        resolution = []
        for rej in decisions.get(row["match_id"], []):
            resolution.append({
                "venue_id": rej.venue_id,
                "slot_id": rej.slot_id,
                "candidate_start_utc": rej.start_utc.isoformat(),
                "reasons": rej.reasons,
                "explanation": [REASON_TEXT.get(x, x) for x in rej.reasons],
            })
        return MatchView(
            match_id=row["match_id"], round_no=row["round_no"],
            home_id=row["home_id"], away_id=row["away_id"],
            venue_id=row["venue_id"], start_utc=start, end_utc=end,
            status=row["status"], change_type=row["change_type"],
            group_name=row["group_name"], start_local=local_str,
            tzname=tzname, resolution=resolution)
