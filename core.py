"""MVP-01 巡检核心：三档阈值判定、指标采集、单次执行。

设计约束：
- 采集抛异常 / 超时 / 返回空值 一律判 UNKNOWN 并记录原因，绝不默认正常。
- 单个检查项失败只影响该项，整体任务仍然完成。
- 健康度（通过率）不含 UNKNOWN：UNKNOWN 不进分子也不进分母。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

NORMAL = "NORMAL"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
UNKNOWN = "UNKNOWN"

STATUS_TEXT = {
    NORMAL: "正常",
    WARNING: "警告",
    CRITICAL: "异常",
    UNKNOWN: "未知",
}


@dataclass(frozen=True)
class Rule:
    """一条巡检规则：指标 + 三档阈值（左闭右开）。"""

    code: str
    name: str
    metric: str
    normal_below: float
    critical_from: float
    unit: str = "%"

    def thresholds_text(self) -> str:
        return (
            f"正常 <{self.normal_below:g}{self.unit} / "
            f"警告 {self.normal_below:g}~{self.critical_from:g}{self.unit} / "
            f"异常 >={self.critical_from:g}{self.unit}"
        )

    def evaluate(self, value: float) -> str:
        """边界语义：value < normal_below 为正常；达到 critical_from 即为异常。"""
        if value < self.normal_below:
            return NORMAL
        if value < self.critical_from:
            return WARNING
        return CRITICAL


BUILTIN_RULES: List[Rule] = [
    Rule("R-INFRA-001", "CPU 使用率", "cpu", normal_below=75.0, critical_from=90.0),
    Rule("R-INFRA-002", "内存使用率", "memory", normal_below=80.0, critical_from=90.0),
    Rule("R-INFRA-003", "磁盘使用率", "disk", normal_below=80.0, critical_from=90.0),
]


@dataclass
class ItemResult:
    rule_code: str
    rule_name: str
    status: str
    value: Optional[float]
    thresholds: str
    reason: str
    started_at: float
    finished_at: float

    @property
    def duration_ms(self) -> int:
        return int(round((self.finished_at - self.started_at) * 1000))

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.status, self.status)

    @property
    def value_text(self) -> str:
        return "—" if self.value is None else f"{self.value:.1f}%"


@dataclass
class RunResult:
    host_id: int
    host_name: str
    started_at: float
    finished_at: float = 0.0
    items: List[ItemResult] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return int(round((self.finished_at - self.started_at) * 1000))

    def count(self, status: str) -> int:
        return sum(1 for item in self.items if item.status == status)

    @property
    def counts(self) -> Dict[str, int]:
        return {s: self.count(s) for s in (NORMAL, WARNING, CRITICAL, UNKNOWN)}

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def pass_rate(self) -> Optional[float]:
        """健康度：normal / (normal + warning + critical)。全为 UNKNOWN 时返回 None。"""
        judged = self.count(NORMAL) + self.count(WARNING) + self.count(CRITICAL)
        if judged == 0:
            return None
        return round(self.count(NORMAL) * 100.0 / judged, 1)

    @property
    def pass_rate_text(self) -> str:
        rate = self.pass_rate
        return "—" if rate is None else f"{rate:.1f}%"


# --------------------------------------------------------------------------
# 采集器：仅使用 Python 标准库读取本机 /proc 与 statvfs
# --------------------------------------------------------------------------

def collect_cpu(interval: float = 0.2) -> float:
    """读取 /proc/stat 两次，计算区间内 CPU 忙碌占比。"""
    def snapshot() -> tuple:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            parts = fh.readline().split()
        values = [int(x) for x in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    total_a, idle_a = snapshot()
    time.sleep(interval)
    total_b, idle_b = snapshot()
    total_delta = total_b - total_a
    idle_delta = idle_b - idle_a
    if total_delta <= 0:
        raise RuntimeError("CPU 采样区间无有效增量")
    return round((1.0 - idle_delta / total_delta) * 100.0, 1)


def collect_memory() -> float:
    """MemTotal 与 MemAvailable 之差作为已用内存。"""
    info: Dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                info[key] = int(parts[0])
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    if not total or available is None:
        raise RuntimeError("无法从 /proc/meminfo 读取内存数据")
    return round((1.0 - available / total) * 100.0, 1)


def collect_disk(path: str = "/") -> float:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        raise RuntimeError("磁盘总量为 0")
    return round((1.0 - free / total) * 100.0, 1)


DEFAULT_COLLECTORS: Dict[str, Callable[[], Optional[float]]] = {
    "cpu": collect_cpu,
    "memory": collect_memory,
    "disk": collect_disk,
}


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def _coerce_value(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"采集返回值不是数值: {raw!r}")
    if value != value:  # NaN
        return None
    return value


def execute_run(
    host_id: int,
    host_name: str,
    collectors: Optional[Dict[str, Callable[[], Optional[float]]]] = None,
    rules: Optional[Iterable[Rule]] = None,
    clock: Callable[[], float] = time.time,
) -> RunResult:
    """逐项采集并判定。任一检查项失败只记为 UNKNOWN，任务照常完成。"""
    active_collectors = dict(DEFAULT_COLLECTORS)
    if collectors:
        active_collectors.update(collectors)
    active_rules = list(rules) if rules is not None else list(BUILTIN_RULES)

    run = RunResult(host_id=host_id, host_name=host_name, started_at=clock())
    for rule in active_rules:
        started = clock()
        status = UNKNOWN
        value: Optional[float] = None
        reason = ""
        collector = active_collectors.get(rule.metric)
        if collector is None:
            reason = f"未注册采集器: {rule.metric}"
        else:
            try:
                value = _coerce_value(collector())
            except Exception as exc:  # 单项失败不影响其它项
                value = None
                reason = f"采集异常: {type(exc).__name__}: {exc}"
            else:
                if value is None:
                    reason = "采集无数据（返回空值）"
                else:
                    status = rule.evaluate(value)
        run.items.append(
            ItemResult(
                rule_code=rule.code,
                rule_name=rule.name,
                status=status,
                value=value,
                thresholds=rule.thresholds_text(),
                reason=reason,
                started_at=started,
                finished_at=clock(),
            )
        )
    run.finished_at = clock()
    return run
