"""MVP-01 Web 入口：Python 3.11 标准库 http.server + sqlite3，零第三方依赖。

启动：python3 app.py --port 8000
"""

from __future__ import annotations

import argparse
import html
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core
import scheduler
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

RUN_STATUS_TEXT = {"COMPLETED": "已完成"}


def _template(name: str) -> str:
    with open(os.path.join(TEMPLATE_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


def _pill(status: str) -> str:
    label = core.STATUS_TEXT.get(status, status)
    return f'<span class="pill p-{html.escape(status)}">{html.escape(label)}</span>'


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def rules_from_store(st: store.Store) -> list:
    """把 rules 表里的启用规则转换成巡检核心的 Rule 对象。"""
    return st.active_rules()


def render_plans(st: store.Store, flash: str = "", error: bool = False) -> str:
    plans = st.list_plans()
    hosts = st.list_hosts()
    host_options = "".join(
        f"<option value='{h['id']}'>{_esc(h['name'])}（{_esc(h['hostname'])}）</option>"
        for h in hosts
    ) or "<option value=''>请先新增主机</option>"

    rows = []
    for p in plans:
        toggle_label = "停用" if p["enabled"] else "启用"
        toggle_value = "0" if p["enabled"] else "1"
        rows.append(
            "<tr><td class='num'>{id}</td><td>{name}</td>"
            "<td>{host}<div class='mono' style='color:#98A2B3'>{hostname}</div></td>"
            "<td>{cycle}</td><td class='mono'>{at}</td>"
            "<td class='mono'>{last}</td><td class='mono'>{next}</td>"
            "<td><span class='pill {cls}'>{state}</span></td>"
            "<td style='white-space:nowrap'>"
            "<form method='post' action='/plans/run' style='display:inline'>"
            "<input type='hidden' name='plan_id' value='{id}'><button type='submit'>立即执行</button></form> "
            "<form method='post' action='/plans/toggle' style='display:inline'>"
            "<input type='hidden' name='plan_id' value='{id}'>"
            "<input type='hidden' name='enabled' value='{tv}'><button type='submit'>{tl}</button></form>"
            "</td></tr>".format(
                id=p["id"],
                name=_esc(p["name"]),
                host=_esc(p.get("host_name") or "—"),
                hostname=_esc(p.get("host_hostname") or ""),
                cycle=core.CYCLE_TEXT.get(p["cycle"], p["cycle"]),
                at=_esc(p["at_time"]),
                last=_esc(p["last_run_at"] or "—"),
                next=_esc(p["next_run_at"]),
                cls="p-on" if p["enabled"] else "p-off",
                state="启用" if p["enabled"] else "停用",
                tv=toggle_value,
                tl=toggle_label,
            )
        )

    page = _template("plans.html")
    flash_html = (
        f'<div class="flash{" err" if error else ""}">{_esc(flash)}</div>' if flash else ""
    )
    return (
        page.replace("{{FLASH}}", flash_html)
        .replace("{{PLAN_COUNT}}", str(len(plans)))
        .replace("{{HOST_OPTIONS}}", host_options)
        .replace(
            "{{PLAN_ROWS}}",
            "".join(rows) or "<tr><td colspan='9' class='empty'>还没有巡检计划。</td></tr>",
        )
    )


def execute_plan_now(st: store.Store, plan: dict) -> int:
    """立即执行一次计划（与定时触发共用执行路径）。"""
    host = st.get_host(plan["host_id"])
    if not host:
        raise ValueError("计划关联的主机不存在")
    rules = st.active_rules() or list(core.BUILTIN_RULES)
    now = core.datetime.now()
    run = core.execute_run(host["id"], host["name"], rules=rules)
    run_id = st.save_run(run, source="PLAN", plan_id=plan["id"])
    st.mark_plan_run(plan["id"], now, core.next_run_time(plan["cycle"], plan["at_time"], now))
    return run_id


def render_rules(st: store.Store, flash: str = "", error: bool = False) -> str:
    rules = st.list_rules()
    rows = []
    for r in rules:
        form_id = f"f-{r['code']}"
        checked = "checked" if r["enabled"] else ""
        rows.append(
            "<tr><td class='mono'>{code}</td><td>{name}</td><td class='mono'>{metric}</td>"
            "<td><input form='{fid}' type='number' name='normal_below' step='0.1' min='0' max='100' value='{normal:g}'></td>"
            "<td><input form='{fid}' type='number' name='critical_from' step='0.1' min='0' max='100' value='{critical:g}'></td>"
            "<td>正常 &lt;{normal:g}% / 警告 {normal:g}~{critical:g}% / 异常 &ge;{critical:g}%</td>"
            "<td><input form='{fid}' type='checkbox' name='enabled' value='1' {checked}> 启用</td>"
            "<td><form id='{fid}' method='post' action='/rules'>"
            "<input type='hidden' name='code' value='{code}'><button type='submit'>保存</button></form></td></tr>".format(
                code=_esc(r["code"]),
                name=_esc(r["name"]),
                metric=_esc(r["metric"]),
                fid=form_id,
                normal=r["normal_below"],
                critical=r["critical_from"],
                checked=checked,
            )
        )
    page = _template("rules.html")
    flash_html = (
        f'<div class="flash{" err" if error else ""}">{_esc(flash)}</div>' if flash else ""
    )
    return (
        page.replace("{{FLASH}}", flash_html)
        .replace("{{RULE_COUNT}}", str(len(rules)))
        .replace("{{RULE_ROWS}}", "".join(rows) or "<tr><td colspan='8' class='empty'>没有规则。</td></tr>")
    )


def render_index(st: store.Store, flash: str = "") -> str:
    hosts = st.list_hosts()
    runs = st.list_runs(limit=20)

    if hosts:
        host_rows = "".join(
            f"<tr><td class='num'>{h['id']}</td><td>{_esc(h['name'])}</td>"
            f"<td class='mono'>{_esc(h['hostname'])}</td><td>{_esc(h['env'])}</td>"
            f"<td>{_esc(h['team'])}</td><td class='mono'>{_esc(h['created_at'])}</td>"
            f"<td><a href='/runs?host_id={h['id']}'>查看执行</a></td></tr>"
            for h in hosts
        )
        host_options = "".join(
            f"<option value='{h['id']}'>{_esc(h['name'])}（{_esc(h['hostname'])} / {_esc(h['env'])} / {_esc(h['team'])}）</option>"
            for h in hosts
        )
    else:
        host_rows = "<tr><td colspan='7' class='empty'>还没有主机，先在上方新增一台。</td></tr>"
        host_options = "<option value=''>请先新增主机</option>"

    run_rows_list = []
    for r in runs:
        pass_text = "—" if r["pass_rate"] is None else "{:.1f}%".format(r["pass_rate"])
        run_rows_list.append(
            "<tr><td class='num'>{}</td><td>{}</td><td>{}</td><td class='mono'>{}</td>"
            "<td class='num'>{:.1f}s</td><td class='num'>{}</td>"
            "<td class='num' style='color:#0B8A52'>{}</td>"
            "<td class='num' style='color:#B25E00'>{}</td>"
            "<td class='num' style='color:#B42318'>{}</td>"
            "<td class='num' style='color:#667085'>{}</td>"
            "<td class='num'>{}</td><td><a href='/runs/{}'>详情</a></td></tr>".format(
                r["id"], _esc(r["host_name"]),
                "定时" if r.get("source") == "PLAN" else "手动",
                _esc(r["started_at"]), r["duration_ms"] / 1000,
                r["total"], r["normal"], r["warning"], r["critical"], r["unknown"],
                pass_text, r["id"],
            )
        )
    run_rows = "".join(run_rows_list) or (
        "<tr><td colspan='12' class='empty'>还没有执行记录。</td></tr>"
    )

    page = _template("index.html")
    return (
        page.replace("{{FLASH}}", f'<div class="flash">{_esc(flash)}</div>' if flash else "")
        .replace("{{HOST_COUNT}}", str(len(hosts)))
        .replace("{{HOST_ROWS}}", host_rows)
        .replace("{{HOST_OPTIONS}}", host_options)
        .replace("{{RUN_ROWS}}", run_rows)
    )


def render_run_detail(run: dict) -> str:
    items = run.get("items", [])
    item_rows = []
    for item in items:
        reason = _esc(item["reason"]) if item["reason"] else "<span style='color:#98A2B3'>—</span>"
        if item["status"] == core.UNKNOWN:
            reason = f"<span class='reason'>{reason}</span>"
        item_rows.append(
            f"<tr><td class='mono'>{_esc(item['rule_code'])}</td><td>{_esc(item['rule_name'])}</td>"
            f"<td class='num'>{_esc(item['value_text'])}</td>"
            f"<td>{_esc(item['thresholds'])}</td><td>{_pill(item['status'])}</td>"
            f"<td class='num'>{item['duration_ms']}ms</td><td>{reason}</td></tr>"
        )
    if not item_rows:
        item_rows.append("<tr><td colspan='7' class='empty'>本次执行没有检查项。</td></tr>")

    pass_rate = "—" if run["pass_rate"] is None else f"{run['pass_rate']:.1f}%"
    page = _template("detail.html")
    return (
        page.replace("{{RUN_ID}}", str(run["id"]))
        .replace("{{HOST_NAME}}", _esc(run["host_name"]))
        .replace("{{STARTED}}", _esc(run["started_at"]))
        .replace("{{DURATION}}", f"{run['duration_ms'] / 1000:.1f}s")
        .replace("{{STATUS_TEXT}}", RUN_STATUS_TEXT.get(run["status"], run["status"]))
        .replace("{{TOTAL}}", str(run["total"]))
        .replace("{{NORMAL}}", str(run["normal"]))
        .replace("{{WARNING}}", str(run["warning"]))
        .replace("{{CRITICAL}}", str(run["critical"]))
        .replace("{{UNKNOWN}}", str(run["unknown"]))
        .replace("{{SOURCE}}", "定时计划" if run.get("source") == "PLAN" else "手动")
        .replace("{{PASS_RATE}}", pass_rate)
        .replace("{{ITEM_ROWS}}", "".join(item_rows))
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "OpsInspectionMVP01/1.0"
    store: store.Store

    # ---------- helpers ----------
    def _send(self, code: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    # ---------- routes ----------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self._send(200, render_index(self.store))
            return
        if path == "/plans":
            self._send(200, render_plans(self.store))
            return
        if path == "/rules":
            self._send(200, render_rules(self.store))
            return
        if path == "/health":
            body = json.dumps(
                {"status": "ok", "hosts": self.store.count_hosts()}, ensure_ascii=False
            )
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/runs":
            host_id = query.get("host_id", [None])[0]
            host = self.store.get_host(int(host_id)) if host_id and host_id.isdigit() else None
            if not host:
                self._redirect("/")
                return
            rules = rules_from_store(self.store)
            if not rules:
                self._send(400, render_index(self.store, flash="没有启用的规则，请先在规则配置页启用"))
                return
            run = core.execute_run(host["id"], host["name"], rules=rules)
            run_id = self.store.save_run(run)
            self._redirect(f"/runs/{run_id}")
            return
        if path.startswith("/runs/"):
            run_id = path.split("/", 2)[2]
            if not run_id.isdigit():
                self._send(404, "<h1>404</h1><p>无效的执行记录编号。</p>")
                return
            run = self.store.get_run(int(run_id))
            if not run:
                self._send(404, "<h1>404</h1><p>执行记录不存在。</p>")
                return
            self._send(200, render_run_detail(run))
            return
        self._send(404, "<h1>404</h1><p>页面不存在。</p>")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/plans":
            form = self._read_form()
            try:
                self.store.add_plan(
                    form.get("name", ""),
                    form.get("host_id", ""),
                    form.get("cycle", ""),
                    form.get("at_time", ""),
                )
            except ValueError as exc:
                self._send(400, render_plans(self.store, flash=str(exc), error=True))
                return
            self._redirect("/plans")
            return

        if path == "/plans/toggle":
            form = self._read_form()
            plan_id = form.get("plan_id", "")
            if not plan_id.isdigit():
                self._send(400, render_plans(self.store, flash="无效的计划编号", error=True))
                return
            try:
                self.store.set_plan_enabled(int(plan_id), form.get("enabled") == "1")
            except ValueError as exc:
                self._send(400, render_plans(self.store, flash=str(exc), error=True))
                return
            self._redirect("/plans")
            return

        if path == "/plans/run":
            form = self._read_form()
            plan_id = form.get("plan_id", "")
            plan = self.store.get_plan(int(plan_id)) if plan_id.isdigit() else None
            if not plan:
                self._send(404, "<h1>404</h1><p>计划不存在。</p>")
                return
            rules = rules_from_store(self.store)
            if not rules:
                self._send(400, render_plans(self.store, flash="没有启用的规则，请先在规则配置页启用", error=True))
                return
            try:
                run_id = execute_plan_now(self.store, plan)
            except ValueError as exc:
                self._send(400, render_plans(self.store, flash=str(exc), error=True))
                return
            self._redirect(f"/runs/{run_id}")
            return

        if path == "/rules":
            form = self._read_form()
            try:
                self.store.update_rule(
                    form.get("code", ""),
                    form.get("normal_below", ""),
                    form.get("critical_from", ""),
                    form.get("enabled") == "1",
                )
            except ValueError as exc:
                self._send(400, render_rules(self.store, flash=str(exc), error=True))
                return
            self._redirect("/rules")
            return

        if path == "/hosts":
            form = self._read_form()
            try:
                self.store.add_host(
                    form.get("name", ""),
                    form.get("hostname", ""),
                    form.get("env", ""),
                    form.get("team", ""),
                )
            except ValueError as exc:
                self._send(400, render_index(self.store, flash=str(exc)))
                return
            self._redirect("/")
            return

        if path == "/runs":
            form = self._read_form()
            host_id = form.get("host_id", "")
            if not host_id.isdigit():
                self._send(400, render_index(self.store, flash="请先选择要巡检的主机"))
                return
            host = self.store.get_host(int(host_id))
            if not host:
                self._send(404, "<h1>404</h1><p>主机不存在。</p>")
                return
            rules = rules_from_store(self.store)
            if not rules:
                self._send(400, render_index(self.store, flash="没有启用的规则，请先在规则配置页启用"))
                return
            run = core.execute_run(host["id"], host["name"], rules=rules)
            run_id = self.store.save_run(run)
            self._redirect(f"/runs/{run_id}")
            return

        self._send(404, "<h1>404</h1><p>页面不存在。</p>")


def create_server(db_path: str = "inspection.db", host: str = "0.0.0.0", port: int = 8000):
    st = store.Store(db_path)
    handler = type("BoundHandler", (Handler,), {"store": st})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd, st


def main() -> None:
    parser = argparse.ArgumentParser(description="MVP-01 主机巡检最小闭环")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=os.path.join(BASE_DIR, "inspection.db"))
    parser.add_argument("--no-scheduler", action="store_true", help="不启动后台定时调度")
    args = parser.parse_args()

    httpd, st = create_server(args.db, args.host, args.port)
    if not args.no_scheduler:
        scheduler.start_scheduler(st)
    print(
        f"主机巡检已启动: http://{args.host}:{args.port}  "
        f"(db={args.db}, hosts={st.count_hosts()}, scheduler={'off' if args.no_scheduler else 'on'})"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
