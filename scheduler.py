"""进程内定时调度：到点执行巡检计划。

不引入第三方调度组件：后台线程每 N 秒扫一次到期计划。进程重启后按
`plans.next_run_at` 继续，不补跑停机期间错过的计划，避免一次补跑造成堆积。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import List, Optional

import core
import store


def run_due_plans(
    st: store.Store, now: Optional[datetime] = None, collectors=None
) -> List[int]:
    """执行所有到期计划，返回本次产生的执行记录 id。"""
    moment = now or datetime.now()
    rules = st.active_rules() or list(core.BUILTIN_RULES)
    executed: List[int] = []
    for plan in st.due_plans(moment):
        next_at = core.next_run_time(plan["cycle"], plan["at_time"], moment)
        host = st.get_host(plan["host_id"])
        if not host:
            # 主机被删除时只推进计划时间，不产生空执行记录
            st.mark_plan_run(plan["id"], moment, next_at)
            continue
        run = core.execute_run(host["id"], host["name"], collectors=collectors, rules=rules)
        run_id = st.save_run(run, source="PLAN", plan_id=plan["id"])
        st.mark_plan_run(plan["id"], moment, next_at)
        executed.append(run_id)
    return executed


def start_scheduler(
    st: store.Store, interval: float = 15.0, stop_event: Optional[threading.Event] = None
):
    """启动后台调度线程，返回 (thread, stop_event)。"""
    stop = stop_event or threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                run_due_plans(st)
            except Exception as exc:  # 调度线程不能因为单次异常退出
                print(f"[scheduler] 执行失败: {type(exc).__name__}: {exc}")
            stop.wait(interval)

    thread = threading.Thread(target=loop, daemon=True, name="inspection-scheduler")
    thread.start()
    return thread, stop
