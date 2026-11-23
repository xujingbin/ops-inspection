"""SQLite 存储：主机对象、规则阈值、巡检计划、执行记录、逐项结果。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    hostname    TEXT NOT NULL,
    env         TEXT NOT NULL,
    team        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    code          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    metric        TEXT NOT NULL,
    normal_below  REAL NOT NULL,
    critical_from REAL NOT NULL,
    unit          TEXT NOT NULL DEFAULT '%',
    enabled       INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    host_id     INTEGER NOT NULL REFERENCES hosts(id),
    cycle       TEXT NOT NULL,
    at_time     TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id     INTEGER NOT NULL REFERENCES hosts(id),
    host_name   TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    total       INTEGER NOT NULL,
    normal      INTEGER NOT NULL,
    warning     INTEGER NOT NULL,
    critical    INTEGER NOT NULL,
    unknown     INTEGER NOT NULL,
    pass_rate   REAL
);

CREATE TABLE IF NOT EXISTS run_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    rule_code   TEXT NOT NULL,
    rule_name   TEXT NOT NULL,
    status      TEXT NOT NULL,
    value       REAL,
    value_text  TEXT NOT NULL,
    thresholds  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    duration_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_host ON runs(host_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_items_run ON run_items(run_id, id);
"""


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _now() -> str:
    return _fmt(datetime.now().timestamp())


class Store:
    def __init__(self, path: str = "inspection.db") -> None:
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
            if "source" not in columns:
                conn.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'MANUAL'")
            if "plan_id" not in columns:
                conn.execute("ALTER TABLE runs ADD COLUMN plan_id INTEGER")
            conn.executemany(
                """INSERT OR IGNORE INTO rules
                   (code, name, metric, normal_below, critical_from, unit, enabled, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                [
                    (r.code, r.name, r.metric, r.normal_below, r.critical_from, r.unit, _now())
                    for r in core.BUILTIN_RULES
                ],
            )

    # ---------------- 主机对象 ----------------

    def add_host(self, name: str, hostname: str, env: str, team: str) -> int:
        name = (name or "").strip()
        hostname = (hostname or "").strip()
        env = (env or "").strip()
        team = (team or "").strip()
        if not name or not hostname or not env or not team:
            raise ValueError("主机名称、hostname、环境、团队均为必填")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO hosts (name, hostname, env, team, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, hostname, env, team, _fmt(datetime.now().timestamp())),
            )
            return int(cur.lastrowid)

    def list_hosts(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM hosts ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def get_host(self, host_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
        return dict(row) if row else None

    # ---------------- 规则阈值 ----------------

    def list_rules(self, enabled_only: bool = False) -> List[Dict]:
        sql = "SELECT * FROM rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY code"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        rules = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            rules.append(item)
        return rules

    def get_rule(self, code: str) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rules WHERE code = ?", (code,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item

    def update_rule(self, code: str, normal_below, critical_from, enabled: bool = True) -> Dict:
        """更新阈值。区间约束：0 <= normal_below < critical_from <= 100。"""
        rule = self.get_rule(code)
        if not rule:
            raise ValueError(f"规则不存在: {code}")
        try:
            normal = float(normal_below)
            critical = float(critical_from)
        except (TypeError, ValueError):
            raise ValueError("阈值必须是数字")
        if not (0 <= normal < critical <= 100):
            raise ValueError("阈值不合法：需满足 0 <= 正常上限 < 异常下界 <= 100")
        with self._connect() as conn:
            conn.execute(
                "UPDATE rules SET normal_below = ?, critical_from = ?, enabled = ?, updated_at = ? "
                "WHERE code = ?",
                (normal, critical, 1 if enabled else 0, _now(), code),
            )
        return self.get_rule(code)

    def active_rules(self) -> List["core.Rule"]:
        """把启用规则转换成巡检核心的 Rule 对象。"""
        return [
            core.Rule(
                r["code"], r["name"], r["metric"], r["normal_below"], r["critical_from"], r["unit"]
            )
            for r in self.list_rules(enabled_only=True)
        ]

    # ---------------- 巡检计划 ----------------

    def add_plan(self, name: str, host_id, cycle: str, at_time: str) -> int:
        name = (name or "").strip()
        if not name:
            raise ValueError("计划名称必填")
        host = None
        if str(host_id).isdigit():
            host = self.get_host(int(host_id))
        if not host:
            raise ValueError("请选择有效的巡检主机")
        if cycle not in core.CYCLE_TEXT:
            raise ValueError("周期只支持每日或每周一")
        next_at = core.next_run_time(cycle, at_time)
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO plans (name, host_id, cycle, at_time, enabled, next_run_at, created_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (name, host["id"], cycle, at_time, _fmt(next_at.timestamp()), _now()),
            )
            return int(cur.lastrowid)

    def list_plans(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT p.*, h.name AS host_name, h.hostname AS host_hostname
                   FROM plans p LEFT JOIN hosts h ON h.id = p.host_id
                   ORDER BY p.id DESC"""
            ).fetchall()
        plans = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            plans.append(item)
        return plans

    def get_plan(self, plan_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item

    def set_plan_enabled(self, plan_id: int, enabled: bool) -> Dict:
        plan = self.get_plan(plan_id)
        if not plan:
            raise ValueError("计划不存在")
        next_run_at = plan["next_run_at"]
        if enabled:
            next_run_at = _fmt(core.next_run_time(plan["cycle"], plan["at_time"]).timestamp())
        with self._connect() as conn:
            conn.execute(
                "UPDATE plans SET enabled = ?, next_run_at = ? WHERE id = ?",
                (1 if enabled else 0, next_run_at, plan_id),
            )
        return self.get_plan(plan_id)

    def due_plans(self, now=None) -> List[Dict]:
        moment = now or datetime.now()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM plans WHERE enabled = 1 AND next_run_at <= ? ORDER BY id",
                (_fmt(moment.timestamp()),),
            ).fetchall()
        plans = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            plans.append(item)
        return plans

    def mark_plan_run(self, plan_id: int, ran_at, next_at) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE plans SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                (_fmt(ran_at.timestamp()), _fmt(next_at.timestamp()), plan_id),
            )

    # ---------------- 执行记录 ----------------

    def save_run(self, run: core.RunResult, source: str = "MANUAL", plan_id: Optional[int] = None) -> int:
        counts = run.counts
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO runs (host_id, host_name, status, started_at, finished_at,
                                     duration_ms, total, normal, warning, critical, unknown, pass_rate,
                                     source, plan_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.host_id,
                    run.host_name,
                    "COMPLETED",
                    _fmt(run.started_at),
                    _fmt(run.finished_at),
                    run.duration_ms,
                    run.total,
                    counts[core.NORMAL],
                    counts[core.WARNING],
                    counts[core.CRITICAL],
                    counts[core.UNKNOWN],
                    run.pass_rate,
                    source,
                    plan_id,
                ),
            )
            run_id = int(cur.lastrowid)
            conn.executemany(
                """INSERT INTO run_items (run_id, rule_code, rule_name, status, value,
                                          value_text, thresholds, reason, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        item.rule_code,
                        item.rule_name,
                        item.status,
                        item.value,
                        item.value_text,
                        item.thresholds,
                        item.reason,
                        item.duration_ms,
                    )
                    for item in run.items
                ],
            )
        return run_id

    def list_runs(self, limit: int = 20, host_id: Optional[int] = None) -> List[Dict]:
        sql = "SELECT * FROM runs"
        params: list = []
        if host_id is not None:
            sql += " WHERE host_id = ?"
            params.append(host_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            run = dict(row)
            items = conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        run["items"] = [dict(i) for i in items]
        return run

    def count_hosts(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0])
